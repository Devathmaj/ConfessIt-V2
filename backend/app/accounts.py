"""The application's account tables and the capabilities each one carries.

This module is the only place an account *kind* is named and a privilege is granted.
Every gate, every Pydantic field and every client-side check derives from what is declared
here, so there is exactly one list to change when an account table is added or a privilege
is moved.

An account's kind is not a column. It is the table the row lives in: ``users`` for
participants, ``admin`` and ``moderator`` for staff. There is no ``role`` anywhere, so the
question "what may this caller do?" is answered by "which table did this caller's row come
from?", which is a fact about storage rather than a value somebody can get wrong.

Two things this deliberately does not do:

* It does not normalise case. ``"Admin"``, ``"ADMIN"`` and ``""`` are not spellings of
  ``"admin"``; they are not account tables at all. Folding them would mean a typo silently
  resolved to the most privileged account kind in the system, and a typo in the other
  direction would silently resolve to a plain participant while the account still looked
  like staff in the database. An unrecognised value is resolved to the least-privileged
  kind instead (see :func:`normalize_account_type`).
* It does not imply a hierarchy. ``moderator`` is not a lesser ``admin``; it is a
  different set of capabilities, and a moderator cannot reach anything on the admin-only
  list without being granted it explicitly.

Capabilities rather than table equality are what makes that enforceable: a route declares
the one capability it needs, and :func:`has_capability` decides. Adding a fourth account
table later cannot accidentally inherit or lose access, because no route ever asks "is this
an admin?".

The per-table capacity ceilings live here for the same reason. "How many of this kind may
exist" is a question about the account tables, so it is answered by adding one entry to
:data:`ACCOUNT_CAP_SETTING_NAMES` next to the kinds themselves rather than by teaching an
endpoint about three specific account types.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple

from .config import settings


class AccountType(str, Enum):
    """The account tables. The value *is* the table name.

    Kept as the physical table name rather than a friendlier noun so that the discriminator
    a caller carries around, the row a lookup resolved, and the table a write went to are
    the same string. A separate label would need a second mapping that could disagree with
    the schema, and a disagreement there is indistinguishable from a privilege escalation.
    """

    USERS = "users"
    ADMIN = "admin"
    MODERATOR = "moderator"


# Ordered for error messages and for the OpenAPI schema, not for any hierarchy.
ACCOUNT_TYPE_VALUES: tuple[str, ...] = tuple(account_type.value for account_type in AccountType)

# Kinds of account that are staff rather than participants. These accounts are kept out of
# matchmaking and love notes entirely, as both the actor and the candidate, so moderation
# data is never polluted by staff. Membership here grants no capability by itself; it only
# removes an account from the participant pool.
STAFF_ACCOUNT_TYPES: frozenset[str] = frozenset(
    {AccountType.ADMIN.value, AccountType.MODERATOR.value}
)


class Capability(str, Enum):
    """A single, named privilege.

    A route depends on exactly one of these rather than on an account kind, so "who can do
    this" is answered by reading this enum and the map below instead of by grepping for
    account-table names across the codebase.
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
    SESSIONS_READ = "sessions:read"
    MESSAGES_ARCHIVE = "messages:archive"
    APP_CONFIG_READ = "app_config:read"


# What a moderator can do: read every content queue and act on what is in it. Nothing here can
# count the platform, create, edit, block, or mint an account, see a session, or touch the
# message archive -- the moderator dashboard this backs measures its own backlog, from the
# queue endpoints below, rather than counting the platform behind them.
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

# What only an admin can do. Account lifecycle sits here because it is the capability from
# which privilege grows: anything that can create an ``admin`` row can mint an account that
# holds every other capability on this list.
#
# ``stats:read`` sits here rather than with the moderation set because what it returns is a
# census of the platform -- how many accounts exist, how many are blocked, who is signed in --
# and not a measure of a queue. A moderator is sent to the moderation dashboard, which shows
# the backlog of the queues they can rule on: a number they can act on, read from the queue
# endpoints. Keeping this one open to them would hand over a figure that describes nothing they
# are able to change.
ADMIN_ONLY_CAPABILITIES: frozenset[str] = frozenset(
    capability.value
    for capability in (
        Capability.STATS_READ,
        Capability.USERS_REVIEW,
        Capability.USERS_CREATE,
        Capability.USERS_UPDATE,
        Capability.USERS_BLOCK,
        Capability.SESSIONS_READ,
        Capability.MESSAGES_ARCHIVE,
        Capability.APP_CONFIG_READ,
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
    """Resolve any stored or supplied value to one of the canonical account-table names.

    Anything not exactly a defined account kind becomes :attr:`AccountType.USERS`. This is
    the fail-closed direction on purpose: a value written before the vocabulary was
    enforced, or by a script that bypassed the API, must not keep a privilege it was never
    validly granted -- and must certainly not be granted one by guessing at intent.
    """
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


# Which ``Settings`` field carries the ceiling for each account table. Expressed as a name and
# resolved through ``getattr`` below rather than as three ``if`` branches over the values, so
# that adding an account table is one entry here and one field on ``Settings`` -- there is no
# second switch that can be left describing the old set of tables.
#
# The mapping is keyed by *table name* rather than by ``Settings`` field name deliberately: the
# question a caller asks is "how many of this kind may exist", and the table is what it has.
ACCOUNT_CAP_SETTING_NAMES: dict[str, str] = {
    AccountType.USERS.value: "MAX_USER_COUNT",
    AccountType.ADMIN.value: "MAX_ADMIN_COUNT",
    AccountType.MODERATOR.value: "MAX_MODERATOR_COUNT",
}

# Stable advisory-lock keys, one per account table, so the "is there room" count and the insert
# that depends on it can be serialised against each other. See ``app.routers.admin.create_user``
# for why a bare SELECT-then-INSERT is not enough.
#
# Arbitrary but fixed: an advisory lock is a number the callers agree on, and these must not be
# reused for an unrelated lock or the two would block each other for no reason. They are namespaced
# by being large and adjacent rather than by sharing a range with anything else.
ACCOUNT_CAP_LOCK_KEYS: dict[str, int] = {
    AccountType.USERS.value: 7_240_001,
    AccountType.ADMIN.value: 7_240_002,
    AccountType.MODERATOR.value: 7_240_003,
}


def account_cap(account_type: object) -> int:
    """The maximum number of accounts of this kind that may exist.

    Read from settings on every call rather than captured at import, so a test that sets a cap is
    obeyed immediately and a value changed in the environment does not need a module reload to
    take effect. An unrecognised account type resolves, like every other question here, to the
    least-privileged kind -- so it gets the participant ceiling rather than an unbounded answer.
    """
    field = ACCOUNT_CAP_SETTING_NAMES[normalize_account_type(account_type)]
    return int(getattr(settings, field))


def account_cap_lock_key(account_type: object) -> int:
    """The advisory-lock key that serialises cap enforcement for this account table."""
    return ACCOUNT_CAP_LOCK_KEYS[normalize_account_type(account_type)]


class AccountRef(NamedTuple):
    """One account, identified the only way it can be identified unambiguously.

    A regno on its own is not an identity in this schema: it is unique *per table*, so a
    participant, a moderator and an admin may all carry the same one, and a bare regno
    handed to a search would come back with whichever row the search happened to reach
    first. This is the type that crosses function boundaries instead -- the regno together
    with the table it was read from and the primary key that pins that exact row -- so no
    caller can re-interpret it as a different account by widening its own lookup.

    ``account_type`` is normalised on the way in, so a ref built from a corrupt or
    hand-written value points at the least-privileged table rather than at whatever the
    string happened to spell.
    """

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
        """Build a reference to an already-resolved account model.

        For the common case where the model is a ``UserDetails`` or ``AccountDetails``, which
        already knows which table it came from. Asset delivery URLs pin the account the
        response is being served *to*, so a moderator opening a love note from a sender who
        has since been blocked is still served, exactly as before this was pinned.
        """
        return cls.from_row(account, account.account_type)
