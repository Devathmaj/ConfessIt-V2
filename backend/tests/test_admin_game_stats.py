"""Games console telemetry: the overview's real play figures.

Pins the contract behind ``GET /admin/games/stats``:

* finished rounds only, counted with the same rules as the player
  dashboard's own summary -- an in-progress puzzle is not play yet;
* today / this week / all-time windows in UTC, with the week starting where
  league scoring starts it;
* distinct players, not sessions: a couple round is one play but two people;
* one Reaction Time session is one round per player, however many valid tries
  each player made;
* per-game totals, so the console's most-played and type panels read the same
  events the player summary does.

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
from app.models import UserDetails
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
    user_id = f"{_UID:08d}-1111-4111-8111-{_UID:012d}"
    regno = f"AS{_UID:06d}"
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
    db, day: date, user_a_id: str, user_b_id: str, *, status: str = "completed"
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
            status=status,
            completed_at=at(day, hour=18) if status == "completed" else None,
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
    db,
    session_id: str,
    user_id: str,
    *,
    attempt: int,
    millis: int,
    day: date,
    status: str = "valid",
) -> None:
    moment = at(day, hour=10 + attempt)
    with db.sessions.begin() as session:
        session.add(
            ReactionTimeSubmissionRecord(
                reaction_time_game_id=session_id,
                user_id=user_id,
                attempt_number=attempt,
                status=status,
                reaction_time_ms=millis if status == "valid" else None,
                started_at=moment,
                green_at=moment,
                clicked_at=moment,
            )
        )


def stats_for(client: TestClient) -> dict:
    response = client.get("/admin/games/stats")
    assert response.status_code == 200, response.text
    return response.json()


class TestEmptyDeployment:
    def test_no_rounds_answers_zeros(self, admin_client: TestClient):
        body = stats_for(admin_client)

        assert body == {
            "server_date": today().isoformat(),
            "plays_today": 0,
            "plays_this_week": 0,
            "plays_all_time": 0,
            "players_today": 0,
            "players_this_week": 0,
            "games": [],
        }


class TestTotalsAcrossPlayers:
    def test_counts_rounds_and_distinct_players_across_the_windows(self, db, admin_client):
        player_a = make_user(db, "a")
        player_b = make_user(db, "b")
        day0 = today()
        old = day0 - timedelta(days=7)

        # Two players finish two rounds today (one Wordle each).
        add_wordle_result(
            db, add_wordle_session(db, day0), player_a.id,
            attempts=3, solved=True, completed_on=day0,
        )
        add_wordle_result(
            db, add_wordle_session(db, day0, answer="slate"), player_b.id,
            attempts=5, solved=True, completed_on=day0,
        )
        # One player makes two valid Reaction Time tries: still one round.
        reaction_id = add_reaction_time_session(db, day0)
        add_reaction_time_result(
            db, reaction_id, player_a.id, attempt=1, millis=410, day=day0
        )
        add_reaction_time_result(
            db, reaction_id, player_a.id, attempt=2, millis=250, day=day0
        )
        # An older round is in the all-time total and in no current window.
        add_wordle_result(
            db, add_wordle_session(db, old), player_b.id,
            attempts=4, solved=False, completed_on=old,
        )

        body = stats_for(admin_client)

        assert body["server_date"] == day0.isoformat()
        assert body["plays_today"] == 3
        assert body["plays_this_week"] == 3
        assert body["plays_all_time"] == 4
        assert body["players_today"] == 2
        assert body["players_this_week"] == 2
        assert body["games"] == [
            {
                "game_id": "reaction-time",
                "plays_today": 1,
                "plays_this_week": 1,
                "plays_all_time": 1,
            },
            {
                "game_id": "wordle",
                "plays_today": 2,
                "plays_this_week": 2,
                "plays_all_time": 3,
            },
        ]

    def test_this_weeks_window_starts_where_league_scoring_starts_it(
        self, db, admin_client
    ):
        player = make_user(db, "window")
        day0 = today()
        week_open = week_start(day0)
        # The day before this week opened belongs to the previous week; the
        # first day of this week is inside it even when today is midweek.
        previous = week_open - timedelta(days=1)
        for day in (week_open, previous):
            add_wordle_result(
                db, add_wordle_session(db, day), player.id,
                attempts=3, solved=True, completed_on=day,
            )

        body = stats_for(admin_client)

        assert body["plays_this_week"] == 1
        assert body["plays_all_time"] == 2
        # "Today" is a UTC calendar day, not a rolling 24 hours.
        assert body["plays_today"] == (1 if week_open == day0 else 0)

    def test_a_shared_reaction_time_session_is_one_round_per_player(
        self, db, admin_client
    ):
        player_a = make_user(db, "reflex-a")
        player_b = make_user(db, "reflex-b")
        day0 = today()
        session_id = add_reaction_time_session(db, day0)
        add_reaction_time_result(
            db, session_id, player_a.id, attempt=1, millis=300, day=day0
        )
        add_reaction_time_result(
            db, session_id, player_b.id, attempt=1, millis=280, day=day0
        )

        body = stats_for(admin_client)

        # Two players each played one round in the same session; grouping by
        # the session alone would have reported one play.
        assert body["plays_today"] == 2
        assert body["players_today"] == 2
        assert body["games"][0]["plays_today"] == 2


class TestCoupleRounds:
    def test_a_couple_round_is_one_play_and_two_players(self, db, admin_client):
        player_a = make_user(db, "couple-a")
        player_b = make_user(db, "couple-b")
        add_couple_session(db, today(), player_a.id, player_b.id)

        body = stats_for(admin_client)

        assert body["plays_today"] == 1
        assert body["players_today"] == 2
        assert body["games"] == [
            {
                "game_id": "couple-questionnaire",
                "plays_today": 1,
                "plays_this_week": 1,
                "plays_all_time": 1,
            }
        ]

    def test_an_unfinished_couple_round_is_not_play(self, db, admin_client):
        player_a = make_user(db, "active-a")
        player_b = make_user(db, "active-b")
        add_couple_session(db, today(), player_a.id, player_b.id, status="active")

        assert stats_for(admin_client)["plays_all_time"] == 0


class TestUnfinishedRounds:
    def test_in_progress_puzzles_and_invalid_tries_are_not_play(
        self, db, admin_client
    ):
        player = make_user(db, "in-progress")
        day0 = today()
        add_in_progress_wordle(db, day0, player.id)
        session_id = add_reaction_time_session(db, day0)
        add_reaction_time_result(
            db, session_id, player.id, attempt=1, millis=120, day=day0,
            status="too_early",
        )

        body = stats_for(admin_client)

        assert body["plays_today"] == 0
        assert body["plays_all_time"] == 0
        assert body["players_today"] == 0
        assert body["games"] == []
