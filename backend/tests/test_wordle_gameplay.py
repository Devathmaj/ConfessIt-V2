"""What a player can do against a day's Wordle, and what the server refuses.

The puzzle itself is configured through the admin endpoints and tested in
``test_admin_wordle``; this file is the other half of the feature -- the participation.
The properties worth pinning:

**Reading never plays.** ``GET /games/wordle`` must not create a participation row, or
opening the screen would spend an attempt a player never made. A fresh player reads no
participation; a returning player reads the exact board they left, with every verdict
re-derived rather than restated.

**One participation per player per puzzle, even when two guesses land at once.** The
unique ``(wordle_game_id, user_id)`` pair is the rule and the lazy insert is the race: the
concurrency test at the bottom fires two first guesses at the same moment and asserts one
row holds both.

**The answer is revealed exactly once.** A participation in progress never carries it; a
completed one does, because the player has lost nothing by being told. There is no other
place the answer appears, so the "is it over" check and the "may it be revealed" check are
the same comparison.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.db_models import WordleGameRecord, WordleUserGameRecord
from app.limits import WORDLE_MAX_ATTEMPTS
from app.main import app
from app.models import UserDetails, WordleGuessRequest
from app.services import wordle_gameplay
from app.services.auth_service import get_current_user
from app.services.wordle_service import guess_words, solution_words
from conftest import override_get_current_user

PLAYER_UUID = "44444444-4444-4444-4444-444444444444"
OTHER_UUID = "55555555-5555-5555-5555-555555555555"


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def other_day(days: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=days)).isoformat()


def seed_game(db, *, word: Optional[str] = None, game_date: Optional[date] = None) -> dict:
    """Publish a puzzle straight into the table.

    Seeded directly rather than through ``POST /admin/wordle`` on purpose: the admin client
    fixture swaps the ``get_current_user`` override, which would clobber the participant
    client's override the instant the two fixtures share a test. The endpoint itself is
    pinned by ``test_admin_wordle``; here the puzzle is a starting point, not the thing
    under test.
    """
    game = WordleGameRecord(
        game_date=game_date or datetime.now(timezone.utc).date(),
        mode="custom",
        answer=word or solution_words()[0],
    )
    with db.sessions.begin() as session:
        session.add(game)
        session.flush()
        return {
            "id": str(game.id),
            "answer": game.answer,
            "game_date": game.game_date.isoformat(),
        }


def wrong_guesses(count: int, answer: str) -> list[str]:
    """``count`` words that are legal guesses but cannot be the right one."""
    return [word for word in guess_words() if word != answer][:count]


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
    guess's ``user_id`` pointing at a row that is not in ``users``. This pins the id and
    creates the row, the same trick conftest's ``_confession_author_client`` uses.
    """
    _insert_user(db, PLAYER_UUID, "wordleplayer", "player@user.com")
    player = UserDetails(
        id=PLAYER_UUID,
        regno="wordleplayer",
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
        return len(session.scalars(select(WordleUserGameRecord)).all())


# --- evaluation ----------------------------------------------------------------


def test_evaluate_guess_marks_correct_then_present_then_absent():
    assert wordle_gameplay.evaluate_guess("abbey", "abyss") == [
        "correct", "correct", "present", "absent", "absent",
    ]
    assert wordle_gameplay.evaluate_guess("cigar", "cigar") == ["correct"] * 5


def test_evaluate_guess_counts_duplicate_letters_once():
    # The answer has 'e' twice and the guess offers it twice more: the position that
    # already matched consumes one 'e', and the two guesses that cannot both be right are
    # each judged against what the answer has left -- so the second 'e' is present and the
    # third and fourth are nothing.
    assert wordle_gameplay.evaluate_guess("eerie", "reeds") == [
        "present", "correct", "present", "absent", "absent",
    ]
    # A guess with more of a letter than the answer has must not borrow from the position
    # that already matched: one 'a' in the answer, presented twice in the guess.
    assert wordle_gameplay.evaluate_guess("abbey", "ababa") == [
        "correct", "correct", "absent", "present", "absent",
    ]


# --- reading -------------------------------------------------------------------


def test_reading_the_board_with_no_puzzle_is_an_empty_game(player_client: TestClient):
    response = player_client.get("/games/wordle")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["game_id"] is None
    assert body["game_date"] is None
    assert body["participation"] is None
    assert body["max_attempts"] == 6
    assert body["word_length"] == 5


def test_reading_the_board_does_not_create_a_participation(player_client: TestClient, db):
    seed_game(db)

    response = player_client.get("/games/wordle")
    assert response.status_code == 200
    assert response.json()["participation"] is None

    assert participation_count(db) == 0


def test_a_returning_player_reads_the_board_they_left(player_client: TestClient, db):
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"})

    response = player_client.get("/games/wordle")
    assert response.status_code == 200
    body = response.json()
    assert body["game_id"] == game["id"]
    participation = body["participation"]
    assert participation["attempt_count"] == 1
    assert participation["guesses"] == ["abaca"]
    assert participation["status"] == "in_progress"
    # The verdict is re-derived on read, so it is present even though the guess happened in
    # a previous request.
    assert participation["results"] == [wordle_gameplay.evaluate_guess(game["answer"], "abaca")]
    assert participation["answer"] is None


def test_a_future_puzzle_is_not_yet_playable(player_client: TestClient, db):
    seed_game(db, game_date=datetime.now(timezone.utc).date() + timedelta(days=3))

    response = player_client.get("/games/wordle")

    assert response.status_code == 200
    assert response.json()["game_id"] is None


def test_an_earlier_puzzle_stays_playable_until_a_newer_day_arrives(player_client: TestClient, db):
    seed_game(db, game_date=datetime.now(timezone.utc).date() - timedelta(days=1))

    response = player_client.get("/games/wordle")

    assert response.status_code == 200
    assert response.json()["game_date"] == other_day(-1)


def test_the_newest_arrived_puzzle_wins(player_client: TestClient, db):
    seed_game(db, game_date=datetime.now(timezone.utc).date() - timedelta(days=2))
    newest = seed_game(db, game_date=datetime.now(timezone.utc).date() - timedelta(days=1))

    body = player_client.get("/games/wordle").json()
    assert body["game_id"] == newest["id"]


# --- one participation per player, per puzzle ----------------------------------


def test_a_second_player_gets_their_own_participation(player_client: TestClient, db):
    """Two players play the same puzzle without sharing a row or an attempt budget.

    The first player commits its guess through the HTTP client; the second cannot share
    that client without swapping the ``get_current_user`` override out from under the
    first, so its guess goes through the service directly -- the same function the router
    calls -- against a user row created the way the fixture creates the first player's.
    """
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"})
    first = player_client.get("/games/wordle").json()["participation"]
    assert first["attempt_count"] == 1 and first["guesses"] == ["abaca"]

    _insert_user(db, OTHER_UUID, "secondplayer", "second@player.com")
    second = wordle_gameplay.submit_guess(
        db, OTHER_UUID, WordleGuessRequest(game_id=game["id"], guess="abaci")
    )

    assert second.attempt_count == 1 and second.guesses == ["abaci"]
    with db.sessions() as session:
        rows = session.scalars(
            select(WordleUserGameRecord).where(
                WordleUserGameRecord.wordle_game_id == game["id"]
            )
        ).all()
    assert {row.user_id for row in rows} == {PLAYER_UUID, OTHER_UUID}
    assert sorted([row.attempt_count for row in rows]) == [1, 1]


def test_the_schema_refuses_two_participations_for_one_pair(player_client: TestClient, db):
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"})

    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                WordleUserGameRecord(
                    wordle_game_id=game["id"],
                    user_id=PLAYER_UUID,
                    attempt_count=0,
                    status="in_progress",
                    success=False,
                    guesses=[],
                )
            )


# --- settling a guess ----------------------------------------------------------


def test_the_first_guess_creates_the_participation(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["wordle_game_id"] == game["id"]
    assert body["attempt_count"] == 1
    assert body["status"] == "in_progress"
    assert body["guesses"] == ["abaca"]
    assert body["guess"] == "abaca"
    assert body["result"] == wordle_gameplay.evaluate_guess(game["answer"], "abaca")
    assert body["answer"] is None
    assert body["max_attempts"] == 6
    assert body["solved_at"] is None
    assert body["completed_at"] is None
    assert participation_count(db) == 1


def test_a_guess_is_normalised_before_being_stored(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "  CIGAR  "}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["guesses"] == ["cigar"]
    # A capitalised guess at the answer is still a win.
    assert body["status"] == "won"


def test_guessing_the_answer_wins(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": game["answer"]}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "won"
    assert body["success"] is True
    assert body["attempt_count"] == 1
    assert body["completed_at"] is not None
    assert body["solved_at"] is not None
    assert body["answer"] == game["answer"]


def test_six_wrong_guesses_lose(player_client: TestClient, db):
    game = seed_game(db)
    response = None
    for guess in wrong_guesses(6, game["answer"]):
        response = player_client.post(
            "/games/wordle/guess", json={"game_id": game["id"], "guess": guess}
        )
        assert response.status_code == 200, response.text

    final = response.json()
    assert final["status"] == "lost"
    assert final["success"] is False
    assert final["attempt_count"] == 6
    assert final["completed_at"] is not None
    # A loss has no solved-at: 'the instant you failed' is a stat nobody wants.
    assert final["solved_at"] is None
    assert final["answer"] == game["answer"]


def test_completed_participation_reveals_the_answer_on_read(player_client: TestClient, db):
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": game["answer"]})

    participation = player_client.get("/games/wordle").json()["participation"]

    assert participation["status"] == "won"
    assert participation["answer"] == game["answer"]


def test_an_in_progress_participation_never_reveals_the_answer(player_client: TestClient, db):
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"})

    participation = player_client.get("/games/wordle").json()["participation"]

    assert participation["status"] == "in_progress"
    assert participation["answer"] is None


def test_guessing_after_the_game_is_finished_is_409(player_client: TestClient, db):
    game = seed_game(db)
    player_client.post("/games/wordle/guess", json={"game_id": game["id"], "guess": game["answer"]})

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"}
    )

    assert response.status_code == 409


def test_a_seventh_guess_is_refused_even_against_a_shadowed_row(player_client: TestClient, db):
    """The attempt_count >= max check is a backstop, not the normal path.

    The service settles a loss at the sixth guess, so a seventh can never legitimately
    arrive at 'in_progress'. But a row written by a build that predates the settle rule, or
    restored from a backup, could be at six guesses and still playing -- and that row has to
    be refused rather than played. Write it directly to prove the guard exists.
    """
    game = seed_game(db)
    with db.sessions.begin() as session:
        session.add(
            WordleUserGameRecord(
                wordle_game_id=game["id"],
                user_id=PLAYER_UUID,
                attempt_count=WORDLE_MAX_ATTEMPTS,
                status="in_progress",
                success=False,
                guesses=[],
            )
        )

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"}
    )

    assert response.status_code == 409


# --- refusal messages and shapes -----------------------------------------------


def test_an_unknown_puzzle_is_404_and_a_malformed_id_is_400(player_client: TestClient):
    missing = player_client.post(
        "/games/wordle/guess",
        json={"game_id": "11111111-1111-1111-1111-111111111111", "guess": "abaca"},
    )
    assert missing.status_code == 404

    malformed = player_client.post(
        "/games/wordle/guess", json={"game_id": "not-an-id", "guess": "abaca"}
    )
    assert malformed.status_code == 400


def test_a_future_puzzle_cannot_be_played_yet(player_client: TestClient, db):
    game = seed_game(db, game_date=datetime.now(timezone.utc).date() + timedelta(days=2))

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abaca"}
    )

    assert response.status_code == 400
    assert "not started" in response.json()["detail"]


def test_a_guess_that_is_not_a_word_is_refused(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "zzzzz"}
    )

    assert response.status_code == 400
    assert "not a word" in response.json()["detail"]


def test_a_guess_of_the_wrong_shape_is_refused(player_client: TestClient, db):
    game = seed_game(db)
    assert player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abc"}
    ).status_code == 400  # too short
    assert player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "abacaish"}
    ).status_code == 400  # too long
    assert player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "12345"}
    ).status_code == 400  # not letters
    assert player_client.post(
        "/games/wordle/guess", json={"game_id": game["id"], "guess": "   "}
    ).status_code == 400  # empty


def test_an_unknown_field_on_the_payload_is_a_422(player_client: TestClient, db):
    game = seed_game(db)

    response = player_client.post(
        "/games/wordle/guess",
        json={"game_id": game["id"], "guess": "abaca", "cheat": True},
    )

    assert response.status_code == 422


def test_an_anonymous_caller_is_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/games/wordle").status_code == 401
    assert anonymous_client.post(
        "/games/wordle/guess", json={"game_id": "11111111-1111-1111-1111-111111111111", "guess": "abaca"}
    ).status_code == 401


def test_staff_cannot_play(admin_client: TestClient, moderator_client: TestClient):
    assert admin_client.get("/games/wordle").status_code == 403
    assert moderator_client.get("/games/wordle").status_code == 403
    assert admin_client.post(
        "/games/wordle/guess", json={"game_id": "11111111-1111-1111-1111-111111111111", "guess": "abaca"}
    ).status_code == 403


# --- concurrency ---------------------------------------------------------------


def test_concurrent_first_guesses_share_one_participation(db):
    """The core regression: simultaneous first guesses must yield one row, not two.

    Each request reads 'no participation yet' and each tries to insert. The unique pair is
    what settles the race: exactly one insert wins, and the loser re-reads the winner's
    row under lock and plays its guess against it -- so the day's puzzle is not two
    puzzles with two attempt budgets.
    """
    _insert_user(db, PLAYER_UUID, "wordleplayer", "player@user.com")
    game = seed_game(db, word=solution_words()[0])
    guesses = wrong_guesses(8, game["answer"])[:2]
    workers = 2
    barrier = threading.Barrier(workers)
    results = []
    lock = threading.Lock()

    def attempt(guess: str) -> None:
        barrier.wait(timeout=30)
        outcome = wordle_gameplay.submit_guess(
            db, PLAYER_UUID, WordleGuessRequest(game_id=game["id"], guess=guess)
        )
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt, word) for word in guesses]:
            future.result(timeout=60)

    # Both requests succeeded and both guesses landed on one row.
    assert len(results) == workers
    assert all(result.attempt_count <= workers for result in results)
    with db.sessions() as session:
        rows = session.scalars(
            select(WordleUserGameRecord).where(
                WordleUserGameRecord.wordle_game_id == game["id"],
                WordleUserGameRecord.user_id == PLAYER_UUID,
            )
        ).all()
    assert len(rows) == 1, "two requests must not create two participations"
    assert rows[0].attempt_count == workers
    assert set(rows[0].guesses) == set(guesses)
    assert rows[0].status == "in_progress"