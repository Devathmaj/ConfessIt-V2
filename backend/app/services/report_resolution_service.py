"""Ruling on a report: dismiss it, or take the reported content down.

There are three report queues -- confessions and their comments, love notes, chat messages --
and until now each one closed out its own report in its own service function, with the three
copies agreeing only by hand. They have to agree, because the screen that renders all three is
one screen and a moderator who gets different behaviour per tab has learned to distrust it.

So the shared part lives here, once:

* the vocabulary -- ``dismissed`` and ``removed``, and nothing else can be written;
* what removal *means* -- the content row is marked removed, not deleted;
* who is told *what* -- the author on a removal, the reporter on both outcomes.

The asymmetry in the notifications is deliberate and is the whole point of the split. A
removal has two interested parties and both need to know: the author, because their post is
gone and the reason is a rule rather than a bug, and the reporter, because they took the
trouble and an answer is the only thing that closes the loop. A dismissal has one: the author
of a post that was fine is not to be troubled about a report that was closed in their
favour, and telling them they were reported is a small punishment for someone else's
complaint. Only the reporter is told, and what they are told is that it was reviewed.

**Removal is soft, everywhere.** The content row stays in the database and so does the report
about it, because the report's foreign key cascades from the content -- deleting the post
would delete the record that it was reported and ruled on, and the audit trail with it. What
changes is visibility: every participant-facing read filters on ``removed_at IS NULL``, staff
surfaces do not and carry the flag instead, and the asset proxy stops serving a removed love
note's image even to a delivery URL that was minted while it was still live.

**What the author is told depends on how the content came down, not on what happened to it.**
The same write is reached two ways: a ``removed`` ruling on a report, and a direct hide from a
review screen with no report behind it. A ruling cites the policy, because a moderator read the
post and decided it broke a rule and the author is owed the reason. A direct hide says only
that the content was removed, because no rule was cited against it and asserting one would be
the application claiming a decision nobody made. ``hide_confession`` and ``hide_comment`` are
the two direct-hide paths; both send the neutral copy.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select

from ..accounts import AccountRef
from ..db_models import (
    ConfessionCommentRecord,
    ConfessionRecord,
    ConversationRecord,
    LoveNoteRecord,
    LoveNoteReportRecord,
    MessageRecord,
    MessageReportRecord,
    ReportRecord,
    UserRecord,
)
from ..logger import get_logger
from ..postgres import Database, is_valid_id
from ..utils import isoformat_datetime
from .notification_service import create_notification_service
from .report_counters import (
    CONFESSION,
    LOVE_NOTE,
    MESSAGE,
    record_removal,
)

logger = get_logger(__name__)


# The three closable statuses. Written into the routers as a ``Query(regex=...)`` so a fourth
# one cannot be invented by a client, and re-asserted here so a caller that is not a router
# still cannot reach the branches below with something unhandled.
#
# ``removed`` and ``dismissed`` are the two the screen offers. ``reviewed`` is the older word
# for the same thing as ``dismissed`` -- a human looked and the content stays -- and it is
# still accepted so a caller holding a stale id and a stale vocabulary is not met with a 422,
# and so rows closed before this change still read back. The branch below treats it as a
# dismissal, which is exactly what it meant.
RESOLUTION_STATUSES: tuple[str, ...] = ("reviewed", "dismissed", "removed")

# The one status that takes content down. Everything else is a judgement that the content
# stays. Spelled once so the branch in each of the three resolvers cannot drift.
REMOVAL_STATUS = "removed"

# ``reviewed`` and ``dismissed`` are one outcome written two ways, so a filter for one of them
# has to return the other too. A row closed out under the old spelling is stored as ``reviewed``,
# but every screen labels it Dismissed -- so filtering on ``dismissed`` and leaving it out would
# hide exactly the rows the moderator is looking for, under the filter that names them.
_DISMISSAL_STATUSES: tuple[str, ...] = ("reviewed", "dismissed")


def resolve_status_filter(status_filter: Optional[str]) -> tuple[str, ...]:
    """Expand a queue's ``?status=`` filter into the stored statuses it should match.

    ``None`` (no filter) stays ``None`` rather than becoming an empty tuple, because the queues
    treat "no filter" and "match nothing" differently and callers pass the result straight on.
    """
    if not status_filter:
        return ()
    if status_filter == "dismissed":
        return _DISMISSAL_STATUSES
    return (status_filter,)

# What the two parties are told, per kind of content. The author is told what kind of thing
# they wrote, because "your content was removed" is a worse message to receive than "your
# confession was removed": one reads as a verdict on a person.
_REMOVAL_HEADINGS: Dict[str, str] = {
    "confession": "Confession removed",
    "comment": "Comment removed",
    "message": "Message removed",
    "love_note": "Love note removed",
}

# What the author is told when a removal came out of a **report queue**.
#
# Here a participant put the content in front of a moderator, a moderator read it and ruled
# that it broke a rule, and the author is being told which rule. Citing the policy is the
# whole point: it is what makes the ruling answerable by the person it was made about, and
# what lets them know it was a decision rather than a fault in the app.
_POLICY_BODIES: Dict[str, str] = {
    "confession": "Your confession was removed because it violated our community policies.",
    "comment": (
        "Your comment on a confession was removed because it violated our community policies."
    ),
    "message": "Your chat message was removed because it violated our community policies.",
    "love_note": "Your love note was removed because it violated our community policies.",
}

# What the author is told when a moderator removed the content **directly**, with no report
# behind it.
#
# Deliberately not the policy copy, and the distinction is not cosmetic. A moderator hiding a
# post from the review screen has not cited a rule against it and no participant has
# complained about it, so asserting a policy violation would be the application making a claim
# about a ruling that was never made -- and doing so about a rule nobody can point to is not
# something an author can act on. This says the thing that actually happened and stops there.
#
# The split is by **path**, not by whether reports happen to exist. A confession that has three
# reports on it and is hidden from the review screen still got no ruling, so it still gets this
# copy. Only ``resolve_*_report`` cites the policy.
_NEUTRAL_BODIES: Dict[str, str] = {
    "confession": "We removed your confession from ConfessIt.",
    "comment": "We removed your comment from ConfessIt.",
    "message": "We removed your chat message from ConfessIt.",
    "love_note": "We removed your love note from ConfessIt.",
}

_REPORTER_REMOVED_HEADING = "Your report was acted on"
_REPORTER_REMOVED_BODY = (
    "Thank you for reporting. The content you reported has been taken down because it "
    "violated our community policies."
)
_REPORTER_DISMISSED_HEADING = "We reviewed your report"
_REPORTER_DISMISSED_BODY = (
    "Thank you for reporting. We have reviewed the content you reported and it did not "
    "violate our community policies, so it was not removed."
)

# None of the four helpers below writes a navigation target, and that is the design rather than
# an omission. Each of these notices is about something the reader can no longer open: the
# content a removal speaks for has already stopped being visible to its author, and a report is
# only ever visible on the staff review queue, which a participant cannot reach and which would
# tell them nothing they were not already told in the body. A target on any of them would send
# the reader to a page that cannot answer the notification -- so the removal notices land on the
# confession feed, the message box and the love notes page respectively, and the two report
# notices go nowhere at all.
#
# Recorded here rather than left implicit because the next removal path added will also be
# tempted to pass an id, and the reason it must not is not obvious from the call site.


def _assert_resolution_status(status_value: str) -> str:
    if status_value not in RESOLUTION_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"A report can only be closed as one of: {', '.join(RESOLUTION_STATUSES)}.",
        )
    return status_value


def _regno_for_user(session, user_id: Optional[str]) -> Optional[str]:
    """The regno of the participant behind a ``users.id``, or None if there is no such row.

    Notifications address a ``recipient_regno``, and the content tables name their author by
    primary key, so this is the join every notification on this page needs. It returns None
    rather than raising because the interesting half of a removal -- the content is gone --
    has already happened by the time a notification is written; a missing account row must not
    turn that into a 500 and leave the reviewer looking at an error for a decision that was
    applied.
    """
    if not user_id:
        return None
    return session.scalar(select(UserRecord.regno).where(UserRecord.id == str(user_id)))


def _notify_removal(
    db: Database,
    *,
    author_regno: Optional[str],
    reporter_regno: Optional[str],
    kind: str,
    cites_policy: bool,
) -> None:
    """Tell the author their content is gone and the reporter it was taken down.

    A reporter who is also the author -- a participant reporting their own confession, which
    the confession report path does not forbid -- gets one notification, not two. The removal
    notice already says everything the reporter notice would have said, and sending both would
    read as two separate events.

    ``cites_policy`` selects which body the author is sent, and it is the one flag that carries
    the whole distinction described above ``_POLICY_BODIES``: a ruling on a report cites the
    policy, a direct removal does not. It is required rather than defaulted, so adding a fourth
    removal path cannot quietly inherit whichever copy the signature happened to default to.
    """
    if author_regno:
        bodies = _POLICY_BODIES if cites_policy else _NEUTRAL_BODIES
        create_notification_service(
            recipient_regno=author_regno,
            heading=_REMOVAL_HEADINGS[kind],
            body=bodies[kind],
            db=db,
        )
    if reporter_regno and reporter_regno != author_regno:
        create_notification_service(
            recipient_regno=reporter_regno,
            heading=_REPORTER_REMOVED_HEADING,
            body=_REPORTER_REMOVED_BODY,
            db=db,
        )


def _notify_direct_removal(db: Database, *, author_regno: Optional[str], kind: str) -> None:
    """Tell the author their content was removed by a moderator, citing no policy.

    The direct-hide half of :func:`_notify_removal`, split out so the ``cites_policy=False``
    decision is written once. Both ``hide_confession`` and ``hide_comment`` reach this, and a
    signature that let a fourth path default the flag would put the choice back in reach of
    whoever adds the path next.

    There is no reporter to notify here: nobody complained, which is the whole reason the
    policy copy is not used.
    """
    if author_regno:
        create_notification_service(
            recipient_regno=author_regno,
            heading=_REMOVAL_HEADINGS[kind],
            body=_NEUTRAL_BODIES[kind],
            db=db,
        )


def _notify_dismissal(db: Database, reporter_regno: Optional[str]) -> None:
    """Tell the reporter the report was reviewed and the content stayed.

    Nobody else is told. In particular the author is not: they were not accused of anything
    that survived review, and a notification saying so would announce to a bystander that
    somebody had complained about them.
    """
    if reporter_regno:
        create_notification_service(
            recipient_regno=reporter_regno,
            heading=_REPORTER_DISMISSED_HEADING,
            body=_REPORTER_DISMISSED_BODY,
            db=db,
        )


def _stamp_removal(row, reviewer_regno: str) -> bool:
    """Mark a content row removed, and say whether this call is the one that did it.

    Re-deciding a report that has already been closed out as ``removed`` must not move the
    timestamp: ``removed_at`` is when the content came down, and re-stamping it would make the
    audit trail say the second click took it down rather than the first.

    The return value is what lets the caller report honestly. ``content_removed`` in the
    response is "this decision took something down", not "the thing was down when we got
    here", so a moderator who re-opens an already-removed report is not told a second post
    came down -- and, more importantly, the side effects that follow a removal (the comment
    count, the conversation preview) are only run for the transition, not for every click.
    """
    if row.removed_at is not None:
        return False
    row.removed_at = datetime.now(timezone.utc)
    row.removed_by_regno = reviewer_regno
    return True


def _recompute_conversation_preview(session, conversation_id: Optional[str]) -> None:
    """Move a conversation's preview to the newest message that is still visible.

    A removed message stays in ``messages`` and is excluded from every participant read, so
    without this the conversation list would keep showing the first 100 characters of a post
    that has been taken down -- the one place the removal would still be leaking its text.
    """
    if not conversation_id:
        return
    conversation = session.get(ConversationRecord, str(conversation_id))
    if conversation is None:
        return
    newest = session.scalar(
        select(MessageRecord)
        .where(
            MessageRecord.conversation_id == str(conversation_id),
            MessageRecord.removed_at.is_(None),
        )
        .order_by(MessageRecord.created_at.desc())
        .limit(1)
    )
    if newest is None:
        conversation.last_message_at = None
        conversation.last_message_preview = None
        return
    conversation.last_message_at = newest.created_at
    conversation.last_message_preview = (newest.body or "")[:100]


def resolve_confession_report(
    db: Database,
    report_id: str,
    status_value: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Close out a confession or comment report, optionally taking the content down.

    The report targets a confession or a comment on one, discriminated by which of the two
    foreign keys is set, and the two are removed by different writes: a confession goes on its
    own row, while removing a comment also has to decrement the parent confession's
    denormalised ``comment_count`` so the feed does not advertise a comment nobody can open.

    That decrement belongs to the *removal*, not to the close-out. It sits inside the
    transition below rather than beside the lookup, because reading it as "this report was
    acted on, so subtract one" makes a **dismissal** quietly eat a comment: the report is
    closed, the comment stays, and the feed stops counting it. Nothing would look broken --
    the count is just quietly one too low from then on, on every read.
    """
    _assert_resolution_status(status_value)
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    reporter_regno: Optional[str] = None
    author_regno: Optional[str] = None
    author_user_id: Optional[str] = None
    removed_content = False

    with db.sessions.begin() as session:
        report = session.get(ReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

        is_comment = report.comment_id is not None
        target_id = report.comment_id if is_comment else report.confession_id
        target = None
        if target_id:
            target = session.get(
                ConfessionCommentRecord if is_comment else ConfessionRecord, str(target_id)
            )

        if target is not None:
            author_user_id = target.user_id if is_comment else target.author_id
            author_regno = _regno_for_user(session, author_user_id)
            # Resolving the reporter here rather than after the commit keeps every read that
            # decides the outcome inside one transaction, so a report closed against content
            # deleted a microsecond earlier takes the same branch as one that was not.
            reporter_regno = _regno_for_user(session, report.reported_by_id)

            if status_value == REMOVAL_STATUS and _stamp_removal(target, reviewer_regno):
                removed_content = True
                if is_comment:
                    parent = session.get(ConfessionRecord, str(target.confession_id))
                    if parent is not None and parent.comment_count > 0:
                        parent.comment_count = parent.comment_count - 1
                # An upheld report against the participant's writing. Inside the transition
                # branch for the same reason as the comment count: re-deciding a report that
                # was already ruled removed is not a second event and must not be counted as
                # one. A confession and a comment share the bucket -- see report_counters.
                record_removal(session, kind=CONFESSION, author_user_id=author_user_id)

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer_regno

    # Written after the commit, as everywhere else in this app: a notification failure must
    # not undo a decision the moderator has been told was applied. No wall-clock time goes in
    # the body -- the row's own created_at renders in the reader's local time.
    #
    # Keyed on the outcome that was asked for rather than on ``removed_content``: re-deciding
    # an already-removed report still has to tell the reporter where it stands, and the author
    # has already been told once.
    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=author_regno,
            reporter_regno=reporter_regno,
            kind="comment" if is_comment else "confession",
            cites_policy=True,
        )
    else:
        _notify_dismissal(db, reporter_regno)

    logger.info(
        f"Confession report {report_id} closed as {status_value} by {reviewer_regno}"
    )
    return {"id": str(report_id), "status": status_value, "content_removed": removed_content}


def resolve_love_note_report(
    db: Database,
    report_id: str,
    status_value: str,
    reviewer: AccountRef,
) -> Dict[str, Any]:
    """Close out a love note report, optionally taking the note down.

    The note's own ``status`` is left alone. It says whether the note was approved or rejected
    as a love note, and a report is one person's word about a note that is judged on its own
    merits -- overwriting the two into one column is how a queue and a note list end up
    disagreeing about a note's state. Removal is recorded on ``removed_at``, which is what the
    recipient's inbox and the image proxy both read.
    """
    _assert_resolution_status(status_value)
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    reporter_regno: Optional[str] = None
    author_regno: Optional[str] = None
    removed_content = False

    with db.sessions.begin() as session:
        report = session.get(LoveNoteReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

        note = session.get(LoveNoteRecord, str(report.love_note_id))
        reporter_regno = report.reporter_regno
        if note is not None:
            author_regno = _regno_for_user(session, note.sender_id)
            if status_value == REMOVAL_STATUS:
                removed_content = _stamp_removal(note, reviewer.regno)
                if removed_content:
                    # Only on the transition: re-deciding an already-removed note is not a
                    # second takedown, and the sender's removal count has to say how many times
                    # their content actually came down.
                    record_removal(session, kind=LOVE_NOTE, author_user_id=note.sender_id)

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer.regno

    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=author_regno,
            reporter_regno=reporter_regno,
            kind="love_note",
            cites_policy=True,
        )
    else:
        _notify_dismissal(db, reporter_regno)

    logger.info(f"Love note report {report_id} closed as {status_value} by {reviewer.regno}")
    return {"id": str(report_id), "status": status_value, "content_removed": removed_content}


def resolve_message_report(
    db: Database,
    report_id: str,
    status_value: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Close out a chat message report, optionally taking the message down.

    Removal also moves the conversation's preview forward, because ``last_message_preview`` is
    a denormalised copy of the newest message's first 100 characters and the conversation list
    reads it for both participants and staff.
    """
    _assert_resolution_status(status_value)
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    reporter_regno: Optional[str] = None
    removed_content = False

    with db.sessions.begin() as session:
        report = session.get(MessageReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

        reporter_regno = report.reporter_regno
        message = session.get(MessageRecord, str(report.message_id))
        if message is not None:
            # A message can only be reported by its receiver, so the sender and the reporter
            # are always two different people here. The self-report case only arises for
            # confessions, and _notify_removal collapses it.
            if status_value == REMOVAL_STATUS and _stamp_removal(message, reviewer_regno):
                # Moved forward off the message just taken down, so the conversation list does
                # not keep previewing content a moderator has ruled against. Gated on the
                # transition for the same reason as the comment count above: re-deciding an
                # already-removed message must not rewind the preview to it.
                _recompute_conversation_preview(session, message.conversation_id)
                removed_content = True
                record_removal(
                    session, kind=MESSAGE, author_regno=report.reported_user_regno
                )

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer_regno

    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=report.reported_user_regno,
            reporter_regno=reporter_regno,
            kind="message",
            cites_policy=True,
        )
    else:
        _notify_dismissal(db, reporter_regno)

    logger.info(f"Message report {report_id} closed as {status_value} by {reviewer_regno}")
    return {"id": str(report_id), "status": status_value, "content_removed": removed_content}


def hide_confession(
    db: Database,
    confession_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Take a confession out of public view from the review screen, with no report behind it.

    The same write a ``removed`` ruling performs, reached on a confession nobody reported. That
    is why it is a soft removal and not a delete: ``removed_at`` is already the column every
    participant-facing read filters on, so reusing it means there is only ever one way for a
    confession to be hidden and staff reads never disagree with the feed about whether it is
    live. A delete would be a second, differently-implemented answer to the same question, and
    it would cascade the report rows away with the post.

    Comments are left alone. They are unreachable while their parent confession is hidden,
    because a comment is only ever read through its confession, and stamping them would rewrite
    the removal state of rows that a report against the comment may have taken down on their
    own evidence.

    **There is no unhide, by design.** Nothing in this app clears ``removed_at``, so the warning
    the confirmation dialog carries describes what actually happens rather than something the
    back end does not honour. Hiding something already hidden is answered rather than refused:
    a double click is a double click, and the response says plainly that nothing changed.

    The author is notified only on the transition, for the same reason the report path notifies
    them there -- one removal is one notice. A second one for the same removal reads as two
    events, and there was only ever one.

    The author is told the post was removed and **nothing about the policies**. No report was
    ruled on here, so there is no rule to cite; see ``_NEUTRAL_BODIES``.
    """
    if not is_valid_id(confession_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confession id")

    author_regno: Optional[str] = None
    hidden = False
    already_hidden = False
    removed_at: Optional[datetime] = None
    removed_by_regno: Optional[str] = None

    with db.sessions.begin() as session:
        confession = session.get(ConfessionRecord, str(confession_id))
        if confession is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confession not found")

        author_user_id = confession.author_id
        author_regno = _regno_for_user(session, author_user_id)
        # Read the row back after stamping rather than trusting the branch below: on the
        # already-hidden path these are somebody else's decision, and the review screen is
        # about to render them as the audit trail they are.
        hidden = _stamp_removal(confession, reviewer_regno)
        already_hidden = not hidden
        removed_at = confession.removed_at
        removed_by_regno = confession.removed_by_regno

        if hidden:
            # A direct hide is charged as a removal even though no report was upheld -- see
            # report_counters. Without this the authoritative count would only move when a
            # complaint happened to exist, and a moderator could take a participant's history
            # down from this screen without it showing up on their record at all.
            record_removal(session, kind=CONFESSION, author_user_id=author_user_id)

    if hidden:
        _notify_direct_removal(db, author_regno=author_regno, kind="confession")
        logger.info(f"Confession {confession_id} hidden by {reviewer_regno}")
    else:
        logger.info(f"Confession {confession_id} was already hidden; left as it was")

    return {
        "id": str(confession_id),
        "content_removed": hidden,
        "already_hidden": already_hidden,
        "removed_at": isoformat_datetime(removed_at),
        "removed_by_regno": removed_by_regno,
    }


def hide_comment(
    db: Database,
    comment_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Take a single comment off a confession, with no report behind it.

    The comment-level counterpart to :func:`hide_confession`, and the same write: mark the row
    removed rather than deleting it, so the reports filed against the comment survive with it.
    ``removed_at`` is already the column every participant-facing comment read filters on, so
    reusing it means a comment hidden from the review screen and one taken down by a report
    ruling land in identical state and staff reads never disagree with the feed.

    **The parent confession is not removed**, and its own ``removed_at`` is left alone, for the
    same reason ``hide_confession`` leaves the comments alone in the other direction: hiding a
    comment does not rewrite the removal state of the post it sits on.

    Its ``comment_count`` is the one thing about the parent that does move, and it has to:
    that number is what the feed shows, so leaving it counting a comment no participant can open
    would advertise something that is not there. It decrements **only on the transition** --
    inside the ``if hidden`` branch rather than beside the lookup, because reading it as "this
    comment was acted on, so subtract one" would let a second click on an already-hidden comment
    take the count to zero while the comment stayed on screen for staff. Same reasoning as the
    identical branch in :func:`resolve_confession_report`.

    The author is notified once, on the transition, and told the comment was removed with no
    claim about the policies -- nothing was ruled on here. **There is no unhide.**
    """
    if not is_valid_id(comment_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid comment id")

    author_regno: Optional[str] = None
    hidden = False
    already_hidden = False
    removed_at: Optional[datetime] = None
    removed_by_regno: Optional[str] = None
    # Read inside the transaction: the session closes and expires the row's attributes before
    # the return below, so touching ``comment`` out here would ask a detached instance to
    # refresh itself and fail. The parent id is the one field the response needs beyond what
    # the branch already captured.
    confession_id: Optional[str] = None

    with db.sessions.begin() as session:
        comment = session.get(ConfessionCommentRecord, str(comment_id))
        if comment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

        author_regno = _regno_for_user(session, comment.user_id)
        confession_id = str(comment.confession_id)
        hidden = _stamp_removal(comment, reviewer_regno)
        already_hidden = not hidden
        removed_at = comment.removed_at
        removed_by_regno = comment.removed_by_regno

        if hidden:
            parent = session.get(ConfessionRecord, confession_id)
            # Guarded on > 0 rather than blind: the column is denormalised and can already be
            # out of step with the rows, and a negative comment count would render as a
            # nonsense number in the feed rather than as an obviously broken one.
            if parent is not None and parent.comment_count > 0:
                parent.comment_count = parent.comment_count - 1
            # Charged to the comment's author, in the same bucket as a confession removal -- a
            # complaint about what somebody wrote on a confession is a complaint about them in
            # the same way. Only on the transition, as above.
            record_removal(session, kind=CONFESSION, author_user_id=comment.user_id)

    if hidden:
        _notify_direct_removal(db, author_regno=author_regno, kind="comment")
        logger.info(f"Comment {comment_id} hidden by {reviewer_regno}")
    else:
        logger.info(f"Comment {comment_id} was already hidden; left as it was")

    return {
        "id": str(comment_id),
        "confession_id": confession_id,
        "content_removed": hidden,
        "already_hidden": already_hidden,
        "removed_at": isoformat_datetime(removed_at),
        "removed_by_regno": removed_by_regno,
    }


def pending_report_totals(db: Database) -> Dict[str, int]:
    """How many reports are still awaiting a decision, per queue.

    Only used by tests and by anyone who needs the three numbers without reading three
    payloads; the queue endpoints remain the one place a report is listed.
    """
    with db.sessions() as session:
        return {
            "confessions": session.scalar(
                select(func.count()).select_from(ReportRecord).where(ReportRecord.status == "pending")
            ) or 0,
            "love_notes": session.scalar(
                select(func.count())
                .select_from(LoveNoteReportRecord)
                .where(LoveNoteReportRecord.status == "pending")
            ) or 0,
            "messages": session.scalar(
                select(func.count())
                .select_from(MessageReportRecord)
                .where(MessageReportRecord.status == "pending")
            ) or 0,
        }
