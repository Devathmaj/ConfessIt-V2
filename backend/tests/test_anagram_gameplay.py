"""What a player can do against a day's Anagram, and what the server refuses.

The puzzle itself is configured through the admin endpoints and tested in
``test_admin_anagram``; this file is the other half of the feature -- the participation.
The properties worth pinning:

**Reading never plays.** ``GET /games/anagram`` must not create a participation row, or
opening the screen would deal a scramble the player never asked for. A fresh player reads
no participation; a returning player reads the exact scramble, guesses, attempts and
banked time they left.

**One scramble per player per puzzle, dealt once.** The reveal creates the row and the
refresh keeps it: the same ``(anagram_game_id, user_id)`` pair always reads the same
``scrambled_word``, and two concurrent reveals race the unique pair the way concurrent
first Wordle guesses do.

**The clock counts play, not wall time.** The timer opens on reveal, banks on pause,
heartbeat and settling guess, and discards a segment left open by a dead browser rather
than crediting the dead hours. Every test below about time goes through the API -- the
server owns every number that persists.

**The answer is revealed exactly once.** A participation in progress never carries it; a
completed one does, because a loss is the one moment the player is owed it. There is no
other place the answer appears, so the "is it over" check and the "may it be revealed"
check are the same comparison.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.db_models import AnagramGameRecord, AnagramUserGameRecord
from app.limits import ANAGRAM_MAX_ATTEMPTS, ANAGRAM_TIMER_STALE_AFTER_SECONDS
from app.main import app
from app.models import UserDetails
from app.services import anagram_gameplay
from app.services.anagram_gameplay import scramble_answer
from app.services.auth_service import get_current_user
from conftest import override_get_current_user

PLAYER_UUID = "44444444-4444-4444-4444-444444444444"
OTHER_UUID = "55555555-5555-5555-5555-555555555555"

ANSWER = "ELEPHANT"


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def other_day(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def seed_game(
    db, *, word: str = ANSWER, game_date: Optional[date] = None, status: str = "active"
) -> dict:
    """Open a session straight into the table.

    Seeded directly rather than through ``POST /admin/anagram`` on purpose: the admin
    client fixture swaps the ``get_current_user`` override, which would clobber the
    participant client's override the instant the two fixtures share a test. The endpoint
    itself is pinned by ``test_admin_anagram``; here the session is a starting point, not
    the thing under test. The period mirrors a daily cadence from the session date.
    """
    day = game_date or datetime.now(timezone.utc).date()
    game = AnagramGameRecord(
        game_date=day,
        mode="custom",
        answer=word,
        created_by="adminuser",
        status=status,
        period_start_date=day,
        period_end_date=day + timedelta(days=1),
        started_at=datetime.now(timezone.utc),
    )
    with db.sessions.begin() as session:
        session.add(game)
        session.flush()
        return {
            "id": str(game.id),
            "answer": game.answer,
            "game_date": game.game_date.isoformat(),
        }


def wrong_guesses(answer: str, count: int) -> list[str]:
    """``count`` rotations of ``answer``: same letters, never the answer itself."""
    return [answer[i:] + answer[:i] for i in range(1, count + 1)]


def _insert_user(db, user_id: str, regno: str, email: str) -> None:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, 'Player', :email, 2, 'Class', 'other') "
                "ON CONFLICT (regno) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "email": email},
        )


@pytest.fixture
def player_client(db):
    """A participant whose ``users`` row exists, so participation rows can land.

    The shared ``client`` fixture resolves ``get_current_user`` to a fresh ``UserDetails``
    whose id defaults to a new uuid on every call -- which is fine for a read but leaves a
    participation's ``user_id`` pointing at a row that is not in ``users``. This pins the
    id and creates the row, the same trick conftest's ``_confession_author_client`` uses.
    """
    _insert_user(db, PLAYER_UUID, "anagramplayer", "player@user.com")
    player = UserDetails(
        id=PLAYER_UUID,
        regno="anagramplayer",
        name="Player",
        email="player@user.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )
    app.dependency_overrides[get_current_user] = lambda: player
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture
def anonymous_client():
    """A client with the auth dependency removed, to exercise the real 401 path."""
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


def participation_count(db) -> int:
    with db.sessions() as session:
        return len(session.scalars(select(AnagramUserGameRecord)).all())


def reveal(client: TestClient, game_id: str):
    # 200 rather than 201: the reveal doubles as resume, so the endpoint is idempotent
    # and answers the current participation either way.
    response = client.post("/games/anagram/start", json={"game_id": game_id})
    assert response.status_code == 200, response.text
    return response.json()


# --- the scramble --------------------------------------------------------------


def test_scramble_is_a_different_ordering_of_the_same_letters():
    """Sequence comparison, not set comparison: duplicates must move, not just exist."""
    for _ in range(25):
        scrambled = scramble_answer("ELEPHANT")
        assert sorted(scrambled) == sorted("ELEPHANT")
        assert scrambled != "ELEPHANT"

    for _ in range(25):
        scrambled = scramble_answer("APPLE")
        assert sorted(scrambled) == sorted("APPLE")
        assert scrambled != "APPLE"


def test_scramble_of_an_unscramblable_word_returns_it_unchanged():
    """A custom answer may be a single repeated letter; shuffling forever is not an
    option, so the answer is dealt as-is rather than looping."""
    assert scramble_answer("A") == "A"
    assert scramble_answer("AAAA") == "AAAA"
    # Two distinct letters always terminate on the swap.
    for _ in range(10):
        assert scramble_answer("AB") == "BA"


# --- reading -------------------------------------------------------------------


def test_reading_the_screen_with_no_puzzle_is_an_empty_game(player_client: TestClient):
    response = player_client.get("/games/anagram")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["game_id"] is None
    assert body["game_date"] is None
    assert body["participation"] is None
    assert body["max_attempts"] == ANAGRAM_MAX_ATTEMPTS


def test_reading_the_screen_does_not_create_a_participation(
    player_client: TestClient, db
):
    seed_game(db)

    response = player_client.get("/games/anagram")
    assert response.status_code == 200
    assert response.json()["participation"] is None

    assert participation_count(db) == 0


def test_a_fresh_player_reads_no_participation_and_no_time(player_client: TestClient, db):
    seed_game(db)

    body = player_client.get("/games/anagram").json()

    assert body["participation"] is None


def test_a_future_puzzle_is_not_yet_playable(player_client: TestClient, db):
    seed_game(db, game_date=datetime.now(timezone.utc).date() + timedelta(days=3))

    response = player_client.get("/games/anagram")

    assert response.status_code == 200
    assert response.json()["game_id"] is None


def test_a_start_on_a_future_puzzle_is_refused(player_client: TestClient, db):
    game = seed_game(db, game_date=datetime.now(timezone.utc).date() + timedelta(days=3))

    response = player_client.post("/games/anagram/start", json={"game_id": game["id"]})

    assert response.status_code == 400


def test_only_one_session_is_current_at_a_time(player_client: TestClient, db):
    """History shares days but only the active session is served; an ended row never
    shadows it, and a session past its boundary reads as absent."""
    seed_game(
        db,
        game_date=datetime.now(timezone.utc).date() - timedelta(days=2),
        status="ended",
    )
    assert player_client.get("/games/anagram").json()["game_id"] is None

    current = seed_game(db)

    body = player_client.get("/games/anagram").json()
    assert body["game_id"] == current["id"]


# --- starting ------------------------------------------------------------------


def test_reveal_deals_a_scramble_and_opens_the_clock(
    player_client: TestClient, db
):
    game = seed_game(db)

    participation = reveal(player_client, game["id"])

    assert participation["anagram_game_id"] == game["id"]
    assert participation["status"] == "in_progress"
    assert participation["success"] is False
    assert participation["attempt_count"] == 0
    assert participation["guesses"] == []
    assert sorted(participation["scrambled_word"]) == sorted(game["answer"])
    assert participation["scrambled_word"] != game["answer"]
    assert participation["active_time_seconds"] == 0
    assert participation["timer_started_at"] is not None
    assert participation["timer_running"] is True
    assert participation["started_at"] is not None
    assert participation["completed_at"] is None
    assert participation["solved_at"] is None
    # The answer stays secret while the game runs.
    assert participation["answer"] is None

    assert participation_count(db) == 1


def test_a_returning_player_reads_the_participation_they_left(
    player_client: TestClient, db
):
    game = seed_game(db)
    first = reveal(player_client, game["id"])
    player_client.post(
        "/games/anagram/guess",
        json={"game_id": game["id"], "guess": wrong_guesses(game["answer"], 1)[0]},
    )

    body = player_client.get("/games/anagram").json()
    participation = body["participation"]

    assert participation["scrambled_word"] == first["scrambled_word"]
    assert participation["attempt_count"] == 1
    assert len(participation["guesses"]) == 1
    assert participation["status"] == "in_progress"
    assert participation["answer"] is None


def test_a_reveal_on_a_finished_game_is_refused(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": game["answer"]}
    )

    response = player_client.post("/games/anagram/start", json={"game_id": game["id"]})

    assert response.status_code == 409
    assert participation_count(db) == 1


def test_a_second_player_gets_their_own_scramble(player_client: TestClient, db):
    """Two players play the same puzzle without sharing a row, a scramble or a budget."""
    game = seed_game(db)
    first = reveal(player_client, game["id"])

    _insert_user(db, OTHER_UUID, "secondplayer", "second@player.com")
    from app.services import anagram_gameplay as gameplay

    second = gameplay.start_participation(db, OTHER_UUID, game["id"])

    assert second.attempt_count == 0
    assert sorted(second.scrambled_word) == sorted(game["answer"])
    assert second.scrambled_word != game["answer"]
    with db.sessions() as session:
        rows = session.scalars(
            select(AnagramUserGameRecord).where(
                AnagramUserGameRecord.anagram_game_id == game["id"]
            )
        ).all()
    assert len(rows) == 2


def test_the_schema_refuses_a_second_row_for_one_player(db):
    game = seed_game(db)
    _insert_user(db, PLAYER_UUID, "anagramplayer", "player@user.com")
    with db.sessions.begin() as session:
        session.add(
            AnagramUserGameRecord(
                anagram_game_id=game["id"],
                user_id=PLAYER_UUID,
                scrambled_word="HEPLTANE",
                attempt_count=0,
                status="in_progress",
                success=False,
                guesses=[],
            )
        )

    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                AnagramUserGameRecord(
                    anagram_game_id=game["id"],
                    user_id=PLAYER_UUID,
                    scrambled_word="HEPLTANE",
                    attempt_count=0,
                    status="in_progress",
                    success=False,
                    guesses=[],
                )
            )


# --- guessing ------------------------------------------------------------------


def test_a_correct_guess_wins_and_stops_the_clock(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])

    response = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": game["answer"]}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["correct"] is True
    assert body["guess"] == game["answer"]
    assert body["status"] == "won"
    assert body["success"] is True
    assert body["attempt_count"] == 1
    assert body["solved_at"] is not None
    assert body["completed_at"] is not None
    assert body["timer_started_at"] is None
    assert body["timer_running"] is False
    # A winner already typed it; the reveal costs nothing now.
    assert body["answer"] == game["answer"]


def test_a_wrong_guess_keeps_the_game_and_the_scramble(player_client: TestClient, db):
    game = seed_game(db)
    first = reveal(player_client, game["id"])
    wrong = wrong_guesses(game["answer"], 1)[0]

    response = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": wrong}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["correct"] is False
    assert body["status"] == "in_progress"
    assert body["success"] is False
    assert body["attempt_count"] == 1
    assert body["guesses"] == [wrong]
    assert body["solved_at"] is None
    assert body["completed_at"] is None
    assert body["answer"] is None
    # No re-deal: the tiles on screen are still the ones the reveal dealt.
    assert body["scrambled_word"] == first["scrambled_word"]
    assert body["timer_running"] is True


def test_five_wrong_guesses_lose_and_reveal_the_answer(
    player_client: TestClient, db
):
    game = seed_game(db)
    reveal(player_client, game["id"])

    for guess in wrong_guesses(game["answer"], ANAGRAM_MAX_ATTEMPTS):
        response = player_client.post(
            "/games/anagram/guess", json={"game_id": game["id"], "guess": guess}
        )
        assert response.status_code == 200, response.text

    body = response.json()
    assert body["status"] == "lost"
    assert body["success"] is False
    assert body["attempt_count"] == ANAGRAM_MAX_ATTEMPTS
    assert len(body["guesses"]) == ANAGRAM_MAX_ATTEMPTS
    assert body["completed_at"] is not None
    assert body["solved_at"] is None
    assert body["timer_started_at"] is None
    assert body["timer_running"] is False
    # The loss is the one moment the answer is owed.
    assert body["answer"] == game["answer"]

    again = player_client.get("/games/anagram").json()["participation"]
    assert again["answer"] == game["answer"]
    assert again["status"] == "lost"


def test_a_sixth_guess_is_refused(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    for guess in wrong_guesses(game["answer"], ANAGRAM_MAX_ATTEMPTS):
        player_client.post(
            "/games/anagram/guess", json={"game_id": game["id"], "guess": guess}
        )

    refused = player_client.post(
        "/games/anagram/guess",
        json={"game_id": game["id"], "guess": wrong_guesses(game["answer"], 6)[5]},
    )

    assert refused.status_code == 409
    body = player_client.get("/games/anagram").json()["participation"]
    assert body["attempt_count"] == ANAGRAM_MAX_ATTEMPTS


def test_a_guess_after_a_win_is_refused(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": game["answer"]}
    )

    refused = player_client.post(
        "/games/anagram/guess",
        json={"game_id": game["id"], "guess": wrong_guesses(game["answer"], 1)[0]},
    )

    assert refused.status_code == 409


def test_a_guess_without_a_reveal_is_refused(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": game["answer"]}
    )

    assert response.status_code == 404


@pytest.mark.parametrize(
    "guess",
    [
        "",  # empty
        "   ",  # whitespace only
        "ELEPH4NT",  # not letters
        "ELEPH ANT",  # a space
        "ELEPHAN",  # one tile short
        "ELEPHANTT",  # one tile too many
        "ABCDEFGH",  # letters the player was never dealt
    ],
)
def test_a_malformed_guess_is_refused_without_spending_an_attempt(
    player_client: TestClient, db, guess
):
    game = seed_game(db)
    reveal(player_client, game["id"])

    response = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": guess}
    )

    assert response.status_code == 400
    body = player_client.get("/games/anagram").json()["participation"]
    assert body["attempt_count"] == 0
    assert body["guesses"] == []


def test_a_lowercase_guess_is_accepted_as_its_uppercase_spelling(
    player_client: TestClient, db
):
    game = seed_game(db)
    reveal(player_client, game["id"])

    response = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": "elephant"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["correct"] is True


def test_an_unknown_field_on_the_payload_is_a_422(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/anagram/guess",
        json={"game_id": game["id"], "guess": game["answer"], "cheat": True},
    )

    assert response.status_code == 422


# --- the clock -----------------------------------------------------------------


def _backdate_open_segment(db, game_id: str, user_id: str, seconds_ago: int) -> None:
    """Age the open segment as if the browser died ``seconds_ago`` without pausing."""
    with db.sessions.begin() as session:
        row = session.scalar(
            select(AnagramUserGameRecord).where(
                AnagramUserGameRecord.anagram_game_id == game_id,
                AnagramUserGameRecord.user_id == user_id,
            )
        )
        assert row is not None and row.timer_started_at is not None
        row.timer_started_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)


def test_pause_banks_the_segment_and_stops_the_clock(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    time.sleep(1.1)

    paused = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()

    assert paused["timer_started_at"] is None
    assert paused["timer_running"] is False
    assert paused["active_time_seconds"] >= 1
    assert paused["status"] == "in_progress"

    # Pausing again changes nothing: idempotent by construction.
    again = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()
    assert again["active_time_seconds"] == paused["active_time_seconds"]
    assert again["timer_started_at"] is None


def test_pause_without_play_time_banks_nothing(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])

    paused = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()

    assert paused["active_time_seconds"] == 0
    assert paused["timer_running"] is False


def test_resume_opens_a_new_segment_without_touching_banked_time(
    player_client: TestClient, db
):
    game = seed_game(db)
    reveal(player_client, game["id"])
    time.sleep(1.1)
    banked = player_client.post("/games/anagram/pause", json={"game_id": game["id"]}).json()[
        "active_time_seconds"
    ]
    assert banked >= 1

    resumed = reveal(player_client, game["id"])

    assert resumed["timer_started_at"] is not None
    assert resumed["timer_running"] is True
    assert resumed["active_time_seconds"] == banked
    assert resumed["status"] == "in_progress"


def test_returning_does_not_restart_the_clock_by_itself(
    player_client: TestClient, db
):
    """Opening the screen is a read: a paused game stays paused until RESUME."""
    game = seed_game(db)
    reveal(player_client, game["id"])
    player_client.post("/games/anagram/pause", json={"game_id": game["id"]})

    body = player_client.get("/games/anagram").json()["participation"]

    assert body["timer_started_at"] is None
    assert body["timer_running"] is False


def test_a_stale_open_segment_reads_as_paused(player_client: TestClient, db):
    """The browser died without pausing: the screen must offer RESUME, not a ticking
    clock, and must not auto-start on load."""
    game = seed_game(db)
    reveal(player_client, game["id"])
    _backdate_open_segment(db, game["id"], PLAYER_UUID, ANAGRAM_TIMER_STALE_AFTER_SECONDS + 60)

    body = player_client.get("/games/anagram").json()["participation"]

    assert body["timer_running"] is False


def test_a_stale_segment_is_discarded_on_pause(player_client: TestClient, db):
    """Navigation away after a death credits nothing for the dead hours."""
    game = seed_game(db)
    reveal(player_client, game["id"])
    _backdate_open_segment(db, game["id"], PLAYER_UUID, ANAGRAM_TIMER_STALE_AFTER_SECONDS + 3600)

    paused = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()

    assert paused["active_time_seconds"] == 0
    assert paused["timer_started_at"] is None


def test_a_stale_segment_is_discarded_on_resume(player_client: TestClient, db):
    """Logout, crash, return: the dead period is dropped and a fresh segment opens, so
    no time is counted while unauthenticated or away."""
    game = seed_game(db)
    reveal(player_client, game["id"])
    _backdate_open_segment(db, game["id"], PLAYER_UUID, ANAGRAM_TIMER_STALE_AFTER_SECONDS + 3600)

    resumed = reveal(player_client, game["id"])

    assert resumed["active_time_seconds"] == 0
    assert resumed["timer_running"] is True


def test_heartbeat_checkpoints_without_stopping(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    time.sleep(1.1)

    checked = player_client.post(
        "/games/anagram/heartbeat", json={"game_id": game["id"]}
    ).json()

    assert checked["timer_started_at"] is not None
    assert checked["timer_running"] is True
    assert checked["active_time_seconds"] >= 1

    # The clock kept running across the checkpoint: a later pause banks more.
    time.sleep(1.1)
    paused = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()
    assert paused["active_time_seconds"] >= checked["active_time_seconds"] + 1


def test_heartbeat_on_a_paused_game_is_a_no_op(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    player_client.post("/games/anagram/pause", json={"game_id": game["id"]})

    checked = player_client.post(
        "/games/anagram/heartbeat", json={"game_id": game["id"]}
    ).json()

    assert checked["timer_started_at"] is None
    assert checked["timer_running"] is False


def test_completion_persists_the_final_active_time(player_client: TestClient, db):
    game = seed_game(db)
    reveal(player_client, game["id"])
    time.sleep(1.1)

    solved = player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": game["answer"]}
    ).json()

    assert solved["status"] == "won"
    assert solved["active_time_seconds"] >= 1
    assert solved["timer_started_at"] is None

    # The persisted total survives the refresh: nothing after completion moves it.
    again = player_client.get("/games/anagram").json()["participation"]
    assert again["active_time_seconds"] == solved["active_time_seconds"]


def test_refresh_restores_scramble_guesses_attempts_and_time(
    player_client: TestClient, db
):
    game = seed_game(db)
    first = reveal(player_client, game["id"])
    wrong = wrong_guesses(game["answer"], 1)[0]
    player_client.post(
        "/games/anagram/guess", json={"game_id": game["id"], "guess": wrong}
    )
    paused = player_client.post(
        "/games/anagram/pause", json={"game_id": game["id"]}
    ).json()

    again = player_client.get("/games/anagram").json()["participation"]

    assert again["scrambled_word"] == first["scrambled_word"]
    assert again["guesses"] == [wrong]
    assert again["attempt_count"] == 1
    assert again["status"] == "in_progress"
    assert again["active_time_seconds"] == paused["active_time_seconds"]
    assert again["timer_running"] is False


# --- who may play --------------------------------------------------------------


def test_an_anonymous_caller_is_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/games/anagram").status_code == 401
    assert anonymous_client.post(
        "/games/anagram/start",
        json={"game_id": "11111111-1111-1111-1111-111111111111"},
    ).status_code == 401
    assert anonymous_client.post(
        "/games/anagram/guess",
        json={"game_id": "11111111-1111-1111-1111-111111111111", "guess": "ELEPHANT"},
    ).status_code == 401


def test_staff_cannot_play(admin_client: TestClient, moderator_client: TestClient):
    assert admin_client.get("/games/anagram").status_code == 403
    assert moderator_client.get("/games/anagram").status_code == 403
    assert admin_client.post(
        "/games/anagram/start",
        json={"game_id": "11111111-1111-1111-1111-111111111111"},
    ).status_code == 403


# --- concurrency ---------------------------------------------------------------


def test_concurrent_reveals_share_one_participation(db):
    """The core regression: simultaneous reveals must yield one row, not two.

    Each request reads 'no participation yet' and each tries to insert. The unique pair
    is what settles the race: exactly one insert wins, and the loser re-reads the
    winner's row under lock and resumes *that* -- so the race cannot mint two scrambles
    or two attempt budgets.
    """
    from app.services import anagram_gameplay as gameplay

    _insert_user(db, PLAYER_UUID, "anagramplayer", "player@user.com")
    game = seed_game(db)
    workers = 2
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=30)
        outcome = gameplay.start_participation(db, PLAYER_UUID, game["id"])
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt) for _ in range(workers)]:
            future.result(timeout=60)

    assert len(results) == workers
    assert all(result.scrambled_word for result in results)
    with db.sessions() as session:
        rows = session.scalars(
            select(AnagramUserGameRecord).where(
                AnagramUserGameRecord.anagram_game_id == game["id"],
                AnagramUserGameRecord.user_id == PLAYER_UUID,
            )
        ).all()
    assert len(rows) == 1, "two requests must not create two participations"
    assert {result.scrambled_word for result in results} == {rows[0].scrambled_word}
