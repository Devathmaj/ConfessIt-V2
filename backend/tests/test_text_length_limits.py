"""Length and character limits for user-supplied text.

Two layers are asserted here, because either one alone leaves a hole:

* the Pydantic cap, which turns an over-long body into a 422 naming the field instead of
  letting it reach the database, and
* the CHECK constraint, which is the actual invariant for writes that never pass through
  an API model at all.

The character rules live here too. A length cap says nothing about whether a string can be
stored: a NUL byte is rejected by PostgreSQL outright and an unpaired surrogate cannot be
encoded to UTF-8 at all, so both are 500s rather than validation messages unless something
refuses them first.
"""
import warnings

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, Text, text
from sqlalchemy.exc import IntegrityError

from app.db_models import UserRecord, _non_character_code_points
from app.accounts import AccountType
from app.limits import (
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    LOVE_NOTE_BODY_MAX,
    MESSAGE_BODY_MAX,
    RECIPIENT_NAME_MAX,
    REPORT_REASON_MAX,
    USER_BIO_MAX,
)
from app.models import CommentCreate, ConfessionCreate, ReportCreate
from app.routers.messages import MessageCreate, MessageReport
from app.services.love_note_service import LoveNoteCreate

warnings.simplefilter("ignore")

AT_LIMIT = "x" * 10

# A minimal decodable PNG. `image_base64` is capped separately and checked first, so a love note
# at the body limit still needs a payload that gets past that check to reach the body field.
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _confession(body, recipient_name="Sam"):
    return ConfessionCreate(
        body=body, anonymous=False, comments_enabled=False, recipient_name=recipient_name
    )


def _love_note(body):
    return LoveNoteCreate(
        recipient_id="22222222-2222-2222-2222-222222333333",
        image_base64=TINY_PNG_BASE64,
        body=body,
        anonymous=False,
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
        "year": 2, "department": "Class",
        "gender": "other",
    }
    payload.update(overrides)
    return payload


def _insert_user(connection, regno):
    """Insert the minimum a confession needs to satisfy its author foreign key."""
    return connection.execute(
        text(
            "INSERT INTO users (id, regno, name, email, year, department, gender) "
            "VALUES (gen_random_uuid(), :regno, 'Owner', :email, 2, 'Class', 'other') "
            "ON CONFLICT (regno) DO NOTHING"
        ),
        {"regno": regno, "email": f"{regno}@user.com"},
    )


def _insert_author(db, regno):
    """The author row an API test needs, given the client rather than a connection."""
    with db.engine.begin() as connection:
        _insert_user(connection, regno)


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
        (lambda n: _love_note("x" * n), LOVE_NOTE_BODY_MAX),
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
        (lambda n: _love_note("x" * n), LOVE_NOTE_BODY_MAX),
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

    A confession also has to be allowed to carry no recipient *and* still be stored, which is
    why the column stays nullable rather than becoming "" by way of a default.
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


# ------------------------------------------------------------------- storable characters


@pytest.mark.parametrize(
    "label, body",
    [
        # Unstorable. PostgreSQL refuses 0x00 in text outright, and a lone surrogate has no
        # UTF-8 encoding, so both used to reach the driver and surface as 500s.
        ("NUL byte", "hi\x00there"),
        ("lone high surrogate", "hi\ud800there"),
        ("lone low surrogate", "hi\udfffthere"),
        # Storable but meaningless, and the raw material of terminal-escape and
        # log-injection payloads.
        ("C0 control", "bell\x07esc\x1b[31m"),
        ("vertical tab", "a\x0bb"),
        ("DEL", "del\x7fchar"),
        ("C1 block", "c1\x9bchar"),
        ("BMP noncharacter", "nonchar\ufffe"),
        # The last two code points of every plane are non-characters too, and they are what
        # a BMP-only rule misses. Nothing about this has to do with U+FFFE looking like a
        # special case.
        ("plane 1 noncharacter", "tail\U0001fffe"),
        ("plane 16 noncharacter", "tail\U0010ffff"),
    ],
)
def test_confession_body_refuses_characters_it_could_not_store(label, body):
    with pytest.raises(ValidationError) as excinfo:
        _confession(body)
    assert excinfo.value.errors()[0]["loc"][-1] == "body"


@pytest.mark.parametrize(
    "label, body",
    [
        # Everything a real confession is actually made of. These are the cases a naive
        # "strip non-ASCII" filter breaks, and they are exactly the ones that must survive.
        ("plain ascii", "I still think about you"),
        ("emoji", "hi \U0001f495 there"),
        ("ZWJ emoji sequence", "\U0001f468‍\U0001f469"),
        ("devanagari", "मेरे है"),
        ("tamil", "நீ நன்றாக இரு"),
        ("arabic", "أشتاق إليك"),
        ("arabic with diacritics", "أَشْتَاقُ"),
        ("RTL override marks", "hello ‮abc‬ world"),
        ("newlines and tabs", "line one\nline two\ttabbed"),
        ("combining marks", "éàü"),
        ("cjk", "そばにいてください"),
        ("variation selector", "❤️"),
        # The assignable neighbours of the astral non-characters. A rule that widened by one
        # step would take these with it, so they are named.
        ("deseret", "\U00010400\U00010401"),
        ("U+1FFFD, next to U+1FFFE", "tail\U0001fffd"),
        ("U+10FFFD, next to U+10FFFF", "tail\U0010fffd"),
    ],
)
def test_confession_body_accepts_real_unicode(label, body):
    """The filter is for unencodable characters, not for foreign ones.

    A rule that rejected these would be rejecting the people least likely to write in
    English, which is not a cost worth paying to prevent a database error.
    """
    assert _confession(body).body == body


def test_confession_recipient_shares_the_character_rule():
    """The name field is stored in the same column type, so it fails the same way."""
    with pytest.raises(ValidationError):
        _confession("a confession", recipient_name="Sam\x00")


def test_whitespace_is_not_a_character_requirement():
    """A confession may be whitespace-only; that is a moderation question, not a validity one."""
    assert _confession("   \n  ").body == "   \n  "


@pytest.mark.parametrize(
    "field, limit",
    [
        ("name", 160),
        ("regno", 100),
        ("department", 80),
    ],
)
def test_admin_user_fields_are_capped(field, limit):
    """What an admin may write, against the width of the column it lands in.

    ``bio`` and ``emoji`` used to appear here. They do not any more, because an admin can no
    longer write them at all, and ``bio``'s cap is asserted on the participant's payload below.
    Leaving them in this list would have kept asserting a constraint against a payload that
    refuses the key outright -- the test would pass for the wrong reason, and would go on
    passing if the cap were later dropped from the route that does write the field.
    """
    from app.models import AdminUserCreate

    AdminUserCreate(**_admin_user(**{field: "x" * limit}))
    with pytest.raises(ValidationError):
        AdminUserCreate(**_admin_user(**{field: "x" * (limit + 1)}))


@pytest.mark.parametrize(
    "field, limit",
    [
        ("bio", USER_BIO_MAX),
    ],
)
def test_profile_fields_are_capped_on_the_participant_payload(field, limit):
    """The cap follows the write, not the field.

    This field is the account's own, so the admin payload no longer carries it and
    ``PUT /profile/update`` is the only route that can set it. The cap therefore has to be
    declared there too: without it the column width is the only limit, and an over-long bio
    comes back as a 500 from the database rather than a 422 that names the field.
    """
    from app.services.profile_service import UserProfileUpdate

    UserProfileUpdate(**{field: "x" * limit})
    with pytest.raises(ValidationError):
        UserProfileUpdate(**{field: "x" * (limit + 1)})


def test_the_love_note_caps_match_the_constraints():
    """The payload cap and the database rule are the same number, deliberately.

    Same reason as the profile fields below, and load-bearing in a specific way for a love
    note: the message is not stored as text that anyone reads back -- it is drawn onto a PNG
    and uploaded -- so the body column is the only durable copy. A payload cap and a database
    rule that disagree means either an over-long note is accepted and stored, or a note is
    refused on insert with a 500 the sender cannot act on.

    The rule is a CHECK rather than a column width. A ``varchar(1000)`` refuses an over-long
    write during input, before any CHECK is evaluated, which left
    ``ck_love_notes_body_length_1000`` unable to reject anything: the database raised
    ``StringDataRightTruncation`` and the constraint was unreachable. ``Text`` plus the CHECK
    is how ``confessions.body`` already enforces the same invariant.
    """
    from app.db_models import LoveNoteRecord

    body = LoveNoteRecord.__table__.c.body
    assert isinstance(body.type, Text)
    lengths = {
        constraint.name: str(constraint.sqltext)
        for constraint in LoveNoteRecord.__table__.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name
    }
    assert lengths == {f"ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}": f"length(body) <= {LOVE_NOTE_BODY_MAX}"}
    assert (
        LoveNoteCreate.model_fields["body"].metadata[0].max_length == LOVE_NOTE_BODY_MAX
    )


def test_the_profile_caps_match_the_columns():
    """The payload cap and the column width are the same number, deliberately.

    Two independent declarations of one limit is how a field ends up writable past its column
    and then failing on insert. This reads both so the pairing is checked rather than assumed.
    """
    from app.db_models import UserRecord
    from app.services.profile_service import UserProfileUpdate

    assert UserRecord.__table__.c.bio.type.length == USER_BIO_MAX
    assert UserProfileUpdate.model_fields["bio"].metadata[0].max_length == USER_BIO_MAX


def test_gender_is_bounded_by_value_rather_than_by_length():
    """Gender is no longer free text, so it has no meaningful length cap.

    It used to appear in the parametrised test above with a 24-character limit. That test
    passed for the wrong reason once the field became a closed vocabulary: a 25-character
    string is rejected because it is not ``male``, ``female`` or ``other``, not because it is
    too long. Keeping it there would have left a length cap in place that nothing enforces
    and that reads as though arbitrary strings were acceptable up to 24 characters.

    The vocabulary is a filter -- matchmaking pairs on it -- so it is the *value* that has to
    be bounded, and both the shortest legal value and a near-miss spelling of one are worth
    asserting explicitly.
    """
    from app.models import AdminUserCreate

    for value in ("male", "female", "other"):
        assert AdminUserCreate(**_admin_user(gender=value)).gender == value

    for value in ("Male", "MALE", "nonbinary", "m", "x" * 24):
        with pytest.raises(ValidationError):
            AdminUserCreate(**_admin_user(gender=value))


def test_year_is_bounded_by_range_rather_than_by_length():
    """Year is a whole number in a range, so it gets the by-value treatment for the same reason.

    It is the field the old ``class_name`` limit of 80 used to cover, so leaving it out of the
    parametrised test above is a deliberate omission rather than an oversight. There is no length
    left to enforce: the column is an ``Integer``, so the only question is whether the number is in
    range, and a year cannot be "too long" in the way a department can.

    The near-misses are the values a stale client would still be sending. Before the split this
    field was free text and every one of these was merely a different cohort; now the column holds
    numbers, and all of them are refused -- which is the property that makes the range meaningful
    rather than advisory.
    """
    from app.limits import USER_YEAR_MAX, USER_YEAR_MIN
    from app.models import AdminUserCreate

    for value in range(USER_YEAR_MIN, USER_YEAR_MAX + 1):
        assert AdminUserCreate(**_admin_user(year=value)).year == value

    for value in (USER_YEAR_MIN - 1, USER_YEAR_MAX + 1, 0, -1, 2.5, "2.5", "2nd Year", "2nd year", ""):
        with pytest.raises(ValidationError):
            AdminUserCreate(**_admin_user(year=value))

    # A string of digits is the one near-miss the range cannot catch, because it is not out of
    # range once parsed: Pydantic coerces `"2"` to 2 and the model hands back a year. That is
    # deliberate -- a client whose JSON holds a quoted number gets the number it meant rather than
    # an error -- but it is worth stating, because the type is only half closed. A value with a
    # fractional part has nothing to round to a year and is refused above; `"2"` has an exact
    # integer in it and is not.
    assert AdminUserCreate(**_admin_user(year="2")).year == 2
    assert AdminUserCreate(**_admin_user(year="12")).year == 12


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
    assert components["LoveNoteCreate"]["properties"]["body"]["maxLength"] == LOVE_NOTE_BODY_MAX


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
        (
            # `POST`, not `GET`: an over-long body on the wrong verb would 405 before validation
            # ran, so the route and the method both have to be right for this to assert anything.
            "/love-notes/send",
            {
                "recipient_id": "22222222-2222-2222-2222-222222333333",
                "image_base64": TINY_PNG_BASE64,
                "body": "x" * (LOVE_NOTE_BODY_MAX + 1),
                "anonymous": False,
            },
        ),
    ],
)
def test_api_rejects_over_long_text_with_422(client: TestClient, path, payload):
    """Before the caps these reached the database and surfaced as an unhandled 500."""
    response = client.post(path, json=payload)
    assert response.status_code == 422, response.text
    assert "detail" in response.json()


@pytest.mark.parametrize(
    "label, body_literal",
    [
        ("NUL byte", b'"hi\\u0000there"'),
        ("lone surrogate", b'"hi\\ud800there"'),
        ("C0 control", b'"bell\\u0007here"'),
        ("DEL", b'"del\\u007fhere"'),
    ],
)
def test_api_rejects_unstorable_characters_with_422(db, client: TestClient, label, body_literal):
    """These used to reach the driver and surface as 500s.

    Sent as an ASCII-escaped JSON document rather than as a Python string because a lone
    surrogate cannot be encoded to UTF-8 by the HTTP client, so handing httpx a str would
    test the client instead of the server. The escape is ordinary ASCII on the wire and only
    becomes an unpaired surrogate when the server parses it.
    """
    _insert_author(db, "limits-chars")
    document = (
        b'{"body": ' + body_literal + b', "anonymous": false, '
        b'"comments_enabled": false, "recipient_name": "Sam"}'
    )
    response = client.post(
        "/confessions", content=document, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422, response.text
    # The error body quotes the rejected input back, which is exactly the value that could
    # not be encoded. If the 422 cannot be rendered, the rejection becomes a 500.
    assert "detail" in response.json()


def test_the_422_body_is_renderable_even_when_the_input_is_not(client: TestClient):
    """A rejected request must not turn into a 500 on the way out.

    The 422 quotes the offending value, and an unencodable value in the response is a
    UnicodeEncodeError while rendering it. This is the one bug in this area that the model
    validator alone cannot fix, because the failure happens after the model has done its
    job.
    """
    document = (
        b'{"body": "hi\\ud800there", "anonymous": false, '
        b'"comments_enabled": false, "recipient_name": "Sam"}'
    )
    response = client.post(
        "/confessions", content=document, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]


def test_api_accepts_unicode_confessions(db, request):
    """End to end, so the rule is not only correct but does not block real confessions.

    Uses its own client rather than the shared ``client`` fixture because a successful
    insert needs an author row whose id is the one the auth override resolves to, and the
    shared fixture mints a fresh id on every call.
    """
    client = request.getfixturevalue("_confession_author_client")
    author_id = "33333333-3333-3333-3333-333333333333"
    for body in (
        "hi \U0001f495 there",
        "मेरे है",
        "line one\nline two",
        "أَشْتَاقُ إليك",
        "\U0001f468‍\U0001f469",
    ):
        # One confession per participant every two hours, so the window is opened again
        # between iterations. This test is about what the text layer accepts, and the
        # cooldown is covered on its own in test_confession_cooldown.py.
        with db.engine.begin() as connection:
            connection.execute(
                text("UPDATE users SET last_confession_at = NULL WHERE id = :id"),
                {"id": author_id},
            )
        response = client.post(
            "/confessions",
            json={
                "body": body,
                "anonymous": True,
                "comments_enabled": True,
                "recipient_name": None,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["body"] == body


# ------------------------------------------------------------------- database backstop


def test_length_constraints_are_installed(db):
    with db.engine.begin() as connection:
        installed = {
            (row[0], row[1])
            for row in connection.execute(
                text(
                    "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                    "WHERE conname IN ("
                    f"  'ck_confessions_body_length_{CONFESSION_BODY_MAX}',"
                    f"  'ck_confession_comments_body_length_{COMMENT_BODY_MAX}',"
                    f"  'ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}')"
                )
            )
        }
    # Chat bodies are ciphertext now, so their 100-character rule is enforced by
    # the Pydantic request model and the service before encryption. A CHECK on
    # ciphertext length would measure the wrong thing and enforce nothing.
    assert installed == {
        ("confessions", f"ck_confessions_body_length_{CONFESSION_BODY_MAX}"),
        (
            "confession_comments",
            f"ck_confession_comments_body_length_{COMMENT_BODY_MAX}",
        ),
        ("love_notes", f"ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}"),
    }


def test_the_pre_convention_unsuffixed_length_constraints_are_retired(db):
    """The name a constraint had before the value moved into it must not outlive the change.

    ``ck_confession_comments_body_length`` is how this one was originally spelled, and it carries
    the *old* limit. Renaming the model to the suffixed name only adds the tight rule; the loose
    one would sit beside it and keep permitting what the API refuses. Two CHECK constraints on a
    column are not a maximum -- each is enforced on its own. The retired ``ck_messages_body_length``
    is checked here too, because chat bodies are ciphertext now and no body-length CHECK should
    survive on that table at all.
    """
    db.apply_check_constraints()
    with db.engine.begin() as connection:
        left_behind = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conname IN ("
                    "  'ck_messages_body_length',"
                    "  'ck_confession_comments_body_length')"
                )
            )
        }
    assert left_behind == set()


def test_no_superseded_confession_length_constraint_is_left_behind(db):
    """Moving the limit has to remove the constraint it replaced, not just sit beside it.

    Two CHECK constraints on the same column are independent, not a maximum: a direct write
    has to satisfy both, so the *tighter* one wins and the old 2000-character rule quietly
    stops constraining anything. The limit would still hold, which is why this went unnoticed,
    but the database would be carrying a constraint that no longer means what its name says --
    and if the limit were later relaxed, that stale rule would immediately become the binding
    one.

    The install is set up here rather than trusted, because a test database created fresh from
    the models never had a predecessor to begin with. It would pass against a cleanup routine
    that does nothing.
    """
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "ALTER TABLE confessions ADD CONSTRAINT ck_confessions_body_length_2000 "
                "CHECK (length(body) <= 2000) NOT VALID"
            )
        )
        installed = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = "
                "'ck_confessions_body_length_2000'"
            )
        ).scalar_one()
    assert installed == 1, "setup failed, so the assertions below would be vacuous"

    db.apply_check_constraints()

    with db.engine.begin() as connection:
        remaining = connection.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'confessions'::regclass "
                "AND conname LIKE 'ck_confessions_body_length%' "
                "AND conname <> :current"
            ),
            {"current": f"ck_confessions_body_length_{CONFESSION_BODY_MAX}"},
        ).scalars().all()
    assert remaining == []

    # And the current limit is still enforced afterwards, so the drop did not remove the rule
    # instead of the stale copy of it.
    with db.engine.begin() as connection:
        savepoint = connection.begin_nested()
        _insert_user(connection, "limits-after-cleanup")
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                    "comments_enabled, created_at, author_id) VALUES "
                    "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                    "(SELECT id FROM users WHERE regno = 'limits-after-cleanup'))"
                ),
                {"body": "x" * (CONFESSION_BODY_MAX + 1)},
            )
        savepoint.rollback()


@pytest.mark.parametrize(
    "table, limit, column",
    [
        ("confessions", CONFESSION_BODY_MAX, "body"),
        ("love_notes", LOVE_NOTE_BODY_MAX, "body"),
    ],
)
def test_database_rejects_over_long_text_written_outside_the_api(db, table, limit, column):
    """The invariant has to hold for a script or migration, not just an HTTP request."""
    # Reconciled first so the assertion is about the *declared* limit rather than whatever a
    # previous run left behind. A test database that predates a limit change keeps the old CHECK,
    # and then an over-long insert is accepted -- the test fails for a reason that has nothing to
    # do with the code under test. `create_all` cannot fix an existing table, so only this does.
    # Chat bodies are not in this parameter set: they are ciphertext now, and their 100-character
    # rule lives in the request model and the send service before encryption.
    db.apply_check_constraints()

    with db.engine.begin() as connection:
        savepoint = connection.begin_nested()
        _insert_user(connection, "limits-owner")
        _insert_user(connection, "limits-peer")
        # `love_notes` carries a foreign key on both `sender_id` and `recipient_id`, so it needs a
        # third participant beyond the two above. Without it the insert fails on the key and the
        # test passes for the wrong reason -- asserting that a `ck_` constraint rejects the row
        # while the database was actually complaining about a missing user.
        _insert_user(connection, "limits-recipient")
        statement = {
            "confessions": (
                "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                "comments_enabled, created_at, author_id) VALUES "
                "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                "(SELECT id FROM users WHERE regno = 'limits-owner'))"
            ),
            "love_notes": (
                # ``image_public_id``/``image_format`` are what the row holds now; the old test
                # inserted a full proxy path here and the insert only ever needed *some*
                # non-null image, since the point of this statement is the body length.
                "INSERT INTO love_notes (id, sender_id, recipient_id, image_public_id, "
                "image_format, body, anonymous, created_at) VALUES "
                "(gen_random_uuid(), "
                "(SELECT id FROM users WHERE regno = 'limits-owner'), "
                "(SELECT id FROM users WHERE regno = 'limits-recipient'), "
                "'love_notes/00000000-0000-0000-0000-000000000000/image', 'png', "
                ":body, false, now())"
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


def test_database_rejects_control_characters_written_outside_the_api(db):
    """The character invariant needs the same second layer the length one has.

    Only the control characters are covered here. A NUL byte cannot be in a row at all, and
    a lone surrogate cannot be encoded into one, so neither is something a CHECK constraint
    is being asked to catch.
    """
    statement = (
        "INSERT INTO confessions (id, body, recipient_name, anonymous, "
        "comments_enabled, created_at, author_id) VALUES "
        "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
        "(SELECT id FROM users WHERE regno = 'limits-controls'))"
    )
    # Each case runs in its own transaction: a failed INSERT poisons the one it is in, so
    # rolling back the savepoint is what keeps the next case from failing for the wrong
    # reason. Re-inserting the author is deliberate -- the rollback took that row too.
    for body in ("bell\x07here", "esc\x1b[31mhere", "del\x7fhere", "vt\x0bhere"):
        with db.engine.begin() as connection:
            savepoint = connection.begin_nested()
            _insert_user(connection, "limits-controls")
            with pytest.raises(IntegrityError) as excinfo:
                connection.execute(text(statement), {"body": body})
            assert "ck_confessions_body_characters" in str(excinfo.value)
            savepoint.rollback()


@pytest.mark.parametrize(
    "label, body",
    [
        ("U+FDD0", "x\uFDD0y"),
        ("U+FDEF", "x\uFDEFy"),
        # The astral ones matter more here than in the model, because the constraint is a
        # regular expression over code points and a five-digit \u escape is what makes them
        # expressible at all.
        ("U+FFFE", "x\uFFFEy"),
        ("U+FFFF", "x\uFFFFy"),
        ("U+1FFFE", "x\U0001fffe y"),
        ("U+10FFFF", "x\U0010ffffy"),
    ],
)
def test_database_rejects_non_characters_written_outside_the_api(db, label, body):
    """The non-character rule reaches the database layer, in every plane.

    PostgreSQL stores these without complaint -- ``convert_to`` will encode U+FFFE to three
    bytes -- so nothing stops them arriving except a rule that says they cannot. The model
    refuses them first, and this is the half that holds for a write that skipped it.
    """
    statement = (
        "INSERT INTO confessions (id, body, recipient_name, anonymous, "
        "comments_enabled, created_at, author_id) VALUES "
        "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
        "(SELECT id FROM users WHERE regno = 'limits-noncharacters'))"
    )
    with db.engine.begin() as connection:
        savepoint = connection.begin_nested()
        _insert_user(connection, "limits-noncharacters")
        with pytest.raises(IntegrityError) as excinfo:
            connection.execute(text(statement), {"body": body})
        assert "ck_confessions_body_unicode_noncharacters" in str(excinfo.value)
        savepoint.rollback()


def test_database_accepts_the_assignable_neighbours_of_the_non_characters(db):
    """The rule has to stop exactly at the boundary, in the database as much as the model.

    A regular expression built by hand reaches U+1FFFD and U+10FFFD by accident only if
    somebody counted the planes correctly, so these are checked where the count is done.
    """
    with db.engine.begin() as connection:
        _insert_user(connection, "limits-neighbours")
        for body in ("x\U0001FFFDy", "x\U0010FFFDy", "x\uFDF0y", "x\uFDCFy"):
            connection.execute(
                text(
                    "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                    "comments_enabled, created_at, author_id) VALUES "
                    "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                    "(SELECT id FROM users WHERE regno = 'limits-neighbours'))"
                ),
                {"body": body},
            )


def test_database_accepts_real_unicode_in_a_confession(db):
    """The constraint must not cost the database layer its non-Latin scripts."""
    with db.engine.begin() as connection:
        _insert_user(connection, "limits-unicode")
        connection.execute(
            text(
                "INSERT INTO confessions (id, body, recipient_name, anonymous, "
                "comments_enabled, created_at, author_id) VALUES "
                "(gen_random_uuid(), :body, 'Sam', false, false, now(), "
                "(SELECT id FROM users WHERE regno = 'limits-unicode'))"
            ),
            {"body": "hi \U0001f495 मेरे है\nsecond line"},
        )


def test_apply_check_constraints_is_idempotent(db):
    """initialize() runs on every boot, so a second application must be a no-op."""
    db.apply_check_constraints()
    db.apply_check_constraints()
    with db.engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM pg_constraint "
                f"WHERE conname = 'ck_confessions_body_length_{CONFESSION_BODY_MAX}'"
            )
        ).scalar_one()
    assert count == 1


def test_the_character_constraint_is_installed_not_valid(db):
    """It is added on an already-deployed database, so it must not validate history.

    A confession written before the rule existed may contain a character the rule now
    refuses. Validating it on boot would refuse to start, and quietly deleting or rewriting
    someone's confession to satisfy a constraint is not a decision this should make.

    The invariant belongs to ``apply_check_constraints``, so it is exercised by removing the
    constraints and letting it install them again. Asserting only that whatever is currently
    installed is NOT VALID cannot work: on a database created from the models -- a fresh test
    database, or a first deployment -- ``create_all`` builds these constraints from
    ``__table_args__`` and PostgreSQL has them validated, because there is no history to spare.
    The flag is therefore a fact about how a constraint was installed, not about the schema,
    and the only way to test it is to install one.
    """
    names = ("ck_confessions_body_characters", "ck_confessions_body_unicode_noncharacters")
    with db.engine.begin() as connection:
        for name in names:
            connection.execute(text(f'ALTER TABLE confessions DROP CONSTRAINT "{name}"'))

    try:
        db.apply_check_constraints()
        with db.engine.begin() as connection:
            validated = {
                name: flag
                for name, flag in connection.execute(
                    text(
                        "SELECT conname, convalidated FROM pg_constraint "
                        "WHERE conname IN ('ck_confessions_body_characters', "
                        "'ck_confessions_body_unicode_noncharacters')"
                    )
                )
            }
        assert set(validated) == set(names)
        assert set(validated.values()) == {False}
    finally:
        # Both are back by now, NOT VALID, which is the state a deployed database is in and the
        # state every other test in this file expects to find them in. Re-running is harmless:
        # apply_check_constraints adds only what is missing.
        db.apply_check_constraints()


def test_the_non_character_pattern_has_no_four_digit_escape_trap(db):
    """The constraint's pattern is built from real characters, and that is load-bearing.

    PostgreSQL's ``\\u`` escape takes exactly four hex digits, so a pattern written as
    ``\\u1FFFE`` means U+1FFF followed by a literal ``E``. Nothing errors: it matches the wrong
    characters, so the constraint passes everything it was written to refuse and refuses
    confessions ending in the letter E.

    This asserts what the installed pattern matches rather than how it is written, because the
    shape of the string is the part that is allowed to change.
    """
    with db.engine.begin() as connection:
        pattern = connection.execute(
            text(
                "SELECT substring(pg_get_constraintdef(oid) from 'body !~ ''(.+)''::text') "
                "FROM pg_constraint WHERE conname = 'ck_confessions_body_unicode_noncharacters'"
            )
        ).scalar_one()
    stripped = pattern[1:-1]

    # Everything the module says is a non-character, matched.
    for code_point in _non_character_code_points():
        assert chr(code_point) in stripped, f"U+{code_point:X} missing from the pattern"

    # And the assignable neighbours of each one, not matched.
    for code_point in (0xFDCF, 0xFDF0, 0xFFFD, 0x1FFFD, 0x10FFFD, 0x20, 0x41):
        assert chr(code_point) not in stripped, f"U+{code_point:X} wrongly in the pattern"

    # The specific false positive the four-digit form produces: an E is not a non-character.
    assert "E" not in stripped


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
        connection.execute(
            text(f"ALTER TABLE confessions DROP CONSTRAINT ck_confessions_body_length_{CONFESSION_BODY_MAX}")
        )
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
                f"WHERE conname = 'ck_confessions_body_length_{CONFESSION_BODY_MAX}'"
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
        assert f"ck_confessions_body_length_{CONFESSION_BODY_MAX}" in str(excinfo.value)
        savepoint.rollback()
