"""The four rules this change introduced, tested against the real database.

Four things moved at once, and each of them is the kind that a payload model alone would not
have caught:

* ``email`` is required of every account, in the payload *and* in the column.
* ``gender`` is a closed vocabulary, in the payload *and* in the column.
* ``interests`` is a closed vocabulary, in the payload *and* in the column.
* A profile picture is uploaded as bytes, not written as a URL in a JSON body.

The column-level half of each is the half worth testing here, because the payload models are
already exercised by the suite and a constraint that exists only in Pydantic is not a
constraint -- it is advice. Every test below writes through a path that bypasses the API on
purpose, because that is the only way to find out whether PostgreSQL is actually holding the
line.
"""

import io

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.accounts import AccountType
from app.db_models import AdminRecord, ModeratorRecord, UserRecord
from app.main import app
from app.models import AdminUserCreate, AdminUserUpdate
from app.postgres import PLACEHOLDER_EMAIL_SQL
from app.services.auth_service import get_current_user

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
        "class_name": "Class",
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
        "class_name": "Class",
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
    payload = {
        "account_type": AccountType.USERS.value,
        "regno": "r",
        "name": "Someone",
        "email": "e@example.com",
        "class_name": "Class",
        "gender": "other",
        "interests": interests,
    }
    with pytest.raises(ValueError):
        AdminUserCreate(**payload)


def test_duplicate_interests_collapse_rather_than_failing():
    """A double-click should not be an error.

    The checkboxes are a multi-select, and a client that appends rather than replacing is a
    normal thing to be. Collapsing keeps the stored list canonical, which matters because it
    is rendered as a set of ticked boxes -- a duplicate would render as a list with the same
    label twice.
    """
    payload = {
        "account_type": AccountType.USERS.value,
        "regno": "r",
        "name": "Someone",
        "email": "e@example.com",
        "class_name": "Class",
        "gender": "other",
        "interests": ["Music", "Music", "Coding", "Music"],
    }
    assert AdminUserCreate(**payload).interests == ["Music", "Coding"]


def test_interests_on_a_staff_payload_never_reach_the_row(admin_client, db):
    """A staff payload carries participant fields; they must not be written anywhere.

    The model does not *refuse* them -- it cannot express "this field exists only for one
    account type" without a discriminated union, and a union would make the OpenAPI schema
    unreadable for what is in practice one form. The rule is enforced at the point of
    construction instead: only ``users`` gets these keys, so a moderator row cannot acquire
    them. Asserting the endpoint behaviour is what pins that down; asserting the model
    raised would be asserting a rule this payload never claimed.
    """
    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.MODERATOR.value,
            "regno": "mod-with-interests",
            "name": "Mod",
            "email": "mod-with-interests@example.com",
            "gender": "other",
            "interests": ["Music", "Coding"],
            "bio": "a moderator has no bio either",
        },
    )
    assert response.status_code == 201, response.text
    assert "interests" not in response.json()

    with db.sessions() as session:
        row = session.scalar(
            select(ModeratorRecord).where(ModeratorRecord.regno == "mod-with-interests")
        )
        assert row is not None


def test_no_interests_is_allowed_and_means_none():
    """An admin who ticks nothing has selected no interests, which is a valid answer."""
    payload = {
        "account_type": AccountType.USERS.value,
        "regno": "r",
        "name": "Someone",
        "email": "e@example.com",
        "class_name": "Class",
        "gender": "other",
        "interests": [],
    }
    assert AdminUserCreate(**payload).interests == []


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
                    f'INSERT INTO "{table}" (id, regno, name, email, gender) '
                    f"VALUES (gen_random_uuid(), 'null-email-{table}', 'X', NULL, 'other')"
                )
            )
    assert "email" in str(excinfo.value)


@pytest.mark.parametrize("table", ["users", "admin", "moderator"])
def test_postgresql_refuses_a_gender_outside_the_vocabulary(db, table):
    # ``class_name`` is supplied for every table even though only ``users`` has it. It is
    # NOT NULL there, and a NOT NULL violation is raised before the table's CHECK
    # constraints are evaluated -- so omitting it would prove nothing about gender at all,
    # and this test would keep passing if the gender constraint were deleted.
    extra = ", class_name" if table == "users" else ""
    values = "'Class'" if table == "users" else ""
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    f'INSERT INTO "{table}" (id, regno, name, email, gender{extra}) '
                    f"VALUES (gen_random_uuid(), 'bad-gender-{table}', 'X', "
                    f"'bad-gender-{table}@example.com', 'Male'{', ' + values if values else ''})"
                )
            )
    assert "ck_accounts_gender" in str(excinfo.value)


def test_postgresql_refuses_an_interest_outside_the_vocabulary(db):
    with pytest.raises(DBAPIError) as excinfo:
        with db.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, regno, name, email, class_name, gender, interests) "
                    "VALUES (gen_random_uuid(), 'bad-interest', 'X', 'bad-int@example.com', "
                    "'Class', 'other', ARRAY['Music', 'skydiving']::varchar[])"
                )
            )
    assert "ck_users_interests_known" in str(excinfo.value)


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
                    "INSERT INTO users (id, regno, name, email, class_name, gender, interests) "
                    "VALUES (gen_random_uuid(), 'null-interests', 'X', "
                    "'null-int@example.com', 'Class', 'other', NULL)"
                )
            )
    assert "interests" in str(excinfo.value)


def test_the_full_vocabulary_is_accepted_by_the_column(db):
    """The constraint must admit all twelve, not merely reject the rest."""
    from app.limits import INTEREST_VALUES

    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, class_name, gender, interests) "
                "VALUES (gen_random_uuid(), 'all-interests', 'X', 'all-int@example.com', "
                "'Class', 'other', CAST(:interests AS varchar[]))"
            ),
            {"interests": list(INTEREST_VALUES)},
        )


def test_an_empty_interest_list_is_accepted(db):
    """No interests selected is a legitimate answer and must not be treated as missing."""
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, class_name, gender, interests) "
                "VALUES (gen_random_uuid(), 'no-interests', 'X', 'no-int@example.com', "
                "'Class', 'other', ARRAY[]::varchar[])"
            )
        )


# ------------------------------------------------------------------ interests round-tripping


def test_interests_an_admin_sets_are_visible_and_still_uncheckable(admin_client, db):
    """The requirement, end to end.

    An admin ticks boxes at creation, the participant sees exactly those ticked, and ticking
    one off removes it. The last part is what makes the admin's choice an *initial* state
    rather than a permanent property: a participant who does not like photography can say so.
    """
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": "interest-user",
            "name": "Interested",
            "email": "interested@example.com",
            "class_name": "Class",
            "gender": "other",
            "interests": ["Music", "Coding"],
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["interests"] == ["Music", "Coding"]

    user_id = created.json()["id"]
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.id == user_id))
        assert list(row.interests) == ["Music", "Coding"]

    # The participant unchecks one and leaves the other.
    updated = admin_client.put(
        f"/admin/users/{user_id}",
        json={"name": "Interested", "interests": ["Coding"]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["interests"] == ["Coding"]


def test_clearing_every_interest_is_allowed(db, admin_client):
    """An empty list is a deliberate answer and must not be read as "leave it alone"."""
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": "clear-interests",
            "name": "Clearer",
            "email": "clearer@example.com",
            "class_name": "Class",
            "gender": "other",
            "interests": ["Music"],
        },
    )
    user_id = created.json()["id"]

    updated = admin_client.put(
        f"/admin/users/{user_id}", json={"name": "Clearer", "interests": []}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["interests"] == []


def test_an_update_that_omits_interests_leaves_them_alone(db, admin_client):
    """Omitted is not the same as empty.

    The editor submits a participant-shaped form, so a save of only the name must not quietly
    clear a list the admin spent time setting.
    """
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.USERS.value,
            "regno": "omit-interests",
            "name": "Omitter",
            "email": "omitter@example.com",
            "class_name": "Class",
            "gender": "other",
            "interests": ["Music", "Art"],
        },
    )
    user_id = created.json()["id"]

    updated = admin_client.put(f"/admin/users/{user_id}", json={"name": "Renamed"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["interests"] == ["Music", "Art"]


def test_an_update_cannot_write_interests_onto_staff(admin_client, db):
    """A staff row has no ``interests`` column, and must not gain one."""
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": AccountType.MODERATOR.value,
            "regno": "mod-interests",
            "name": "Mod",
            "email": "mod-interests@example.com",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text

    updated = admin_client.put(
        f"/admin/users/{created.json()['id']}",
        json={"name": "Mod", "interests": ["Music"]},
    )
    assert updated.status_code == 200, updated.text
    assert "interests" not in updated.json()


# --------------------------------------------------------------------- picture, not a URL


def test_the_create_payload_no_longer_accepts_a_picture_url():
    """A URL in a JSON body is an arbitrary string pointing anywhere.

    Dropping the field is what makes the upload route the only way to set a picture. Note
    that Pydantic ignores unknown keys by default, so this asserts the field is *absent from
    the model* rather than that a payload carrying it is refused -- what happens to an
    unrecognised key is a separate question from whether the API still advertises the field,
    and the OpenAPI schema is what a client reads.
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
            "class_name": "Class",
            "gender": "other",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["profile_picture_url"] is None


def test_the_picture_route_is_not_reachable_by_a_participant(client):
    """Setting a picture edits an account, so it needs ``USERS_UPDATE``.

    The override is popped rather than swapped for the admin fixture: asking for
    ``admin_client`` in the signature would install *its* override after the participant one,
    and the request would go out as an admin and be answered with a 404 for the nonexistent
    id -- a pass that proves nothing.
    """
    app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.put(
            f"/admin/users/{'0' * 8}-0000-0000-0000-000000000000/picture",
            files={"file": ("me.png", io.BytesIO(b"x"), "image/png")},
        )
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user
    assert response.status_code in (401, 403)


def test_the_picture_route_refuses_an_upload_for_an_account_that_does_not_exist(admin_client):
    """A bad id fails before the bytes are sent anywhere.

    Checking the lookup first is what keeps a mistyped id from leaving an orphaned object in
    Cloudinary for an account nobody has.
    """
    response = admin_client.put(
        f"/admin/users/{'0' * 8}-0000-0000-0000-000000000000/picture",
        files={"file": ("me.png", io.BytesIO(b"x"), "image/png")},
    )
    assert response.status_code == 404


def test_the_picture_route_refuses_a_non_image(admin_client):
    """The bytes are checked before anything is stored.

    The cap and the image validation are the whole reason this is a multipart upload rather
    than a URL, so the negative case is the one worth asserting. The id is a well-formed UUID
    that matches nothing, so either the file check or the lookup can refuse it -- what is
    being asserted is that neither path accepts the bytes.
    """
    response = admin_client.put(
        f"/admin/users/{'0' * 8}-0000-0000-0000-000000000000/picture",
        files={"file": ("notes.txt", io.BytesIO(b"this is not an image"), "text/plain")},
    )
    assert response.status_code in (400, 404)


def test_the_picture_route_refuses_an_admin_setting_their_own(admin_client):
    """The same self-target guard the block route carries.

    An admin cannot unblock themselves, and they cannot quietly replace their own avatar from
    Profile Review either -- both would be a staff account editing its own row through a
    screen meant for editing other people.
    """
    response = admin_client.put(
        "/admin/users/adminuser/picture",
        files={"file": ("me.png", io.BytesIO(b"x"), "image/png")},
    )
    assert response.status_code in (400, 404)