"""Length limits for user-supplied text.

Two layers are asserted here, because either one alone leaves a hole:

* the Pydantic cap, which turns an over-long body into a 422 naming the field instead of
  letting it reach the database, and
* the CHECK constraint, which is the actual invariant for writes that never pass through
  an API model at all.
"""
import warnings

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db_models import UserRecord
from app.accounts import AccountType
from app.limits import (
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    MESSAGE_BODY_MAX,
    RECIPIENT_NAME_MAX,
    REPORT_REASON_MAX,
)
from app.models import CommentCreate, ConfessionCreate, ReportCreate
from app.routers.messages import MessageCreate, MessageReport

warnings.simplefilter("ignore")

AT_LIMIT = "x" * 10


def _confession(body, recipient_name="Sam"):
    return ConfessionCreate(
        body=body, anonymous=False, comments_enabled=False, recipient_name=recipient_name
    )


def _string_cap(schema):
    """The maxLength of a field, however the schema wraps it.

    A required ``str`` publishes ``maxLength`` directly. An optional one publishes it inside
    ``anyOf`` next to a ``null`` branch, because the value can be absent as well as long.
    Reading the cap off the field directly would break the moment a field became optional,
    which is the wrong moment to discover the limit stopped being discoverable.
    """
    if "maxLength" in schema:
        return schema["maxLength"]
    for branch in schema.get("anyOf", []):
        if "maxLength" in branch:
            return branch["maxLength"]
    raise AssertionError(f"no string cap published: {schema}")


def _admin_user(**overrides):
    payload = {
        "regno": "r1",
        "name": "Name",
        "email": "admin-create@user.com",
        "class_name": "Class",
        "gender": "other",
    }
    payload.update(overrides)
    return payload


def _insert_user(connection, regno):
    """Insert the minimum a confession needs to satisfy its author foreign key."""
    return connection.execute(
        text(
            "INSERT INTO users (id, regno, name, email, class_name, gender) "
            "VALUES (gen_random_uuid(), :regno, 'Owner', :email, 'Class', 'other')"
        ),
        {"regno": regno, "email": f"{regno}@user.com"},
    )


# --------------------------------------------------------------------------- models


@pytest.mark.parametrize(
    "factory, limit",
    [
        (lambda n: _confession("x" * n), CONFESSION_BODY_MAX),
        (lambda n: _confession("b", recipient_name="x" * n), RECIPIENT_NAME_MAX),
        (lambda n: CommentCreate(body="x" * n), COMMENT_BODY_MAX),
        (lambda n: ReportCreate(reason="x" * n), REPORT_REASON_MAX),
        (lambda n: MessageCreate(conversation_id="c", body="x" * n), MESSAGE_BODY_MAX),
        (lambda n: MessageReport(message_id="m", reason="x" * n), REPORT_REASON_MAX),
    ],
)
def test_text_field_accepts_exactly_the_limit(factory, limit):
    assert factory(limit)


@pytest.mark.parametrize(
    "factory, limit",
    [
        (lambda n: _confession("x" * n), CONFESSION_BODY_MAX),
        (lambda n: _confession("b", recipient_name="x" * n), RECIPIENT_NAME_MAX),
        (lambda n: CommentCreate(body="x" * n), COMMENT_BODY_MAX),
        (lambda n: ReportCreate(reason="x" * n), REPORT_REASON_MAX),
        (lambda n: MessageCreate(conversation_id="c", body="x" * n), MESSAGE_BODY_MAX),
        (lambda n: MessageReport(message_id="m", reason="x" * n), REPORT_REASON_MAX),
    ],
)
def test_text_field_rejects_one_past_the_limit(factory, limit):
    with pytest.raises(ValidationError) as excinfo:
        factory(limit + 1)
    assert excinfo.value.errors()[0]["type"] == "string_too_long"


def test_admin_user_keeps_its_account_type_default():
    """The cap must not turn an optional field into a required one."""
    from app.models import AdminUserCreate

    assert AdminUserCreate(**_admin_user()).account_type == AccountType.USERS


def test_confession_recipient_is_optional_and_normalized_to_none():
    """A confession is not always about someone.

    Three spellings of "no recipient" have to land the same way, because the client can send
    any of them: the field absent, null, or a string of whitespace. All three store NULL
    rather than an empty string, so a later filter on "has a recipient" has one condition
    instead of two.
    """
    absent = ConfessionCreate(body="just venting", anonymous=False, comments_enabled=True)
    nulled = ConfessionCreate(
        body="just venting", anonymous=False, comments_enabled=True, recipient_name=None
    )
    blank = _confession("just venting", recipient_name="   ")

    assert absent.recipient_name is None
    assert nulled.recipient_name is None
    assert blank.recipient_name is None

    named = _confession("for you", recipient_name="  Sam  ")
    assert named.recipient_name == "Sam"


@pytest.mark.parametrize(
    "field, limit",
    [
        ("name", 160),
        ("regno", 100),
        ("class_name", 80),
        ("gender", 24),
        ("bio", 500),
        ("emoji", 16),
    ],
)
def test_admin_user_fields_are_capped(field, limit):
    from app.models import AdminUserCreate

    AdminUserCreate(**_admin_user(**{field: "x" * limit}))
    with pytest.raises(ValidationError):
        AdminUserCreate(**_admin_user(**{field: "x" * (limit + 1)}))


# ------------------------------------------------------------------------- openapi


def test_openapi_publishes_the_limits(client: TestClient):
    """Clients should be able to discover the limits from the schema, not guess them."""
    schema = client.get("/openapi.json").json()
    components = schema["components"]["schemas"]

    confession = components["ConfessionCreate"]["properties"]
    assert confession["body"]["maxLength"] == CONFESSION_BODY_MAX
    assert _string_cap(confession["recipient_name"]) == RECIPIENT_NAME_MAX
    assert components["CommentCreate"]["properties"]["body"]["maxLength"] == COMMENT_BODY_MAX
    assert components["ReportCreate"]["properties"]["reason"]["maxLength"] == REPORT_REASON_MAX
    assert components["MessageCreate"]["properties"]["body"]["maxLength"] == MESSAGE_BODY_MAX
    assert components["MessageReport"]["properties"]["reason"]["maxLength"] == REPORT_REASON_MAX


# ----------------------------------------------------------------------------- api


@pytest.mark.parametrize(
    "path, payload",
    [
        (
            "/confessions",
            {
                "body": "x" * (CONFESSION_BODY_MAX + 1),
                "anonymous": False,
                "comments_enabled": False,
                "recipient_name": "Sam",
            },
        ),
        ("/confessions/1/comment", {"body": "x" * (COMMENT_BODY_MAX + 1)}),
        ("/confessions/1/report", {"reason": "x" * (REPORT_REASON_MAX + 1)}),
        ("/messages/send", {"conversation_id": "c", "body": "x" * (MESSAGE_BODY_MAX + 1)}),
    ],
)
def test_api_rejects_over_long_text_with_422(client: TestClient, path, payload):
    """Before the caps these reached the database and surfaced as an unhandled 500."""
    response = client.post(path, json=payload)
    assert response.status_code == 422, response.text
    assert "detail" in response.json()


# ------------------------------------------------------------------- database backstop


def test_length_constraints_are_installed(db):
    with db.engine.begin() as connection:
        installed = {
            (row[0], row[1])
            for row in connection.execute(
                text(
                    "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                    "WHERE conname IN ("
                    "  'ck_confessions_body_length',"
                    "  'ck_confession_comments_body_length',"
                    "  'ck_messages_body_length')"
                )
            )
        }
    assert installed == {
        ("confessions", "ck_confessions_body_length"),
        ("confession_comments", "ck_confession_comments_body_length"),
        ("messages", "ck_messages_body_length"),
    }


@pytest.mark.parametrize(
    "table, limit, column",
    [
        ("confessions", CONFESSION_BODY_MAX, "body"),
        ("messages", MESSAGE_BODY_MAX, "body"),
    ],
)
def test_database_rejects_over_long_text_written_outside_the_api(db, table, limit, column):
    """The invariant has to hold for a script or migration, not just an HTTP request."""
    with db.engine.begin() as connection:
        savepoint = connection.begin_nested()
        _insert_user(connection, "limits-owner")
        statement = {
            "confessions": (
                "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                "comments_enabled, created_at, author_id) VALUES "
                "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                "(SELECT id FROM users WHERE regno = 'limits-owner'))"
            ),
            "messages": (
                "INSERT INTO messages (id, conversation_id, sender_regno, receiver_regno, "
                "body, created_at) VALUES (gen_random_uuid(), gen_random_uuid(), "
                "'limits-owner', 'limits-peer', :body, now())"
            ),
        }[table]
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(text(statement), {"body": "x" * (limit + 1)})
        assert "ck_" in str(excinfo.value)
        savepoint.rollback()


def test_database_accepts_text_at_the_limit(db):
    with db.engine.begin() as connection:
        _insert_user(connection, "limits-ok")
        connection.execute(
            text(
                "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                "comments_enabled, created_at, author_id) VALUES "
                "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                "(SELECT id FROM users WHERE regno = 'limits-ok'))"
            ),
            {"body": "x" * CONFESSION_BODY_MAX},
        )


def test_apply_check_constraints_is_idempotent(db):
    """initialize() runs on every boot, so a second application must be a no-op."""
    db.apply_check_constraints()
    db.apply_check_constraints()
    with db.engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM pg_constraint WHERE conname = 'ck_confessions_body_length'")
        ).scalar_one()
    assert count == 1


def test_migration_preserves_rows_that_predate_the_limit(db):
    """A limit added to an unbounded column must not delete existing content.

    Rows written before the limit existed are left untouched, because the constraint is
    added NOT VALID. Truncating user content to make a migration fit is not a decision the
    migration gets to make on its own.

    Each step commits on its own. The drop has to be visible to the migration -- otherwise
    the migration sees a constraint that is on its way out, skips re-adding it, and the
    row survives because nothing is constraining it rather than because the constraint was
    added without validating history. That version of this test passed, and quietly left
    the test database without the constraint it claimed to be checking.
    """
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE confessions DROP CONSTRAINT ck_confessions_body_length"))
        _insert_user(connection, "limits-legacy")
        connection.execute(
            text(
                "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                "comments_enabled, created_at, author_id) VALUES "
                "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                "(SELECT id FROM users WHERE regno = 'limits-legacy'))"
            ),
            {"body": "legacy " + "x" * CONFESSION_BODY_MAX},
        )

    db.apply_check_constraints()

    with db.engine.begin() as connection:
        validated = connection.execute(
            text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conname = 'ck_confessions_body_length'"
            )
        ).scalar_one()
        surviving = connection.execute(
            text("SELECT body FROM confessions WHERE recipient_name = 'Sam'")
        ).scalar_one()

    assert surviving == "legacy " + "x" * CONFESSION_BODY_MAX
    # Present, but not enforced against what is already stored.
    assert validated is False


def test_migration_enforces_the_limit_on_new_rows(db):
    """NOT VALID skips the history check, not the constraint itself.

    An unvalidated constraint still rejects a new over-long write, so migrating a limit
    in does not leave the table permanently open.
    """
    with db.engine.begin() as connection:
        savepoint = connection.begin_nested()
        _insert_user(connection, "limits-new")
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(
                text(
                    "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                    "comments_enabled, created_at, author_id) VALUES "
                    "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                    "(SELECT id FROM users WHERE regno = 'limits-new'))"
                ),
                {"body": "x" * (CONFESSION_BODY_MAX + 1)},
            )
        assert "ck_confessions_body_length" in str(excinfo.value)
        savepoint.rollback()
