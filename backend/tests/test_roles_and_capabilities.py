"""Tests for the exact role vocabulary and the capability split between admin and moderator.

The rule under test is blunt on purpose: there are three roles, they are matched exactly,
and a role the server does not recognise is downgraded to ``user`` rather than being
trusted. Moderators get the moderation tools, but never user management, role changes,
active sessions, or archives.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db_models import UserRecord
from app.models import AdminUserCreate, AdminUserUpdate, UserDetails
from app.roles import (
    ADMIN_ONLY_CAPABILITIES,
    MODERATION_CAPABILITIES,
    ROLE_VALUES,
    Capability,
    Role,
    capabilities_for,
    has_capability,
    is_valid_role,
    is_staff,
    normalize_role,
)


# --- the vocabulary itself -------------------------------------------------------


def test_exactly_three_roles_exist():
    """A guard against the vocabulary quietly growing a 'superadmin' or 'owner'.

    Every mapping and check in the app is written against this closed set, so a new member
    would silently pass through as an unrecognised role and get normalised to user.
    """
    assert ROLE_VALUES == ("user", "admin", "moderator")


@pytest.mark.parametrize("role", ["user", "admin", "moderator"])
def test_canonical_roles_are_valid(role):
    assert is_valid_role(role)
    assert normalize_role(role) == role


@pytest.mark.parametrize(
    "stored",
    [
        "",
        " ",
        "User",
        "Admin",
        "ADMIN",
        "MODERATOR",
        "Moderator",
        " moderator",
        "moderator ",
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
def test_non_canonical_roles_are_rejected(stored):
    """Case and whitespace variants must not smuggle in elevated access.

    A case-insensitive match is how "Admin" turns into an authorisation bypass, and
    a trailing space defeats naive string equality without tripping any leniency check.
    """
    assert not is_valid_role(stored)
    assert normalize_role(stored) == Role.USER.value


def test_normalize_survives_an_exploding_object():
    """A junk column value must not be able to take down the auth path.

    Anything that raises on comparison or attribute access is treated as unrecognised
    rather than propagated, because this runs while resolving who the caller is.
    """

    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("boom")

        def __hash__(self):
            raise RuntimeError("boom")

    assert normalize_role(Hostile()) == Role.USER.value


# --- capability matrix -----------------------------------------------------------


def test_user_has_no_capabilities():
    assert capabilities_for(Role.USER) == frozenset()
    assert capabilities_for("user") == frozenset()


def test_admin_holds_every_capability():
    """Admin is the superset. This catches a capability that is granted to nobody."""
    assert capabilities_for(Role.ADMIN) == frozenset(Capability)


def test_moderator_is_unambiguously_neither_user_nor_admin():
    """The interesting case: a moderator has a real, non-empty, non-total grant.

    An empty set would mean moderators cannot moderate; a full set would mean the
    admin-only boundary has leaked.
    """
    caps = capabilities_for(Role.MODERATOR)
    assert caps == frozenset(MODERATION_CAPABILITIES)
    assert caps
    assert caps != frozenset(Capability)


def test_admin_only_capabilities_do_not_overlap_moderation():
    """The two sets are defined as complements, so an overlap would mean a leak.

    ADMIN_ONLY_CAPABILITIES is the half admin gets that moderator does not; a
    capability in both sets would be moderation that is not actually admin-only.
    """
    assert not (MODERATION_CAPABILITIES & ADMIN_ONLY_CAPABILITIES)
    assert MODERATION_CAPABILITIES | ADMIN_ONLY_CAPABILITIES == frozenset(Capability)


@pytest.mark.parametrize("capability", sorted(ADMIN_ONLY_CAPABILITIES, key=str))
def test_moderator_lacks_every_admin_only_capability(capability):
    assert not has_capability(Role.MODERATOR, capability)
    assert has_capability(Role.ADMIN, capability)


@pytest.mark.parametrize("capability", sorted(MODERATION_CAPABILITIES, key=str))
def test_moderator_holds_every_moderation_capability(capability):
    assert has_capability(Role.MODERATOR, capability)


@pytest.mark.parametrize("capability", sorted(Capability, key=str))
def test_no_capability_implies_admin(capability):
    """Asymmetric check, so a capability added to one side but not the other is caught."""
    admin_can = has_capability(Role.ADMIN, capability)
    moderator_can = has_capability(Role.MODERATOR, capability)
    assert admin_can
    assert moderator_can == (capability in MODERATION_CAPABILITIES)


@pytest.mark.parametrize("stored", ["", "Admin", "owner", None])
def test_capabilities_fail_closed_on_unrecognised_roles(stored):
    """An unreadable role grants nothing at all, not even the moderator subset."""
    assert capabilities_for(stored) == frozenset()
    assert not any(has_capability(stored, capability) for capability in Capability)


def test_is_staff_matches_capability_grants():
    """Staff-ness is derived from capabilities so the two cannot disagree."""
    assert is_staff(Role.ADMIN)
    assert is_staff(Role.MODERATOR)
    assert not is_staff(Role.USER)
    assert not is_staff("Admin")


# --- API payload validation ------------------------------------------------------


def _create_payload(**overrides):
    """A minimally valid create payload, so each test varies only the role."""
    payload = {
        "regno": "r1",
        "name": "Someone",
        "email": "someone@user.com",
        "class_name": "Class",
        "gender": "other",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize("role", ["user", "admin", "moderator"])
def test_user_create_accepts_canonical_roles(role):
    assert AdminUserCreate(**_create_payload(role=role)).role == Role(role)


def test_user_create_defaults_to_user():
    assert AdminUserCreate(**_create_payload()).role == Role.USER


@pytest.mark.parametrize("role", ["", "Admin", "ADMIN", "owner", "superadmin", "mod"])
def test_user_create_rejects_non_canonical_roles(role):
    """Role assignment is the privilege-escalation primitive, so it validates strictly.

    A 422 here is the only thing stopping a typo like "Moderator" from creating an
    account that silently has no capabilities.
    """
    with pytest.raises(ValueError):
        AdminUserCreate(**_create_payload(role=role))


def test_user_update_rejects_non_canonical_roles():
    """Same rule on the update path, which is the one used to change an existing role."""
    with pytest.raises(ValueError):
        AdminUserUpdate(role="Admin")


def test_user_update_allows_omitting_role():
    """role=None means 'leave it alone'; it must not be read as a request to clear it."""
    assert AdminUserUpdate(name="Renamed").role is None


# --- the value the client is actually given --------------------------------------


def _details_payload(**overrides):
    """A minimally valid UserDetails, so each test varies only the role."""
    payload = {
        "regno": "u",
        "name": "U",
        "email": "u@user.com",
        "class_name": "Class",
        "gender": "other",
        "matchmaking_enabled": True,
        "notifications_enabled": True,
    }
    payload.update(overrides)
    return payload


def test_user_details_exposes_capabilities_for_a_moderator():
    details = UserDetails(**_details_payload(role="moderator"))
    assert details.role == "moderator"
    assert set(details.capabilities) == MODERATION_CAPABILITIES
    assert details.capabilities == sorted(details.capabilities), "should be stable for snapshot tests"


def test_user_details_gives_a_user_nothing():
    assert UserDetails(**_details_payload(role="user")).capabilities == []


def test_user_details_normalises_a_corrupted_stored_role():
    """The important one: a row that predates the check constraint is still demoted.

    This is the read path for every authenticated request, so a legacy or hand-edited
    role cannot retain authority.
    """
    details = UserDetails(**_details_payload(role="Admin"))
    assert details.role == "user"
    assert details.capabilities == []


def test_user_details_role_defaults_to_user_when_absent():
    assert UserDetails(**_details_payload()).role == "user"


# --- database level --------------------------------------------------------------


def _insert_user(db, regno, role):
    with db.sessions.begin() as session:
        session.add(
            UserRecord(
                regno=regno,
                name=f"User {regno}",
                email=f"{regno}@test.com",
                class_name="Test_Class",
                gender="other",
                role=role,
            )
        )
        session.flush()


def _stored_role(db, regno):
    with db.sessions.begin() as session:
        return session.execute(select(UserRecord.role).where(UserRecord.regno == regno)).scalar_one()


def test_user_record_column_rejects_an_invalid_role(db):
    """The database is the last line of defence if validation is ever bypassed.

    This is what protects the guarantee if some future code path writes a role without
    going through a Pydantic model. The check constraint exists precisely so that a
    hand-edited row or a direct SQL script cannot create a role the app would have
    rejected at the edge.
    """
    with pytest.raises(IntegrityError):
        _insert_user(db, "bad-role", "superadmin")


def test_user_record_column_rejects_a_case_variant(db):
    """A CHECK with case-insensitive matching would be the bug, not the feature."""
    with pytest.raises(IntegrityError):
        _insert_user(db, "shouty-role", "Admin")


def test_user_record_column_rejects_an_empty_role(db):
    with pytest.raises(IntegrityError):
        _insert_user(db, "blank-role", "")


def test_user_record_column_accepts_moderator(db):
    _insert_user(db, "good-role", Role.MODERATOR.value)
    assert _stored_role(db, "good-role") == "moderator"
