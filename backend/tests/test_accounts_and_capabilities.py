"""Tests for the account-table vocabulary and the capability split between admin and moderator.

The rule under test replaced a ``role`` column with the table a row lives in: there are
exactly three account tables (``users``, ``admin``, ``moderator``), the table name is matched
exactly, and any value that is not one of them is resolved to the least-privileged kind rather
than trusted. Moderators get the moderation tools, but never user management or session reads.

Where the old suite asserted on a column, this one asserts on the schema and on the model
shape: a staff account cannot hold participant fields, and there is no field anywhere for a
caller to set a privilege with.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.accounts import (
    ACCOUNT_TYPE_CAPABILITIES,
    ACCOUNT_TYPE_VALUES,
    ADMIN_ONLY_CAPABILITIES,
    MODERATION_CAPABILITIES,
    STAFF_ACCOUNT_TYPES,
    AccountType,
    Capability,
    capabilities_for,
    has_capability,
    is_staff,
    is_valid_account_type,
    normalize_account_type,
)
from app.db_models import (ACCOUNT_MODELS, AdminRecord, ModeratorRecord, UserRecord,
                          find_account_in_table)
from app.models import AccountDetails, AdminUserCreate, AdminUserUpdate, UserDetails


# --- the vocabulary itself -------------------------------------------------------


def test_exactly_three_account_tables_exist():
    """A guard against the vocabulary quietly growing a fourth staff table.

    Every mapping and check in the app is written against this closed set, so a new member
    would silently resolve as an unrecognised value and be treated as a participant.
    """
    assert ACCOUNT_TYPE_VALUES == ("users", "admin", "moderator")


def test_account_type_values_are_the_physical_table_names():
    """The discriminator a caller carries is the table a lookup reads.

    This is a deliberate design constraint: a friendlier label would need a second mapping
    back to the schema, and a disagreement between the two is indistinguishable from a
    privilege escalation.
    """
    assert ACCOUNT_MODELS["users"].__tablename__ == "users"
    assert ACCOUNT_MODELS["admin"].__tablename__ == "admin"
    assert ACCOUNT_MODELS["moderator"].__tablename__ == "moderator"
    assert set(ACCOUNT_MODELS) == set(ACCOUNT_TYPE_VALUES)


@pytest.mark.parametrize("account_type", ["users", "admin", "moderator"])
def test_canonical_account_types_are_valid(account_type):
    assert is_valid_account_type(account_type)
    assert normalize_account_type(account_type) == account_type


@pytest.mark.parametrize(
    "stored",
    [
        "",
        " ",
        "Users",
        "USER",
        "Admin",
        "ADMIN",
        "MODERATOR",
        "Moderator",
        " admin",
        "admin ",
        "owner",
        "superadmin",
        "staff",
        "mod",
        "user\x00",
        None,
        1,
        True,
    ],
)
def test_non_canonical_account_types_are_rejected(stored):
    """Case and whitespace variants must not smuggle in an elevated account kind.

    A case-insensitive match is how "Admin" turns into an authorisation bypass, and a
    trailing space defeats naive string equality without tripping any leniency check.
    """
    assert not is_valid_account_type(stored)
    assert normalize_account_type(stored) == AccountType.USERS.value


def test_normalize_survives_an_exploding_object():
    """A junk value must not be able to take down the auth path.

    Anything that raises on comparison or attribute access is treated as unrecognised rather
    than propagated, because this runs while resolving who the caller is.
    """

    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("boom")

        def __hash__(self):
            raise RuntimeError("boom")

    assert normalize_account_type(Hostile()) == AccountType.USERS.value


# --- capability matrix -----------------------------------------------------------


def test_participants_have_no_capabilities():
    assert capabilities_for(AccountType.USERS) == frozenset()
    assert capabilities_for("users") == frozenset()


def test_admin_holds_every_capability():
    """Admin is the superset. This catches a capability that is granted to nobody."""
    assert capabilities_for(AccountType.ADMIN) == frozenset(capability.value for capability in Capability)


def test_moderator_is_unambiguously_neither_participant_nor_admin():
    """The interesting case: a moderator has a real, non-empty, non-total grant.

    An empty set would mean moderators cannot moderate; a full set would mean the admin-only
    boundary has leaked.
    """
    caps = capabilities_for(AccountType.MODERATOR)
    assert caps == frozenset(MODERATION_CAPABILITIES)
    assert caps
    assert caps != frozenset(capability.value for capability in Capability)


def test_admin_only_capabilities_do_not_overlap_moderation():
    """The two sets are defined as complements, so an overlap would mean a leak.

    ADMIN_ONLY_CAPABILITIES is the half admin gets that moderator does not; a capability in
    both sets would be moderation that is not actually admin-only.
    """
    assert not (MODERATION_CAPABILITIES & ADMIN_ONLY_CAPABILITIES)
    assert MODERATION_CAPABILITIES | ADMIN_ONLY_CAPABILITIES == frozenset(
        capability.value for capability in Capability
    )


@pytest.mark.parametrize("capability", sorted(ADMIN_ONLY_CAPABILITIES))
def test_moderator_lacks_every_admin_only_capability(capability):
    assert not has_capability(AccountType.MODERATOR, capability)
    assert has_capability(AccountType.ADMIN, capability)


@pytest.mark.parametrize("capability", sorted(MODERATION_CAPABILITIES))
def test_moderator_holds_every_moderation_capability(capability):
    assert has_capability(AccountType.MODERATOR, capability)


@pytest.mark.parametrize("capability", sorted(Capability, key=str))
def test_no_capability_implies_admin(capability):
    """Asymmetric check, so a capability added to one side but not the other is caught."""
    admin_can = has_capability(AccountType.ADMIN, capability)
    moderator_can = has_capability(AccountType.MODERATOR, capability)
    assert admin_can
    assert moderator_can == (capability.value in MODERATION_CAPABILITIES)


@pytest.mark.parametrize("stored", ["", "Admin", "owner", None])
def test_capabilities_fail_closed_on_unrecognised_account_types(stored):
    """An unreadable account kind grants nothing at all, not even the moderator subset."""
    assert capabilities_for(stored) == frozenset()
    assert not any(has_capability(stored, capability) for capability in Capability)


def test_is_staff_matches_the_staff_table_set():
    """Staff-ness is a property of the table, not of a capability grant."""
    assert is_staff(AccountType.ADMIN)
    assert is_staff(AccountType.MODERATOR)
    assert not is_staff(AccountType.USERS)
    assert not is_staff("Admin")
    assert STAFF_ACCOUNT_TYPES == frozenset({"admin", "moderator"})


def test_account_type_capabilities_has_a_row_for_every_table():
    """A missing row would be a KeyError on the read path rather than a closed gate."""
    assert set(ACCOUNT_TYPE_CAPABILITIES) == set(ACCOUNT_TYPE_VALUES)


# --- schema shape ----------------------------------------------------------------


def test_staff_tables_do_not_hold_participant_columns():
    """The split is only meaningful if a staff row cannot carry participant fields.

    ``email`` is deliberately absent from this set. It is how a person is reached, which is
    true of a moderator as much as a student, so it is a shared column on all three tables --
    see :func:`test_every_account_table_can_hold_an_email` for what that does and does not
    imply.

    ``gender`` and the two profile-picture columns are in the set because they were moved
    out of the shared mixin: gender is a matchmaking filter and a picture only ever arrives
    through a participant's own upload route, so neither has anything to say about a staff
    row. The CHECK that closes gender's vocabulary went with it -- ``ck_accounts_gender`` is
    declared on ``users`` alone, asserted below.
    """
    participant_only = {
        "bio", "year", "department", "interests",
        "matchmaking_enabled", "love_notes_receive_enabled",
        "love_notes_send_enabled", "last_matchmaking_at",
        "gender", "profile_picture_public_id", "profile_picture_format",
    }
    for model in (AdminRecord, ModeratorRecord):
        assert participant_only.isdisjoint(model.__table__.columns.keys())


def test_the_gender_constraint_is_declared_on_users_alone():
    """The vocabulary check follows the column, and the column is participant-only.

    Asserted separately because a constraint is easy to leave behind: ``apply_check_constraints``
    only ever adds what is missing, so a database created before the move would keep the old
    staff-level constraint even if the models stopped declaring it.
    """
    assert "ck_accounts_gender" in {c.name for c in UserRecord.__table__.constraints}
    for model in (AdminRecord, ModeratorRecord):
        assert "ck_accounts_gender" not in {
            c.name for c in model.__table__.constraints
        }, model.__tablename__


def test_every_account_table_can_hold_an_email():
    """All three tables carry an address, and all three require one."""
    for model in (UserRecord, AdminRecord, ModeratorRecord):
        assert "email" in model.__table__.columns, model.__tablename__
        # NOT NULL on every table, staff included. It was nullable before, on the reasoning
        # that minting a staff account is a deliberate act by somebody who already knows the
        # regno; the column is how the holder is contacted, and the cost of requiring it is
        # one field on a form the operator is already filling in.
        assert not model.__table__.columns["email"].nullable, model.__tablename__


def test_email_uniqueness_is_declared_per_table_not_across_tables():
    """The constraint lives on each table's own column, so the three never collide.

    Stated as a property of the schema rather than of a test run, because that is what makes
    an address shared between a participant and a moderator safe: nothing in the database
    treats it as a conflict, and nothing in the application looks an account up by email.
    """
    for model in (UserRecord, AdminRecord, ModeratorRecord):
        email_column = model.__table__.columns["email"]
        assert email_column.unique is True, model.__tablename__
        assert email_column.index is True, model.__tablename__

    # Sanity check on the reasoning above: if any code path resolved an account by email,
    # a shared address would be an ambiguity there. Sign-in is regno-based and sessions are
    # pinned by primary key, so it is not.
    from app.db_models import resolve_account_by_regno, resolve_account
    assert "email" not in resolve_account_by_regno.__code__.co_varnames
    assert "email" not in resolve_account.__code__.co_varnames


def test_no_account_table_has_a_role_column():
    """There is no column a privilege could be stored in, which is the point.

    Asserted across all three tables, and it has to stay a *schema* assertion rather than a
    behavioural one: a role column that nothing writes is harmless today and is exactly how
    one that something writes arrives later.
    """
    for model in (UserRecord, AdminRecord, ModeratorRecord):
        assert "role" not in model.__table__.columns


def test_report_counts_live_on_participants_only():
    """The six report counters exist on ``users`` and on neither staff table.

    A staff account has no content in anybody's report queue, so a ``confession_removal_count``
    on ``moderator`` would be a column that reads 0 forever -- indistinguishable, in a staff
    list rendered beside participants, from a moderator with a clean record. Same reasoning as
    ``bio`` being participant-only, and asserted separately from ``role`` above
    because this one is a deliberate feature rather than an absence.

    Two columns per kind of content, and never a single combined total: ``reports`` counts
    complaints and ``removals`` counts upheld ones, and the second is the number an
    administrator is meant to act on. See ``UserRecord``.
    """
    expected = {
        "confession_report_count", "confession_removal_count",
        "love_note_report_count", "love_note_removal_count",
        "message_report_count", "message_removal_count",
    }
    assert expected <= set(UserRecord.__table__.columns.keys())

    for model in (AdminRecord, ModeratorRecord):
        assert not (expected & set(model.__table__.columns.keys())), model.__tablename__

    # No combined "total reports" column anywhere: it would be the sum of a number that
    # includes dismissed reports and a number that does not, which is the exact comparison
    # that is not meaningful.
    for model in (UserRecord, AdminRecord, ModeratorRecord):
        assert "reported_count" not in model.__table__.columns, model.__tablename__


# --- API payload validation ------------------------------------------------------


def _create_payload(**overrides):
    """A minimally valid create payload, so each test varies only the account type.

    The participant fields are added only when the payload is for a participant. ``gender``,
    ``year`` and ``department`` are ``users`` columns; a staff payload that carried one would
    be exercising the router's drop rule rather than this model's.
    """
    payload = {
        "regno": "r1",
        "name": "Someone",
        "email": "someone@user.com",
    }
    payload.update(overrides)
    if payload.get("account_type", AccountType.USERS.value) == AccountType.USERS.value:
        payload.setdefault("year", 2)
        payload.setdefault("department", "Class")
        payload.setdefault("gender", "other")
    return payload


@pytest.mark.parametrize("account_type", ["users", "admin", "moderator"])
def test_user_create_accepts_canonical_account_types(account_type):
    assert AdminUserCreate(**_create_payload(account_type=account_type)).account_type == AccountType(account_type)


def test_user_create_defaults_to_a_participant():
    assert AdminUserCreate(**_create_payload()).account_type == AccountType.USERS


@pytest.mark.parametrize("account_type", ["", "Admin", "ADMIN", "owner", "superadmin", "mod"])
def test_user_create_rejects_non_canonical_account_types(account_type):
    """Choosing the table is the privilege primitive, so it validates strictly.

    A 422 here is the only thing stopping a typo like "Moderator" from creating an account
    that resolves as a participant and silently has no capabilities.
    """
    with pytest.raises(ValueError):
        AdminUserCreate(**_create_payload(account_type=account_type))


def test_staff_account_create_does_not_require_participant_fields():
    """A moderator row has no gender, year or department, so the payload must not demand any."""
    payload = AdminUserCreate(**_create_payload(account_type="moderator"))
    assert payload.account_type == AccountType.MODERATOR
    assert payload.gender is None
    assert payload.year is None
    assert payload.department is None


def test_participant_create_requires_participant_fields():
    """A ``users`` row without an email, gender, year or department would be unusable and unreadable."""
    with pytest.raises(ValueError):
        AdminUserCreate(**_create_payload(email=None, year=None, department=None, gender=None))


def test_participant_create_requires_a_gender_of_its_own():
    """The column is NOT NULL and matchmaking filters on it, so an absent one is not optional."""
    with pytest.raises(ValueError):
        AdminUserCreate(**_create_payload(gender=None))


def test_user_update_has_no_account_type_field():
    """Editing cannot move a row between tables: it is fixed at creation.

    Eleven other tables hold foreign keys into ``users`` by id and regno, so relocating a
    row would either orphan them or rewrite them silently. The field simply does not exist.
    """
    assert "account_type" not in AdminUserUpdate.model_fields


def test_user_update_allows_omitting_participant_fields():
    """None means "leave it alone"; it must not be read as a request to clear it."""
    assert AdminUserUpdate(name="Renamed").email is None


# --- the value the client is actually given --------------------------------------


def _details_payload(**overrides):
    """A minimally valid UserDetails, so each test varies only the account type."""
    payload = {
        "regno": "u",
        "name": "U",
        "email": "u@user.com",
        "year": 2, "department": "Class",
        "gender": "other",
        "matchmaking_enabled": True,
        "notifications_enabled": True,
    }
    payload.update(overrides)
    return payload


def test_user_details_exposes_capabilities_for_a_moderator():
    # No ``gender``: a staff row has no such column, so ``AccountDetails`` does not carry one.
    details = AccountDetails(
        regno="m", name="M", email="m@example.com", account_type=AccountType.MODERATOR.value
    )
    assert details.account_type == "moderator"
    assert set(details.capabilities) == MODERATION_CAPABILITIES
    assert details.capabilities == sorted(details.capabilities), "should be stable for snapshot tests"
    assert details.is_staff is True


def test_user_details_gives_a_participant_nothing():
    details = UserDetails(**_details_payload())
    assert details.account_type == "users"
    assert details.capabilities == []
    assert details.is_staff is False


def test_account_details_normalises_a_corrupted_stored_type():
    """The important one: a row that predates the check constraint is still demoted.

    This is the read path for every authenticated request, so a legacy or hand-edited value
    cannot retain authority.
    """
    details = AccountDetails(
        regno="u", name="U", email="u@example.com", account_type="Admin"
    )
    assert details.account_type == "users"
    assert details.capabilities == []


# --- database level --------------------------------------------------------------


def _insert_user(db, regno):
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno=regno,
                name=f"User {regno}",
                email=f"{regno}@test.com",
                year=2, department="Test_Class",
                gender="other",
            )
        )
        session.flush()


def test_users_regno_is_unique_within_its_table(db):
    """Each table still enforces its own uniqueness; cross-table uniqueness is app-level."""
    _insert_user(db, "dup-user")
    with pytest.raises(IntegrityError):
        _insert_user(db, "dup-user")


def test_cross_table_regno_uniqueness_is_not_a_database_constraint(db):
    """The database cannot see across the three tables, and no longer needs to.

    Two different tables can each hold the same regno; nothing in PostgreSQL stops it. That
    used to be a gap the create endpoint had to close, because login searched all three tables
    and a shared regno would have resolved to whichever row the search reached first. Sign-in
    is now scoped to one table per page and the session is pinned by primary key, so a shared
    regno resolves correctly on every page and the create endpoint allows it. See
    ``test_sign_in_categories.py``.
    """
    with db.sessions.begin() as session:
        session.add(ModeratorRecord(regno="cross-table", name="M", email="cross-mod@example.com"))
        session.add(AdminRecord(regno="cross-table", name="A", email="cross-admin@example.com"))
        session.flush()


def test_cross_table_email_uniqueness_is_not_a_database_constraint(db):
    """The same reasoning as regno, applied to the address.

    An email may exist as a participant and as a moderator at the same time, because nothing
    resolves an account by it: sign-in is regno-based and a session is pinned to a primary
    key. A shared address is a display fact, not an ambiguity, and the per-table constraint
    is what keeps it unambiguous within a table where a duplicate really would be two rows
    claiming to be the same person.
    """
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno="shared-email-user",
                name="Participant",
                email="shared-address@example.com",
                year=2, department="Test_Class",
                gender="other",
            )
        )
        session.add(
            ModeratorRecord(
                regno="shared-email-mod",
                name="Moderator",
                email="shared-address@example.com",
            )
        )
        session.add(
            AdminRecord(
                regno="shared-email-admin",
                name="Admin",
                email="shared-address@example.com",
            )
        )
        session.flush()


def test_postgresql_still_refuses_a_duplicate_email_within_one_table(db):
    """Where the per-table guarantee actually lives."""
    with db.sessions.begin() as session:
        session.add(
            ModeratorRecord(
                regno="dup-email-a", name="A", email="only-once@example.com"
            )
        )
        session.flush()

    with db.sessions.begin() as session:
        with pytest.raises(IntegrityError):
            session.add(
                ModeratorRecord(
                    regno="dup-email-b", name="B", email="only-once@example.com"
                )
            )
            session.flush()


def test_a_staff_account_can_be_created_with_an_email(admin_client, db):
    """The end-to-end version: a staff row carries an address through the real endpoint."""
    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": "moderator",
            "regno": "mod-with-email",
            "name": "Moderator With Email",
            "email": "mod.contact@example.com",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["email"] == "mod.contact@example.com"
    # The moderator row has no gender column, so the response cannot carry one either.
    assert "gender" not in response.json()
    assert "profile_picture_url" not in response.json()


def test_a_staff_account_cannot_be_created_without_an_email(admin_client, db):
    """Email is required of staff too, so the create payload refuses to omit it."""
    response = admin_client.post(
        "/admin/users",
        json={
            "account_type": "admin",
            "regno": "admin-without-email",
            "name": "Admin Without Email",
        },
    )

    assert response.status_code == 422, response.text
    with db.sessions.begin() as session:
        assert find_account_in_table(session, "admin", "admin-without-email") is None


def test_the_same_email_is_refused_within_one_table_but_allowed_across_tables(admin_client, db):
    """The per-table rule, through the endpoint that has to implement it."""
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": "admin",
            "regno": "email-admin-1",
            "name": "Admin One",
            "email": "reused@example.com",
        },
    )
    assert created.status_code == 201, created.text

    # Same table, same address: two admin rows claiming one address is a real conflict.
    same_table = admin_client.post(
        "/admin/users",
        json={
            "account_type": "admin",
            "regno": "email-admin-2",
            "name": "Admin Two",
            "email": "reused@example.com",
        },
    )
    assert same_table.status_code == 409, same_table.text

    # Different table, same address: allowed, for the reason above.
    other_table = admin_client.post(
        "/admin/users",
        json={
            "account_type": "moderator",
            "regno": "email-mod-1",
            "name": "Moderator One",
            "email": "reused@example.com",
        },
    )
    assert other_table.status_code == 201, other_table.text
    assert other_table.json()["email"] == "reused@example.com"


def test_a_staff_email_is_editable(admin_client, db):
    """`email` left the participant-only field list, so an update to staff writes it."""
    created = admin_client.post(
        "/admin/users",
        json={
            "account_type": "moderator",
            "regno": "mod-email-edit",
            "name": "Editable",
            "email": "before@example.com",
        },
    )
    assert created.status_code == 201, created.text

    account_id = created.json()["id"]
    assert created.json()["email"] == "before@example.com"

    # `name` is sent alongside because the update model inherits a required `name`; the
    # point of the test is the email, and this keeps the payload valid.
    updated = admin_client.put(
        f"/admin/users/{account_id}",
        json={"name": "Editable", "email": "new.address@example.com"},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["email"] == "new.address@example.com"


def test_a_staff_regno_is_absent_from_users(db):
    """The invariant the participant gates rely on: staff have no ``users`` row."""
    with db.sessions.begin() as session:
        session.add(AdminRecord(regno="only-admin", name="A", email="only-admin@example.com"))
        session.flush()
    with db.sessions.begin() as session:
        assert session.scalar(select(UserRecord.id).where(UserRecord.regno == "only-admin")) is None
