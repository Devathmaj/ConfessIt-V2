"""Per-participant tallies of how often their content was reported, and how often it came down.

**The one reason this module exists.** Nine places across four services now have to say "this
belongs to the participant who wrote it" and then bump a column: four filing paths and five
removal paths. Written inline at each of them they would drift -- a new content kind would get
six call sites updated and one forgotten, and the missing one would be a counter that silently
reads low, which is the failure mode a "was this person reported?" number must never have.

So the kind-to-column mapping lives here, once, and the two events are two functions:

* :func:`record_report_filed` -- called when a report row is created.
* :func:`record_removal` -- called when content is actually taken down.

**Why there are two numbers and not one.** See ``UserRecord``. The short version: the total
counts complaints and the removal count counts upheld ones, and only the second should ever be
used to judge a participant. They are incremented at different moments on purpose, so they
genuinely diverge -- and they are *supposed* to, because a participant whose content was
reported once and cleared is not in the same position as one whose content was taken down once.

**A direct hide counts as a removal.** ``hide_confession`` and ``hide_comment`` perform the
same ``removed_at`` write a ``removed`` ruling performs, so they call :func:`record_removal`
too. If they did not, a moderator could take a participant's entire history down from the review
screen without the authoritative count moving at all, and the tally would then be a measure of
which screen the ruling happened on rather than of what was done to the person.

**Idempotency is the caller's job, and it already is.** Removal counters must be incremented
only on the transition to removed, not on every call -- so ``resolve_*_report`` and the two
``hide_*`` functions all gate on the boolean ``_stamp_removal`` returns, and only the ``True``
branch reaches here. A repeat request against already-removed content increments nothing. This
is the same gate that keeps the notification count and the ``comment_count`` decrement correct,
for the same reason: one removal is one event.

**Writes are atomic SQL increments.** ``SET n = n + 1`` issued as a Core ``UPDATE``, not
``row.counter += 1`` on a loaded ORM object. Two reports against the same participant filed at
the same moment is not a theoretical case -- it is what a popular participant's post attracting
two complaints in the same second looks like -- and read-modify-write would silently drop one.
It also avoids a stale-identity problem: the caller's session may already hold the
``UserRecord`` instance this row belongs to.
"""

from typing import Any, Dict, Optional

from sqlalchemy import update

from ..db_models import UserRecord

# The three kinds of content a participant can be reported for. A confession and a comment on
# one share a kind -- see ``UserRecord`` for why.
CONFESSION = "confession"
LOVE_NOTE = "love_note"
MESSAGE = "message"

REPORT_KINDS: tuple[str, ...] = (CONFESSION, LOVE_NOTE, MESSAGE)

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
    """
    return _bump(
        session, kind=kind, author_user_id=author_user_id, author_regno=author_regno,
        which=_REMOVED_AT,
    )


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