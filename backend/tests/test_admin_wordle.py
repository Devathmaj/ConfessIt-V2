"""What the Wordle configuration endpoints do, and what they refuse.

These tests are about *publishing a puzzle*, not playing one: there is no guess, no attempt
and no result anywhere below, because none of those exist in the product yet. The properties
worth pinning are the ones a careless implementation would get quietly wrong:

**The answer shown is the answer stored.** The preview draws on the server and signs the word
together with the date, and ``POST`` re-derives that signature. So an admin who is shown a
word cannot be given a different one on save, and -- the direction that matters more -- a
client cannot supply an answer of its own under ``mode="random"``, because a word without a
matching signature is refused rather than re-drawn.

**One day, one answer.** Uniqueness is checked by the service for the message and enforced by
the schema for the race; the last two tests below prove both halves by writing past the
service and letting the database refuse.

**The word list is a closed set.** Membership is checked on every write, so a word that is
five letters but not a solution never reaches the column, and a word that is on the list is
stored lowercase whatever case it arrived in.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db_models import WordleGameRecord
from app.main import app
from app.services.auth_service import get_current_user
from app.services.wordle_service import solution_words


@pytest.fixture
def anonymous_client():
    """A client with the auth dependency override removed, to exercise the real 401 path."""
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def draw(client: TestClient, game_date: str | None = None) -> dict:
    """Preview a draw, as the admin screen does before it lets anything be saved."""
    params = {} if game_date is None else {"game_date": game_date}
    response = client.get("/admin/wordle/preview", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def save(client: TestClient, payload: dict):
    return client.post("/admin/wordle", json=payload)


# --- the approved list ---------------------------------------------------------


def test_the_approved_list_is_five_lowercase_letters_and_unique():
    """The dataset itself, loaded from the committed file rather than trusted from it."""
    words = solution_words()

    assert len(words) > 1000
    assert all(len(word) == 5 and word.isalpha() and word.islower() for word in words)
    assert len(set(words)) == len(words)


# --- creating a puzzle ---------------------------------------------------------


def test_a_custom_word_is_stored_for_today(admin_client: TestClient):
    word = solution_words()[0]

    response = save(admin_client, {"mode": "custom", "word": word})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer"] == word
    assert body["mode"] == "custom"
    assert body["game_date"] == today()
    assert body["id"]


def test_a_custom_word_is_normalized_to_lowercase(admin_client: TestClient):
    """The column holds one spelling of a word, whatever the admin typed."""
    word = solution_words()[1]

    response = save(admin_client, {"mode": "custom", "word": word.upper()})

    assert response.status_code == 201, response.text
    assert response.json()["answer"] == word


def test_a_day_can_be_named_in_advance(admin_client: TestClient):
    """Future manual words are scheduled, bounded by the finite horizon."""
    upcoming = (datetime.now(timezone.utc).date() + timedelta(days=3)).isoformat()

    response = admin_client.post(
        "/admin/wordle/schedule",
        json={"target_date": upcoming, "word": solution_words()[2]},
    )

    assert response.status_code == 201, response.text
    assert response.json()["target_date"] == upcoming


# --- the draw ------------------------------------------------------------------


def test_the_word_the_admin_is_shown_is_the_word_that_is_stored(admin_client: TestClient):
    """The whole point of the preview: what is displayed is what gets published."""
    preview = draw(admin_client)

    response = save(
        admin_client,
        {"mode": "random", "word": preview["word"], "token": preview["token"]},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer"] == preview["word"]
    assert body["mode"] == "random"


def test_a_random_game_without_a_draw_is_refused(admin_client: TestClient):
    """No re-draw at save time: a word the server did not sign for this day is not an
    answer, and answering 201 with a different word would publish something nobody saw."""
    response = save(admin_client, {"mode": "random", "word": solution_words()[0]})

    assert response.status_code == 400


def test_a_draw_cannot_be_spent_on_another_day(admin_client: TestClient):
    """The signature covers the date, so changing the date field after drawing is refused
    rather than quietly publishing a word chosen for a different puzzle."""
    preview = draw(admin_client, game_date=today())
    tomorrow = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

    response = save(
        admin_client,
        {"mode": "random", "word": preview["word"], "token": preview["token"], "game_date": tomorrow},
    )

    assert response.status_code == 400


def test_the_preview_serves_a_word_from_the_approved_list(admin_client: TestClient):
    preview = draw(admin_client)

    assert preview["word"] in solution_words()
    assert preview["token"]


# --- refusal of a word that is not on the list --------------------------------


def test_a_word_off_the_list_is_refused(admin_client: TestClient):
    response = save(admin_client, {"mode": "custom", "word": "zzzzz"})

    assert response.status_code == 400
    assert "approved list" in response.json()["detail"]


def test_an_answer_of_the_wrong_length_is_refused(admin_client: TestClient):
    response = save(admin_client, {"mode": "custom", "word": "abc"})

    assert response.status_code == 400
    assert "5 letters" in response.json()["detail"]


def test_a_custom_game_with_no_word_is_refused(admin_client: TestClient):
    response = save(admin_client, {"mode": "custom"})

    assert response.status_code == 400


def test_an_unrecognised_mode_is_a_validation_error(admin_client: TestClient):
    """A ``Literal`` on the payload, so an unknown mode is a 422 naming the field rather
    than a 400 from deeper in the service."""
    response = save(admin_client, {"mode": "daily", "word": solution_words()[0]})

    assert response.status_code == 422


# --- one day, one answer -------------------------------------------------------


def test_an_update_ends_the_current_session_and_opens_a_new_one(admin_client: TestClient, db):
    """"Update" is terminate-then-create: the old row survives as ended history."""
    from sqlalchemy import select

    first = save(admin_client, {"mode": "custom", "word": solution_words()[0]})
    assert first.status_code == 201, first.text

    second = save(admin_client, {"mode": "custom", "word": solution_words()[1]})

    assert second.status_code == 201, second.text
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["answer"] == solution_words()[1]
    with db.sessions() as session:
        rows = session.scalars(select(WordleGameRecord)).all()
        assert len(rows) == 2
        by_id = {str(row.id): row for row in rows}
        assert by_id[first.json()["id"]].status == "ended"
        assert by_id[second.json()["id"]].status == "active"


def test_the_schema_refuses_two_simultaneously_active_sessions(db):
    """The single-active rule lives in a partial unique index, not in game_date.

    Two ended rows may share a day (that is the history an Update keeps); two
    active rows may not, which is what serializes a scheduler run racing an
    admin Update.
    """
    day = datetime.now(timezone.utc).date()
    with db.sessions.begin() as session:
        session.add(
            WordleGameRecord(
                game_date=day, mode="custom", answer="cigar", status="ended"
            )
        )
        session.add(
            WordleGameRecord(
                game_date=day, mode="custom", answer="rebut", status="ended"
            )
        )

    with db.sessions.begin() as session:
        session.add(
            WordleGameRecord(
                game_date=day, mode="custom", answer="cigar", status="active"
            )
        )

    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                WordleGameRecord(
                    game_date=day, mode="custom", answer="rebut", status="active"
                )
            )


def test_the_schema_refuses_an_answer_that_is_not_five_letters(db):
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                WordleGameRecord(
                    game_date=datetime.now(timezone.utc).date(), mode="custom", answer="abc"
                )
            )


# --- reading back --------------------------------------------------------------


def test_puzzles_are_listed_newest_first_and_readable_by_id(admin_client: TestClient):
    created = save(admin_client, {"mode": "custom", "word": solution_words()[0]})
    assert created.status_code == 201, created.text
    updated = save(admin_client, {"mode": "custom", "word": solution_words()[1]})
    assert updated.status_code == 201, updated.text

    listing = admin_client.get("/admin/wordle")
    assert listing.status_code == 200
    body = listing.json()
    assert [entry["id"] for entry in body] == [updated.json()["id"], created.json()["id"]]
    # The answer comes back with the row: seeing what will be published is the screen's job.
    assert body[1]["answer"] == solution_words()[0]
    # History carries its lifecycle: the first session ended when it was replaced.
    assert body[1]["status"] == "ended"
    assert body[0]["status"] == "active"

    detail = admin_client.get(f"/admin/wordle/{created.json()['id']}")
    assert detail.status_code == 200
    assert detail.json()["id"] == created.json()["id"]


def test_an_unknown_id_is_404_and_a_malformed_one_is_400(admin_client: TestClient):
    missing = admin_client.get("/admin/wordle/11111111-1111-1111-1111-111111111111")
    assert missing.status_code == 404

    malformed = admin_client.get("/admin/wordle/not-an-id")
    assert malformed.status_code == 400


# --- who may configure a puzzle ------------------------------------------------


def test_a_moderator_may_not_configure_or_read(moderator_client: TestClient):
    """The capability, not the account table: a moderator holds every moderation privilege
    and still may not publish, because publishing is not a ruling on a queue."""
    assert moderator_client.get("/admin/wordle").status_code == 403
    assert moderator_client.get("/admin/wordle/preview").status_code == 403
    assert save(moderator_client, {"mode": "custom", "word": solution_words()[0]}).status_code == 403
    assert (
        moderator_client.get(
            "/admin/wordle/11111111-1111-1111-1111-111111111111"
        ).status_code
        == 403
    )


def test_a_normal_account_may_not_configure_a_puzzle(client: TestClient):
    assert client.get("/admin/wordle").status_code == 403
    assert save(client, {"mode": "custom", "word": solution_words()[0]}).status_code == 403


def test_an_anonymous_caller_is_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/admin/wordle").status_code == 401
    assert anonymous_client.get("/admin/wordle/preview").status_code == 401
    assert (
        save(anonymous_client, {"mode": "custom", "word": solution_words()[0]}).status_code
        == 401
    )
