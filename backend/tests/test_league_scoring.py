"""Unified league scoring and leaderboards: points, periods, separation, reliability.

Covers the scoring contract end to end:

* split awards: the placement-relative game half (first 100, last 0,
  proportional middles, averaged ties, solo 50, deterministic rounding,
  exact decimals) feeding per-game boards, and the absolute-performance
  global half (fixed versioned profiles per game, 0-100, never rank or
  field size) feeding global boards; legacy columns mirror the global
  half with per-half policy versions;
* the featured 2x multiplier on each half independently (snapshot
  semantics, no stacking, couples multiplied once);
* individual/couple separation with canonical pair identity;
* UTC daily/weekly/monthly/all-time aggregation with frequency
  normalization, missed opportunities as zero, and late finalization
  attributed to the official event;
* idempotent finalization, the scheduler closure hook, explicit
  correction, endpoint auth/validation/pagination, /me consistency, and
  no leakage of native secrets through leaderboard responses.

Fixtures insert ended sessions directly with dates relative to the real
UTC today, so the suite cannot rot as the calendar moves. The one
scheduler test drives a genuine period boundary the same way.
"""

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    AnagramGameRecord,
    AnagramUserGameRecord,
    ConnectionsGameRecord,
    ConnectionsSubmissionRecord,
    CoupleGameRecord,
    CoupleGameSessionAnswerRecord,
    CoupleGameSessionPredictionRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    FeaturedGameRecord,
    GameScoreAwardRecord,
    GameScoreOpportunityRecord,
    ReactionTimeGameRecord,
    ReactionTimeSubmissionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
    WordleGameRecord,
    WordleUserGameRecord,
)
from app.main import app
from app.models import UserDetails
from app.services import game_scheduler, league_boards, league_finalization
from app.services.auth_service import get_current_user
from app.services.league_scoring import (
    awarded_points,
    averaged_tie_points,
    base_points_for_rank,
    canonical_couple,
    month_key,
    period_bounds,
    quantize_points,
    week_start,
)
from conftest import override_get_admin_user

UTC = timezone.utc


def today() -> date:
    return datetime.now(UTC).date()


def at(day: date, hour: int = 12) -> datetime:
    return datetime.combine(day, time(hour, 0), tzinfo=UTC)


def midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=UTC)


_UID = 0


def make_user(db, tag: str) -> UserDetails:
    """A participant row plus the identity model, with a unique regno."""
    global _UID
    _UID += 1
    user_id = f"{_UID:08d}-0000-4000-8000-{_UID:012d}"
    regno = f"LG{_UID:06d}"
    with db.engine.begin() as connection:
        from sqlalchemy import text

        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', 'other') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {
                "id": user_id,
                "regno": regno,
                "name": f"League {tag} {_UID}",
                "email": f"{regno}@t.com",
            },
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=f"League {tag} {_UID}",
        email=f"{regno}@t.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )


def user_client(user: UserDetails) -> TestClient:
    # Mirror the suite convention: the auth override is application-global and
    # the autouse fixture reinstalls the default before every test, so leaving
    # this test's identity in place is safe and per-request resolution sees it.
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, follow_redirects=False)


def admin_client() -> TestClient:
    app.dependency_overrides[get_current_user] = override_get_admin_user
    return TestClient(app, follow_redirects=False)


def add_wordle_session(db, start: date, answer: str = "cigar", status: str = "ended") -> str:
    with db.sessions.begin() as session:
        row = WordleGameRecord(
            game_date=start,
            mode="random",
            answer=answer,
            status=status,
            period_start_date=start,
            period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)) if status == "ended" else None,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_wordle_result(db, session_id: str, user_id: str, attempts: int, solved: bool) -> None:
    moment = at(today())
    with db.sessions.begin() as session:
        session.add(
            WordleUserGameRecord(
                wordle_game_id=session_id,
                user_id=user_id,
                attempt_count=attempts,
                status="won" if solved else "lost",
                success=solved,
                guesses=["cigar"] * attempts,
                completed_at=moment,
                solved_at=moment if solved else None,
            )
        )


def add_featured(db, day: date, game_id: str) -> None:
    with db.sessions.begin() as session:
        session.add(FeaturedGameRecord(game_date=day, game_id=game_id, created_by="TEST"))


def live_awards(db, **filters) -> list:
    with db.sessions() as session:
        query = select(GameScoreAwardRecord).where(
            GameScoreAwardRecord.superseded_at.is_(None)
        )
        for key, value in filters.items():
            query = query.where(getattr(GameScoreAwardRecord, key) == value)
        return session.scalars(query).all()


# ---------------------------------------------------------------------------
# Placement math (pure, no database)
# ---------------------------------------------------------------------------

class TestPlacementMath:
    def test_endpoints_of_the_scale(self):
        assert base_points_for_rank(1, 4) == Decimal("100.00")
        assert base_points_for_rank(4, 4) == Decimal("0.00")

    def test_four_player_example_from_the_contract(self):
        assert base_points_for_rank(2, 4) == Decimal("66.67")
        assert base_points_for_rank(3, 4) == Decimal("33.33")

    def test_larger_fields_stay_proportional(self):
        assert base_points_for_rank(1, 6) == Decimal("100.00")
        assert base_points_for_rank(2, 6) == Decimal("80.00")
        assert base_points_for_rank(3, 6) == Decimal("60.00")
        assert base_points_for_rank(6, 6) == Decimal("0.00")

    def test_solo_result_is_not_an_automatic_win(self):
        assert base_points_for_rank(1, 1) == Decimal("50.00")

    def test_ties_share_averaged_positions(self):
        # Two tied for first in a field of four occupy 1st and 2nd.
        assert averaged_tie_points([1, 2], 4) == Decimal("83.34")
        # Three tied for last in a field of four occupy 2nd, 3rd, 4th.
        assert averaged_tie_points([2, 3, 4], 4) == Decimal("33.33")

    def test_featured_doubles_after_placement(self):
        final, multiplier = awarded_points(Decimal("66.67"), True)
        assert (final, multiplier) == (Decimal("133.34"), 2)
        final, multiplier = awarded_points(Decimal("66.67"), False)
        assert (final, multiplier) == (Decimal("66.67"), 1)
        # Last place stays last even when featured.
        final, _ = awarded_points(Decimal("0.00"), True)
        assert final == Decimal("0.00")

    def test_quantization_is_deterministic(self):
        assert quantize_points(Decimal("10.005")) == Decimal("10.01")
        assert quantize_points(Decimal("10.004")) == Decimal("10.00")

    def test_canonical_couples(self):
        assert canonical_couple("b-id", "a-id") == ("a-id", "b-id")
        assert canonical_couple("a-id", "b-id") == ("a-id", "b-id")
        assert canonical_couple("x", "y") != canonical_couple("x", "z")


class TestPeriodBoundaries:
    def test_daily_is_one_utc_day(self):
        day = date(2026, 10, 8)
        assert period_bounds("daily", day) == (day, day)

    def test_weekly_is_monday_to_sunday_utc(self):
        # Thu Oct 8 2026 sits in the Mon Oct 5 - Sun Oct 11 week.
        assert period_bounds("weekly", date(2026, 10, 8)) == (
            date(2026, 10, 5), date(2026, 10, 11),
        )
        assert week_start(date(2026, 10, 11)) == date(2026, 10, 5)
        assert week_start(date(2026, 10, 12)) == date(2026, 10, 12)

    def test_monthly_handles_lengths_and_year_ends(self):
        assert period_bounds("monthly", date(2026, 2, 15)) == (
            date(2026, 2, 1), date(2026, 2, 28),
        )
        assert period_bounds("monthly", date(2024, 2, 15))[1] == date(2024, 2, 29)
        assert period_bounds("monthly", date(2026, 12, 31)) == (
            date(2026, 12, 1), date(2026, 12, 31),
        )
        assert month_key(date(2026, 10, 8)) == "2026-10"


# ---------------------------------------------------------------------------
# Finalization: ranking, snapshots, idempotency
# ---------------------------------------------------------------------------

class TestWordleFinalization:
    def test_places_four_and_persists_exact_decimals(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"W{i}") for i in range(4)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 3, True)
        add_wordle_result(db, session_id, users[2].id, 5, True)
        add_wordle_result(db, session_id, users[3].id, 6, False)

        outcome = league_finalization.finalize_opportunity(db, "wordle", session_id)

        assert outcome["action"] == "finalized"
        awards = {str(a.user_id): a for a in live_awards(db, game_id="wordle")}
        assert [awards[u.id].rank for u in users] == [1, 2, 3, 4]
        # Game half: placement-relative, unchanged behavior.
        assert awards[users[0].id].game_base_points == Decimal("100.00")
        assert awards[users[1].id].game_base_points == Decimal("66.67")
        assert awards[users[2].id].game_base_points == Decimal("33.33")
        assert awards[users[3].id].game_base_points == Decimal("0.00")
        # Global half: absolute attempts table (2/3/5 solves, one failure).
        assert awards[users[0].id].global_base_points == Decimal("90")
        assert awards[users[1].id].global_base_points == Decimal("80")
        assert awards[users[2].id].global_base_points == Decimal("60")
        assert awards[users[3].id].global_base_points == Decimal("0")
        # Legacy columns mirror the global path; versions stamp each half.
        assert all(a.participant_count == 4 for a in awards.values())
        assert all(a.featured_multiplier == 1 for a in awards.values())
        assert all(a.awarded_points == a.global_points for a in awards.values())
        assert all(a.base_points == a.global_base_points for a in awards.values())
        assert all(a.global_scoring_version == 2 for a in awards.values())
        assert all(a.game_scoring_version == 1 for a in awards.values())
        # Period attribution follows the session, and no secret travels.
        assert awards[users[0].id].competition_at == midnight(day)
        assert "cigar" not in str(awards[users[0].id].raw_metrics)
        assert awards[users[0].id].raw_metrics == {"solved": True, "attempts": 2}

    def test_tied_solvers_share_averaged_points_and_rank(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"T{i}") for i in range(3)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 3, True)
        add_wordle_result(db, session_id, users[1].id, 3, True)
        add_wordle_result(db, session_id, users[2].id, 6, False)

        league_finalization.finalize_opportunity(db, "wordle", session_id)

        awards = {str(a.user_id): a for a in live_awards(db, game_id="wordle")}
        # Game half: the tie still shares averaged placement points.
        assert awards[users[0].id].game_base_points == Decimal("75.00")
        assert awards[users[1].id].game_base_points == Decimal("75.00")
        # Global half: identical native results earn identical absolute
        # scores -- no tie averaging on this path.
        assert awards[users[0].id].global_base_points == Decimal("80")
        assert awards[users[1].id].global_base_points == Decimal("80")
        assert awards[users[0].id].rank == 1
        assert awards[users[1].id].rank == 1
        assert awards[users[2].id].game_base_points == Decimal("0.00")
        assert awards[users[2].id].global_base_points == Decimal("0")

    def test_repeat_finalization_settles_on_the_same_rows(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"R{i}") for i in range(2)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 4, True)

        first = league_finalization.finalize_opportunity(db, "wordle", session_id)
        second = league_finalization.finalize_opportunity(db, "wordle", session_id)
        scan = league_finalization.finalize_due_opportunities(db, ["wordle"])

        assert first["action"] == "finalized"
        assert live_awards(db, game_id="wordle", opportunity_id=session_id)
        assert len(live_awards(db, game_id="wordle", opportunity_id=session_id)) == 2
        assert second["awards"] == 0
        assert scan == {"wordle": {"snapshots": 0, "finalized": 0}}

    def test_open_events_are_refused(self, db):
        day = today()
        session_id = add_wordle_session(db, day, status="active")
        user = make_user(db, "Open")
        add_wordle_result(db, session_id, user.id, 2, True)

        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            league_finalization.finalize_opportunity(db, "wordle", session_id)
        assert caught.value.status_code == 409

    def test_in_progress_rows_never_rank(self, db):
        day = today() - timedelta(days=3)
        user = make_user(db, "Prog")
        session_id = add_wordle_session(db, day)
        with db.sessions.begin() as session:
            session.add(
                WordleUserGameRecord(
                    wordle_game_id=session_id,
                    user_id=user.id,
                    attempt_count=2,
                    status="in_progress",
                    success=False,
                    guesses=["cigar", "cigar"],
                )
            )

        outcome = league_finalization.finalize_opportunity(db, "wordle", session_id)
        assert outcome["action"] == "no_results"
        assert live_awards(db, game_id="wordle") == []


class TestFeaturedMultiplier:
    def test_featured_opportunity_awards_double(self, db):
        day = today() - timedelta(days=2)
        users = [make_user(db, f"F{i}") for i in range(2)]
        add_featured(db, day, "wordle")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 5, True)

        league_finalization.finalize_opportunity(db, "wordle", session_id)

        awards = {str(a.user_id): a for a in live_awards(db, game_id="wordle")}
        # Game half keeps the relative rule (winner 100, loser 0), doubled once.
        assert awards[users[0].id].game_base_points == Decimal("100.00")
        assert awards[users[0].id].game_points == Decimal("200.00")
        assert awards[users[1].id].game_points == Decimal("0.00")
        # Global half: absolute attempts table (90 / 60), doubled once.
        assert awards[users[0].id].global_base_points == Decimal("90")
        assert awards[users[0].id].global_points == Decimal("180.00")
        assert awards[users[1].id].global_points == Decimal("120.00")
        # Legacy columns mirror the global half.
        assert awards[users[0].id].base_points == Decimal("90")
        assert awards[users[0].id].awarded_points == Decimal("180.00")
        assert awards[users[0].id].featured_multiplier == 2
        assert awards[users[0].id].is_featured_snapshot is True

    def test_later_featured_changes_do_not_rewrite_history(self, db):
        day = today() - timedelta(days=2)
        user = make_user(db, "Hist")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)
        before = live_awards(db, game_id="wordle")[0].awarded_points

        # Featuring the date after finalization must not move the award.
        add_featured(db, day, "wordle")
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        awards = live_awards(db, game_id="wordle")
        assert len(awards) == 1
        # Solo solve in 2: global absolute 90 (the old relative solo 50
        # lives on only in the game half now).
        assert awards[0].awarded_points == before == Decimal("90.00")
        assert awards[0].game_points == Decimal("50.00")
        assert awards[0].featured_multiplier == 1

    def test_spotlight_for_another_game_does_not_apply(self, db):
        day = today() - timedelta(days=2)
        user = make_user(db, "Other")
        add_featured(db, day, "anagram")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)

        league_finalization.finalize_opportunity(db, "wordle", session_id)

        assert live_awards(db, game_id="wordle")[0].featured_multiplier == 1


# ---------------------------------------------------------------------------
# Every game's native comparison
# ---------------------------------------------------------------------------

class TestNativeComparisons:
    def test_anagram_orders_by_solved_attempts_then_time(self, db):
        day = today() - timedelta(days=4)
        users = [make_user(db, f"A{i}") for i in range(3)]
        with db.sessions.begin() as session:
            row = AnagramGameRecord(
                game_date=day, mode="random", answer="garden", status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            session_id = str(row.id)
            moment = at(day)
            for user, attempts, active, won in [
                (users[0], 2, 30, True),
                (users[1], 2, 90, True),
                (users[2], 5, 10, False),
            ]:
                session.add(
                    AnagramUserGameRecord(
                        anagram_game_id=session_id, user_id=user.id,
                        scrambled_word="nrgdea", attempt_count=attempts,
                        status="won" if won else "lost", success=won,
                        guesses=["x"] * attempts, active_time_seconds=active,
                        completed_at=moment, solved_at=moment if won else None,
                    )
                )
        league_finalization.finalize_opportunity(db, "anagram", session_id)
        awards = {str(a.user_id): a for a in live_awards(db, game_id="anagram")}
        assert [awards[u.id].rank for u in users] == [1, 2, 3]
        assert "garden" not in str(awards[users[0].id].raw_metrics)

    def test_connections_orders_by_groups_then_words(self, db):
        day = today() - timedelta(days=4)
        users = [make_user(db, f"C{i}") for i in range(3)]
        with db.sessions.begin() as session:
            row = ConnectionsGameRecord(
                game_date=day, status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            session_id = str(row.id)
            for user, groups, words in [(users[0], 4, 16), (users[1], 2, 10), (users[2], 2, 8)]:
                session.add(
                    ConnectionsSubmissionRecord(
                        connections_game_id=session_id, user_id=user.id,
                        groups=[], correct_groups=groups, correct_words=words,
                        submitted_at=at(day),
                    )
                )
        league_finalization.finalize_opportunity(db, "connections", session_id)
        awards = {str(a.user_id): a for a in live_awards(db, game_id="connections")}
        assert [awards[u.id].rank for u in users] == [1, 2, 3]
        assert awards[users[0].id].raw_metrics == {"correct_groups": 4, "correct_words": 16}

    def test_trivia_orders_by_correctness_then_speed(self, db):
        day = today() - timedelta(days=4)
        users = [make_user(db, f"Q{i}") for i in range(3)]
        released = midnight(day)
        with db.sessions.begin() as session:
            row = TriviaDropQuestionRecord(
                question_text="?", status="ended",
                released_at=released, ended_at=released + timedelta(hours=1),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            question_id = str(row.id)
            for user, correct, ms in [(users[0], True, 1200), (users[1], True, 3400), (users[2], False, 300)]:
                session.add(
                    TriviaDropSubmissionRecord(
                        question_id=question_id, user_id=user.id,
                        selected_options=[0], is_correct=correct,
                        submitted_at=released + timedelta(milliseconds=ms),
                        response_time_ms=ms,
                    )
                )
        league_finalization.finalize_opportunity(db, "trivia-drop", question_id)
        awards = {str(a.user_id): a for a in live_awards(db, game_id="trivia-drop")}
        assert [awards[u.id].rank for u in users] == [1, 2, 3]
        assert awards[users[0].id].competition_at == released

    def test_reaction_time_ranks_best_valid_and_ignores_false_starts(self, db):
        day = today() - timedelta(days=4)
        users = [make_user(db, f"V{i}") for i in range(3)]
        with db.sessions.begin() as session:
            row = ReactionTimeGameRecord(
                game_date=day, status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            session_id = str(row.id)
            moment = at(day)
            submissions = [
                (users[0], 1, "valid", 210, None),
                (users[0], 2, "valid", 190, None),
                (users[1], 1, "valid", 250, None),
                (users[2], 1, "too_early", None, 400),
            ]
            for user, attempt, status, reaction, elapsed in submissions:
                session.add(
                    ReactionTimeSubmissionRecord(
                        reaction_time_game_id=session_id, user_id=user.id,
                        attempt_number=attempt, status=status,
                        reaction_time_ms=reaction, elapsed_ms=elapsed,
                        started_at=moment, green_at=moment, clicked_at=moment,
                    )
                )
        league_finalization.finalize_opportunity(db, "reaction-time", session_id)
        awards = {str(a.user_id): a for a in live_awards(db, game_id="reaction-time")}
        assert set(awards) == {users[0].id, users[1].id}
        assert awards[users[0].id].rank == 1
        assert awards[users[0].id].raw_metrics["best_ms"] == 190
        # Game half keeps the relative rule; global half is absolute ms.
        assert awards[users[0].id].game_base_points == Decimal("100.00")
        assert awards[users[1].id].game_base_points == Decimal("0.00")
        assert awards[users[0].id].global_base_points == Decimal("90")
        assert awards[users[1].id].global_base_points == Decimal("80")


_BANK = 0


def _bank_ids(db, count: int = 5) -> list[str]:
    """Real bank rows (the session-question FK requires them)."""
    global _BANK
    from app.db_models import CoupleGameQuestionRecord

    ids = []
    with db.sessions.begin() as session:
        for _ in range(count):
            _BANK += 1
            row = CoupleGameQuestionRecord(
                stable_key=f"league-q-{_BANK:04d}",
                sequence_number=(_BANK % 50) + 1,
                question_text=f"Would X agree with choice {_BANK}?",
                option_a="Always",
                option_b="Often",
                option_c="Rarely",
                option_d="Never",
            )
            session.add(row)
            session.flush()
            ids.append(str(row.id))
    return ids


def add_couple_session(
    db, start: date, user_a: UserDetails, user_b: UserDetails,
    correct_a: int, correct_b: int, status: str = "completed",
) -> tuple[str, str]:
    """One pair session with a tiny shared question set, answered+predicted."""
    member_a, member_b = canonical_couple(user_a.id, user_b.id)
    bank_ids = _bank_ids(db)
    with db.sessions.begin() as session:
        glob = CoupleGameRecord(
            game_date=start, status="ended",
            period_start_date=start, period_end_date=start + timedelta(days=7),
            started_at=midnight(start), ended_at=midnight(start + timedelta(days=7)),
            created_by="TEST",
        )
        session.add(glob)
        session.flush()
        pair = CoupleGameSessionRecord(
            couple_game_id=str(glob.id), user_a_id=member_a, user_b_id=member_b,
            status=status,
            completed_at=at(start) if status == "completed" else None,
        )
        session.add(pair)
        session.flush()
        pair_id = str(pair.id)
        # Five shared questions: each side answers all five, then predicts.
        for bank_id in bank_ids:
            question = CoupleGameSessionQuestionRecord(
                session_id=pair_id, question_id=bank_id,
                assignment="shared",
            )
            session.add(question)
            session.flush()
        return str(glob.id), pair_id


def answer_and_predict(db, pair_id: str, user_a: UserDetails, user_b: UserDetails,
                       correct_a: int, correct_b: int) -> None:
    from app.db_models import CoupleGameSessionAnswerRecord

    with db.sessions.begin() as session:
        questions = session.scalars(
            select(CoupleGameSessionQuestionRecord).where(
                CoupleGameSessionQuestionRecord.session_id == pair_id
            )
        ).all()
        # Originals exist so predictions have something to be judged against.
        for question in questions:
            for user in (user_a, user_b):
                session.add(
                    CoupleGameSessionAnswerRecord(
                        session_id=pair_id, session_question_id=str(question.id),
                        user_id=user.id, selected_option_index=0,
                    )
                )
        session.flush()
        for index, question in enumerate(questions):
            session.add(
                CoupleGameSessionPredictionRecord(
                    session_id=pair_id, session_question_id=str(question.id),
                    predictor_user_id=user_a.id, predicted_option_index=0,
                    is_correct=index < correct_a,
                )
            )
            session.add(
                CoupleGameSessionPredictionRecord(
                    session_id=pair_id, session_question_id=str(question.id),
                    predictor_user_id=user_b.id, predicted_option_index=0,
                    is_correct=index < correct_b,
                )
            )


class TestCoupleGames:
    def test_questionnaire_ranks_pairs_by_joint_accuracy(self, db):
        start = today() - timedelta(days=10)
        a1, b1 = make_user(db, "PA1"), make_user(db, "PB1")
        a2, b2 = make_user(db, "PA2"), make_user(db, "PB2")
        glob1, pair1 = add_couple_session(db, start, a1, b1, 0, 0)
        glob2, pair2 = add_couple_session(db, start, a2, b2, 0, 0)
        # Pair one is sharper together (5+4) than pair two (3+2). Both belong
        # to one global period each; finalize each global separately below by
        # pointing the pairs at a single shared global.
        answer_and_predict(db, pair1, a1, b1, 5, 4)
        answer_and_predict(db, pair2, a2, b2, 3, 2)
        with db.sessions.begin() as session:
            second = session.get(CoupleGameSessionRecord, pair2)
            second.couple_game_id = glob1
        with db.sessions.begin() as session:
            orphan = session.get(CoupleGameRecord, glob2)
            session.delete(orphan)

        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob1)

        awards = live_awards(db, game_id="couple-questionnaire")
        assert len(awards) == 2
        by_pair = {(a.member_a_id, a.member_b_id): a for a in awards}
        first = by_pair[canonical_couple(a1.id, b1.id)]
        second = by_pair[canonical_couple(a2.id, b2.id)]
        # Both halves are absolute here: the couple board rule is the
        # proportional accuracy itself (9/10 and 5/10 of the session's
        # scoreable questions), not a placement/duel between the pairs.
        assert first.game_base_points == Decimal("90.00")
        assert second.game_base_points == Decimal("50.00")
        assert first.global_base_points == Decimal("90.00")
        assert second.global_base_points == Decimal("50.00")
        assert first.base_points == Decimal("90.00")
        assert second.base_points == Decimal("50.00")
        assert first.raw_metrics == {"correct_predictions": 9, "total_questions": 10}
        assert second.raw_metrics == {"correct_predictions": 5, "total_questions": 10}
        assert first.user_id is None

    def test_incomplete_pair_sessions_are_not_ranked(self, db):
        start = today() - timedelta(days=10)
        a, b = make_user(db, "IA"), make_user(db, "IB")
        glob, _pair = add_couple_session(db, start, a, b, 0, 0, status="prediction")

        outcome = league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)
        assert outcome["action"] == "no_results"

    def test_couple_boards_rank_pairs_and_serve_me(self, db):
        start = week_start(today()) - timedelta(days=7)
        a1, b1 = make_user(db, "CA1"), make_user(db, "CB1")
        a2, b2 = make_user(db, "CA2"), make_user(db, "CB2")
        glob, pair1 = add_couple_session(db, start, a1, b1, 0, 0)
        _glob2, pair2 = add_couple_session(db, start, a2, b2, 0, 0)
        answer_and_predict(db, pair1, a1, b1, 5, 5)
        answer_and_predict(db, pair2, a2, b2, 2, 1)
        with db.sessions.begin() as session:
            second = session.get(CoupleGameSessionRecord, pair2)
            second.couple_game_id = glob
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)

        game_board = league_boards.game_board(
            db, game_id="couple-questionnaire", competition_type="couple",
            period="weekly", anchor=start,
        )
        assert [entry["rank"] for entry in game_board["entries"]] == [1, 2]
        first = game_board["entries"][0]
        assert {first["member_a_id"], first["member_b_id"]} == {a1.id, b1.id}
        assert first["score"] == 100.0

        # One entry per pair: member order never duplicates a couple.
        pairs = [(e["member_a_id"], e["member_b_id"]) for e in game_board["entries"]]
        assert all(a <= b for a, b in pairs)
        assert len(set(pairs)) == 2

        everyone = league_boards.global_board(
            db, competition_type="couple", period="weekly", anchor=start,
        )
        assert everyone["entries"][0]["per_game"] == {"couple-questionnaire": 100.0}

        mine = league_boards.my_standing(
            db, user_id=a2.id, competition_type="couple",
            period="weekly", anchor=start,
        )
        assert len(mine["entries"]) == 1
        assert mine["entries"][0]["rank"] == 2

        # Couples never leak onto the individual board.
        solo = league_boards.global_board(
            db, competition_type="individual", period="weekly", anchor=start,
        )
        assert solo["entries"] == []

    def test_two_key_escape_ranks_finished_by_elapsed(self, db):
        start = today() - timedelta(days=10)
        fast_a, fast_b = make_user(db, "FA"), make_user(db, "FB")
        slow_a, slow_b = make_user(db, "SA"), make_user(db, "SB")
        idle_a, idle_b = make_user(db, "NA"), make_user(db, "NB")
        # Overlays name the mutual-selection session that resolved the pair.
        _glob, fast_pair = add_couple_session(db, start, fast_a, fast_b, 0, 0)
        _glob, slow_pair = add_couple_session(db, start, slow_a, slow_b, 0, 0)
        _glob, idle_pair = add_couple_session(db, start, idle_a, idle_b, 0, 0)
        with db.sessions.begin() as session:
            game = TwoKeyEscapeGameRecord(
                game_date=start, status="ended",
                period_start_date=start, period_end_date=start + timedelta(days=1),
                started_at=midnight(start), ended_at=midnight(start + timedelta(days=1)),
                created_by="TEST",
                grid_size=12, maze_seed="seed",
                start_row=0, start_col=0, exit_row=11, exit_col=11,
                walls=[], locked_cells=[], key_mapping={}, gate_indexes=[],
            )
            session.add(game)
            session.flush()
            game_id = str(game.id)
            moment = at(start)
            for (pair_id, elapsed, done) in [
                (fast_pair, 95.5, True),
                (slow_pair, 210.0, True),
                (idle_pair, None, False),
            ]:
                pair = session.get(CoupleGameSessionRecord, pair_id)
                member_a, member_b = canonical_couple(str(pair.user_a_id), str(pair.user_b_id))
                session.add(
                    TwoKeyEscapeSessionRecord(
                        two_key_escape_game_id=game_id, couple_session_id=pair_id,
                        user_a_id=member_a, user_b_id=member_b,
                        player_row=0, player_col=0,
                        total_moves=10, total_keys=2,
                        elapsed_seconds=elapsed, completed=done,
                        completed_at=moment if done else None,
                    )
                )
        league_finalization.finalize_opportunity(db, "two-key-escape", game_id)
        awards = live_awards(db, game_id="two-key-escape")
        assert len(awards) == 2
        by_pair = {(a.member_a_id, a.member_b_id): a for a in awards}
        # Both halves pay every completed escape the same 100: no time
        # bonus on either path, and the slower pair is not zeroed for
        # losing a duel it was never supposed to run.
        assert by_pair[canonical_couple(fast_a.id, fast_b.id)].game_base_points == Decimal("100")
        assert by_pair[canonical_couple(slow_a.id, slow_b.id)].game_base_points == Decimal("100")
        assert by_pair[canonical_couple(fast_a.id, fast_b.id)].global_base_points == Decimal("100")
        assert by_pair[canonical_couple(slow_a.id, slow_b.id)].global_base_points == Decimal("100")


# ---------------------------------------------------------------------------
# Period aggregation, normalization, all-time
# ---------------------------------------------------------------------------

class TestPeriodAggregation:
    def test_missed_opportunities_count_as_zero(self, db):
        monday = week_start(today()) - timedelta(days=7)
        day_one, day_two = monday, monday + timedelta(days=2)
        alice, bob = make_user(db, "M1"), make_user(db, "M2")
        first = add_wordle_session(db, day_one)
        add_wordle_result(db, first, alice.id, 2, True)
        second = add_wordle_session(db, day_two)
        add_wordle_result(db, second, alice.id, 2, True)
        add_wordle_result(db, second, bob.id, 5, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="weekly", anchor=day_two,
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        # Alice: (50 solo + 100) / 2 opportunities. Bob missed the first: (0 + 0) / 2.
        assert scores[alice.id] == 75.0
        assert scores[bob.id] == 0.0
        assert board["opportunity_count"] == 2

    def test_daily_window_is_exact(self, db):
        day_one = today() - timedelta(days=6)
        day_two = today() - timedelta(days=5)
        user = make_user(db, "D1")
        first = add_wordle_session(db, day_one)
        add_wordle_result(db, first, user.id, 2, True)
        second = add_wordle_session(db, day_two)
        add_wordle_result(db, second, user.id, 2, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="daily", anchor=day_one,
        )
        assert [entry["user_id"] for entry in board["entries"]] == [user.id]
        assert board["entries"][0]["score"] == 50.0

    def test_trivia_frequency_is_averaged_not_summed(self, db):
        day = today() - timedelta(days=6)
        speedy, steady = make_user(db, "S1"), make_user(db, "S2")
        released = midnight(day)
        with db.sessions.begin() as session:
            questions = []
            for index in range(2):
                row = TriviaDropQuestionRecord(
                    question_text=f"q{index}", status="ended",
                    released_at=released + timedelta(hours=index),
                    ended_at=released + timedelta(hours=index + 1),
                    created_by="TEST",
                )
                session.add(row)
                session.flush()
                questions.append(str(row.id))
            moment = released
            session.add_all([
                TriviaDropSubmissionRecord(
                    question_id=questions[0], user_id=speedy.id,
                    selected_options=[0], is_correct=True,
                    submitted_at=moment, response_time_ms=500,
                ),
                TriviaDropSubmissionRecord(
                    question_id=questions[0], user_id=steady.id,
                    selected_options=[0], is_correct=True,
                    submitted_at=moment, response_time_ms=5000,
                ),
                TriviaDropSubmissionRecord(
                    question_id=questions[1], user_id=speedy.id,
                    selected_options=[1], is_correct=False,
                    submitted_at=moment, response_time_ms=400,
                ),
                TriviaDropSubmissionRecord(
                    question_id=questions[1], user_id=steady.id,
                    selected_options=[0], is_correct=True,
                    submitted_at=moment, response_time_ms=900,
                ),
            ])
        league_finalization.finalize_due_opportunities(db, ["trivia-drop"])

        board = league_boards.game_board(
            db, game_id="trivia-drop", competition_type="individual",
            period="daily", anchor=day,
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        # Game boards score correctness plus absolute speed here, not
        # placement: speedy (99.60 + 0) / 2, steady (96.60 + 99.40) / 2.
        # (These legacy questions carry no option rows, so accuracy falls
        # back to the stored server-side exact-match verdict.)
        assert scores[speedy.id] == 49.8
        assert scores[steady.id] == 98.0

    def test_all_time_accumulates_weekly_contributions(self, db):
        week_one = week_start(today()) - timedelta(days=14)
        week_two = week_start(today()) - timedelta(days=7)
        user = make_user(db, "AT")
        for monday in (week_one, week_two):
            session_id = add_wordle_session(db, monday)
            add_wordle_result(db, session_id, user.id, 2, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.game_board(
            db, game_id="wordle", competition_type="individual", period="all_time",
        )
        assert board["entries"][0]["score"] == 100.0

    def test_late_finalization_keeps_the_event_period(self, db):
        # A session from last week finalized today still belongs to last week.
        start = week_start(today()) - timedelta(days=9)
        user = make_user(db, "LZ")
        session_id = add_wordle_session(db, start)
        add_wordle_result(db, session_id, user.id, 3, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)

        this_week = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="weekly", anchor=today(),
        )
        last_week = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="weekly", anchor=start,
        )
        assert this_week["entries"] == []
        assert [entry["user_id"] for entry in last_week["entries"]] == [user.id]

    def test_global_board_sums_games_without_mixing_types(self, db):
        day = today() - timedelta(days=6)
        user = make_user(db, "G1")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        individual = league_boards.global_board(
            db, competition_type="individual", period="weekly", anchor=day,
        )
        couples = league_boards.global_board(
            db, competition_type="couple", period="weekly", anchor=day,
        )
        # Solo solve in 2: absolute global 90 (relative solo 50 survives
        # only on per-game boards).
        assert individual["entries"][0]["per_game"] == {"wordle": 90.0}
        assert individual["entries"][0]["score"] == 90.0
        assert couples["entries"] == []

    def test_featured_doubles_before_the_average(self, db):
        monday = week_start(today()) - timedelta(days=7)
        user = make_user(db, "FD")
        add_featured(db, monday, "wordle")
        first = add_wordle_session(db, monday)
        add_wordle_result(db, first, user.id, 2, True)
        second = add_wordle_session(db, monday + timedelta(days=1))
        add_wordle_result(db, second, user.id, 2, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="weekly", anchor=monday,
        )
        # (100 featured solo + 50 plain solo) / 2 opportunities: a solo
        # win is 50 base, doubled to 100 when featured.
        assert board["entries"][0]["score"] == 75.0
        assert board["entries"][0]["is_featured"] is True


# ---------------------------------------------------------------------------
# Correction path
# ---------------------------------------------------------------------------

class TestCorrection:
    def test_reprocess_supersedes_and_reissues(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"X{i}") for i in range(2)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 4, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)

        outcome = league_finalization.reprocess_opportunity(db, "wordle", session_id)

        assert outcome["action"] == "finalized"
        with db.sessions() as session:
            from app.db_models import GameScoreAwardRecord as Award

            rows = session.scalars(
                select(Award).where(
                    Award.game_id == "wordle", Award.opportunity_id == session_id
                )
            ).all()
            assert len(rows) == 4
            assert sum(1 for row in rows if row.superseded_at is None) == 2
            assert sum(1 for row in rows if row.superseded_at is not None) == 2
        live = live_awards(db, game_id="wordle")
        # Legacy columns mirror the global half: absolute 2-guess (90) and
        # 4-guess (70) scores, unchanged by the reprocess itself.
        assert {str(a.user_id): a.awarded_points for a in live} == {
            users[0].id: Decimal("90.00"),
            users[1].id: Decimal("70.00"),
        }
        assert {str(a.user_id): a.game_points for a in live} == {
            users[0].id: Decimal("100.00"),
            users[1].id: Decimal("0.00"),
        }
        assert all(a.global_scoring_version == 2 for a in live)
        assert all(a.game_scoring_version == 1 for a in live)

    def test_reprocess_without_awards_is_refused(self, db):
        day = today() - timedelta(days=3)
        session_id = add_wordle_session(db, day)

        import pytest
        from fastapi import HTTPException

        league_finalization.finalize_opportunity(db, "wordle", session_id)
        with pytest.raises(HTTPException) as caught:
            league_finalization.reprocess_opportunity(db, "wordle", session_id)
        assert caught.value.status_code == 404

    def test_reprocess_of_an_open_event_keeps_live_awards(self, db):
        """ENG-006: a refused recompute must not strand superseded-only state.

        Reprocess used to supersede in one transaction and recompute in a second,
        so an event that was no longer closed left its originals superseded with
        no live awards. Supersede and recompute now commit atomically: the 409
        below rolls both halves back and the board keeps showing the originals.
        """
        import pytest
        from fastapi import HTTPException

        day = today() - timedelta(days=3)
        users = [make_user(db, f"X{i}") for i in range(2)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 4, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)
        assert len(live_awards(db, game_id="wordle")) == 2

        # Reopen the event: sessions never reopen in production, which is exactly
        # why the old code met this state only by interleaving -- and stranded it.
        with db.sessions.begin() as session:
            session.get(WordleGameRecord, session_id).status = "active"

        with pytest.raises(HTTPException) as caught:
            league_finalization.reprocess_opportunity(db, "wordle", session_id)
        assert caught.value.status_code == 409

        kept = live_awards(db, game_id="wordle")
        assert len(kept) == 2
        assert all(row.superseded_at is None for row in kept)

    def test_due_scan_logs_skipped_events(self, db, caplog):
        """ENG-006: unfinalizable events are retried visibly, not silently skipped."""
        import logging
        import uuid

        from app.db_models import GameScoreOpportunityRecord

        ghost_id = str(uuid.uuid4())
        with db.sessions.begin() as session:
            session.add(GameScoreOpportunityRecord(
                game_id="wordle",
                opportunity_id=ghost_id,
                competition_type="individual",
                activation_date=today(),
                competition_at=at(today()),
            ))

        with caplog.at_level(logging.WARNING, logger="app.services.league_finalization"):
            summary = league_finalization.finalize_due_opportunities(db, ["wordle"])

        assert summary == {"wordle": {"snapshots": 0, "finalized": 0}}
        assert ghost_id in caplog.text
        assert "skipped finalization" in caplog.text


# ---------------------------------------------------------------------------
# Publish-time finalization (admin Updates and trivia releases)
# ---------------------------------------------------------------------------

class TestPublishTimeFinalization:
    def test_wordle_update_scores_the_session_it_closes(self, db):
        user = make_user(db, "PU")
        game_scheduler.ensure_single_game(db, "wordle", midnight(today()))
        with db.sessions() as session:
            current = session.scalar(
                select(WordleGameRecord).where(WordleGameRecord.status == "active")
            )
            session_id, answer = str(current.id), current.answer
        from app.models import WordleGuessRequest
        from app.services import wordle_gameplay

        wordle_gameplay.submit_guess(
            db, user.id, WordleGuessRequest(game_id=session_id, guess=answer)
        )

        admin = admin_client()
        drawn = admin.get("/admin/wordle/preview").json()
        updated = admin.post(
            "/admin/wordle",
            json={"mode": "random", "word": drawn["word"], "token": drawn["token"]},
        )
        assert updated.status_code == 201, updated.text

        # No reset run involved: the Update itself finalized the session it
        # ended, and snapshotted the one it opened. One winning guess:
        # absolute global 100 (relative solo 50 on the game half).
        awards = live_awards(db, game_id="wordle", opportunity_id=session_id)
        assert len(awards) == 1
        assert awards[0].awarded_points == Decimal("100.00")
        assert awards[0].game_points == Decimal("50.00")
        with db.sessions() as session:
            assert (
                session.scalar(
                    select(GameScoreOpportunityRecord).where(
                        GameScoreOpportunityRecord.game_id == "wordle",
                        GameScoreOpportunityRecord.opportunity_id == updated.json()["id"],
                    )
                )
                is not None
            )


# ---------------------------------------------------------------------------
# Scheduler closure hook
# ---------------------------------------------------------------------------

class TestSchedulerHook:
    def test_crossing_a_boundary_finalizes_the_ended_session(self, db):
        day = today() - timedelta(days=1)
        user = make_user(db, "SH")
        session_id = add_wordle_session(db, day, status="active")
        add_wordle_result(db, session_id, user.id, 2, True)
        # Today opens featured: the new session's snapshot must say so even
        # if the spotlight moves again before anything finalizes.
        add_featured(db, today(), "wordle")

        summary = game_scheduler.ensure_game_sessions(db)

        assert summary["wordle"]["action"] in ("created", "current")
        awards = live_awards(db, game_id="wordle", opportunity_id=session_id)
        assert len(awards) == 1
        # Solo solve in 2: absolute global 90 (relative solo 50 on game half).
        assert awards[0].awarded_points == Decimal("90.00")
        assert awards[0].game_points == Decimal("50.00")

        new_id = summary["wordle"].get("session_id")
        assert new_id and new_id != session_id
        with db.sessions() as session:
            snapshot = session.scalar(
                select(GameScoreOpportunityRecord).where(
                    GameScoreOpportunityRecord.game_id == "wordle",
                    GameScoreOpportunityRecord.opportunity_id == new_id,
                )
            )
            assert snapshot is not None
            assert snapshot.is_featured_snapshot is True

        # Unfeaturing today afterwards cannot rewrite that snapshot.
        with db.sessions.begin() as session:
            row = session.scalar(
                select(FeaturedGameRecord).where(
                    FeaturedGameRecord.game_date == today(),
                    FeaturedGameRecord.game_id == "wordle",
                )
            )
            session.delete(row)
        league_finalization.finalize_due_opportunities(db, ["wordle"])
        with db.sessions() as session:
            again = session.scalar(
                select(GameScoreOpportunityRecord).where(
                    GameScoreOpportunityRecord.game_id == "wordle",
                    GameScoreOpportunityRecord.opportunity_id == new_id,
                )
            )
            assert again.is_featured_snapshot is True


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

class TestLeaderboardEndpoints:
    def _seed(self, db):
        # Inside the current UTC week, so the default weekly board sees it
        # whatever day the suite runs on.
        day = max(today() - timedelta(days=2), week_start(today()))
        user = make_user(db, "EP")
        session_id = add_wordle_session(db, day, answer="mango")
        add_wordle_result(db, session_id, user.id, 2, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)
        return user

    def test_individual_board_shape_and_sort(self, db):
        user = self._seed(db)
        client = user_client(user)
        response = client.get("/leaderboards/individual?period=weekly")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["period"] == "weekly"
        assert body["total"] == 1
        entry = body["entries"][0]
        assert entry["user_id"] == user.id
        assert entry["rank"] == 1
        # Solo solve in 2: absolute global 90 on the combined board.
        assert entry["score"] == 90.0
        assert "mango" not in response.text

    def test_game_board_and_me(self, db):
        user = self._seed(db)
        client = user_client(user)
        game = client.get("/leaderboards/games/wordle?competition_type=individual&period=all_time")
        assert game.status_code == 200, game.text
        assert game.json()["entries"][0]["native"] == {"solved": True, "attempts": 2}
        me = client.get("/leaderboards/me?competition_type=individual&period=all_time")
        assert me.status_code == 200, me.text
        assert me.json()["entries"][0]["rank"] == 1

    def test_mismatched_combinations_are_refused(self, db):
        user = self._seed(db)
        client = user_client(user)
        assert client.get("/leaderboards/games/wordle?competition_type=couple").status_code == 400
        assert client.get("/leaderboards/games/couple-questionnaire?competition_type=individual").status_code == 400
        assert client.get("/leaderboards/individual?period=fortnightly").status_code == 400
        assert client.get("/leaderboards/games/nope?competition_type=individual").status_code == 400

    def test_pagination(self, db):
        day = today() - timedelta(days=2)
        users = [make_user(db, f"PG{i}") for i in range(3)]
        session_id = add_wordle_session(db, day)
        for index, user in enumerate(users):
            add_wordle_result(db, session_id, user.id, index + 2, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)
        client = user_client(users[0])
        first = client.get("/leaderboards/individual?period=all_time&limit=2")
        second = client.get("/leaderboards/individual?period=all_time&skip=2&limit=2")
        assert first.json()["total"] == 3
        assert len(first.json()["entries"]) == 2
        assert len(second.json()["entries"]) == 1

    def test_auth_gates(self, db):
        self._seed(db)
        app.dependency_overrides.pop(get_current_user, None)
        try:
            anonymous = TestClient(app, follow_redirects=False)
            assert anonymous.get("/leaderboards/individual").status_code in (401, 403)
        finally:
            from conftest import override_get_current_user as _default_user

            app.dependency_overrides[get_current_user] = _default_user
        app.dependency_overrides[get_current_user] = override_get_admin_user
        staff = TestClient(app, follow_redirects=False)
        assert staff.get("/leaderboards/individual").status_code == 403

    def test_admin_finalize_and_summary(self, db):
        day = today() - timedelta(days=2)
        user = make_user(db, "AF")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        admin = admin_client()

        summary_before = admin.get("/admin/leaderboards/summary")
        assert summary_before.status_code == 200, summary_before.text

        response = admin.post(
            "/admin/leaderboards/finalize",
            json={"game_id": "wordle", "opportunity_id": session_id},
        )
        assert response.status_code == 200, response.text
        assert response.json()["action"] == "finalized"

        summary = admin.get("/admin/leaderboards/summary").json()
        assert summary["games"]["wordle"]["opportunities"] == 1
        assert summary["games"]["wordle"]["awards"] == 1

        reprocess = admin.post(
            "/admin/leaderboards/reprocess",
            json={"game_id": "wordle", "opportunity_id": session_id},
        )
        assert reprocess.status_code == 200, reprocess.text

        refused = admin.post(
            "/admin/leaderboards/finalize",
            json={"game_id": "wordle", "opportunity_id": "00000000-0000-4000-8000-000000000000"},
        )
        assert refused.status_code in (404, 409)

    def test_participants_cannot_finalize(self, db):
        user = self._seed(db)
        client = user_client(user)
        response = client.post(
            "/admin/leaderboards/finalize",
            json={"game_id": "wordle", "opportunity_id": "00000000-0000-4000-8000-000000000000"},
        )
        assert response.status_code == 403

    def test_admin_boards_mirror_the_player_boards(self, db):
        user = self._seed(db)
        admin = admin_client()

        individual = admin.get("/admin/leaderboards/individual?period=all_time")
        assert individual.status_code == 200, individual.text
        assert individual.json()["entries"][0]["user_id"] == user.id

        game = admin.get(
            "/admin/leaderboards/games/wordle?competition_type=individual&period=all_time"
        )
        assert game.status_code == 200, game.text
        assert game.json()["entries"][0]["native"] == {"solved": True, "attempts": 2}

        refused = admin.get(
            "/admin/leaderboards/games/wordle?competition_type=couple"
        )
        assert refused.status_code == 400

    def test_participants_cannot_read_admin_boards(self, db):
        user = self._seed(db)
        client = user_client(user)
        assert client.get("/admin/leaderboards/individual").status_code == 403
        assert client.get("/admin/leaderboards/couples").status_code == 403
        assert (
            client.get("/admin/leaderboards/games/wordle").status_code == 403
        )


# ---------------------------------------------------------------------------
# Absolute global profiles (pure unit tests: same result, same score)
# ---------------------------------------------------------------------------

class TestGlobalProfiles:
    def test_wordle_table_and_invariants(self):
        from app.services.league_global_scoring import wordle_global_base

        assert wordle_global_base({"solved": True, "attempts": 1}) == Decimal("100")
        assert wordle_global_base({"solved": True, "attempts": 4}) == Decimal("70")
        assert wordle_global_base({"solved": True, "attempts": 5}) == Decimal("60")
        assert wordle_global_base({"solved": True, "attempts": 6}) == Decimal("50")
        assert wordle_global_base({"solved": False, "attempts": 6}) == Decimal("0")
        assert wordle_global_base({"solved": False, "attempts": 1}) == Decimal("0")
        # Fewer always beats more; solving always beats failing.
        assert wordle_global_base({"solved": True, "attempts": 4}) > wordle_global_base(
            {"solved": True, "attempts": 5}
        )

    def test_anagram_tiers_never_overlap(self):
        from app.services.league_global_scoring import anagram_global_base

        # Slowest 4-attempt solve still beats the fastest 5-attempt solve.
        assert anagram_global_base(
            {"solved": True, "attempts": 4, "active_time_seconds": 10_000}
        ) == Decimal("61")
        assert anagram_global_base(
            {"solved": True, "attempts": 5, "active_time_seconds": 1}
        ) == Decimal("60")
        assert anagram_global_base(
            {"solved": True, "attempts": 1, "active_time_seconds": 10}
        ) == Decimal("100")
        assert anagram_global_base(
            {"solved": False, "attempts": 1, "active_time_seconds": 5}
        ) == Decimal("0")

    def test_reaction_is_monotonic_in_absolute_ms(self):
        from app.services.league_global_scoring import reaction_global_base

        times = [150, 190, 250, 280, 330, 380, 450, 600, 750, 900, 5_000]
        scores = [reaction_global_base({"best_ms": ms}) for ms in times]
        assert scores == sorted(scores, reverse=True)
        assert scores[0] == Decimal("100")
        assert all(score >= Decimal("1") for score in scores)
        assert reaction_global_base({}) == Decimal("0")

    def test_trivia_accuracy_plus_fixed_speed(self):
        from app.services.league_global_scoring import trivia_global_base

        assert trivia_global_base({"accuracy": 1.0, "response_time_ms": 0}) == Decimal("100")
        # Slow correct keeps its 80 accuracy points, no speed bonus.
        assert trivia_global_base({"accuracy": 1.0, "response_time_ms": 60_000}) == Decimal("80")
        assert trivia_global_base({"accuracy": 0.0, "response_time_ms": 100}) == Decimal("0")
        # Half credit, instant: 40 + 10.
        assert trivia_global_base({"accuracy": 0.5, "response_time_ms": 0}) == Decimal("50")

    def test_connections_partial_progress(self):
        from app.services.league_global_scoring import connections_global_base

        assert connections_global_base({"correct_words": 1, "correct_groups": 0}) == Decimal("4")
        assert connections_global_base({"correct_words": 4, "correct_groups": 1}) == Decimal("25")
        assert connections_global_base({"correct_words": 16, "correct_groups": 4}) == Decimal("100")
        assert connections_global_base({"correct_words": 0, "correct_groups": 0}) == Decimal("0")

    def test_couple_proportion_and_two_key_binary(self):
        from app.services.league_global_scoring import (
            couple_global_base,
            two_key_escape_global_base,
        )

        assert couple_global_base({"correct_predictions": 1, "total_questions": 10}) == Decimal("10")
        assert couple_global_base({"correct_predictions": 7, "total_questions": 10}) == Decimal("70")
        assert couple_global_base({"correct_predictions": 0, "total_questions": 10}) == Decimal("0")
        assert couple_global_base({"correct_predictions": 3, "total_questions": 0}) == Decimal("0")
        assert two_key_escape_global_base({"elapsed_seconds": 95.5}) == Decimal("100")
        assert two_key_escape_global_base({"elapsed_seconds": 9_999.0}) == Decimal("100")

    def test_unknown_game_is_refused_not_invented(self):
        import pytest

        from app.services.league_global_scoring import global_base_for

        with pytest.raises(Exception):
            global_base_for("no-such-game", {"solved": True})


class TestTriviaAccuracy:
    """The anti-select-all safeguard across key distributions."""

    def test_all_selected_single_correct_scores_zero(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([0, 1, 2, 3], [2], [0, 1, 2, 3]) == 0.0

    def test_all_selected_three_correct_scores_zero(self):
        from app.services.league_global_scoring import trivia_accuracy

        # The reported loophole: (3 - 1) / 3 = 0.667 under the old formula.
        assert trivia_accuracy([0, 1, 2, 3], [0, 1, 2], [0, 1, 2, 3]) == 0.0

    def test_exact_match_is_full_credit(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([1], [1], [0, 1, 2, 3]) == 1.0
        assert trivia_accuracy([0, 2], [0, 2], [0, 1, 2, 3]) == 1.0
        # Selecting every option of an all-correct key is an exact match.
        assert trivia_accuracy([0, 1], [0, 1], [0, 1]) == 1.0

    def test_partial_without_wrong_picks_keeps_credit(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([0], [0, 1], [0, 1, 2, 3]) == 0.5

    def test_partial_with_wrong_pick_is_reduced(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([0, 2], [0, 1], [0, 1, 2, 3]) == 0.0
        # Adding the wrong pick lowered 0.5 to 0; wrong picks never raise.
        assert trivia_accuracy([0, 2], [0, 1], [0, 1, 2, 3]) <= trivia_accuracy(
            [0], [0, 1], [0, 1, 2, 3]
        )

    def test_empty_selection_scores_zero(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([], [1], [0, 1, 2, 3]) == 0.0
        assert trivia_accuracy(None, [1], [0, 1, 2, 3]) == 0.0

    def test_wrong_single_answer_scores_zero(self):
        from app.services.league_global_scoring import trivia_accuracy

        assert trivia_accuracy([3], [1], [0, 1, 2, 3]) == 0.0

    def test_speed_never_pays_for_wrong_answers(self):
        from app.services.league_global_scoring import trivia_global_base

        assert trivia_global_base({"accuracy": 0.0, "response_time_ms": 0}) == Decimal("0")

    def test_identical_answers_differ_only_by_speed(self):
        from app.services.league_global_scoring import trivia_global_base

        fast = trivia_global_base({"accuracy": 1.0, "response_time_ms": 500})
        slow = trivia_global_base({"accuracy": 1.0, "response_time_ms": 15_000})
        assert fast == Decimal("99.60")
        assert slow == Decimal("90.00")
        assert fast > slow

    def test_slow_correct_keeps_correctness_points(self):
        from app.services.league_global_scoring import trivia_global_base

        assert trivia_global_base({"accuracy": 1.0, "response_time_ms": 120_000}) == Decimal("80")

    def test_all_selected_end_to_end_scores_zero(self, db):
        from app.db_models import TriviaDropOptionRecord

        day = today() - timedelta(days=4)
        blanket = make_user(db, "BL")
        released = midnight(day)
        with db.sessions.begin() as session:
            row = TriviaDropQuestionRecord(
                question_text="pick three", status="ended",
                released_at=released, ended_at=released + timedelta(hours=1),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            question_id = str(row.id)
            for index, correct in [(0, True), (1, True), (2, True), (3, False)]:
                session.add(
                    TriviaDropOptionRecord(
                        question_id=question_id, option_index=index,
                        option_text=f"opt{index}", is_correct=correct,
                    )
                )
            session.flush()
            session.add(
                TriviaDropSubmissionRecord(
                    question_id=question_id, user_id=blanket.id,
                    selected_options=[0, 1, 2, 3], is_correct=False,
                    submitted_at=released, response_time_ms=100,
                )
            )
        league_finalization.finalize_opportunity(db, "trivia-drop", question_id)

        awards = live_awards(db, game_id="trivia-drop")
        assert len(awards) == 1
        assert awards[0].raw_metrics["accuracy"] == 0.0
        assert awards[0].global_base_points == Decimal("0")
        assert awards[0].game_base_points == Decimal("0")


# ---------------------------------------------------------------------------
# Absolute global boards (integration: fields, not ranks, decide)
# ---------------------------------------------------------------------------

class TestAbsoluteGlobalBoards:
    def test_worse_rank_can_outscore_better_rank_globally(self, db):
        # The spec's example: a 4-guess last place beats a 5-guess first
        # place on the global board, while game boards keep the ranks.
        # Separate weeks so each weekly contribution is undiluted.
        week_a = week_start(today()) - timedelta(days=14)
        week_b = week_start(today()) - timedelta(days=7)
        lone = make_user(db, "G5")
        first_session = add_wordle_session(db, week_a)
        add_wordle_result(db, first_session, lone.id, 5, True)
        rivals = [make_user(db, f"G4{i}") for i in range(2)]
        second_session = add_wordle_session(db, week_b)
        add_wordle_result(db, second_session, rivals[0].id, 2, True)
        add_wordle_result(db, second_session, rivals[1].id, 4, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        Kate = league_boards.global_board(
            db, competition_type="individual", period="all_time",
        )
        scores = {entry["user_id"]: entry["score"] for entry in Kate["entries"]}
        assert scores[lone.id] == 60.0
        assert scores[rivals[1].id] == 70.0
        assert scores[rivals[0].id] == 90.0

        game = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="all_time",
        )
        ranks = {entry["user_id"]: entry["rank"] for entry in game["entries"]}
        assert ranks[rivals[0].id] == 1
        assert ranks[lone.id] == 2
        assert ranks[rivals[1].id] == 3

    def test_same_result_same_global_score_across_fields(self, db):
        week_a = week_start(today()) - timedelta(days=14)
        week_b = week_start(today()) - timedelta(days=7)
        users = [make_user(db, f"EQ{i}") for i in range(2)]
        for user, monday in zip(users, (week_a, week_b)):
            session_id = add_wordle_session(db, monday)
            add_wordle_result(db, session_id, user.id, 4, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.global_board(
            db, competition_type="individual", period="all_time",
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        # Identical 4-guess solves in different fields and weeks: equal.
        assert scores[users[0].id] == 70.0
        assert scores[users[1].id] == 70.0

    def test_equal_global_weight_across_games(self, db):
        day = today() - timedelta(days=4)
        user = make_user(db, "W8")
        wordle_id = add_wordle_session(db, day)
        add_wordle_result(db, wordle_id, user.id, 3, True)
        with db.sessions.begin() as session:
            row = ReactionTimeGameRecord(
                game_date=day, status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            reaction_id = str(row.id)
            moment = at(day)
            session.add(
                ReactionTimeSubmissionRecord(
                    reaction_time_game_id=reaction_id, user_id=user.id,
                    attempt_number=1, status="valid",
                    reaction_time_ms=250, elapsed_ms=250,
                    started_at=moment, green_at=moment, clicked_at=moment,
                )
            )
        league_finalization.finalize_due_opportunities(db, ["wordle", "reaction-time"])

        board = league_boards.global_board(
            db, competition_type="individual", period="daily", anchor=day,
        )
        entry = board["entries"][0]
        # 3-guess wordle and 250ms reaction both map to 80: one performance
        # language, two games.
        assert entry["per_game"] == {"wordle": 80.0, "reaction-time": 80.0}
        assert entry["score"] == 160.0

    def test_missed_opportunities_dilute_the_global_average(self, db):
        monday = week_start(today()) - timedelta(days=7)
        day_one, day_two = monday, monday + timedelta(days=2)
        alice, bob = make_user(db, "GA1"), make_user(db, "GA2")
        first = add_wordle_session(db, day_one)
        add_wordle_result(db, first, alice.id, 2, True)
        second = add_wordle_session(db, day_two)
        add_wordle_result(db, second, alice.id, 2, True)
        add_wordle_result(db, second, bob.id, 5, True)
        league_finalization.finalize_due_opportunities(db, ["wordle"])

        board = league_boards.global_board(
            db, competition_type="individual", period="weekly", anchor=day_two,
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        assert scores[alice.id] == 90.0
        assert scores[bob.id] == 30.0

    def test_partial_couple_session_keeps_earned_credit(self, db):
        from app.db_models import (
            CoupleGameSessionAnswerRecord,
            CoupleGameSessionPredictionRecord,
        )

        start = today() - timedelta(days=10)
        a, b = make_user(db, "PC1"), make_user(db, "PC2")
        _glob, pair_id = add_couple_session(db, start, a, b, 0, 0)
        # The session draws 5 shared questions (10 prediction
        # opportunities), but the couple submits a single original answer
        # and one correct prediction before the event closes. The
        # denominator still counts the whole drawn set.
        with db.sessions.begin() as session:
            questions = session.scalars(
                select(CoupleGameSessionQuestionRecord).where(
                    CoupleGameSessionQuestionRecord.session_id == pair_id
                )
            ).all()
            assert len(questions) == 5
            session.add(
                CoupleGameSessionAnswerRecord(
                    session_id=pair_id, session_question_id=str(questions[0].id),
                    user_id=a.id, selected_option_index=0,
                )
            )
            session.flush()
            session.add(
                CoupleGameSessionPredictionRecord(
                    session_id=pair_id, session_question_id=str(questions[0].id),
                    predictor_user_id=b.id, predicted_option_index=0,
                    is_correct=True,
                )
            )
            session.get(CoupleGameSessionRecord, pair_id).status = "completed"
        glob = session_id_for_pair(db, pair_id)
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)

        awards = live_awards(db, game_id="couple-questionnaire")
        assert len(awards) == 1
        # 1 of 10 opportunities: partial credit survives, the rest are zero.
        assert awards[0].global_base_points == Decimal("10.00")
        assert awards[0].game_base_points == Decimal("10.00")
        assert awards[0].raw_metrics == {"correct_predictions": 1, "total_questions": 10}

    def test_couple_seven_of_ten_scores_seventy(self, db):
        start = today() - timedelta(days=10)
        a, b = make_user(db, "P71"), make_user(db, "P72")
        glob, pair_id = add_couple_session(db, start, a, b, 0, 0)
        answer_and_predict(db, pair_id, a, b, 4, 3)
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)

        awards = live_awards(db, game_id="couple-questionnaire")
        assert len(awards) == 1
        assert awards[0].global_base_points == Decimal("70.00")
        assert awards[0].raw_metrics == {"correct_predictions": 7, "total_questions": 10}

    def test_couple_no_valid_answers_scores_zero(self, db):
        start = today() - timedelta(days=10)
        a, b = make_user(db, "P01"), make_user(db, "P02")
        glob, pair_id = add_couple_session(db, start, a, b, 0, 0)
        answer_and_predict(db, pair_id, a, b, 0, 0)
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)

        awards = live_awards(db, game_id="couple-questionnaire")
        assert len(awards) == 1
        assert awards[0].global_base_points == Decimal("0")
        assert awards[0].game_base_points == Decimal("0")

    def test_reversed_partner_order_is_one_couple(self, db):
        start = today() - timedelta(days=10)
        a, b = make_user(db, "RV1"), make_user(db, "RV2")
        _glob, pair_id = add_couple_session(db, start, a, b, 0, 0)
        # Swap the stored member order away from canonical; the adapter
        # still resolves one canonical identity and issues one award.
        with db.sessions.begin() as session:
            pair = session.get(CoupleGameSessionRecord, pair_id)
            pair.user_a_id, pair.user_b_id = pair.user_b_id, pair.user_a_id
        answer_and_predict(db, pair_id, a, b, 5, 5)
        glob = session_id_for_pair(db, pair_id)
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob)

        awards = live_awards(db, game_id="couple-questionnaire")
        assert len(awards) == 1
        assert (awards[0].member_a_id, awards[0].member_b_id) == canonical_couple(a.id, b.id)
        assert awards[0].global_base_points == Decimal("100.00")

    def test_multi_answer_trivia_partial_credit(self, db):
        from app.db_models import TriviaDropOptionRecord

        day = today() - timedelta(days=4)
        partial, greedy, wrong = (make_user(db, f"MP{i}") for i in range(3))
        released = midnight(day)
        with db.sessions.begin() as session:
            row = TriviaDropQuestionRecord(
                question_text="pick two", status="ended",
                released_at=released, ended_at=released + timedelta(hours=1),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            question_id = str(row.id)
            for index, correct in [(0, True), (1, True), (2, False), (3, False)]:
                session.add(
                    TriviaDropOptionRecord(
                        question_id=question_id, option_index=index,
                        option_text=f"opt{index}", is_correct=correct,
                    )
                )
            session.flush()
            moment = released
            session.add_all([
                # One of two correct, instantly: 0.5 accuracy -> 40 + 10.
                TriviaDropSubmissionRecord(
                    question_id=question_id, user_id=partial.id,
                    selected_options=[0], is_correct=False,
                    submitted_at=moment, response_time_ms=0,
                ),
                # Selecting everything: hits cancelled by false picks -> 0.
                TriviaDropSubmissionRecord(
                    question_id=question_id, user_id=greedy.id,
                    selected_options=[0, 1, 2, 3], is_correct=False,
                    submitted_at=moment, response_time_ms=0,
                ),
                # Entirely incorrect -> 0.
                TriviaDropSubmissionRecord(
                    question_id=question_id, user_id=wrong.id,
                    selected_options=[2], is_correct=False,
                    submitted_at=moment, response_time_ms=0,
                ),
            ])
        league_finalization.finalize_opportunity(db, "trivia-drop", question_id)

        awards = {str(a.user_id): a for a in live_awards(db, game_id="trivia-drop")}
        assert awards[partial.id].global_base_points == Decimal("50.00")
        assert awards[greedy.id].global_base_points == Decimal("0")
        assert awards[wrong.id].global_base_points == Decimal("0")

    def test_legacy_rows_fall_back_to_awarded_points(self, db):
        from sqlalchemy import text as sql_text

        day = today() - timedelta(days=3)
        user = make_user(db, "LB")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        league_finalization.finalize_opportunity(db, "wordle", session_id)
        # Simulate a pre-split row: halves and versions wiped, legacy stays.
        with db.engine.begin() as connection:
            connection.execute(
                sql_text(
                    "UPDATE game_score_awards SET global_base_points = NULL,"
                    " game_base_points = NULL, global_points = NULL,"
                    " game_points = NULL, global_scoring_version = NULL,"
                    " game_scoring_version = NULL"
                    " WHERE game_id = 'wordle'"
                )
            )

        board = league_boards.global_board(
            db, competition_type="individual", period="daily", anchor=day,
        )
        assert board["entries"][0]["score"] == 90.0
        game = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="daily", anchor=day,
        )
        assert game["entries"][0]["score"] == 90.0


def session_id_for_pair(db, pair_id: str) -> str:
    """The global (opportunity) id owning a couple pair session."""
    with db.sessions() as session:
        pair = session.get(CoupleGameSessionRecord, pair_id)
        return str(pair.couple_game_id)


# ---------------------------------------------------------------------------
# Individual-game boards follow their intended board rules
# ---------------------------------------------------------------------------

class TestIndividualGameBoards:
    def test_trivia_game_board_credits_correct_even_when_last(self, db):
        day = today() - timedelta(days=4)
        quick, slow, wrong = (make_user(db, f"GB{i}") for i in range(3))
        released = midnight(day)
        with db.sessions.begin() as session:
            row = TriviaDropQuestionRecord(
                question_text="?", status="ended",
                released_at=released, ended_at=released + timedelta(hours=1),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            question_id = str(row.id)
            for user, correct, ms in [(quick, True, 500), (slow, True, 9000), (wrong, False, 100)]:
                session.add(
                    TriviaDropSubmissionRecord(
                        question_id=question_id, user_id=user.id,
                        selected_options=[0] if correct else [1], is_correct=correct,
                        submitted_at=released + timedelta(milliseconds=ms),
                        response_time_ms=ms,
                    )
                )
        league_finalization.finalize_opportunity(db, "trivia-drop", question_id)

        board = league_boards.game_board(
            db, game_id="trivia-drop", competition_type="individual",
            period="daily", anchor=day,
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        ranks = {entry["user_id"]: entry["rank"] for entry in board["entries"]}
        # Absolute correctness-plus-speed: every correct answer pays, the
        # wrong one is zero, and placement order still holds here.
        assert scores[quick.id] == 99.6
        assert scores[slow.id] == 94.0
        assert scores[wrong.id] == 0.0
        assert [ranks[quick.id], ranks[slow.id], ranks[wrong.id]] == [1, 2, 3]

    def test_connections_game_board_pays_partial_progress(self, db):
        day = today() - timedelta(days=4)
        user = make_user(db, "GP")
        with db.sessions.begin() as session:
            row = ConnectionsGameRecord(
                game_date=day, status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            session_id = str(row.id)
            session.add(
                ConnectionsSubmissionRecord(
                    connections_game_id=session_id, user_id=user.id,
                    groups=[], correct_groups=0, correct_words=1,
                    submitted_at=at(day),
                )
            )
        league_finalization.finalize_opportunity(db, "connections", session_id)

        board = league_boards.game_board(
            db, game_id="connections", competition_type="individual",
            period="daily", anchor=day,
        )
        # One placed word with no complete group: 4 points, not a solo 50
        # and not a placement zero.
        assert board["entries"][0]["score"] == 4.0

    def test_couple_game_board_is_proportional_not_a_duel(self, db):
        start = today() - timedelta(days=10)
        a1, b1 = make_user(db, "GD1"), make_user(db, "GD2")
        a2, b2 = make_user(db, "GD3"), make_user(db, "GD4")
        glob1, pair1 = add_couple_session(db, start, a1, b1, 0, 0)
        glob2, pair2 = add_couple_session(db, start, a2, b2, 0, 0)
        answer_and_predict(db, pair1, a1, b1, 5, 4)
        answer_and_predict(db, pair2, a2, b2, 3, 2)
        with db.sessions.begin() as session:
            session.get(CoupleGameSessionRecord, pair2).couple_game_id = glob1
        with db.sessions.begin() as session:
            session.delete(session.get(CoupleGameRecord, glob2))
        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob1)

        board = league_boards.game_board(
            db, game_id="couple-questionnaire", competition_type="couple",
            period="all_time",
        )
        scores = {
            (entry["member_a_id"], entry["member_b_id"]): entry["score"]
            for entry in board["entries"]
        }
        assert scores[canonical_couple(a1.id, b1.id)] == 90.0
        assert scores[canonical_couple(a2.id, b2.id)] == 50.0

    def test_two_key_game_board_pays_every_escape(self, db):
        start = today() - timedelta(days=10)
        fast_a, fast_b = make_user(db, "GE1"), make_user(db, "GE2")
        slow_a, slow_b = make_user(db, "GE3"), make_user(db, "GE4")
        _glob, fast_pair = add_couple_session(db, start, fast_a, fast_b, 0, 0)
        _glob, slow_pair = add_couple_session(db, start, slow_a, slow_b, 0, 0)
        with db.sessions.begin() as session:
            game = TwoKeyEscapeGameRecord(
                game_date=start, status="ended",
                period_start_date=start, period_end_date=start + timedelta(days=1),
                started_at=midnight(start), ended_at=midnight(start + timedelta(days=1)),
                created_by="TEST",
                grid_size=12, maze_seed="seed",
                start_row=0, start_col=0, exit_row=11, exit_col=11,
                walls=[], locked_cells=[], key_mapping={}, gate_indexes=[],
            )
            session.add(game)
            session.flush()
            game_id = str(game.id)
            moment = at(start)
            for pair_id, elapsed in [(fast_pair, 95.5), (slow_pair, 210.0)]:
                pair = session.get(CoupleGameSessionRecord, pair_id)
                member_a, member_b = canonical_couple(str(pair.user_a_id), str(pair.user_b_id))
                session.add(
                    TwoKeyEscapeSessionRecord(
                        two_key_escape_game_id=game_id, couple_session_id=pair_id,
                        user_a_id=member_a, user_b_id=member_b,
                        player_row=0, player_col=0,
                        total_moves=10, total_keys=2,
                        elapsed_seconds=elapsed, completed=True,
                        completed_at=moment,
                    )
                )
        league_finalization.finalize_opportunity(db, "two-key-escape", game_id)

        board = league_boards.game_board(
            db, game_id="two-key-escape", competition_type="couple",
            period="all_time",
        )
        assert len(board["entries"]) == 2
        assert {entry["score"] for entry in board["entries"]} == {100.0}
        assert {entry["rank"] for entry in board["entries"]} == {1}

    def test_wordle_game_board_keeps_relative_placement(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"GR{i}") for i in range(3)]
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, users[0].id, 2, True)
        add_wordle_result(db, session_id, users[1].id, 4, True)
        add_wordle_result(db, session_id, users[2].id, 6, False)
        league_finalization.finalize_opportunity(db, "wordle", session_id)

        board = league_boards.game_board(
            db, game_id="wordle", competition_type="individual",
            period="daily", anchor=day,
        )
        ranks = {entry["user_id"]: entry["rank"] for entry in board["entries"]}
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        # Fewer guesses outranks more; unsolved trails; points stay relative.
        assert [ranks[users[0].id], ranks[users[1].id], ranks[users[2].id]] == [1, 2, 3]
        assert scores[users[0].id] == 100.0
        assert scores[users[1].id] == 50.0
        assert scores[users[2].id] == 0.0

    def test_reaction_game_board_keeps_relative_comparison(self, db):
        day = today() - timedelta(days=4)
        users = [make_user(db, f"GH{i}") for i in range(2)]
        with db.sessions.begin() as session:
            row = ReactionTimeGameRecord(
                game_date=day, status="ended",
                period_start_date=day, period_end_date=day + timedelta(days=1),
                started_at=midnight(day), ended_at=midnight(day + timedelta(days=1)),
                created_by="TEST",
            )
            session.add(row)
            session.flush()
            session_id = str(row.id)
            moment = at(day)
            for user, ms in [(users[0], 190), (users[1], 900)]:
                session.add(
                    ReactionTimeSubmissionRecord(
                        reaction_time_game_id=session_id, user_id=user.id,
                        attempt_number=1, status="valid",
                        reaction_time_ms=ms, elapsed_ms=ms,
                        started_at=moment, green_at=moment, clicked_at=moment,
                    )
                )
        league_finalization.finalize_opportunity(db, "reaction-time", session_id)

        board = league_boards.game_board(
            db, game_id="reaction-time", competition_type="individual",
            period="daily", anchor=day,
        )
        scores = {entry["user_id"]: entry["score"] for entry in board["entries"]}
        # Relative duel on the game board (100/0) while the global halves
        # are absolute (90 for 190ms, 10 for 900ms): no leakage either way.
        assert scores[users[0].id] == 100.0
        assert scores[users[1].id] == 0.0
        awards = {str(a.user_id): a for a in live_awards(db, game_id="reaction-time")}
        assert awards[users[0].id].global_base_points == Decimal("90")
        assert awards[users[1].id].global_base_points == Decimal("10")


# ---------------------------------------------------------------------------
# Cross-game calibration: documented representatives on one scale
# ---------------------------------------------------------------------------

class TestCalibrationTable:
    """Each row names the intended meaning; bands are the common scale.

    0 failing/unsuccessful, 1-20 minimal, 21-40 low, 41-60 moderate,
    61-80 strong, 81-100 excellent. These tests pin the profiles to their
    documented meanings -- they do not derive thresholds from live data and
    would fail if any profile silently changed.
    """

    def test_representatives_hit_their_bands(self):
        from app.services.league_global_scoring import (
            anagram_global_base,
            connections_global_base,
            couple_global_base,
            reaction_global_base,
            trivia_global_base,
            two_key_escape_global_base,
            wordle_global_base,
        )

        representatives = [
            ("wordle 2-guess solve is excellent", wordle_global_base({"solved": True, "attempts": 2}), 81, 100),
            ("anagram 2-attempt quick solve is excellent", anagram_global_base({"solved": True, "attempts": 2, "active_time_seconds": 25}), 81, 100),
            ("reaction 250ms is strong", reaction_global_base({"best_ms": 250}), 61, 80),
            ("trivia instant correct is excellent", trivia_global_base({"accuracy": 1.0, "response_time_ms": 200}), 81, 100),
            ("connections half progress is moderate", connections_global_base({"correct_words": 8, "correct_groups": 2}), 41, 60),
            ("couple 7/10 is strong", couple_global_base({"correct_predictions": 7, "total_questions": 10}), 61, 80),
            ("escape success is excellent", two_key_escape_global_base({}), 81, 100),
        ]
        for label, value, low, high in representatives:
            assert low <= int(value) <= high, label

    def test_profiles_are_deterministic_and_field_free(self):
        from app.services.league_global_scoring import global_base_for

        first = global_base_for("wordle", {"solved": True, "attempts": 4})
        second = global_base_for("wordle", {"solved": True, "attempts": 4})
        assert first == second == Decimal("70")
        # No participant-count/rank argument exists to vary: the signature
        # takes only the finalized native metrics.
        import inspect

        for name in (
            "wordle_global_base", "anagram_global_base", "reaction_global_base",
            "trivia_global_base", "connections_global_base", "couple_global_base",
            "two_key_escape_global_base",
        ):
            params = list(inspect.signature(getattr(
                __import__("app.services.league_global_scoring", fromlist=[name]), name
            )).parameters)
            assert params == ["raw"], name

    def test_unsuccessful_outcomes_cannot_score(self):
        from app.services.league_global_scoring import (
            anagram_global_base,
            connections_global_base,
            couple_global_base,
            reaction_global_base,
            trivia_global_base,
            wordle_global_base,
        )

        assert wordle_global_base({"solved": False, "attempts": 1}) == Decimal("0")
        assert anagram_global_base({"solved": False, "attempts": 1, "active_time_seconds": 1}) == Decimal("0")
        assert trivia_global_base({"accuracy": 0.0, "response_time_ms": 0}) == Decimal("0")
        assert connections_global_base({"correct_words": 0, "correct_groups": 0}) == Decimal("0")
        assert couple_global_base({"correct_predictions": 0, "total_questions": 10}) == Decimal("0")
        assert couple_global_base({"correct_predictions": 5, "total_questions": 0}) == Decimal("0")
        assert reaction_global_base({}) == Decimal("0")
