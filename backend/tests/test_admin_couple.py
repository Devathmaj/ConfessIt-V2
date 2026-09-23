"""What the Couple Prediction configuration endpoints do, and what they refuse.

These tests are about *featuring a day*, not playing a game: there is no mode, no
word, no answer and no per-day content anywhere below, because the 50-question
bank is shared application content seeded at startup rather than input an
administrator supplies. The properties worth pinning are the ones a careless
implementation would get quietly wrong:

**Featuring stores the day and nothing else.** The payload carries no mode, word
or token -- ``extra="forbid"`` refuses any of them -- and the row holds the
date, the creator and the timestamps.

**One day, one featured instance.** Uniqueness is checked by the service for the
message and enforced by the schema for the race.

**The bank is seeded, shared and idempotent.** Fifty templates naming ``X``
with four options each exist after the seed, a second seed inserts nothing, and
the formatted question substitutes the name at presentation time without touching
the stored template.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db_models import CoupleGameQuestionRecord, CoupleGameRecord
from app.limits import COUPLE_OPTIONS_PER_QUESTION, COUPLE_QUESTIONS_COUNT
from app.main import app
from app.services.auth_service import get_current_user
from app.services.couple_service import (
    COUPLE_QUESTION_BANK,
    format_couple_question,
    list_couple_questions,
    seed_couple_questions,
)


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
    return client.post("/admin/couple", json=payload)


# --- the question bank ---------------------------------------------------------


def test_the_bank_holds_fifty_templates_with_four_options_each():
    assert len(COUPLE_QUESTION_BANK) == COUPLE_QUESTIONS_COUNT
    for question_text, options in COUPLE_QUESTION_BANK:
        assert "X" in question_text
        assert len(options) == COUPLE_OPTIONS_PER_QUESTION


def test_the_seed_inserts_the_bank_and_is_idempotent(db):
    first = seed_couple_questions(db)
    assert first == COUPLE_QUESTIONS_COUNT

    questions = list_couple_questions(db)
    assert len(questions) == COUPLE_QUESTIONS_COUNT
    assert [question.sequence_number for question in questions] == list(
        range(1, COUPLE_QUESTIONS_COUNT + 1)
    )
    assert [question.stable_key for question in questions] == [
        f"couple-q-{index:02d}" for index in range(1, COUPLE_QUESTIONS_COUNT + 1)
    ]
    for question in questions:
        assert "X" in question.question_text
        assert len(question.options) == COUPLE_OPTIONS_PER_QUESTION

    # Restarting the application inserts nothing and overwrites nothing.
    assert seed_couple_questions(db) == 0
    assert len(list_couple_questions(db)) == COUPLE_QUESTIONS_COUNT


def test_the_seed_leaves_existing_rows_alone(db):
    seed_couple_questions(db)
    with db.sessions() as session:
        before = {
            row.stable_key: row.question_text
            for row in session.scalars(select(CoupleGameQuestionRecord)).all()
        }

    assert seed_couple_questions(db) == 0

    with db.sessions() as session:
        after = {
            row.stable_key: row.question_text
            for row in session.scalars(select(CoupleGameQuestionRecord)).all()
        }
    assert before == after


def test_the_schema_refuses_a_template_without_the_placeholder(db):
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                CoupleGameQuestionRecord(
                    stable_key="couple-q-99",
                    sequence_number=99,
                    question_text="What would you prefer for a perfect date?",
                    option_a="A",
                    option_b="B",
                    option_c="C",
                    option_d="D",
                )
            )


def test_the_name_is_substituted_at_presentation_time_not_in_storage(db):
    seed_couple_questions(db)
    stored = list_couple_questions(db)[0].question_text

    assert format_couple_question(stored, "Alex") == stored.replace("X", "Alex")
    assert "Alex" not in list_couple_questions(db)[0].question_text
    # Both directions read through the same substitution.
    assert "Sam" in format_couple_question(stored, "Sam")


# --- featuring a day -----------------------------------------------------------


def test_featuring_a_day_stores_the_day_and_the_creator(admin_client: TestClient, db):
    created = save(admin_client, {"game_date": day(1)})
    assert created.status_code == 201, created.text

    with db.sessions() as session:
        row = session.get(CoupleGameRecord, created.json()["id"])

    assert row is not None
    assert row.game_date.isoformat() == day(1)
    assert row.created_by == "adminuser"
    assert row.created_at is not None
    assert row.updated_at is not None


def test_the_date_defaults_to_today(admin_client: TestClient):
    response = save(admin_client, {})

    assert response.status_code == 201, response.text
    assert response.json()["game_date"] == today()


def test_a_payload_cannot_carry_puzzle_fields(admin_client: TestClient):
    """No mode, word, answer or token: ``extra="forbid"`` refuses anything but the date."""
    assert save(admin_client, {"game_date": day(1), "mode": "custom"}).status_code == 422
    assert save(admin_client, {"game_date": day(1), "word": "garden"}).status_code == 422
    assert save(admin_client, {"game_date": day(1), "created_by": "someone-else"}).status_code == 422


def test_a_second_feature_for_the_same_day_is_refused(admin_client: TestClient):
    first = save(admin_client, {})
    assert first.status_code == 201, first.text

    second = save(admin_client, {})

    assert second.status_code == 409
    listing = admin_client.get("/admin/couple")
    assert len(listing.json()) == 1


def test_the_schema_refuses_a_second_row_for_one_day(db):
    """The service's 409 is a message; this is the rule. Written straight to the table to
    prove the constraint holds for a caller that never went through the service."""
    day_value = datetime.now(timezone.utc).date()
    with db.sessions.begin() as session:
        session.add(CoupleGameRecord(game_date=day_value, created_by="adminuser"))

    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(CoupleGameRecord(game_date=day_value, created_by="adminuser"))


# --- reading back --------------------------------------------------------------


def test_featured_days_are_listed_newest_first_and_readable_by_id(admin_client: TestClient):
    earlier = day(-2)
    later = day(-1)

    created = save(admin_client, {"game_date": earlier})
    assert created.status_code == 201, created.text
    save(admin_client, {"game_date": later})

    listing = admin_client.get("/admin/couple")
    assert listing.status_code == 200
    body = listing.json()
    assert [entry["game_date"] for entry in body] == [later, earlier]
    assert body[1]["created_by"] == "adminuser"

    detail = admin_client.get(f"/admin/couple/{created.json()['id']}")
    assert detail.status_code == 200
    assert detail.json() == created.json()


def test_an_unknown_id_is_404_and_a_malformed_one_is_400(admin_client: TestClient):
    missing = admin_client.get("/admin/couple/11111111-1111-1111-1111-111111111111")
    assert missing.status_code == 404

    malformed = admin_client.get("/admin/couple/not-an-id")
    assert malformed.status_code == 400


def test_the_game_is_not_exposed_on_the_player_side(client: TestClient):
    """No participation surface exists yet: the user-facing game list gains no route,
    so a player guessing at one meets a 404 rather than a half-built game."""
    assert client.get("/games/couple").status_code == 404


# --- who may feature a day -----------------------------------------------------


def test_a_moderator_may_not_feature_or_read(moderator_client: TestClient):
    assert moderator_client.get("/admin/couple").status_code == 403
    assert save(moderator_client, {}).status_code == 403
    assert (
        moderator_client.get("/admin/couple/11111111-1111-1111-1111-111111111111").status_code
        == 403
    )


def test_a_normal_account_may_not_feature_a_day(client: TestClient):
    assert client.get("/admin/couple").status_code == 403
    assert save(client, {}).status_code == 403


def test_an_anonymous_caller_is_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/admin/couple").status_code == 401
    assert save(anonymous_client, {}).status_code == 401
    assert (
        anonymous_client.get("/admin/couple/11111111-1111-1111-1111-111111111111").status_code
        == 401
    )
