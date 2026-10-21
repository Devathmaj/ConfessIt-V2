"""Maintain participant report and upheld-removal tallies.

Filing, removal, and direct-hide paths share this counter mapping. Report counts record
complaints; removal counts record upheld actions. Caller-side conditional transitions make
removals idempotent, while atomic SQL increments prevent lost concurrent updates. Crossing
the threshold blocks the account and retires its sessions in the same transaction.
"""

from typing import Any, Dict, Optional

from sqlalchemy import func, update

from ..accounts import AccountType
from ..db_models import UserRecord
from .auth_service import retire_account_sessions
from .staff_notification_service import notify_user_auto_blocked

# The three kinds of content a participant can be reported for. A confession and a comment on
# one share a kind -- see ``UserRecord`` for why.
CONFESSION = "confession"
LOVE_NOTE = "love_note"
MESSAGE = "message"

REPORT_KINDS: tuple[str, ...] = (CONFESSION, LOVE_NOTE, MESSAGE)

# Block after more than five upheld removals across all content kinds. The admin queue
# reads this same threshold so its flagged list matches enforcement.
REMOVAL_AUTO_BLOCK_THRESHOLD = 5

# kind -> (total reports filed, reports that were upheld by a removal).
_KIND_COLUMNS: Dict[str, tuple[str, str]] = {
    CONFESSION: ("confession_report_count", "confession_removal_count"),
    LOVE_NOTE: ("love_note_report_count", "love_note_removal_count"),
    MESSAGE: ("message_report_count", "message_removal_count"),
}

_REPORTED_AT = 0
_REMOVED_AT = 1


def _column(kind: str, which: int) -> str:
    """The ``users`` column holding one of the two counts for one kind.

    Raises rather than guessing on an unknown kind. Every caller passes a literal from the
    module's own vocabulary, so a bad value is a typo to be caught at once, not a content kind
    that quietly lands in no counter at all.
    """
    try:
        return _KIND_COLUMNS[kind][which]
    except KeyError:
        raise ValueError(
            f"unknown report kind {kind!r}; expected one of {', '.join(REPORT_KINDS)}"
        ) from None


def _author_clause(author_user_id: Optional[str], author_regno: Optional[str]):
    """The WHERE that names one participant, however the content table happens to name them.

    Accepts either identifier because the three content tables disagree: ``confessions``,
    ``confession_comments`` and ``love_notes`` name their author by ``users.id`` while
    ``messages`` names its sender by ``users.regno``. Both identify exactly one row, so
    supporting both keeps every call site free of a kind-specific lookup it would otherwise
    have to get right.

    ``author_user_id`` wins when both are passed; the two name the same person in every path
    that has both, so there is nothing to reconcile.

    Returns None when neither is given, which the callers treat as "no author to charge" -- the
    report or removal still stands, it simply cannot be attributed to anybody.
    """
    if author_user_id:
        return UserRecord.id == str(author_user_id)
    if author_regno:
        return UserRecord.regno == str(author_regno)
    return None


def _bump(
    session,
    *,
    kind: str,
    author_user_id: Optional[str],
    author_regno: Optional[str],
    which: int,
) -> bool:
    clause = _author_clause(author_user_id, author_regno)
    if clause is None:
        return False
    column = _column(kind, which)
    result = session.execute(
        update(UserRecord)
        .where(clause)
        .values({column: getattr(UserRecord, column) + 1})
    )
    return result.rowcount > 0


def _total_removals_expr():
    """The participant's upheld-removal total, summed across the three kinds.

    ``coalesce`` because a database old enough to have predated the columns can still hold a
    NULL in one of them, and ``NULL + n`` would read as no total at all -- the one reading
    that silently disables the block.
    """
    return (
        func.coalesce(UserRecord.confession_removal_count, 0)
        + func.coalesce(UserRecord.love_note_removal_count, 0)
        + func.coalesce(UserRecord.message_removal_count, 0)
    )


def _block_if_over_threshold(session, clause) -> bool:
    """Block the participant named by ``clause`` if their removal total is over the line.

    Called only when a removal was just counted, so the increment this module made a statement
    ago is what the condition reads. The ``is_blocked`` guard means the transition, not the
    condition, is what writes: an account already blocked stays as it is and its retired
    sessions are not retired again. A participant an administrator has unblocked is blocked
    again by their *next* removal -- the rule is "over the threshold", not "just crossed it" --
    which is the same line the flagged queue shows.

    The flag, the session retirement and the admin's notice share the caller's transaction: a
    block whose live sessions stay ``active`` is a session table that disagrees with the
    account table, and an alert about a block that rolled back is an admin chasing a state
    that does not exist. The returning row carries the identity and the new total the notice's
    copy needs, so one statement both blocks and answers "who, and how far over".
    """
    blocked = session.execute(
        update(UserRecord)
        .where(clause)
        .where(UserRecord.is_blocked.is_(False))
        .where(_total_removals_expr() > REMOVAL_AUTO_BLOCK_THRESHOLD)
        .values(is_blocked=True)
        .returning(
            UserRecord.id,
            UserRecord.regno,
            UserRecord.name,
            _total_removals_expr().label("removal_total"),
        )
    ).first()
    if blocked is None:
        return False
    retire_account_sessions(session, str(blocked.id), AccountType.USERS.value)
    notify_user_auto_blocked(
        session,
        regno=blocked.regno,
        name=blocked.name,
        removal_total=int(blocked.removal_total),
    )
    return True


def record_report_filed(
    session,
    *,
    kind: str,
    author_user_id: Optional[str] = None,
    author_regno: Optional[str] = None,
) -> bool:
    """Count one report against the participant who wrote the content. Returns whether it counted.

    Call this in the same transaction that creates the report row, so a rolled-back report never
    leaves a phantom complaint on somebody's record.

    A missing author row is a no-op rather than an error. The report has been filed and the
    moderation queue is the thing that matters; failing the request because the participant's
    account has since gone would throw away the report itself over a counter nobody can read.
    """
    return _bump(
        session, kind=kind, author_user_id=author_user_id, author_regno=author_regno,
        which=_REPORTED_AT,
    )


def record_removal(
    session,
    *,
    kind: str,
    author_user_id: Optional[str] = None,
    author_regno: Optional[str] = None,
) -> bool:
    """Count one upheld removal against the participant who wrote the content.

    Only call this on the *transition* to removed. Passing a kind here that is not the kind of
    content just taken down would put the removal in the wrong column, so the kind is required
    and never defaulted.

    Same-transaction and same-missing-author reasoning as :func:`record_report_filed`: the
    content is already down by the time this runs, and a counter must not be able to undo that.

    This is also the one place the automatic block happens (see the module docstring): the
    removal that takes the participant's total past ``REMOVAL_AUTO_BLOCK_THRESHOLD`` blocks
    the account and stages one admin notice about it, and every removal path in the
    application funnels through here, so no caller has to remember to ask. The return value
    stays "did this removal count", not "was the participant blocked" or "was an admin told",
    so no call site changes meaning because the block exists.
    """
    counted = _bump(
        session, kind=kind, author_user_id=author_user_id, author_regno=author_regno,
        which=_REMOVED_AT,
    )
    if counted:
        _block_if_over_threshold(session, _author_clause(author_user_id, author_regno))
    return counted


def tally_for_row(row: Any) -> Dict[str, Dict[str, int]]:
    """The six counts on a ``users`` row, grouped by kind, for a staff-facing payload.

    Returned as ``{"confession": {"reports": n, "removals": n}, ...}`` rather than six flat keys
    so the pair for a kind travels together -- the two numbers only mean anything relative to
    each other, and a payload that let a client read ``removals`` without also having
    ``reports`` in hand would invite comparing a removal against nothing.

    Staff rows have none of these columns, and callers use this only for ``users`` rows; for
    ``admin`` and ``moderator`` the whole key is omitted rather than sent as zeros, because a
    staff account with "0 removals" reads as a clean record rather than as a field that does not
    apply to it.
    """
    return {
        kind: {
            "reports": int(getattr(row, report_column, 0) or 0),
            "removals": int(getattr(row, removal_column, 0) or 0),
        }
        for kind, (report_column, removal_column) in _KIND_COLUMNS.items()
    }


def report_column(kind: str) -> str:
    """The ``users`` column counting every report filed for one kind of content."""
    return _column(kind, _REPORTED_AT)


def removal_column(kind: str) -> str:
    """The ``users`` column counting the upheld removals for one kind of content."""
    return _column(kind, _REMOVED_AT)
