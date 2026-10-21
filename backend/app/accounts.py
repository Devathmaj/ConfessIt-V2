"""The application's account tables and the capabilities each one carries."""

from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple

from .config import settings


class AccountType(str, Enum):
    """The account tables. The value *is* the table name."""

    USERS = "users"
    ADMIN = "admin"
    MODERATOR = "moderator"


# Ordered for error messages and for the OpenAPI schema, not for any hierarchy.
ACCOUNT_TYPE_VALUES: tuple[str, ...] = tuple(account_type.value for account_type in AccountType)

# Staff rather than participants. Kept out of matchmaking and love notes
# entirely, as both the actor and the candidate, so moderation data is never
# polluted by staff. Membership here grants no capability by itself.
STAFF_ACCOUNT_TYPES: frozenset[str] = frozenset(
    {AccountType.ADMIN.value, AccountType.MODERATOR.value}
)


class Capability(str, Enum):
    """A single, named privilege.

    A route depends on exactly one of these rather than on an account kind, so
    "who can do this" is answered by reading this enum and the map below instead
    of grepping for account-table names across the codebase.
    """

    # --- content moderation (admin + moderator) ---
    CONFESSIONS_REVIEW = "confessions:review"
    CONFESSIONS_DELETE = "confessions:delete"
    LOVE_NOTES_REVIEW = "love_notes:review"
    LOVE_NOTES_MODERATE = "love_notes:moderate"
    LOVE_NOTES_DELETE = "love_notes:delete"
    REPORTS_REVIEW = "reports:review"
    REPORTS_MODERATE = "reports:moderate"

    # --- account and platform administration (admin only) ---
    STATS_READ = "stats:read"
    USERS_REVIEW = "users:review"
    USERS_CREATE = "users:create"
    USERS_UPDATE = "users:update"
    USERS_BLOCK = "users:block"
    APP_CONFIG_READ = "app_config:read"
    GAMES_MANAGE = "games:manage"


# What a moderator can do: read every content queue and act on what is in it.
# Nothing here can count the platform, create, edit, block or mint an account --
# the moderator dashboard this backs measures its own backlog from the queue
# endpoints rather than counting the platform behind them.
MODERATION_CAPABILITIES: frozenset[str] = frozenset(
    capability.value
    for capability in (
        Capability.CONFESSIONS_REVIEW,
        Capability.CONFESSIONS_DELETE,
        Capability.LOVE_NOTES_REVIEW,
        Capability.LOVE_NOTES_MODERATE,
        Capability.LOVE_NOTES_DELETE,
        Capability.REPORTS_REVIEW,
        Capability.REPORTS_MODERATE,
    )
)

# What only an admin can do. Account lifecycle sits here because it is the
# capability from which privilege grows: anything that can create an ``admin``
# row can mint an account holding every other capability on this list.
#
ADMIN_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    capability.value
    for capability in (
        Capability.STATS_READ,
        Capability.USERS_REVIEW,
        Capability.USERS_CREATE,
        Capability.USERS_UPDATE,
        Capability.USERS_BLOCK,
        Capability.APP_CONFIG_READ,
        Capability.GAMES_MANAGE,
    )
)

ACCOUNT_TYPE_CAPABILITIES: dict[str, frozenset[str]] = {
    AccountType.USERS.value: frozenset(),
    AccountType.MODERATOR.value: MODERATION_CAPABILITIES,
    AccountType.ADMIN.value: MODERATION_CAPABILITIES | ADMIN_ONLY_CAPABILITIES,
}


def is_valid_account_type(value: object) -> bool:
    """Whether ``value`` is exactly one of the defined account tables.

    A plain, case-sensitive membership test. Used for request payloads, where an
    unrecognised value is a client error and must be rejected rather than reinterpreted.
    """
    if isinstance(value, AccountType):
        return True
    return isinstance(value, str) and value in ACCOUNT_TYPE_CAPABILITIES


def normalize_account_type(value: object) -> str:
    """Resolve any stored or supplied value to one of the canonical account-table names."""
    if isinstance(value, AccountType):
        return value.value
    if isinstance(value, str) and value in ACCOUNT_TYPE_CAPABILITIES:
        return value
    return AccountType.USERS.value


def capabilities_for(account_type: object) -> frozenset[str]:
    """Every capability an account of this kind carries."""
    return ACCOUNT_TYPE_CAPABILITIES[normalize_account_type(account_type)]


def has_capability(account_type: object, capability: Capability | str) -> bool:
    """Whether an account of this kind carries ``capability``.

    The single authorization primitive. Accepts a raw stored value, so a caller that has
    not normalised yet cannot accidentally ask the question of an unvalidated string and
    get a privileged answer out of it.
    """
    if isinstance(capability, Capability):
        capability = capability.value
    return capability in capabilities_for(account_type)


def is_staff(account_type: object) -> bool:
    """Whether this kind of account is staff rather than a participant account."""
    return normalize_account_type(account_type) in STAFF_ACCOUNT_TYPES


# Which ``Settings`` field carries the ceiling for each account table. Resolved
# through ``getattr`` below rather than as three ``if`` branches, so adding an
# account table is one entry here and one field on ``Settings`` -- no second
# switch that can be left describing the old set. Keyed by *table name* because
ACCOUNT_CAP_SETTING_NAMES: dict[str, str] = {
    AccountType.USERS.value: "MAX_USER_COUNT",
    AccountType.ADMIN.value: "MAX_ADMIN_COUNT",
    AccountType.MODERATOR.value: "MAX_MODERATOR_COUNT",
}

# Stable advisory-lock keys, one per account table, so the "is there room" count
# and the insert that depends on it are serialised against each other (see
# ``app.routers.admin.create_user`` for why a bare SELECT-then-INSERT is not
# enough). Arbitrary but fixed, and namespaced by being large and adjacent --
ACCOUNT_CAP_LOCK_KEYS: dict[str, int] = {
    AccountType.USERS.value: 7_240_001,
    AccountType.ADMIN.value: 7_240_002,
    AccountType.MODERATOR.value: 7_240_003,
}


def account_cap(account_type: object) -> int:
    """The maximum number of accounts of this kind that may exist."""
    field = ACCOUNT_CAP_SETTING_NAMES[normalize_account_type(account_type)]
    return int(getattr(settings, field))


def account_cap_lock_key(account_type: object) -> int:
    """The advisory-lock key that serialises cap enforcement for this account table."""
    return ACCOUNT_CAP_LOCK_KEYS[normalize_account_type(account_type)]


class AccountRef(NamedTuple):
    """One account, identified the only way it can be identified unambiguously."""

    regno: str
    account_id: str
    account_type: str

    @classmethod
    def from_row(cls, row: Any, account_type: object) -> "AccountRef":
        """Build a reference to ``row``, which must live in ``account_type``."""
        return cls(
            regno=str(row.regno),
            account_id=str(row.id),
            account_type=normalize_account_type(account_type),
        )

    @classmethod
    def of(cls, account: Any) -> "AccountRef":
        """Build a reference to an already-resolved account model."""
        return cls.from_row(account, account.account_type)
