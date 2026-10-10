"""Per-game leaderboard categories: metric rankings across sessions.

Covers the category contract end to end:

* category descriptors per game (ids, scopes, defaults) and 400s;
* cross-session metric ordering (never session placement) with deterministic
  tie-breaks and shared competition ranks;
* period windows vs all-time record scope (records ignore the window);
* finalized-only reads: in-progress rows, active sessions/questions and
  unfinished runs never appear; active answers stay redacted from details;
* category `/me` ranks over the full filtered set, and pagination slices
  precomputed ranks;
* global scoring untouched (asserted by test_league_scoring.py, which runs
  unmodified alongside this file).
"""

from datetime import timedelta
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
    CoupleGameSessionRecord,
    ReactionTimeGameRecord,
    ReactionTimeSubmissionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
    WordleGameRecord,
)
from app.main import app
from app.models import UserDetails
from app.services import league_game_boards
from app.services.auth_service import get_current_user
from test_league_scoring import (
    _bank_ids,
    add_couple_session,
    add_wordle_result,
    add_wordle_session,
    answer_and_predict,
    at,
    live_awards,
    make_user,
    midnight,
    session_id_for_pair,
    today,
    user_client,
)
from app.services.league_scoring import week_start


def category_ids(db, game_id: str) -> list[str]:
    with db.sessions() as session:
        return [c["id"] for c in league_game_boards.list_categories(game_id)]


def board(db, game_id, category, period="weekly", anchor=None, skip=0, limit=50):
    return league_game_boards.category_board(
        db, game_id=game_id, category=category, period=period,
        anchor=anchor, skip=skip, limit=limit,
    )


def add_anagram_session(db, start, answer="garden", status="ended"):
    with db.sessions.begin() as session:
        row = AnagramGameRecord(
            game_date=start, mode="random", answer=answer, status=status,
            period_start_date=start, period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)) if status == "ended" else None,
            created_by="TEST",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_anagram_result(db, session_id, user_id, attempts, active, solved):
    with db.sessions.begin() as session:
        session.add(
            AnagramUserGameRecord(
                anagram_game_id=session_id, user_id=user_id,
                scrambled_word="nrgdea", attempt_count=attempts,
                status="won" if solved else "lost", success=solved,
                guesses=["x"] * attempts, active_time_seconds=active,
                completed_at=at(today()), solved_at=at(today()) if solved else None,
            )
        )


def add_reaction_session(db, start, status="ended"):
    with db.sessions.begin() as session:
        row = ReactionTimeGameRecord(
            game_date=start, status=status,
            period_start_date=start, period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)) if status == "ended" else None,
            created_by="TEST",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_reaction_result(db, session_id, user_id, ms, status="valid", attempt=1):
    with db.sessions.begin() as session:
        moment = at(today())
        session.add(
            ReactionTimeSubmissionRecord(
                reaction_time_game_id=session_id, user_id=user_id,
                attempt_number=attempt, status=status,
                reaction_time_ms=ms if status == "valid" else None,
                elapsed_ms=400 if status == "too_early" else ms,
                started_at=moment, green_at=moment, clicked_at=moment,
            )
        )


def add_connections_session(db, start, status="ended"):
    with db.sessions.begin() as session:
        row = ConnectionsGameRecord(
            game_date=start, status=status,
            period_start_date=start, period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)) if status == "ended" else None,
            created_by="TEST",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_connections_result(db, session_id, user_id, groups, words):
    with db.sessions.begin() as session:
        session.add(
            ConnectionsSubmissionRecord(
                connections_game_id=session_id, user_id=user_id,
                groups=[], correct_groups=groups, correct_words=words,
                submitted_at=at(today()),
            )
        )


def add_trivia_question(db, released, status="ended", text="q?"):
    with db.sessions.begin() as session:
        row = TriviaDropQuestionRecord(
            question_text=text, status=status,
            released_at=released, ended_at=released + timedelta(hours=1),
            created_by="TEST",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_trivia_answer(db, question_id, user_id, correct, ms):
    with db.sessions.begin() as session:
        session.add(
            TriviaDropSubmissionRecord(
                question_id=question_id, user_id=user_id,
                selected_options=[0] if correct else [1], is_correct=correct,
                submitted_at=midnight(today()), response_time_ms=ms,
            )
        )


def _rank_of(entries, user_id):
    return next(e["rank"] for e in entries if e["user_id"] == user_id)


class TestCategoryDescriptors:
    def test_wordle_lists_four_views_with_performance_default(self, db):
        from app.services.league_game_boards import list_categories

        infos = {c["id"]: c for c in list_categories("wordle")}
        assert set(infos) == {"performance", "most_solved", "fewest_guesses", "fastest_solve"}
        assert infos["performance"]["is_default"] is True
        assert infos["fastest_solve"]["scope"] == "record"
        assert infos["most_solved"]["scope"] == "period"

    def test_connections_has_no_fastest_view(self, db):
        ids = category_ids(db, "connections")
        assert ids == ["performance", "best_quality"]

    def test_reaction_defaults_to_average(self, db):
        from app.services.league_game_boards import list_categories

        infos = {c["id"]: c for c in list_categories("reaction-time")}
        assert infos["average"]["is_default"] is True
        assert infos["fastest"]["scope"] == "record"

    def test_unknown_game_and_category_are_refused(self, db):
        import pytest
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            league_game_boards.list_categories("nope")
        assert caught.value.status_code == 400
        with pytest.raises(HTTPException) as caught:
            league_game_boards.category_board(
                db, game_id="wordle", category="fastest_completion", period="weekly",
            )
        assert caught.value.status_code == 400


class TestWordleCategories:
    def test_performance_rewards_solving_and_participation(self, db):
        day = today() - timedelta(days=3)
        efficient = make_user(db, "WP1")
        grinder = make_user(db, "WP2")
        first = add_wordle_session(db, day)
        add_wordle_result(db, first, efficient.id, 2, True)
        second = add_wordle_session(db, day)
        add_wordle_result(db, second, efficient.id, 3, True)
        add_wordle_result(db, second, grinder.id, 6, True)
        third = add_wordle_session(db, day)
        add_wordle_result(db, third, grinder.id, 6, True)
        add_wordle_result(db, third, efficient.id, 6, False)
        fourth = add_wordle_session(db, day)
        add_wordle_result(db, fourth, grinder.id, 6, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        result = board(db, "wordle", "performance", period="daily", anchor=day)
        ranks = {e["user_id"]: e["rank"] for e in result["entries"]}
        # Grinder: 3 solves (90+90+90 internal); efficient: 2 solves + a miss.
        assert ranks[grinder.id] == 1
        assert ranks[efficient.id] == 2
        entry = next(e for e in result["entries"] if e["user_id"] == grinder.id)
        assert entry["metrics"]["solved"] == 3
        assert "perf" not in entry["metrics"]

    def test_most_solved_counts_and_breaks_ties(self, db):
        day = today() - timedelta(days=3)
        steady, streaky = make_user(db, "WM1"), make_user(db, "WM2")
        for user, attempts, solved in [
            (steady, 2, True), (streaky, 2, True),
            (steady, 3, True), (streaky, 5, True),
        ]:
            session_id = add_wordle_session(db, day)
            add_wordle_result(db, session_id, user.id, attempts, solved)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        result = board(db, "wordle", "most_solved", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # Same solved count and rate: a shared rank, ordered by fewer
        # average guesses.
        assert by_user[steady.id]["rank"] == 1
        assert by_user[streaky.id]["rank"] == 1
        assert [e["user_id"] for e in result["entries"]] == [steady.id, streaky.id]
        assert by_user[steady.id]["metrics"]["avg_guesses"] == 2.5

    def test_fewest_guesses_ranks_best_solve(self, db):
        day = today() - timedelta(days=3)
        sharp, lucky, slow = make_user(db, "WF1"), make_user(db, "WF2"), make_user(db, "WF3")
        first = add_wordle_session(db, day)
        add_wordle_result(db, first, sharp.id, 2, True)
        add_wordle_result(db, first, lucky.id, 2, True)
        second = add_wordle_session(db, day)
        add_wordle_result(db, second, sharp.id, 2, True)
        third = add_wordle_session(db, day)
        add_wordle_result(db, third, slow.id, 3, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        result = board(db, "wordle", "fewest_guesses", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # Same best (2): shared rank, more solves first; best 3 trails.
        assert by_user[sharp.id]["rank"] == 1
        assert by_user[lucky.id]["rank"] == 1
        assert by_user[slow.id]["rank"] == 3
        assert [e["user_id"] for e in result["entries"]][0] == sharp.id
        assert by_user[sharp.id]["metrics"]["best_guesses"] == 2
        assert by_user[sharp.id]["records"][0]["guesses"] == 2
        assert by_user[sharp.id]["records"][0]["word"] == "cigar"

    def test_fastest_solve_ignores_the_window(self, db):
        old = today() - timedelta(days=60)
        user = make_user(db, "WZ")
        session_id = add_wordle_session(db, old)
        with db.sessions.begin() as session:
            from app.db_models import WordleUserGameRecord

            start = midnight(old).replace(hour=10)
            session.add(
                WordleUserGameRecord(
                    wordle_game_id=session_id, user_id=user.id,
                    attempt_count=3, status="won", success=True,
                    guesses=["a", "b", "cigar"], started_at=start,
                    completed_at=start + timedelta(seconds=45),
                    solved_at=start + timedelta(seconds=45),
                )
            )
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        result = board(db, "wordle", "fastest_solve", period="daily", anchor=today())
        assert result["period"] == "all_time"
        assert len(result["entries"]) == 1
        assert result["entries"][0]["metrics"]["fastest_s"] == 45.0

    def test_active_answer_is_redacted_but_still_counts(self, db):
        day = today() - timedelta(days=3)
        user = make_user(db, "WR")
        ended_id = add_wordle_session(db, day, answer="mango")
        add_wordle_result(db, ended_id, user.id, 2, True)
        add_wordle_session(db, today(), answer="mango", status="active")
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        result = board(db, "wordle", "fewest_guesses", period="all_time")
        entry = result["entries"][0]
        assert entry["metrics"]["solved"] == 1
        assert entry["records"][0]["word"] is None

    def test_in_progress_attempts_never_appear(self, db):
        day = today() - timedelta(days=3)
        user = make_user(db, "WI")
        session_id = add_wordle_session(db, day)
        with db.sessions.begin() as session:
            from app.db_models import WordleUserGameRecord

            session.add(
                WordleUserGameRecord(
                    wordle_game_id=session_id, user_id=user.id,
                    attempt_count=2, status="in_progress", success=False,
                    guesses=["a", "b"],
                )
            )
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        assert board(db, "wordle", "most_solved", period="all_time")["total"] == 0
        assert board(db, "wordle", "performance", period="all_time")["total"] == 0


class TestAnagramCategories:
    def test_fastest_uses_active_time_not_attempts(self, db):
        day = today() - timedelta(days=4)
        quick, slow = make_user(db, "AF1"), make_user(db, "AF2")
        first = add_anagram_session(db, day)
        add_anagram_result(db, first, quick.id, 3, 240, True)
        add_anagram_result(db, first, slow.id, 3, 60, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["anagram"])

        result = board(db, "anagram", "fastest_solve", period="all_time")
        by_user = {e["user_id"]: e for e in result["entries"]}
        assert by_user[slow.id]["rank"] == 1
        assert by_user[slow.id]["metrics"]["fastest_s"] == 60

    def test_fewest_attempts_breaks_ties_by_active_time(self, db):
        day = today() - timedelta(days=4)
        quick, slow = make_user(db, "AA1"), make_user(db, "AA2")
        session_id = add_anagram_session(db, day)
        add_anagram_result(db, session_id, quick.id, 2, 30, True)
        add_anagram_result(db, session_id, slow.id, 2, 90, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["anagram"])

        result = board(db, "anagram", "fewest_attempts", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # Same best attempt count: shared rank, quicker active time first.
        assert by_user[quick.id]["rank"] == 1
        assert by_user[slow.id]["rank"] == 1
        assert [e["user_id"] for e in result["entries"]] == [quick.id, slow.id]
        assert by_user[quick.id]["metrics"]["best_active_s"] == 30


class TestReactionCategories:
    def test_average_is_default_and_uses_valid_results(self, db):
        day = today() - timedelta(days=4)
        steady, flash = make_user(db, "RA1"), make_user(db, "RA2")
        session_id = add_reaction_session(db, day)
        add_reaction_result(db, session_id, steady.id, 250, attempt=1)
        add_reaction_result(db, session_id, steady.id, 350, attempt=2)
        add_reaction_result(db, session_id, flash.id, 190, attempt=1)
        add_reaction_result(db, session_id, flash.id, 900, attempt=2)
        add_reaction_result(db, session_id, flash.id, 500, status="too_early", attempt=3)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["reaction-time"])

        result = board(db, "reaction-time", "average", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # steady 300 avg beats flash 545 avg; the too-early click is absent.
        assert by_user[steady.id]["rank"] == 1
        assert by_user[steady.id]["metrics"] == {"avg_ms": 300.0, "attempts": 2}
        assert by_user[flash.id]["metrics"]["attempts"] == 2

    def test_fastest_is_all_time_record(self, db):
        old = today() - timedelta(days=60)
        user = make_user(db, "RF")
        session_id = add_reaction_session(db, old)
        add_reaction_result(db, session_id, user.id, 175)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["reaction-time"])

        result = board(db, "reaction-time", "fastest", period="daily", anchor=today())
        assert result["period"] == "all_time"
        assert result["entries"][0]["metrics"]["fastest_ms"] == 175


class TestConnectionsCategories:
    def test_performance_sums_quality(self, db):
        day = today() - timedelta(days=4)
        grinder, sharp = make_user(db, "CP1"), make_user(db, "CP2")
        first = add_connections_session(db, day)
        add_connections_result(db, first, grinder.id, 2, 8)
        add_connections_result(db, first, sharp.id, 2, 10)
        second = add_connections_session(db, day)
        add_connections_result(db, second, grinder.id, 2, 8)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["connections"])

        result = board(db, "connections", "performance", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # grinder 50 + 50 beats sharp's single 58: participation counts.
        assert by_user[grinder.id]["rank"] == 1
        assert by_user[grinder.id]["metrics"]["total_quality"] == 100.0
        assert by_user[sharp.id]["metrics"]["total_quality"] == 58.0

    def test_best_quality_prefers_groups_then_words(self, db):
        day = today() - timedelta(days=4)
        grouped, spread = make_user(db, "CQ1"), make_user(db, "CQ2")
        session_id = add_connections_session(db, day)
        add_connections_result(db, session_id, grouped.id, 2, 8)
        add_connections_result(db, session_id, spread.id, 1, 8)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["connections"])

        result = board(db, "connections", "best_quality", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        # 2 groups + 8 words (50) beats 1 group + 8 words (41).
        assert by_user[grouped.id]["rank"] == 1
        assert by_user[grouped.id]["metrics"]["groups"] == 2
        assert by_user[spread.id]["metrics"]["best_quality"] == 41.0


class TestTriviaCategories:
    def test_answer_performance_sums_per_question_scores(self, db):
        day = today() - timedelta(days=4)
        speedy, steady = make_user(db, "TP1"), make_user(db, "TP2")
        released = midnight(day)
        first = add_trivia_question(db, released, text="first?")
        add_trivia_answer(db, first, speedy.id, True, 500)
        add_trivia_answer(db, first, steady.id, True, 5000)
        second = add_trivia_question(db, released + timedelta(hours=1), text="second?")
        add_trivia_answer(db, second, speedy.id, False, 400)
        add_trivia_answer(db, second, steady.id, True, 900)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["trivia-drop"])

        result = board(db, "trivia-drop", "answer_performance", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        assert by_user[speedy.id]["metrics"]["total_score"] == 99.6
        assert by_user[steady.id]["metrics"]["total_score"] == 196.0
        assert by_user[steady.id]["rank"] == 1

    def test_most_answered_counts_wrong_answers_too(self, db):
        day = today() - timedelta(days=4)
        chatty, precise = make_user(db, "TM1"), make_user(db, "TM2")
        released = midnight(day)
        for index in range(3):
            question = add_trivia_question(db, released + timedelta(hours=index), text=f"q{index}?")
            add_trivia_answer(db, question, chatty.id, index != 2, 800)
        solo = add_trivia_question(db, released + timedelta(hours=9), text="solo?")
        add_trivia_answer(db, solo, precise.id, True, 800)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["trivia-drop"])

        result = board(db, "trivia-drop", "most_answered", period="daily", anchor=day)
        by_user = {e["user_id"]: e for e in result["entries"]}
        assert by_user[chatty.id]["rank"] == 1
        assert by_user[chatty.id]["metrics"] == {"answered": 3, "correct": 2, "avg_response_ms": 800.0}

    def test_fastest_answer_discloses_only_ended_questions(self, db):
        day = today() - timedelta(days=4)
        user = make_user(db, "TF")
        released = midnight(day)
        ended = add_trivia_question(db, released, text="ended?")
        add_trivia_answer(db, ended, user.id, True, 700)
        live = add_trivia_question(db, midnight(today()), status="active", text="live?")
        add_trivia_answer(db, live, user.id, True, 100)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["trivia-drop"])

        result = board(db, "trivia-drop", "fastest_answer", period="all_time")
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["metrics"]["fastest_ms"] == 700
        assert entry["records"][0]["question_text"] == "ended?"
        assert "live?" not in str(result)


class TestCoupleAndEscapeCategories:
    def test_couple_score_matches_the_game_board(self, db):
        from app.services import league_boards

        start = today() - timedelta(days=10)
        a1, b1 = make_user(db, "GS1"), make_user(db, "GS2")
        a2, b2 = make_user(db, "GS3"), make_user(db, "GS4")
        glob1, pair1 = add_couple_session(db, start, a1, b1, 0, 0)
        glob2, pair2 = add_couple_session(db, start, a2, b2, 0, 0)
        answer_and_predict(db, pair1, a1, b1, 5, 4)
        answer_and_predict(db, pair2, a2, b2, 3, 2)
        with db.sessions.begin() as session:
            session.get(CoupleGameSessionRecord, pair2).couple_game_id = glob1
        with db.sessions.begin() as session:
            orphan = session.get(CoupleGameRecord, glob2)
            session.delete(orphan)
        from app.services import league_finalization

        league_finalization.finalize_opportunity(db, "couple-questionnaire", glob1)

        mine = board(db, "couple-questionnaire", "score", period="all_time")
        game = league_boards.game_board(
            db, game_id="couple-questionnaire", competition_type="couple", period="all_time",
        )
        assert [e["score"] for e in game["entries"]] == [e["metrics"]["score"] for e in mine["entries"]]

    def test_escape_performance_counts_completions_only(self, db):
        from app.db_models import TwoKeyEscapeSessionRecord

        start = today() - timedelta(days=10)
        fast_a, fast_b = make_user(db, "GE1"), make_user(db, "GE2")
        slow_a, slow_b = make_user(db, "GE3"), make_user(db, "GE4")
        idle_a, idle_b = make_user(db, "GE5"), make_user(db, "GE6")
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
            moment = midnight(start).replace(hour=12)
            for pair_id, elapsed, done in [(fast_pair, 95.5, True), (slow_pair, 210.0, True), (idle_pair, None, False)]:
                pair = session.get(CoupleGameSessionRecord, pair_id)
                from app.services.league_scoring import canonical_couple

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
        from app.services import league_finalization

        league_finalization.finalize_opportunity(db, "two-key-escape", game_id)

        perf = board(db, "two-key-escape", "escape_performance", period="all_time")
        # Failed runs are absent, never zero-point participants.
        assert perf["total"] == 2
        ranks = {tuple(sorted((e["member_a_id"], e["member_b_id"]))): e["rank"] for e in perf["entries"]}
        from app.services.league_scoring import canonical_couple as cc

        assert ranks[cc(fast_a.id, fast_b.id)] == 1
        assert perf["entries"][0]["metrics"]["escapes"] == 1

        fastest = board(db, "two-key-escape", "fastest_escape", period="daily", anchor=today())
        assert fastest["period"] == "all_time"
        assert fastest["entries"][0]["metrics"]["fastest_s"] == 95.5


class TestCategoryMeAndPagination:
    def test_me_returns_full_dataset_rank(self, db):
        day = today() - timedelta(days=3)
        users = [make_user(db, f"ME{i}") for i in range(3)]
        first = add_wordle_session(db, day)
        add_wordle_result(db, first, users[0].id, 2, True)
        add_wordle_result(db, first, users[1].id, 3, True)
        second = add_wordle_session(db, day)
        add_wordle_result(db, second, users[0].id, 2, True)
        add_wordle_result(db, second, users[2].id, 4, True)
        add_wordle_result(db, second, users[1].id, 6, False)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        # Solved: u0 = 2, u1 = 1 (rate 1/2), u2 = 1 (rate 1/1).
        page = board(db, "wordle", "most_solved", period="daily", anchor=day, skip=1, limit=1)
        assert page["total"] == 3
        assert len(page["entries"]) == 1
        assert page["entries"][0]["user_id"] == users[2].id
        assert page["entries"][0]["rank"] == 2

        me = league_game_boards.category_me(
            db, game_id="wordle", category="most_solved",
            user_id=users[1].id, period="daily", anchor=day,
        )
        assert len(me["entries"]) == 1
        assert me["entries"][0]["rank"] == 3

        stranger = league_game_boards.category_me(
            db, game_id="wordle", category="most_solved",
            user_id="00000000-0000-4000-8000-000000000000", period="daily", anchor=day,
        )
        assert stranger["entries"] == []

    def test_most_solved_respects_the_window(self, db):
        week_one = week_start(today()) - timedelta(days=14)
        week_two = week_start(today()) - timedelta(days=7)
        user = make_user(db, "MW")
        add_wordle_result(db, add_wordle_session(db, week_one), user.id, 2, True)
        add_wordle_result(db, add_wordle_session(db, week_two), user.id, 2, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        this_week = board(db, "wordle", "most_solved", period="weekly", anchor=week_two)
        assert this_week["entries"][0]["metrics"]["solved"] == 1
        everything = board(db, "wordle", "most_solved", period="all_time")
        assert everything["entries"][0]["metrics"]["solved"] == 2


class TestCategoryEndpoints:
    def test_categories_board_and_me_round_trip(self, db):
        day = today() - timedelta(days=3)
        user = make_user(db, "EP")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])
        client = user_client(user)

        categories = client.get("/leaderboards/games/wordle/categories")
        assert categories.status_code == 200, categories.text
        assert {c["id"] for c in categories.json()} >= {"performance", "most_solved"}

        game = client.get("/leaderboards/games/wordle/boards/most_solved?period=all_time")
        assert game.status_code == 200, game.text
        body = game.json()
        assert body["total"] == 1
        assert body["entries"][0]["metrics"]["solved"] == 1
        assert body["entries"][0]["rank"] == 1

        me = client.get("/leaderboards/games/wordle/boards/most_solved/me?period=all_time")
        assert me.status_code == 200, me.text
        assert me.json()["entries"][0]["rank"] == 1

        assert client.get("/leaderboards/games/wordle/boards/nope").status_code == 400
        assert client.get("/leaderboards/games/nope/categories").status_code == 400


class TestAdminCategoryEndpoints:
    def test_admin_reads_the_expanded_rankings(self, db, admin_client):
        day = today() - timedelta(days=3)
        user = make_user(db, "AC")
        session_id = add_wordle_session(db, day)
        add_wordle_result(db, session_id, user.id, 2, True)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle"])

        categories = admin_client.get("/admin/leaderboards/games/wordle/categories")
        assert categories.status_code == 200, categories.text
        assert {c["id"] for c in categories.json()} == {
            "performance", "most_solved", "fewest_guesses", "fastest_solve",
        }

        game = admin_client.get(
            "/admin/leaderboards/games/wordle/boards/fewest_guesses?period=all_time"
        )
        assert game.status_code == 200, game.text
        body = game.json()
        assert body["category"] == "fewest_guesses"
        assert body["entries"][0]["user_id"] == user.id
        assert body["entries"][0]["metrics"]["best_guesses"] == 2

        assert admin_client.get(
            "/admin/leaderboards/games/wordle/boards/nope"
        ).status_code == 400
        assert admin_client.get(
            "/admin/leaderboards/games/nope/categories"
        ).status_code == 400

    def test_admin_reads_the_headline_records(self, db, admin_client):
        day = today() - timedelta(days=3)
        wordle_user = make_user(db, "AR1")
        reflex_user = make_user(db, "AR2")
        # An explicit start/solve pair: fastest_solve needs a real duration,
        # and the shared result helper stamps "now" for both ends.
        session_id = add_wordle_session(db, day)
        with db.sessions.begin() as session:
            from app.db_models import WordleUserGameRecord

            start = midnight(day).replace(hour=10)
            session.add(
                WordleUserGameRecord(
                    wordle_game_id=session_id, user_id=wordle_user.id,
                    attempt_count=3, status="won", success=True,
                    guesses=["a", "b", "cigar"], started_at=start,
                    completed_at=start + timedelta(seconds=45),
                    solved_at=start + timedelta(seconds=45),
                )
            )
        reaction = add_reaction_session(db, day)
        add_reaction_result(db, reaction, reflex_user.id, 205)
        from app.services import league_finalization

        league_finalization.finalize_due_opportunities(db, ["wordle", "reaction-time"])

        response = admin_client.get("/admin/leaderboards/records")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["period"] == "all_time"
        highlights = {entry["game_id"]: entry for entry in body["highlights"]}
        assert set(highlights) == {
            "wordle", "anagram", "reaction-time", "connections",
            "trivia-drop", "couple-questionnaire", "two-key-escape", "plot-twist",
        }
        wordle = highlights["wordle"]
        assert wordle["category"] == "fastest_solve"
        assert wordle["label"] == "Fastest Solve"
        assert wordle["entry"]["display_name"] == wordle_user.name
        assert wordle["entry"]["metrics"]["fastest_s"] == 45.0
        reaction = highlights["reaction-time"]
        assert reaction["label"] == "All-Time Fastest"
        assert reaction["entry"]["metrics"]["fastest_ms"] == 205
        # A game with no finalized play keeps its slot with no entry.
        assert highlights["connections"]["entry"] is None

    def test_participants_cannot_read_the_admin_rankings(self, db):
        client = TestClient(app, follow_redirects=False)
        assert client.get(
            "/admin/leaderboards/games/wordle/categories"
        ).status_code == 403
        assert client.get("/admin/leaderboards/records").status_code == 403
