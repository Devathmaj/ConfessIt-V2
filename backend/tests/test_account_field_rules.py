"""The rules the account columns themselves hold, tested against the real database.

Four things are asserted here, and each of them is the kind that a payload model alone would
not have caught:

* ``email`` is required of every account, in the payload *and* in the column.
* ``gender`` is a closed vocabulary, in the payload *and* in the column -- on ``users``, which
  is the only table that has one.
* ``interests`` is a closed vocabulary, on the participant's own profile payload *and* in
  the column.
* A profile picture is the account's to upload, not an admin's to write.

The column-level half of each is the half worth testing, because the payload models are
already exercised by the suite and a constraint that exists only in Pydantic is not a
constraint -- it is advice. Every column test below writes through a path that bypasses the
API on purpose, because that is the only way to find out whether PostgreSQL is actually
holding the line.

The admin half of the last two is the reverse: an admin payload that carried a profile field
used to have it silently dropped by Pydantic's ``extra="ignore"`` default and answered ``201``.
Both admin payloads now set ``extra="forbid"``, and the tests below assert the 422 and the
unchanged row rather than trusting the status code alone.
"""

import io

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.accounts import AccountType
from app.db_models import AdminRecord, ModeratorRecord, UserRecord
from app.limits import USER_YEAR_MAX
from app.main import app
from app.models import AdminUserCreate, AdminUserUpdate
from app.postgres import PLACEHOLDER_EMAIL_SQL
from app.services.auth_service import get_current_user
from app.services.profile_service import UserProfileUpdate

from conftest import override_get_current_user


# ------------------------------------------------------------------- email backfill


def _placeholder_for(connection, regno):
    # The expression reads a bare ``regno`` column, as it does in the real UPDATE. The
    # subquery supplies one, so this exercises the same text the migration runs.
    return connection.execute(
        text(
            f"SELECT {PLACEHOLDER_EMAIL_SQL} AS email "
            "FROM (SELECT CAST(:regno AS text) AS regno) AS src"
        ),
        {"regno": regno},
    ).scalar_one()


def test_a_clean_regno_becomes_a_readable_placeholder(db):
    """The common case should not be made ugly by the machinery that handles the rare one."""
    with db.engine.begin() as connection:
        email = _placeholder_for(connection, "22CS0455")
    assert email == "22CS0455@unassigned.invalid"


def test_a_regno_needing_sanitising_is_still_given_a_placeholder(db):
    with db.engine.begin() as connection:
        email = _placeholder_for(connection, "22/CS 0455")
    # Reserved TLD, so it can never resolve and can never be mistaken for a real inbox.
    assert email.endswith("@unassigned.invalid")
    assert " " not in email and "/" not in email


@pytest.mark.parametrize(
    "left, right",
    [
        # The case that broke boot. Every illegal character sanitises to "-", so `a/b` and
        # `a?b` used to produce the same placeholder -- and `email` is uniquely indexed, so the
        # backfill UPDATE aborted and the application refused to start.
        ("22/CS0455", "22?CS0455"),
        ("a@b", "a#b"),
        # Truncation used to collide the same way: both lose everything past 64 characters.
        ("X" * 70, "X" * 70 + "Y"),
        ("X" * 64 + "1", "X" * 64 + "2"),
    ],
)
def test_distinct_regnos_never_collide_on_one_placeholder(db, left, right):
    """Uniqueness is not a nicety here: the unique index turns a collision into a failed boot."""
    with db.engine.begin() as connection:
        assert _placeholder_for(connection, left) != _placeholder_for(connection, right)


def test_the_placeholder_local_part_fits_the_address_limit(db):
    """64 characters is the longest local part permitted, and this expression builds its own."""
    with db.engine.begin() as connection:
        for regno in ("X" * 200, "22/CS0455", "a" * 64):
            email = _placeholder_for(connection, regno)
            assert len(email.split("@", 1)[0]) <= 64


def test_a_placeholder_cannot_be_produced_twice_for_one_regno(db):
    """Deterministic, or a second run would invent a second address for the same row."""
    with db.engine.begin() as connection:
        assert _placeholder_for(connection, "22/CS0455") == _placeholder_for(
            connection, "22/CS0455"
        )


# --------------------------------------------------------------------------- vocabulary


def test_the_authoritative_gender_values_are_the_three_that_are_enforced():
    """The tuple and the type must not drift apart.

    ``limits.USER_GENDER_VALUES`` is what the CHECK constraint and the frontend Select are
    both generated from, and ``models.Gender`` is what the payload accepts. Nothing forces
    those two to agree -- they are written out separately, because a ``Literal`` cannot be
    built from a tuple at runtime -- so the only thing catching a divergence is a test that
    compares them.
    """
    import typing

    from app.limits import USER_GENDER_VALUES
    from app.models import Gender

    assert set(typing.get_args(Gender)) == set(USER_GENDER_VALUES)


def test_the_authoritative_interest_values_are_the_twelve_that_are_enforced():
    """The same check for interests, where a drift would break existing rows."""
    import typing

    from app.limits import INTEREST_VALUES
    from app.models import Interest

    assert set(typing.get_args(Interest)) == set(INTEREST_VALUES)


@pytest.mark.parametrize("gender", ["male", "female", "other"])
def test_the_three_genders_are_accepted(gender):
    payload = {
        "account_type": AccountType.USERS.value,
        "regno": f"g-{gender}",
        "name": "Someone",
        "email": f"g-{gender}@example.com",
        "year": 2, "department": "Class",
        "gender": gender,
    }
    assert AdminUserCreate(**payload).gender == gender


@pytest.mark.parametrize("gender", ["Male", "MALE", "nonbinary", "m", "", "  ", "x" * 24])
def test_anything_outside_the_gender_vocabulary_is_refused(gender):
    """Including case variants, which matchmaking treats as different people."""
    payload = {
        "account_type": AccountType.USERS.value,
        "regno": "r",
        "name": "Someone",
        "email": "e@example.com",
        "year": 2, "department": "Class",
        "gender": gender,
    }
    with pytest.raises(ValueError):
        AdminUserCreate(**payload)


@pytest.mark.parametrize(
    "interests",
    [
        ["music"],          # right word, wrong case
        ["MUSIC"],
        ["Musics"],
        ["Music "],         # trailing whitespace is not a trim away from legal
        ["", "  "],
        ["Classical"],
    ],
)
def test_interests_outside_the_vocabulary_are_refused(interests):
    """The rule now lives on the participant's own profile payload.

    ``AdminUserCreate`` no longer has an ``interests`` field to validate: interests are the
    account's, and an admin cannot write them, so the closed vocabulary is enforced where the
    write actually happens -- ``UserProfileUpdate``, on ``PUT /profile/update``.
    """
    with pytest.raises(ValueError):
        UserProfileUpdate(**{"interests": interests})


def test_duplicate_interests_collapse_rather_than_failing():
    """A double-click should not be an error.

    The checkboxes are a multi-select, and a client that appends rather than replacing is a
    normal thing to be. Collapsing keeps the stored list canonical, which matters because it
    is rendered as a set of ticked boxes -- a duplicate would render as a list with the same
    label twice.
    """
    assert UserProfileUpdate(
        interests=["Music", "Music", "Coding", "Music"]
    ).interests == ["Music", "Coding"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("interests", ["Music"]),
        ("emoji", "x"),
        ("bio", "x"),
        ("matchmaking_enabled", False),
        ("notifications_enabled", False),
        ("love_notes_receive_enabled", False),
    ],
)
def test_an_admin_cannot_write_a_field_the_account_owns(admin_client, field, value):
    """The 422, which is the whole point of the change.

    Pydantic's default is ``extra="ignore"``, so a payload carrying ``bio`` used to be accepted,
    the key dropped, and the caller answered ``201`` -- a success for an insert that never
    happened. Both admin payloads set ``extra="forbid"`` instead, so a caller that still sends
    one is told, by name, that it is not theirs to send. Removing the form control alone would
    have left the write reachable with curl.
    """
    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": f"forbidden-{field}",
            "name": "Someone",
            "email": f"forbidden-{field}@example.com",
            "year": 2, "department": "Class",
            "gender": "other",
            field: value,
        },
    )
    assert response.status_code == 422, response.text
    assert field in response.text


@pytest.mark.parametrize(
    "field, value",
    [
        ("profile_picture_url", "https://example.test/whatever.png"),
        ("emoji", "x"),
        ("bio", "x"),
        ("interests", ["Music"]),
        ("matchmaking_enabled", False),
        ("love_notes_receive_enabled", False),
        ("love_notes_send_enabled", True),
        ("notifications_enabled", False),
        ("is_blocked", True),
        ("last_matchmaking_at", "2026-01-01T00:00:00Z"),
    ],
)
def test_an_admin_update_refuses_a_field_the_account_owns(admin_client, db, field, value):
    """Same rule on the way in through the edit route.

    ``last_matchmaking_at`` is here because it was never a form field at all -- it only ever
    arrived by hand, and the old payload would have written it straight onto the row.
    """
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": f"forbidden-update-{field}",
            "name": "Someone",
            "email": f"forbidden-update-{field}@example.com",
            "year": 2, "department": "Class",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text
    user_id = created.json()["id"]

    response = admin_client.put(f"/admin/users/{user_id}", json={"name": "Renamed", field: value})
    assert response.status_code == 422, response.text
    assert field in response.text

    # Refused means nothing moved, not merely that the response said so.
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.id == user_id))
        assert row.name == "Someone"
        assert row.bio is None
        assert row.emoji is None
        assert list(row.interests) == []


def test_an_admin_cannot_write_a_participant_field_onto_staff(admin_client, db):
    """A staff row has no such columns, so it cannot gain them through the back door."""
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.MODERATOR.value,
            "regno": "mod-forbidden",
            "name": "Mod",
            "email": "mod-forbidden@example.com",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text
    # Gender is accepted by the payload and dropped by the router, because ``users`` is the
    # only table with a column to write it to.
    assert "gender" not in created.json()

    # Year, department and gender are real fields on the admin payload -- they are simply
    # dropped for a staff row, which is a different rule from refusing them by name.
    updated = admin_client.put(
        f"/admin/users/{created.json()['id']}",
        json={"name": "Mod Renamed", "gender": "male", "year": 2, "department": "Class"},
    )
    assert updated.status_code == 200, updated.text
    assert "year" not in updated.json()
    assert "gender" not in updated.json()

    refused = admin_client.put(
        f"/admin/users/{created.json()['id']}",
        json={"name": "Mod Renamed", "interests": ["Music"], "bio": "x"},
    )
    assert refused.status_code == 422, refused.text

    with db.sessions() as session:
        row = session.scalar(
            select(ModeratorRecord).where(ModeratorRecord.regno == "mod-forbidden")
        )
        assert row is not None
        assert row.name == "Mod Renamed"


def test_no_interests_is_allowed_and_means_none():
    """A participant who unticks everything has selected no interests, which is a valid answer."""
    assert UserProfileUpdate(interests=[]).interests == []


# ------------------------------------------------------------------- the columns themselves


@pytest.mark.parametrize(
    "model, table",
    [
        (UserRecord, "users"),
        (AdminRecord, "admin"),
        (ModeratorRecord, "moderator"),
    ],
)
def test_postgresql_refuses_a_null_email_on_every_account_table(db, model, table):
    """The payload enforces it; this proves the column does too.

    Written with raw SQL on purpose. Going through the ORM would prove only that the model
    class has ``nullable=False`` set, which is what the declaration already says -- it is the
    database that has to hold the line for anything written by a script, a migration, or a
    future endpoint that forgets the model.
    """
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    # ``gender`` is written only for ``users``: it is a participants-only
                    # column now, and naming it on a staff table would fail with
                    # ``column ... does not exist`` before the NULL email was ever looked at.
                    f'INSERT INTO "{table}" (id, regno, name, email'
                    f'{", gender" if table == "users" else ""}) '
                    f"VALUES (gen_random_uuid(), 'null-email-{table}', 'X', NULL"
                    f"{", 'other'" if table == "users" else ""})"
                )
            )
    assert "email" in str(excinfo.value)


@pytest.mark.parametrize("table", ["users", "admin", "moderator"])
def test_only_participants_still_have_a_gender_column(db, table):
    """The scoping itself, asserted against the database rather than the model.

    The boot path drops retired columns only when it finds them, so this is the check that a
    deployed schema actually reached the same shape as a fresh one -- and it is what makes the
    tests below meaningful: an insert that names ``gender`` on a staff table has to fail for
    the column's absence, not for something else.
    """
    with db.engine.begin() as connection:
        present = connection.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns "
                "WHERE table_name = :table AND column_name = 'gender')"
            ),
            {"table": table},
        ).scalar_one()
    assert present is (table == "users")


def test_postgresql_refuses_a_gender_outside_the_vocabulary(db):
    """On ``users``, the only table that still has a gender column.

    The staff tables used to be in the parametrised list too. Their copy of the column was
    retired along with the constraint, so an insert naming ``gender`` there now fails for the
    column's absence -- which is a different rule, and one ``test_only_participants_still_
    have_a_gender_column`` above pins.
    """
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, gender, year, department) "
                    "VALUES (gen_random_uuid(), 'bad-gender', 'X', "
                    "'bad-gender@example.com', 'Male', 2, 'Class')"
                )
            )
    assert "ck_accounts_gender" in str(excinfo.value)


def test_postgresql_refuses_an_interest_outside_the_vocabulary(db):
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, year, department, gender, interests) "
                    "VALUES (gen_random_uuid(), 'bad-interest', 'X', 'bad-int@example.com', "
                    "2, 'Class', 'other', ARRAY['Music', 'skydiving']::varchar[])"
                )
            )
    assert "ck_users_interests_known" in str(excinfo.value)


@pytest.mark.parametrize("year", [0, -1, 13, 99])
def test_postgresql_refuses_a_year_outside_the_range(db, year):
    """The range is the backstop, whatever got past the request layer.

    Read alongside ``test_year_is_bounded_by_range_rather_than_by_length`` in
    ``test_text_length_limits.py``: that one shows the API refuses these, and this one shows what
    happens if something reaches the database without going through the API -- a seed script, a
    migration, a psql session.

    ``0`` and ``13`` are the interesting cases. Both are integers, so a column with no range at
    all would store them happily, and either one names a cohort that can never match anybody: the
    operator would be told the save worked and left with a silently empty pool.
    """
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, year, department, gender) "
                    "VALUES (gen_random_uuid(), :regno, 'X', :email, :year, 'Class', 'other')"
                ),
                {"regno": f"bad-year-{year}", "email": f"bad-year-{year}@example.com", "year": year},
            )
    assert f"ck_users_year_known_{USER_YEAR_MAX}" in str(excinfo.value)


@pytest.mark.parametrize("year", ["2nd Year", "2nd year", ""])
def test_postgresql_refuses_a_year_that_is_not_a_number_at_all(db, year):
    """The column is an ``int``, so the vocabulary it used to hold cannot be stored at all.

    These are all readable as a year to somebody, which is exactly why they are worth a test: they
    are what a stale client, an old seed script or a hand-edited migration would send now that the
    column holds numbers. They are refused by the *type*, before ``ck_users_year_known_{USER_YEAR_MAX}`` is ever
    consulted, which is why this test asserts the constraint is not mentioned -- a check that
    caught these would be a check on the wrong thing, and would stop being the thing it was written
    to be the moment the type changed.
    """
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, year, department, gender) "
                    "VALUES (gen_random_uuid(), :regno, 'X', :email, :year, 'Class', 'other')"
                ),
                {"regno": f"bad-year2-{year}", "email": f"bad-year2-{year}@example.com", "year": year},
            )
    message = str(excinfo.value)
    assert "integer" in message
    assert f"ck_users_year_known_{USER_YEAR_MAX}" not in message


@pytest.mark.parametrize(
    ("year", "stored"),
    [("2", 2), (2.5, 2)],
)
def test_postgresql_silently_coerces_a_numeric_year_rather_than_refusing_it(db, year, stored):
    """The near-misses the database does *not* catch, which is why the API type check matters.

    A string of digits and a float both survive the trip into an ``integer`` column, because
    PostgreSQL casts on assignment rather than rejecting. Nothing here raises,
    ``ck_users_year_known_{USER_YEAR_MAX}`` is satisfied, and the row is stored.

    That is a trap worth pinning down rather than leaving to a reading of the PostgreSQL manual. A
    stale client that sends ``year: "2"`` gets no error and a plausible answer, and one that sends
    ``year: 2.5`` gets a participant filed under a year nobody chose.

    The stored value for 2.5 is 2, and that is a property of the *driver* rather than of the
    column: psycopg sends a Python float as ``float8``, and the float-to-integer cast truncates.
    The same literal typed as ``numeric`` rounds to 3 instead (``2.5::numeric::integer`` is 3,
    ``2.5::float8::integer`` is 2). So which year a fractional value lands in is decided by the
    parameter type the client happened to use, and nothing in the database reports that choice.

    This is not a claim that the database is doing its job. It is the reason the closed ``Year``
    type in ``models.py`` is load-bearing rather than decorative: the request layer is what
    refuses a fractional or string year, and these values are exactly what it refuses on the way in.
    """
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (gen_random_uuid(), :regno, 'X', :email, :year, 'Class', 'other')"
            ),
            {"regno": f"coerced-year-{year}", "email": f"coerced-year-{year}@example.com", "year": year},
        )
        stored_year = connection.execute(
            text("SELECT year FROM users WHERE regno = :regno"),
            {"regno": f"coerced-year-{year}"},
        ).scalar_one()
    assert stored_year == stored


def test_postgresql_accepts_every_year_in_the_range(db):
    """A constraint that rejected the legal values would pass the tests above for the wrong reason."""
    from app.limits import USER_YEAR_MAX, USER_YEAR_MIN

    with db.engine.begin() as connection:
        for year in range(USER_YEAR_MIN, USER_YEAR_MAX + 1):
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, year, department, gender) "
                    "VALUES (gen_random_uuid(), :regno, 'X', :email, :year, 'Class', 'other')"
                ),
                {
                    "regno": f"good-year-{year}",
                    "email": f"good-year-{year}@example.com",
                    "year": year,
                },
            )


def test_a_department_is_free_text(db):
    """Unlike year, the department has no list, because the list belongs to the institution.

    Asserted explicitly so that tightening this later is a deliberate change with a test to
    update, rather than a drive-by edit that quietly starts refusing real departments.
    """
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (gen_random_uuid(), 'odd-dept', 'X', 'odd@example.com', "
                "1, 'Electrical & Communication Engineering', 'other')"
            )
        )


def test_postgresql_refuses_a_null_interest_list(db):
    """NULL is not the same as an empty list.

    Two things turn a NULL list down here, and the order matters. The column is declared NOT
    NULL, so PostgreSQL refuses the insert on that before it looks at any CHECK -- which is why
    this test asserts only that the write is refused, and not which of the two did it.

    The CHECK's own ``interests IS NOT NULL`` arm is deliberate redundancy. A containment
    check evaluates to NULL for a NULL column, and a CHECK passes when it evaluates to NULL,
    so without that arm the constraint would be satisfied by exactly the value it exists to
    exclude. Belt and braces, because the arm is what keeps the constraint correct if the
    column is ever relaxed -- which is the kind of change that happens years later, by
    somebody who is not looking at this comment.
    """
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, year, department, gender, interests) "
                    "VALUES (gen_random_uuid(), 'null-interests', 'X', "
                    "'null-int@example.com', 2, 'Class', 'other', NULL)"
                )
            )
    assert "interests" in str(excinfo.value)


def test_the_full_vocabulary_is_accepted_by_the_column(db):
    """The constraint must admit all twelve, not merely reject the rest."""
    from app.limits import INTEREST_VALUES

    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender, interests) "
                "VALUES (gen_random_uuid(), 'all-interests', 'X', 'all-int@example.com', "
                "2, 'Class', 'other', CAST(:interests AS varchar[]))"
            ),
            {"interests": list(INTEREST_VALUES)},
        )


def test_an_empty_interest_list_is_accepted(db):
    """No interests selected is a legitimate answer and must not be treated as missing."""
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender, interests) "
                "VALUES (gen_random_uuid(), 'no-interests', 'X', 'no-int@example.com', "
                "2, 'Class', 'other', ARRAY[]::varchar[])"
            )
        )


# ------------------------------------------ what a new account starts life holding


def test_a_created_account_starts_from_the_declared_defaults(admin_client, db):
    """Every column the payload no longer carries still has to have a value.

    The admin no longer chooses these, so the default moves from the request to the column
    declaration in ``db_models`` -- which is the only place left that can be sure. Asserting
    the row rather than the response matters: the response is assembled by a serializer with
    its own fallbacks, and it is the row that matchmaking, love notes and notifications read.
    """
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": "defaults",
            "name": "Defaulted",
            "email": "defaults@example.com",
            "year": 2, "department": "Class",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text

    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.id == created.json()["id"]))
        # "No picture" is one state held in two columns, so both are asserted: an id with no
        # format would render a broken image rather than no avatar at all.
        assert row.profile_picture_public_id is None
        assert row.profile_picture_format is None
        assert row.emoji is None
        assert row.bio is None
        assert list(row.interests) == []
        assert row.notifications_enabled is True
        assert row.matchmaking_enabled is True
        assert row.love_notes_receive_enabled is True
        # Off until the participant sends their first love note, which is the only other thing
        # that sets it.
        assert row.love_notes_send_enabled is False
        assert row.is_blocked is False


def test_a_created_participant_agrees_with_a_self_registered_one(db):
    """The defaults cannot drift from the participant path, because both read the column.

    ``UserDetails`` states the same four booleans in Pydantic for the sign-up path. Two
    declarations of the same default is a risk, so this compares the two against the same
    database default rather than trusting either to stay put.
    """
    from app.models import UserDetails

    declared = UserDetails.model_fields
    assert declared["notifications_enabled"].default is True
    assert declared["matchmaking_enabled"].default is True
    assert declared["love_notes_receive_enabled"].default is True
    assert declared["love_notes_send_enabled"].default is False


# --------------------------------------------------------------------- the picture is not an admin's


def test_the_admin_payloads_no_longer_accept_a_picture_url():
    """A URL in a JSON body is an arbitrary string pointing anywhere.

    Both the field and the upload route are gone, so there is no admin-side path to a picture
    at all. The model assertion is about what the API advertises; the 422 is asserted by
    ``test_an_admin_cannot_write_a_field_the_account_owns`` above, and the 404 by
    ``test_the_admin_picture_route_is_gone`` below.
    """
    assert "profile_picture_url" not in AdminUserCreate.model_fields
    assert "profile_picture_url" not in AdminUserUpdate.model_fields


def test_a_created_account_starts_with_no_picture(admin_client):
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": "no-picture",
            "name": "Faceless",
            "email": "faceless@example.com",
            "year": 2, "department": "Class",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["profile_picture_url"] is None


@pytest.mark.parametrize("caller", ["client", "admin_client"])
def test_the_admin_picture_route_is_gone(request, caller):
    """404, for everyone, with or without the capability.

    The route used to be the admin's way to set any account's picture. Removing it is what
    makes the restriction real: there is no endpoint left to call, so the picture can only
    arrive as bytes from the participant it belongs to.
    """
    if caller == "client":
        # The override is popped rather than swapped for the admin fixture: asking for
        # ``admin_client`` here would install *its* override after the participant one, and the
        # request would go out as an admin.
        app.dependency_overrides.pop(get_current_user, None)
        try:
            response = request.getfixturevalue(caller).put(
                f"/admin/users/{'0' * 8}-0000-0000-0000-000000000000/picture",
                files={"file": ("me.png", io.BytesIO(b"x"), "image/png")},
            )
        finally:
            app.dependency_overrides[get_current_user] = override_get_current_user
    else:
        response = request.getfixturevalue(caller).put(
            f"/admin/users/{'0' * 8}-0000-0000-0000-000000000000/picture",
            files={"file": ("me.png", io.BytesIO(b"x"), "image/png")},
        )
    assert response.status_code == 404
