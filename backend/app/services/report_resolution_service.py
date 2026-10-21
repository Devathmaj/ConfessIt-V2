"""Resolve reports consistently across confession, love-note, and chat queues.

Dismissal notifies the reporter; removal notifies both parties and soft-removes content while
preserving its audit row. Direct hides use neutral wording. A conditional UPDATE gates
counters and notices so concurrent rulings count one removal only.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select, update

from ..accounts import AccountRef
from ..db_models import (
    CaseClosedMessageRecord,
    CaseClosedMessageReportRecord,
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
from .chat_encryption import ChatDecryptionError
from .chat_message_crypto import (
    decrypt_matchmaking_message,
    encrypt_matchmaking_preview,
)
from .notification_service import create_notification_service
from .storage_service import storage_service
from .report_counters import (
    CONFESSION,
    LOVE_NOTE,
    MESSAGE,
    record_removal,
)

logger = get_logger(__name__)


# Accepted report outcomes. ``reviewed`` remains a legacy alias for dismissal.
RESOLUTION_STATUSES: tuple[str, ...] = ("reviewed", "dismissed", "removed")

# The one status that takes content down. Everything else is a judgement that the content
# stays. Spelled once so the branch in each of the three resolvers cannot drift.
REMOVAL_STATUS = "removed"

# ``reviewed`` is the legacy spelling of ``dismissed``; either filter must include both
# stored values so older rows remain visible in the moderator queue.
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

# Policy copy for removals upheld from the report queue.
_POLICY_BODIES: Dict[str, str] = {
    "confession": "Your confession was removed because it violated our community policies.",
    "comment": (
        "Your comment on a confession was removed because it violated our community policies."
    ),
    "message": "Your chat message was removed because it violated our community policies.",
    "love_note": "Your love note was removed because it violated our community policies.",
}

# Neutral copy for direct hides, where no report ruling or policy finding was made.
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

# Resolution notices omit navigation targets because removed content and staff reports
# are not accessible from participant-facing links.


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
    took_it_down: bool,
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

    **The author is told once, and only by the call that took the content down.** ``took_it_down``
    is the answer to "was this the transition?", and it is the only thing that decides whether
    the author hears about it. Two reports on the same comment, resolved one after the other,
    are one takedown: the second ruling finds the content already down and has nothing new to
    tell the author, who was told when it came down. Sending a second notice would read as a
    second event, and there was only ever one.

    The reporter is told either way. Their complaint is about *their* report, and each report
    is answered on its own merits: the second reporter is told their complaint was upheld even
    though somebody else got there first, because silence on an upheld report reads exactly like
    being ignored.
    """
    if took_it_down and author_regno:
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


def _stamp_removal(session, row, reviewer_regno: str) -> bool:
    """Mark a content row removed, and say whether this call is the one that did it.

    **The transition is a single conditional ``UPDATE``, not a read followed by a write.** That
    is the whole point of this function. Two moderators acting on the same content at the same
    moment -- two participants reporting the same comment and both reports being resolved
    together, a double click on "hide" -- used to both read ``removed_at IS NULL`` before either
    committed, so both were told they had taken the content down. Each then decremented the
    parent confession's ``comment_count``, charged the author a second upheld removal, and sent
    a second notice. The tally that an appeal is argued from ended up inflated, and an account
    crossed the automatic-block line one removal early.

    The database settles it instead. ``WHERE removed_at IS NULL`` matches the row only while it
    is still live, and PostgreSQL re-checks that predicate after the row lock is taken, so of
    two racers exactly one sees ``rowcount == 1``. The loser is told the content was already
    down and none of its side effects run. Same mechanism, and for the same reason, as the
    one-note-per-season claim in ``love_note_service``.

    Re-deciding a report that has already been closed out as ``removed`` must not move the
    timestamp: ``removed_at`` is when the content came down, and re-stamping it would make the
    audit trail say the second click took it down rather than the first.

    The return value is what lets the caller report honestly. ``content_removed`` in the
    response is "this decision took something down", not "the thing was down when we got
    here", so a moderator who re-opens an already-removed report is not told a second post
    came down -- and, more importantly, the side effects that follow a removal (the comment
    count, the conversation preview) are only run for the transition, not for every click.
    """
    now = datetime.now(timezone.utc)
    model = type(row)
    result = session.execute(
        update(model)
        .where(model.id == str(row.id), model.removed_at.is_(None))
        .values(removed_at=now, removed_by_regno=reviewer_regno)
        # The session's copy of the row is brought back into step below rather than by the
        # update itself: it was read before the write, so it still says the content is live.
        .execution_options(synchronize_session=False)
    )
    # Expire the ORM copy so subsequent reads reflect the winning transaction's removal state.
    session.expire(row, ["removed_at", "removed_by_regno"])
    return result.rowcount == 1


def _decrement_comment_count(session, confession_id) -> bool:
    """Take one comment off the count its parent confession advertises.

    ``comment_count`` is denormalised onto the confession, so removing a comment has to walk
    back up and subtract one. The write is a single conditional ``UPDATE`` for the same reason
    the removal stamp is one: a read followed by a decrement loses updates when two removals
    land on the same parent at once, and the column is exactly the number the feed shows, so a
    lost update is a count that disagrees with the thread for as long as the row survives.

    ``WHERE comment_count > 0`` is the guard against a negative count, and it lives in the
    database where it cannot be raced past -- the column is denormalised and can already be out
    of step with the rows underneath it, and a parent in that state simply does not move.

    Returns whether the count moved. That is not the same question as "was the comment
    removed": the caller only gets here for a removal that actually happened, so a count that
    did not move means the column was already out of step, not that a removal went missing.
    """
    result = session.execute(
        update(ConfessionRecord)
        .where(
            ConfessionRecord.id == str(confession_id),
            ConfessionRecord.comment_count > 0,
        )
        .values(comment_count=ConfessionRecord.comment_count - 1)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def _clear_conversation_preview(conversation) -> None:
    conversation.last_message_at = None
    conversation.last_message_preview_ciphertext = None
    conversation.last_message_preview_nonce = None
    conversation.last_message_preview_key_version = None
    conversation.last_message_preview_message_id = None
    conversation.last_message_preview_sender_regno = None
    conversation.last_message_preview_delete_after = None


def _recompute_conversation_preview(session, conversation_id: Optional[str]) -> None:
    """Move a conversation's preview to the newest message that is still visible.

    A removed message stays in ``messages`` and is excluded from every participant read, so
    without this the conversation list would keep showing the first 100 characters of a post
    that has been taken down -- the one place the removal would still be leaking its text.

    The preview is an encrypted copy, so this decrypts the replacement message and re-encrypts
    its first 100 characters under the preview's own AAD. If the replacement cannot be
    decrypted the preview is cleared rather than left pointing at older text.

    The conversation row is locked before the newest message is chosen, and that is the
    whole point of the lock: the send path writes this same row while holding this same
    lock, so the two writers cannot interleave. Without it, a removal that runs while a
    send is still open chooses its replacement from a snapshot that does not contain the
    message being sent, and then writes that older preview over the newer one -- the
    conversation list regressing to a message that is not the newest, and briefly showing
    text a moderator has just taken down.
    """
    if not conversation_id:
        return
    conversation = session.scalar(
        select(ConversationRecord)
        .where(ConversationRecord.id == str(conversation_id))
        .with_for_update()
    )
    if conversation is None:
        return
    now = datetime.now(timezone.utc)
    newest = session.scalar(
        select(MessageRecord)
        .where(
            MessageRecord.conversation_id == str(conversation_id),
            MessageRecord.removed_at.is_(None),
            MessageRecord.delete_after > now,
        )
        .order_by(MessageRecord.created_at.desc())
        .limit(1)
    )
    if newest is None:
        _clear_conversation_preview(conversation)
        return
    try:
        plaintext = decrypt_matchmaking_message(newest)
    except ChatDecryptionError as exc:
        logger.error(
            "Conversation preview recompute could not decrypt message %s (%s); clearing preview",
            newest.id,
            exc.code,
        )
        _clear_conversation_preview(conversation)
        return
    preview = encrypt_matchmaking_preview(
        conversation_id=str(conversation_id),
        message_id=str(newest.id),
        sender_regno=newest.sender_regno,
        plaintext=(plaintext or "")[:100],
    )
    conversation.last_message_at = newest.created_at
    conversation.last_message_preview_ciphertext = preview.ciphertext
    conversation.last_message_preview_nonce = preview.nonce
    conversation.last_message_preview_key_version = preview.key_version
    conversation.last_message_preview_message_id = str(newest.id)
    conversation.last_message_preview_sender_regno = newest.sender_regno
    conversation.last_message_preview_delete_after = newest.delete_after


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

            if status_value == REMOVAL_STATUS and _stamp_removal(session, target, reviewer_regno):
                removed_content = True
                if is_comment:
                    _decrement_comment_count(session, target.confession_id)
                # An upheld report against the participant's writing. Inside the transition
                # branch for the same reason as the comment count: re-deciding a report that
                # was already ruled removed is not a second event and must not be counted as
                # one. A confession and a comment share the bucket -- see report_counters.
                record_removal(session, kind=CONFESSION, author_user_id=author_user_id)

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer_regno

    # Notify after commit so delivery failures cannot undo moderation. Reporters receive
    # the ruling; authors are notified only by the call that performed the removal.
    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=author_regno,
            reporter_regno=reporter_regno,
            kind="comment" if is_comment else "confession",
            cites_policy=True,
            took_it_down=removed_content,
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
    recipient's inbox and the entitlement check behind a delivery URL both read.
    """
    _assert_resolution_status(status_value)
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    reporter_regno: Optional[str] = None
    author_regno: Optional[str] = None
    removed_content = False
    image_to_destroy: Optional[str] = None

    with db.sessions.begin() as session:
        report = session.get(LoveNoteReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

        note = session.get(LoveNoteRecord, str(report.love_note_id))
        reporter_regno = report.reporter_regno
        if note is not None:
            author_regno = _regno_for_user(session, note.sender_id)
            if status_value == REMOVAL_STATUS:
                removed_content = _stamp_removal(session, note, reviewer.regno)
                if removed_content:
                    # Only on the transition: re-deciding an already-removed note is not a
                    # second takedown, and the sender's removal count has to say how many times
                    # their content actually came down.
                    record_removal(session, kind=LOVE_NOTE, author_user_id=note.sender_id)
                    # The note is invisible from here on, but its signed delivery URLs are
                    # not: they carry no expiry and Cloudinary keeps serving them. The
                    # object is destroyed after the commit below so a minted URL stops
                    # resolving instead of serving a picture of something taken down.
                    image_to_destroy = note.image_public_id

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer.regno

    if image_to_destroy:
        try:
            storage_service.delete_love_note_asset(image_to_destroy)
        except HTTPException:
            # The ruling stands -- the row is stamped removed either way. What failed is
            # the URL kill, and swallowing it would leave a live bearer nobody knows
            # about; log it with the note so the orphan can be found and destroyed later.
            logger.error(
                "Love note report %s removed its note but the image %s could not be "
                "destroyed; previously minted URLs may still resolve",
                report_id, image_to_destroy, exc_info=True,
            )

    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=author_regno,
            reporter_regno=reporter_regno,
            kind="love_note",
            cites_policy=True,
            took_it_down=removed_content,
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
        # message_id is nullable once ordinary retention has purged the row; the
        # report and its encrypted evidence survive and the ruling still closes.
        message = session.get(MessageRecord, str(report.message_id)) if report.message_id else None
        if message is not None:
            # A message can only be reported by its receiver, so the sender and the reporter
            # are always two different people here. The self-report case only arises for
            # confessions, and _notify_removal collapses it.
            if status_value == REMOVAL_STATUS and _stamp_removal(session, message, reviewer_regno):
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
            took_it_down=removed_content,
        )
    else:
        _notify_dismissal(db, reporter_regno)

    logger.info(f"Message report {report_id} closed as {status_value} by {reviewer_regno}")
    return {"id": str(report_id), "status": status_value, "content_removed": removed_content}


def resolve_case_closed_message_report(
    db: Database,
    report_id: str,
    status_value: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Close out a Case Closed detective-thread message report.

    The detective-thread counterpart to :func:`resolve_message_report`, and the
    same ruling: a ``removed`` decision stamps ``removed_at`` on the turn so the
    participant transcript (which filters on it) drops it, and a ``dismissed``
    decision leaves the turn exactly as it was. Unlike matchmaking chat there is
    no conversation-list preview to move forward -- the thread is only ever read
    whole -- so removal is the single write.
    """
    _assert_resolution_status(status_value)
    if not is_valid_id(report_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid report id")

    reporter_regno: Optional[str] = None
    author_regno: Optional[str] = None
    removed_content = False

    with db.sessions.begin() as session:
        report = session.get(CaseClosedMessageReportRecord, str(report_id))
        if report is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Report not found")

        reporter_regno = report.reporter_regno
        message = (
            session.get(CaseClosedMessageRecord, str(report.message_id))
            if report.message_id
            else None
        )
        if message is not None:
            author_regno = _regno_for_user(session, message.sender_user_id)
            if status_value == REMOVAL_STATUS and _stamp_removal(session, message, reviewer_regno):
                removed_content = True
                record_removal(session, kind=MESSAGE, author_regno=author_regno)
        elif report.reported_user_regno is not None:
            # The turn is gone from storage entirely; still charge the removal to
            # whoever wrote it, using the regno the report recorded at filing time.
            author_regno = report.reported_user_regno
            # There is no content row left to stamp, so the report's own status is the only
            # thing that can say whether this removal has already been charged. Without this
            # a re-decision would charge the writer a second removal for a turn that went
            # down once -- the same double count the content stamp prevents everywhere else.
            if status_value == REMOVAL_STATUS and report.status != REMOVAL_STATUS:
                removed_content = True
                record_removal(session, kind=MESSAGE, author_regno=author_regno)

        report.status = status_value
        report.reviewed_at = datetime.now(timezone.utc)
        report.reviewed_by_regno = reviewer_regno

    if status_value == REMOVAL_STATUS:
        _notify_removal(
            db,
            author_regno=author_regno,
            reporter_regno=reporter_regno,
            kind="message",
            cites_policy=True,
            took_it_down=removed_content,
        )
    else:
        _notify_dismissal(db, reporter_regno)

    logger.info(
        f"Case Closed message report {report_id} closed as {status_value} by {reviewer_regno}"
    )
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
        hidden = _stamp_removal(session, confession, reviewer_regno)
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
    identical branch in :func:`resolve_confession_report`. The decrement itself is a single
    conditional ``UPDATE`` (see :func:`_decrement_comment_count`), so two moderators taking
    two comments off the same parent at once cannot lose one another's subtraction.

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
        hidden = _stamp_removal(session, comment, reviewer_regno)
        already_hidden = not hidden
        removed_at = comment.removed_at
        removed_by_regno = comment.removed_by_regno

        if hidden:
            # Guarded on > 0 in the database rather than blind: the column is denormalised and
            # can already be out of step with the rows, and a negative comment count would
            # render as a nonsense number in the feed rather than as an obviously broken one.
            _decrement_comment_count(session, confession_id)
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


def _is_automated_removal(removed_by_regno: Optional[str]) -> bool:
    """Whether this hidden row was taken down by Azure Content Safety."""
    try:
        from .content_safety_service import AUTOMATED_REMOVED_BY
    except Exception:
        AUTOMATED_REMOVED_BY = "azure-content-safety"
    return str(removed_by_regno or "") == str(AUTOMATED_REMOVED_BY)


def unhide_confession(
    db: Database,
    confession_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Restore an Azure-hidden confession to public view.

    Only rows with ``removed_by_regno`` marking the automated system may be
    restored. Anything hidden by an admin/moderator (human regno) is refused
    with 409 so a second human cannot undo a colleague's decision here, and
    visible rows answer ``already_visible`` instead of erroring on a double
    click. Counters are left intact (the removal happened); reports stay for
    audit. No author notice: restoration is a reviewer override, not a ruling.
    """
    if not is_valid_id(confession_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confession id")

    with db.sessions.begin() as session:
        confession = session.get(ConfessionRecord, str(confession_id))
        if confession is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confession not found")
        if confession.removed_at is None:
            return {
                "id": str(confession_id),
                "content_removed": False,
                "already_visible": True,
                "removed_at": None,
                "removed_by_regno": None,
            }
        if not _is_automated_removal(confession.removed_by_regno):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This confession was hidden by a moderator and cannot be restored here.",
            )
        confession.removed_at = None
        confession.removed_by_regno = None

    logger.info(f"Confession {confession_id} restored by {reviewer_regno}")
    return {
        "id": str(confession_id),
        "content_removed": False,
        "already_visible": False,
        "removed_at": None,
        "removed_by_regno": None,
    }


def unhide_comment(
    db: Database,
    comment_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Restore an Azure-hidden comment to public view.

    Same gate as :func:`unhide_confession`: only system-hidden rows restore.
    The parent ``comment_count`` is incremented back (guarded, like the hide
    decrement) so the feed stops hiding a comment it can now open. Counters
    and reports stay for audit.
    """
    if not is_valid_id(comment_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid comment id")

    confession_id: Optional[str] = None
    with db.sessions.begin() as session:
        comment = session.get(ConfessionCommentRecord, str(comment_id))
        if comment is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")
        if comment.removed_at is None:
            return {
                "id": str(comment_id),
                "confession_id": str(comment.confession_id),
                "content_removed": False,
                "already_visible": True,
                "removed_at": None,
                "removed_by_regno": None,
            }
        if not _is_automated_removal(comment.removed_by_regno):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This comment was hidden by a moderator and cannot be restored here.",
            )
        confession_id = str(comment.confession_id)
        comment.removed_at = None
        comment.removed_by_regno = None
        parent = session.get(ConfessionRecord, confession_id)
        if parent is not None:
            parent.comment_count = int(parent.comment_count or 0) + 1

    logger.info(f"Comment {comment_id} restored by {reviewer_regno}")
    return {
        "id": str(comment_id),
        "confession_id": confession_id,
        "content_removed": False,
        "already_visible": False,
        "removed_at": None,
        "removed_by_regno": None,
    }


def unhide_message(
    db: Database,
    message_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Restore an Azure-hidden chat message to every participant read.

    Same gate as :func:`unhide_confession` and :func:`unhide_comment`: only
    system-hidden rows restore, manual hides answer 409 so a reviewer cannot
    undo a colleague's decision here. Already-visible rows answer
    ``already_visible`` instead of erroring on a double click.

    The conversation preview is recomputed, because it is a denormalised copy of
    the newest *visible* message and the restored message may now be that one.
    Counters are left intact -- the removal happened -- and the automated report
    stays for audit. No author notice: restoration is a reviewer override, not
    a ruling.
    """
    if not is_valid_id(message_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message id")

    with db.sessions.begin() as session:
        message = session.get(MessageRecord, str(message_id))
        if message is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        if message.removed_at is None:
            return {
                "id": str(message_id),
                "content_removed": False,
                "already_visible": True,
                "removed_at": None,
                "removed_by_regno": None,
            }
        if not _is_automated_removal(message.removed_by_regno):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This message was hidden by a moderator and cannot be restored here.",
            )
        message.removed_at = None
        message.removed_by_regno = None
        _recompute_conversation_preview(session, message.conversation_id)

    logger.info(f"Message {message_id} restored by {reviewer_regno}")
    return {
        "id": str(message_id),
        "content_removed": False,
        "already_visible": False,
        "removed_at": None,
        "removed_by_regno": None,
    }


def unhide_case_closed_message(
    db: Database,
    message_id: str,
    reviewer_regno: str,
) -> Dict[str, Any]:
    """Restore an Azure-hidden Case Closed detective-thread message.

    Same gate as the other ``unhide_*`` functions: only system-hidden rows
    restore, manual hides answer 409 so a reviewer cannot undo a colleague's
    decision here, and already-visible rows answer ``already_visible``. There is
    no conversation preview to recompute -- the thread is read whole -- so the
    single write is clearing the removal stamp. Counters and the automated
    report stay for audit, and no author is notified: this is a reviewer
    override, not a ruling.
    """
    if not is_valid_id(message_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid message id")

    with db.sessions.begin() as session:
        message = session.get(CaseClosedMessageRecord, str(message_id))
        if message is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
        if message.removed_at is None:
            return {
                "id": str(message_id),
                "content_removed": False,
                "already_visible": True,
                "removed_at": None,
                "removed_by_regno": None,
            }
        if not _is_automated_removal(message.removed_by_regno):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This message was hidden by a moderator and cannot be restored here.",
            )
        message.removed_at = None
        message.removed_by_regno = None

    logger.info(f"Case Closed message {message_id} restored by {reviewer_regno}")
    return {
        "id": str(message_id),
        "content_removed": False,
        "already_visible": False,
        "removed_at": None,
        "removed_by_regno": None,
    }


def pending_report_totals(db: Database) -> Dict[str, int]:
    """How many reports are still awaiting a decision, per queue.

    The one place the three queue totals are counted, so the dashboard's backlog figure and the
    queues behind it cannot disagree; the queue endpoints remain the one place a report is
    listed. A status that has already been ruled on is not counted, so a closed report cannot
    inflate what looks like outstanding work.
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
            "messages": (
                session.scalar(
                    select(func.count())
                    .select_from(MessageReportRecord)
                    .where(MessageReportRecord.status == "pending")
                ) or 0
            )
            + (
                session.scalar(
                    select(func.count())
                    .select_from(CaseClosedMessageReportRecord)
                    .where(CaseClosedMessageReportRecord.status == "pending")
                ) or 0
            ),
        }
