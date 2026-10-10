"""Player activity summary: the Games dashboard's real stats and history.

Pins the contract the dashboard reads from ``GET /games/summary``:

* finished rounds only -- an in-progress puzzle is not history yet;
* finished-round counts, current/longest daily streaks in UTC (with the
  "yesterday keeps the streak alive" grace), and this week's distinct games;
* newest-first recent rounds with their display summaries and native
  performance numbers;
* couple rounds showing for both partners;
* an empty history answering zeros rather than inventing anything.

Fixtures insert participation rows directly with dates relative to the real
UTC today, so the suite cannot rot as the calendar moves.
"""

from datetime import date, datetime, time, timedelta, timezone

from fastapi.testclient import TestClient

from app.accounts import AccountType
from app.db_models import (
    CoupleGameRecord,
    CoupleGameSessionRecord,
    ReactionTimeGameRecord,
    ReactionTimeSubmissionRecord,
    WordleGameRecord,
    WordleUserGameRecord,
)
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user
from app.services.league_scoring import week_start

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
    regno = f"PS{_UID:06d}"
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
                "name": f"Player {tag} {_UID}",
                "email": f"{regno}@t.com",
            },
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=f"Player {tag} {_UID}",
        email=f"{regno}@t.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )


def user_client(user: UserDetails) -> TestClient:
    # Mirror the suite convention: the auth override is application-global and
    # the autouse fixture reinstalls the default before every test, so leaving
    # this test's identity in place is safe.
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app, follow_redirects=False)


def add_wordle_session(db, start: date, answer: str = "cigar") -> str:
    with db.sessions.begin() as session:
        row = WordleGameRecord(
            game_date=start,
            mode="random",
            answer=answer,
            status="ended",
            period_start_date=start,
            period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_wordle_result(
    db,
    session_id: str,
    user_id: str,
    *,
    attempts: int,
    solved: bool,
    completed_on: date,
) -> None:
    moment = at(completed_on)
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


def add_in_progress_wordle(db, start: date, user_id: str) -> None:
    """A started-but-unsettled puzzle: participation, not history."""
    session_id = add_wordle_session(db, start)
    with db.sessions.begin() as session:
        session.add(
            WordleUserGameRecord(
                wordle_game_id=session_id,
                user_id=user_id,
                attempt_count=1,
                status="in_progress",
                success=False,
                guesses=["cigar"],
            )
        )


def add_couple_session(
    db, day: date, user_a_id: str, user_b_id: str
) -> str:
    with db.sessions.begin() as session:
        game = CoupleGameRecord(
            game_date=day,
            status="ended",
            period_start_date=day,
            period_end_date=day + timedelta(days=1),
            started_at=midnight(day),
            ended_at=midnight(day + timedelta(days=1)),
            created_by="TEST",
        )
        session.add(game)
        session.flush()
        row = CoupleGameSessionRecord(
            couple_game_id=str(game.id),
            user_a_id=user_a_id,
            user_b_id=user_b_id,
            status="completed",
            completed_at=at(day, hour=18),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_reaction_time_session(db, start: date) -> str:
    with db.sessions.begin() as session:
        row = ReactionTimeGameRecord(
            game_date=start,
            status="ended",
            period_start_date=start,
            period_end_date=start + timedelta(days=1),
            started_at=midnight(start),
            ended_at=midnight(start + timedelta(days=1)),
            created_by="TEST",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def add_reaction_time_result(
    db, session_id: str, user_id: str, *, attempt: int, millis: int, day: date
) -> None:
    moment = at(day, hour=10 + attempt)
    with db.sessions.begin() as session:
        session.add(
            ReactionTimeSubmissionRecord(
                reaction_time_game_id=session_id,
                user_id=user_id,
                attempt_number=attempt,
                status="valid",
                reaction_time_ms=millis,
                started_at=moment,
                green_at=moment,
                clicked_at=moment,
            )
        )


def summary_for(client: TestClient) -> dict:
    response = client.get("/games/summary")
    assert response.status_code == 200, response.text
    return response.json()


class TestEmptyHistory:
    def test_a_player_with_no_rounds_answers_zeros(self, db):
        user = make_user(db, "empty")
        body = summary_for(user_client(user))

        assert body == {
            "games_played": 0,
            "current_streak_days": 0,
            "longest_streak_days": 0,
            "plays_this_week": 0,
            "games_this_week": [],
            "recent_plays": [],
        }

    def test_an_unsettled_round_is_not_history(self, db):
        user = make_user(db, "unsettled")
        add_in_progress_wordle(db, today(), user.id)

        body = summary_for(user_client(user))

        assert body["games_played"] == 0
        assert body["recent_plays"] == []


class TestStatsAndHistory:
    def test_finished_rounds_count_and_the_history_reads_newest_first(self, db):
        user = make_user(db, "rounds")
        day0 = today()
        # Settled rounds on today, yesterday and two days ago (one run), plus
        # an older loss that leaves a gap and so starts no second run.
        add_wordle_result(
            db, add_wordle_session(db, day0), user.id, attempts=3, solved=True, completed_on=day0
        )
        add_wordle_result(
            db,
            add_wordle_session(db, day0 - timedelta(days=1)),
            user.id,
            attempts=6,
            solved=False,
            completed_on=day0 - timedelta(days=1),
        )
        add_wordle_result(
            db,
            add_wordle_session(db, day0 - timedelta(days=2)),
            user.id,
            attempts=2,
            solved=True,
            completed_on=day0 - timedelta(days=2),
        )
        add_wordle_result(
            db,
            add_wordle_session(db, day0 - timedelta(days=5)),
            user.id,
            attempts=4,
            solved=True,
            completed_on=day0 - timedelta(days=5),
        )

        body = summary_for(user_client(user))

        assert body["games_played"] == 4
        assert body["current_streak_days"] == 3
        assert body["longest_streak_days"] == 3
        assert [play["played_at"][:10] for play in body["recent_plays"]] == [
            (day0 - timedelta(days=offset)).isoformat() for offset in (0, 1, 2, 5)
        ]
        newest = body["recent_plays"][0]
        assert newest["game_id"] == "wordle"
        assert newest["summary"] == "Solved in 3"
        assert newest["score"] == 3.0
        assert newest["score_label"] == "guesses"
        assert body["recent_plays"][1]["summary"] == "Not solved"

    def test_yesterday_keeps_the_streak_alive(self, db):
        user = make_user(db, "grace")
        day0 = today()
        add_wordle_result(
            db,
            add_wordle_session(db, day0 - timedelta(days=1)),
            user.id,
            attempts=3,
            solved=True,
            completed_on=day0 - timedelta(days=1),
        )

        body = summary_for(user_client(user))

        assert body["current_streak_days"] == 1

    def test_couple_rounds_show_for_both_partners(self, db):
        user_a = make_user(db, "partner-a")
        user_b = make_user(db, "partner-b")
        day0 = today()
        add_couple_session(db, day0, user_a.id, user_b.id)

        for user in (user_a, user_b):
            body = summary_for(user_client(user))
            assert body["games_played"] == 1
            assert body["recent_plays"][0]["game_id"] == "couple-questionnaire"
            assert body["recent_plays"][0]["summary"] == "Questionnaire complete"

    def test_reaction_time_sessions_are_one_round_with_the_best_time(self, db):
        user = make_user(db, "reflex")
        day0 = today()
        session_id = add_reaction_time_session(db, day0)
        add_reaction_time_result(db, session_id, user.id, attempt=1, millis=410, day=day0)
        add_reaction_time_result(db, session_id, user.id, attempt=2, millis=280, day=day0)

        body = summary_for(user_client(user))

        assert body["games_played"] == 1
        play = body["recent_plays"][0]
        assert play["game_id"] == "reaction-time"
        assert play["summary"] == "Best of 2 tries"
        assert play["score"] == 280.0
        assert play["score_label"] == "ms"

    def test_the_recent_list_caps_at_five(self, db):
        user = make_user(db, "busy")
        day0 = today()
        for offset in range(7):
            day = day0 - timedelta(days=offset)
            add_wordle_result(
                db,
                add_wordle_session(db, day),
                user.id,
                attempts=3,
                solved=True,
                completed_on=day,
            )

        body = summary_for(user_client(user))

        assert body["games_played"] == 7
        assert len(body["recent_plays"]) == 5


class TestWeeklyActivity:
    def test_this_weeks_rounds_and_distinct_games(self, db):
        user = make_user(db, "weekly")
        day0 = today()
        add_wordle_result(
            db, add_wordle_session(db, day0), user.id, attempts=3, solved=True, completed_on=day0
        )
        # A round is one game per session: a second Wordle result today is a
        # second round, still the same distinct title.
        add_wordle_result(
            db,
            add_wordle_session(db, day0, answer="slate"),
            user.id,
            attempts=5,
            solved=True,
            completed_on=day0,
        )
        session_id = add_reaction_time_session(db, day0)
        add_reaction_time_result(db, session_id, user.id, attempt=1, millis=250, day=day0)
        # A week ago is always in a previous week, whatever weekday today is.
        add_wordle_result(
            db,
            add_wordle_session(db, day0 - timedelta(days=7)),
            user.id,
            attempts=4,
            solved=True,
            completed_on=day0 - timedelta(days=7),
        )

        body = summary_for(user_client(user))

        assert body["plays_this_week"] == 3
        assert body["games_this_week"] == ["reaction-time", "wordle"]

    def test_a_play_just_inside_and_outside_the_week_window(self, db):
        user = make_user(db, "window")
        day0 = today()
        week_open = week_start(day0)
        week_close = week_open + timedelta(days=6)
        inside = week_close - timedelta(days=1)
        assert week_open <= inside <= week_close
        # The day before the week opened belongs to the previous week.
        outside = week_open - timedelta(days=1)
        for day in (inside, outside):
            add_wordle_result(
                db,
                add_wordle_session(db, day),
                user.id,
                attempts=3,
                solved=True,
                completed_on=day,
            )

        body = summary_for(user_client(user))

        assert body["games_played"] == 2
        assert body["plays_this_week"] == 1
