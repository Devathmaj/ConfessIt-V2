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
"""

from __future__ import annotations

from enum import Enum
from typing import Any, NamedTuple


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

    # --- content and conversation moderation (admin + moderator) ---
    CONFESSIONS_REVIEW = "confessions:review"
    CONFESSIONS_DELETE = "confessions:delete"
    LOVE_NOTES_REVIEW = "love_notes:review"
    LOVE_NOTES_MODERATE = "love_notes:moderate"
    LOVE_NOTES_DELETE = "love_notes:delete"
    CONVERSATIONS_REVIEW = "conversations:review"
    CONVERSATIONS_READ_MESSAGES = "conversations:read_messages"
    CONVERSATIONS_TERMINATE = "conversations:terminate"
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
# describe it. Nothing here can create, edit, block, or mint an account, see a session, or
# touch the message archive.
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
        Capability.REPORTS_REVIEW,
        Capability.REPORTS_MODERATE,
        Capability.STATS_READ,
    )
)

# What only an admin can do. Account lifecycle sits here because it is the capability from
# which privilege grows: anything that can create an ``admin`` row can mint an account that
# holds every other capability on this list.
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
