"""A confession's settings are booleans, and null is not one of them.

``ConfessionUpdate`` typed both settings as ``Optional[bool]``, which made ``null`` a
legal *value* for a field that has no third state. The service then read it as one of
the two booleans rather than as a mistake, and the two booleans are opposites:

* ``comments_enabled: null`` was written into a NOT NULL column, so the request
  answered **500** -- a server fault for a payload that was simply wrong;
* ``anonymous: null`` was read as "not anonymous", because ``not None`` is ``True``.
  A post somebody chose to publish anonymously was de-anonymised by a request that
  changed nothing they asked for.

There is no safe default to substitute, which is why the null is refused rather than
coerced: the two defaults are opposites, and one of them is a privacy decision the
participant made.

Leaving the field out is a different thing and stays allowed -- that is how a patch
says "I am not touching this", and the service reads ``exclude_unset``, so an absent
field is never written.

The bundled UI never sends null, so this is a guard on the API rather than a fix to a
visible symptom; the point is that a non-UI client gets a 422 naming the field instead
of a 500 or a de-anonymised post.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text

from app.db_models import ConfessionRecord, UserRecord
from app.main import app
from app.models import ConfessionUpdate
from app.services.confession_service import ConfessionService
from app.services.auth_service import get_current_user
from datetime import datetime, timezone

REACTIONS = {"heart": [], "haha": [], "whoa": [], "heartbreak": []}


def make_user(db, regno):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_confession(db, author_id, *, anonymous=True, comments_enabled=True):
    with db.sessions.begin() as session:
        row = ConfessionRecord(
            body="a confession",
            recipient_name="Someone In Particular",
            anonymous=anonymous,
            comments_enabled=comments_enabled,
            created_at=datetime.now(timezone.utc),
            author_id=author_id,
            reactions=REACTIONS,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def confession_row(db, confession_id):
    with db.sessions() as session:
        return session.get(ConfessionRecord, confession_id)


@pytest.fixture
def author_client(db):
    """A client whose participant owns the confession under test."""
    author_id = "44444444-4444-4444-4444-444444444444"
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, 'authoruser', 'Author', 'author@user.com', 2, 'Class', 'other') "
                "ON CONFLICT (regno) DO NOTHING"
            ),
            {"id": author_id},
        )
    from app.accounts import AccountType
    from app.models import UserDetails

    author = UserDetails(
        id=author_id, regno="authoruser", name="Author", email="author@user.com",
        year=2, department="Class", gender="other", account_type=AccountType.USERS.value,
    )
    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: author
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = previous


# ------------------------------------------------------------------ the model


def test_a_field_left_out_is_accepted():
    """A patch that names nothing changes nothing."""
    update = ConfessionUpdate()
    assert update.model_dump(exclude_unset=True) == {}


def test_a_boolean_is_accepted():
    update = ConfessionUpdate(comments_enabled=False, anonymous=True)
    assert update.model_dump(exclude_unset=True) == {
        "comments_enabled": False, "anonymous": True,
    }


def test_comments_enabled_null_is_refused():
    with pytest.raises(ValidationError) as excinfo:
        ConfessionUpdate(comments_enabled=None)
    assert "comments_enabled" in str(excinfo.value)


def test_anonymous_null_is_refused():
    """The one that mattered: null is not "not anonymous"."""
    with pytest.raises(ValidationError) as excinfo:
        ConfessionUpdate(anonymous=None)
    assert "anonymous" in str(excinfo.value)


def test_both_null_at_once_are_refused():
    with pytest.raises(ValidationError):
        ConfessionUpdate(anonymous=None, comments_enabled=None)


def test_one_null_among_two_valid_values_is_refused():
    """The whole payload is refused, not just the offending field.

    A partial acceptance would leave the client guessing which half of its request was
    understood.
    """
    with pytest.raises(ValidationError):
        ConfessionUpdate(comments_enabled=True, anonymous=None)


# ------------------------------------------------------------------ the route


def test_a_null_payload_answers_422(db, author_client):
    """A 422 naming the field, rather than a 500 from the database."""
    confession_id = make_confession(db, "44444444-4444-4444-4444-444444444444")

    response = author_client.put(
        f"/confessions/{confession_id}", json={"comments_enabled": None}
    )

    assert response.status_code == 422, response.text
    assert "comments_enabled" in response.text


def test_a_null_anonymous_payload_answers_422(db, author_client):
    confession_id = make_confession(db, "44444444-4444-4444-4444-444444444444")

    response = author_client.put(f"/confessions/{confession_id}", json={"anonymous": None})

    assert response.status_code == 422, response.text
    assert "anonymous" in response.text


def test_an_empty_payload_is_accepted(db, author_client):
    """Naming nothing is still a valid patch."""
    confession_id = make_confession(db, "44444444-4444-4444-4444-444444444444")

    response = author_client.put(f"/confessions/{confession_id}", json={})

    assert response.status_code == 200, response.text


def test_a_real_boolean_still_works(db, author_client):
    confession_id = make_confession(db, "44444444-4444-4444-4444-444444444444")

    response = author_client.put(
        f"/confessions/{confession_id}", json={"comments_enabled": False}
    )

    assert response.status_code == 200, response.text
    assert confession_row(db, confession_id).comments_enabled is False


# ------------------------------------------------------------------ the service


def test_a_null_does_not_de_anonymise_a_post(db):
    """The consequence that mattered, pinned at the level it happened.

    The service is called with a model, so it can no longer be handed a null by a
    route; this asserts the end of the chain anyway, because "the model refuses it"
    and "the post stays anonymous" are two different claims and only the second one is
    the promise.
    """
    author_id = make_user(db, "author")
    confession_id = make_confession(db, author_id, anonymous=True)

    service = ConfessionService()
    # The model refuses to be built with a null, so the only way a null can reach the
    # service is a payload that was never validated -- which is exactly what the
    # route used to hand it.
    with pytest.raises(ValidationError):
        ConfessionUpdate(anonymous=None)

    service.update_confession(confession_id, ConfessionUpdate(), author_id)

    row = confession_row(db, confession_id)
    assert row.anonymous is True
    assert row.comments_enabled is True
