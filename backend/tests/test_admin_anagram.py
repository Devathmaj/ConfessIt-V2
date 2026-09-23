"""What the Anagram configuration endpoints do, and what they refuse.

These tests are about *publishing a puzzle*, not playing one: there is no scramble, no
guess, no attempt and no result anywhere below, because none of those tables exist yet.
The properties worth pinning are the ones a careless implementation would get quietly
wrong:

**Custom and random obey different length rules.** A typed word is accepted at any length
-- ``cat`` and ``encyclopedia`` are both legal answers -- while the draw only ever returns
a word of at least five letters. One shared minimum would either reject legal custom
answers or let the draw hand out trivia, so the floor lives on the draw alone.

**The answer shown is the answer stored.** The preview draws on the server and signs the
word together with the date, and ``POST`` re-derives that signature. So an admin who is
shown a word and saves it publishes exactly that word -- and can draw again as often as
they like first -- while a word without a matching signature is refused rather than
re-drawn.

**One day, one answer, uppercase and stored.** Uniqueness is checked by the service for the
message and enforced by the schema for the race; every answer -- typed or drawn -- reaches
the column in exactly one spelling, uppercase, so ``elephant`` and ``ElePhAnT`` are one
puzzle rather than two.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.db_models import AnagramGameRecord
from app.limits import ANAGRAM_RANDOM_MIN_LENGTH
from app.main import app
from app.services import anagram_service
from app.services.anagram_service import anagram_words, draw_answer
from app.services.auth_service import get_current_user

WORDS_PATH = Path(__file__).resolve().parent.parent / "assets" / "anagram_words.txt"


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


def day(offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def save(client: TestClient, payload: dict):
    return client.post("/admin/anagram", json=payload)


def draw(client: TestClient, game_date: str | None = None):
    """Preview a draw, as the admin screen does before it lets anything be saved."""
    params = {"game_date": game_date} if game_date else {}
    response = client.get("/admin/anagram/preview", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# --- the word list -------------------------------------------------------------


def test_the_word_list_is_uppercase_letters_only_and_unique():
    """The committed file, cleaned on the way in rather than trusted: contractions and
    short entries are legal in a dictionary and illegal as an anagram answer."""
    words = anagram_words()

    assert len(words) > 1000
    assert all(word.isalpha() and word.isupper() for word in words)
    assert len(set(words)) == len(words)
    # Every loaded word is a spelling of a line in the file, so the pool cannot have
    # drifted to some other source.
    raw = {
        line.strip().upper()
        for line in WORDS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert set(words) <= raw


def test_a_missing_word_file_is_a_loud_error_not_a_fallback(monkeypatch):
    """No silent fall back to another dictionary: the draw either reads the committed file
    or fails with the reason attached."""
    monkeypatch.setattr(
        anagram_service, "_WORDS_PATH", WORDS_PATH.with_name("no_such_words.txt")
    )
    monkeypatch.setattr(anagram_service, "_anagram_words", None)

    with pytest.raises(RuntimeError) as excinfo:
        draw_answer()

    assert "no_such_words.txt" in str(excinfo.value)


# --- the draw ------------------------------------------------------------------


def test_the_draw_returns_a_word_of_at_least_five_letters_from_the_list():
    words = set(anagram_words())

    for _ in range(10):
        drawn = draw_answer()
        assert drawn.word in words
        assert drawn.word.isupper()
        assert len(drawn.word) >= ANAGRAM_RANDOM_MIN_LENGTH
        assert drawn.token


def test_repeated_draws_are_not_hard_coded_to_one_word():
    """The pool is filtered, not a single pre-selected entry: several draws land on
    several different words."""
    answers = {draw_answer().word for _ in range(10)}

    assert len(answers) > 1


# --- the preview ---------------------------------------------------------------


def test_the_preview_serves_a_word_from_the_eligible_pool(admin_client: TestClient):
    """The whole point of the preview: what is displayed is what gets published."""
    preview = draw(admin_client)

    assert preview["word"] in set(anagram_words())
    assert len(preview["word"]) >= ANAGRAM_RANDOM_MIN_LENGTH
    assert preview["token"]

    created = save(
        admin_client,
        {"mode": "random", "word": preview["word"], "token": preview["token"]},
    )
    assert created.status_code == 201, created.text
    assert created.json()["answer"] == preview["word"]
    assert created.json()["mode"] == "random"


def test_the_preview_signs_for_the_day_it_was_asked_for(admin_client: TestClient):
    """The signature covers the date, so changing the date field after drawing is refused
    rather than silently publishing a word for a day it was never chosen for."""
    preview = draw(admin_client, game_date=today())

    refused = save(
        admin_client,
        {
            "mode": "random",
            "word": preview["word"],
            "token": preview["token"],
            "game_date": day(1),
        },
    )

    assert refused.status_code == 400
    assert "does not match this date" in refused.json()["detail"]


def test_a_word_sent_with_a_foreign_signature_is_refused(admin_client: TestClient):
    """The token is an HMAC the client cannot forge, so a word of the caller's own
    choosing -- even a word that happens to be in the pool -- is refused rather than
    stored under ``mode="random"``."""
    preview = draw(admin_client)

    refused = save(
        admin_client, {"mode": "random", "word": "GARDEN", "token": preview["token"]}
    )

    assert refused.status_code == 400


def test_a_random_game_with_no_draw_is_refused(admin_client: TestClient):
    refused = save(admin_client, {"mode": "random"})

    assert refused.status_code == 400


# --- creating a puzzle: custom mode -------------------------------------------


def test_a_three_letter_custom_word_is_accepted(admin_client: TestClient):
    """Three letters is below the draw's floor and perfectly legal for a typed answer --
    which is the whole reason the two modes do not share a length rule."""
    response = save(admin_client, {"mode": "custom", "word": "cat"})

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer"] == "CAT"
    assert body["mode"] == "custom"
    assert body["game_date"] == today()
    assert body["id"]


def test_a_long_custom_word_is_accepted(admin_client: TestClient):
    response = save(admin_client, {"mode": "custom", "word": "encyclopedia"})

    assert response.status_code == 201, response.text
    assert response.json()["answer"] == "ENCYCLOPEDIA"


def test_a_custom_word_does_not_have_to_be_on_the_word_list(admin_client: TestClient):
    """The list is the *draw's* vocabulary. A typed answer is the administrator's choice,
    whether the file holds it or not."""
    response = save(admin_client, {"mode": "custom", "word": "xylophoner"})

    assert response.status_code == 201, response.text
    assert response.json()["answer"] == "XYLOPHONER"


def test_a_custom_word_is_normalized_to_uppercase(admin_client: TestClient):
    """One spelling of a word, whatever the admin typed -- and mixed case is refused as a
    *storage* question rather than preserved as three different answers."""
    response = save(admin_client, {"mode": "custom", "word": "  ElEpHaNt  "})

    assert response.status_code == 201, response.text
    assert response.json()["answer"] == "ELEPHANT"


@pytest.mark.parametrize(
    "word",
    [
        "",           # empty
        "   ",        # whitespace only
        "two words",  # a space
        "word2",      # a digit
        "don't",      # punctuation
        "word!",      # punctuation
    ],
)
def test_an_invalid_custom_word_is_refused(admin_client: TestClient, word):
    response = save(admin_client, {"mode": "custom", "word": word})

    assert response.status_code == 400


def test_a_custom_game_with_no_word_is_refused(admin_client: TestClient):
    response = save(admin_client, {"mode": "custom"})

    assert response.status_code == 400


def test_an_unrecognised_mode_is_a_validation_error(admin_client: TestClient):
    """A ``Literal`` on the payload, so an unknown mode is a 422 naming the field rather
    than a 400 from deeper in the service."""
    response = save(admin_client, {"mode": "daily", "word": "ELEPHANT"})

    assert response.status_code == 422


# --- creating a puzzle: random mode -------------------------------------------


def test_a_drawn_word_can_be_saved_after_redrawing(admin_client: TestClient):
    """Drawing twice publishes the second word: each preview is an offer, and only the
    offer sent back is stored."""
    draw(admin_client)  # discarded: an offer the admin did not take
    second = draw(admin_client)

    created = save(
        admin_client,
        {"mode": "random", "word": second["word"], "token": second["token"]},
    )

    assert created.status_code == 201, created.text
    assert created.json()["answer"] == second["word"]


def test_repeated_previews_draw_from_the_eligible_pool(admin_client: TestClient):
    """Several draws, several days, several words -- each one a legal member of the pool
    rather than one hard-coded entry."""
    answers = set()
    for offset in range(5):
        preview = draw(admin_client, game_date=day(offset))
        response = save(
            admin_client,
            {
                "mode": "random",
                "word": preview["word"],
                "token": preview["token"],
                "game_date": day(offset),
            },
        )
        assert response.status_code == 201, response.text
        answers.add(response.json()["answer"])

    words = set(anagram_words())
    assert answers <= words
    assert all(len(answer) >= ANAGRAM_RANDOM_MIN_LENGTH for answer in answers)
    assert len(answers) > 1


# --- persistence ---------------------------------------------------------------


def test_the_answer_and_mode_are_persisted(admin_client: TestClient, db):
    created = save(
        admin_client,
        {"mode": "custom", "word": "garden", "game_date": day(1)},
    )
    assert created.status_code == 201, created.text

    with db.sessions() as session:
        row = session.get(AnagramGameRecord, created.json()["id"])

    assert row is not None
    assert row.answer == "GARDEN"
    assert row.mode == "custom"
    assert row.game_date.isoformat() == day(1)
    assert row.created_at is not None
    assert row.updated_at is not None


def test_the_creator_is_recorded_from_the_caller_not_the_payload(admin_client: TestClient):
    """Attribution comes from the authenticated account, so a puzzle cannot be published
    in somebody else's name."""
    response = save(admin_client, {"mode": "custom", "word": "garden"})

    assert response.status_code == 201, response.text
    assert response.json()["created_by"] == "adminuser"


def test_a_payload_cannot_choose_its_own_creator(admin_client: TestClient):
    """``extra="forbid"``, so a key the payload does not carry is refused outright rather
    than accepted and dropped."""
    response = save(
        admin_client, {"mode": "custom", "word": "garden", "created_by": "someone-else"}
    )

    assert response.status_code == 422


# --- one day, one answer -------------------------------------------------------


def test_a_second_puzzle_for_the_same_day_is_refused(admin_client: TestClient):
    first = save(admin_client, {"mode": "custom", "word": "garden"})
    assert first.status_code == 201, first.text

    second = save(admin_client, {"mode": "custom", "word": "elephant"})

    assert second.status_code == 409
    listing = admin_client.get("/admin/anagram")
    assert len(listing.json()) == 1


def test_a_drawn_puzzle_cannot_replace_a_custom_one_for_the_same_day(
    admin_client: TestClient,
):
    first = save(admin_client, {"mode": "custom", "word": "garden"})
    assert first.status_code == 201, first.text

    preview = draw(admin_client)
    refused = save(
        admin_client,
        {"mode": "random", "word": preview["word"], "token": preview["token"]},
    )

    assert refused.status_code == 409


def test_the_schema_refuses_a_second_row_for_one_day(db):
    """The service's 409 is a message; this is the rule. Written straight to the table to
    prove the constraint holds for a caller that never went through the service -- which is
    the race the uniqueness check exists to close."""
    day_value = datetime.now(timezone.utc).date()
    with db.sessions.begin() as session:
        session.add(
            AnagramGameRecord(
                game_date=day_value, mode="custom", answer="GARDEN", created_by="adminuser"
            )
        )

    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                AnagramGameRecord(
                    game_date=day_value, mode="custom", answer="ELEPHANT", created_by="adminuser"
                )
            )


def test_the_schema_refuses_a_mode_outside_the_two(db):
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                AnagramGameRecord(
                    game_date=datetime.now(timezone.utc).date(),
                    mode="daily",
                    answer="GARDEN",
                    created_by="adminuser",
                )
            )


def test_the_schema_refuses_an_empty_answer(db):
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                AnagramGameRecord(
                    game_date=datetime.now(timezone.utc).date(),
                    mode="custom",
                    answer="",
                    created_by="adminuser",
                )
            )


# --- reading back --------------------------------------------------------------


def test_puzzles_are_listed_newest_first_and_readable_by_id(admin_client: TestClient):
    earlier = day(-2)
    later = day(-1)

    created = save(
        admin_client, {"mode": "custom", "word": "garden", "game_date": earlier}
    )
    save(admin_client, {"mode": "custom", "word": "elephant", "game_date": later})

    listing = admin_client.get("/admin/anagram")
    assert listing.status_code == 200
    body = listing.json()
    assert [entry["game_date"] for entry in body] == [later, earlier]
    # The answer and the creator come back with the row: seeing what will be published, and
    # by whom, is the screen's job.
    assert body[1]["answer"] == "GARDEN"
    assert body[1]["created_by"] == "adminuser"

    detail = admin_client.get(f"/admin/anagram/{created.json()['id']}")
    assert detail.status_code == 200
    assert detail.json() == created.json()


def test_an_unknown_id_is_404_and_a_malformed_one_is_400(admin_client: TestClient):
    missing = admin_client.get("/admin/anagram/11111111-1111-1111-1111-111111111111")
    assert missing.status_code == 404

    malformed = admin_client.get("/admin/anagram/not-an-id")
    assert malformed.status_code == 400


def test_a_day_can_be_named_in_advance(admin_client: TestClient):
    """``game_date`` is optional because the usual save is today's puzzle, not because
    scheduling is forbidden -- and the preview is asked for the same day it saves to."""
    preview = draw(admin_client, game_date=day(3))
    response = save(
        admin_client,
        {
            "mode": "random",
            "word": preview["word"],
            "token": preview["token"],
            "game_date": day(3),
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["game_date"] == day(3)


# --- who may configure a puzzle ------------------------------------------------


def test_a_moderator_may_not_configure_or_read(moderator_client: TestClient):
    """The capability, not the account table: a moderator holds every moderation privilege
    and still may not publish, because publishing is not a ruling on a queue."""
    assert moderator_client.get("/admin/anagram").status_code == 403
    assert moderator_client.get("/admin/anagram/preview").status_code == 403
    assert save(moderator_client, {"mode": "custom", "word": "garden"}).status_code == 403
    assert (
        moderator_client.get(
            "/admin/anagram/11111111-1111-1111-1111-111111111111"
        ).status_code
        == 403
    )


def test_a_normal_account_may_not_configure_a_puzzle(client: TestClient):
    assert client.get("/admin/anagram").status_code == 403
    assert save(client, {"mode": "custom", "word": "garden"}).status_code == 403


def test_an_anonymous_caller_is_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/admin/anagram").status_code == 401
    assert anonymous_client.get("/admin/anagram/preview").status_code == 401
    assert save(anonymous_client, {"mode": "custom", "word": "garden"}).status_code == 401
    assert (
        anonymous_client.get("/admin/anagram/11111111-1111-1111-1111-111111111111").status_code
        == 401
    )
