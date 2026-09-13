"""The application's roles and the capabilities each one carries.

This module is the only place a role is named and a privilege is granted. Every gate, every
column constraint, every Pydantic field and every client-side check derives from what is
declared here, so there is exactly one list to change when a role is added or a privilege is
moved.

Two things this deliberately does not do:

* It does not normalise case. ``"Admin"``, ``"ADMIN"`` and ``""`` are not spellings of
  ``"admin"``; they are not roles at all. Folding them would mean a typo silently granted the
  most privileged role in the system, and a typo in the other direction would silently
  revoke it while the account still looked like staff in the database. An unrecognised value
  is resolved to the least-privileged role instead (see :func:`normalize_role`).
* It does not imply a hierarchy. ``moderator`` is not a lesser ``admin``; it is a different
  set of capabilities, and a moderator cannot reach anything on the admin-only list without
  being granted it explicitly.

Capabilities rather than role equality is what makes that enforceable: a route declares the
one capability it needs, and :func:`has_capability` decides. Adding a fourth role later
cannot accidentally inherit or lose access, because no route ever asks "is this an admin?".
"""

from __future__ import annotations

from enum import Enum


class Role(str, Enum):
    """The complete set of roles. The value is what is stored in ``users.role``."""

    USER = "user"
    ADMIN = "admin"
    MODERATOR = "moderator"


# Ordered for error messages and for the OpenAPI schema, not for any hierarchy.
ROLE_VALUES: tuple[str, ...] = tuple(role.value for role in Role)

# Roles that are staff rather than participants. These accounts are kept out of matchmaking
# and love notes entirely, as both the actor and the candidate, so moderation data is never
# polluted by staff. Membership here grants no capability by itself; it only removes an
# account from the participant pool.
STAFF_ROLE_VALUES: frozenset[str] = frozenset({Role.ADMIN.value, Role.MODERATOR.value})


class Capability(str, Enum):
    """A single, named privilege.

    A route depends on exactly one of these rather than on a role, so "who can do this" is
    answered by reading this enum and the map below instead of by grepping for role strings
    across the codebase.
    """

    # --- content and conversation moderation (admin + moderator) ---
    CONFESSIONS_REVIEW = "confessions:review"
    CONFESSIONS_DELETE = "confessions:delete"
    LOVE_NOTES_REVIEW = "love_notes:review"
    LOVE_NOTES_MODERATE = "love_notes:moderate"
    LOVE_NOTES_DELETE = "love_notes:delete"
    CONVERSATIONS_REVIEW = "conversations:review"
    CONVERSATIONS_READ_MESSAGES = "conversations:read_messages"
    CONVERSATIONS_TERMINATE = "conversations:terminate"
    MATCHMAKING_REVIEW = "matchmaking:review"
    MATCHMAKING_MODERATE = "matchmaking:moderate"
    REPORTS_REVIEW = "reports:review"
    REPORTS_MODERATE = "reports:moderate"
    STATS_READ = "stats:read"

    # --- account and platform administration (admin only) ---
    USERS_REVIEW = "users:review"
    USERS_CREATE = "users:create"
    USERS_UPDATE = "users:update"
    USERS_BLOCK = "users:block"
    SESSIONS_READ = "sessions:read"
    MESSAGES_ARCHIVE = "messages:archive"


# What a moderator can do: review and act on content, and read the aggregate counters that
# describe it. Nothing here can create, edit, block, promote or demote an account, see a
# session, or touch the message archive.
MODERATION_CAPABILITIES: frozenset[str] = frozenset(
    capability.value
    for capability in (
        Capability.CONFESSIONS_REVIEW,
        Capability.CONFESSIONS_DELETE,
        Capability.LOVE_NOTES_REVIEW,
        Capability.LOVE_NOTES_MODERATE,
        Capability.LOVE_NOTES_DELETE,
        Capability.CONVERSATIONS_REVIEW,
        Capability.CONVERSATIONS_READ_MESSAGES,
        Capability.CONVERSATIONS_TERMINATE,
        Capability.MATCHMAKING_REVIEW,
        Capability.MATCHMAKING_MODERATE,
        Capability.REPORTS_REVIEW,
        Capability.REPORTS_MODERATE,
        Capability.STATS_READ,
    )
)

# What only an admin can do. Account lifecycle and role assignment sit here because they are
# the capabilities from which privilege grows: anything that can write ``users.role`` can mint
# an account that holds every other capability on this list.
ADMIN_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    capability.value
    for capability in (
        Capability.USERS_REVIEW,
        Capability.USERS_CREATE,
        Capability.USERS_UPDATE,
        Capability.USERS_BLOCK,
        Capability.SESSIONS_READ,
        Capability.MESSAGES_ARCHIVE,
    )
)

ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    Role.USER.value: frozenset(),
    Role.MODERATOR.value: MODERATION_CAPABILITIES,
    Role.ADMIN.value: MODERATION_CAPABILITIES | ADMIN_ONLY_CAPABILITIES,
}


def is_valid_role(value: object) -> bool:
    """Whether ``value`` is exactly one of the defined roles.

    A plain, case-sensitive membership test. Used for request payloads, where an
    unrecognised value is a client error and must be rejected rather than reinterpreted.
    """
    if isinstance(value, Role):
        return True
    return isinstance(value, str) and value in ROLE_CAPABILITIES


def normalize_role(value: object) -> str:
    """Resolve any stored or supplied value to one of the three canonical role strings.

    Anything not exactly a defined role becomes :attr:`Role.USER`. This is the fail-closed
    direction on purpose: a row written before the vocabulary was enforced, or by a script
    that bypassed the API, must not keep a privilege it was never validly granted -- and
    must certainly not be granted one by guessing at intent.
    """
    if isinstance(value, Role):
        return value.value
    if isinstance(value, str) and value in ROLE_CAPABILITIES:
        return value
    return Role.USER.value


def capabilities_for(role: object) -> frozenset[str]:
    """Every capability ``role`` carries, after resolving it to a defined role."""
    return ROLE_CAPABILITIES[normalize_role(role)]


def has_capability(role: object, capability: Capability | str) -> bool:
    """Whether ``role`` carries ``capability``.

    The single authorization primitive. Accepts a raw stored value, so a caller that has not
    normalised yet cannot accidentally ask the question of an unvalidated string and get a
    privileged answer out of it.
    """
    if isinstance(capability, Capability):
        capability = capability.value
    return capability in capabilities_for(role)


def is_staff(role: object) -> bool:
    """Whether ``role`` is staff rather than a participant account."""
    return normalize_role(role) in STAFF_ROLE_VALUES
