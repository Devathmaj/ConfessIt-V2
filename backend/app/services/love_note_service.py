from sqlalchemy import func, select
from ..postgres import Database
from ..db_models import LoveNoteRecord, LoveNoteReportRecord, UserRecord
from ..logger import get_logger
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from ..limits import REPORT_REASON_MAX
from fastapi import HTTPException, status

from ..models import UserDetails, LoveNote
from ..services.storage_service import storage_service
from ..services import asset_delivery_service
from ..services.report_resolution_service import resolve_status_filter
from ..accounts import AccountRef
from ..services.matchmaking_slot_service import list_participant_cohorts
from .report_counters import LOVE_NOTE, record_report_filed

# Logger
logger = get_logger(__name__)

class LoveNoteCreate(BaseModel):
    """
    Pydantic model for creating a love note.
    """
    recipient_id: str
    # Capped here so an oversized body is rejected during validation, before the
    # service (or base64.b64decode) allocates anything. This is the same budget
    # storage_service applies to the encoded string, so a payload that validates is
    # still within what the decoder will accept once the data URI prefix and any
    # embedded whitespace are folded out.
    image_base64: str = Field(max_length=storage_service.max_base64_chars)
    # Matches the String(2000) column in db_models.LoveNoteRecord; without a cap here
    # this field is just as unbounded as the image was.
    body: str = Field(max_length=2000)
    anonymous: bool

class LoveNoteReportCreate(BaseModel):
    """The body of a report filed by the recipient of a love note.

    The same reason field, and the same cap, as the confession and comment reports. A note
    may have been sent anonymously, which changes what the recipient knows and nothing
    about what staff see: the sender is withheld from the recipient and named here.
    """

    reason: str = Field(max_length=REPORT_REASON_MAX)


async def get_all_users_service(db: Database, viewer: AccountRef) -> List[dict]:
    """
    Service to fetch all users available as love note recipients, excluding the
    active user, any administrator accounts, and users who opted out of
    receiving love notes.

    Accounts a staff member has blocked are deliberately still listed: love notes are
    moderated independently of matchmaking, so a note may still reach a blocked
    recipient. The ``is_blocked`` flag travels with each row so the sender is asked
    to confirm before the note is sent.
    """
    with db.sessions() as session:
        rows = session.scalars(select(UserRecord).where(
            UserRecord.regno != viewer.regno,
            # No staff exclusion filter is needed: staff have no row in ``users``, so they
            # cannot appear as candidates in the first place. The rule is now a property of
            # the schema rather than a clause that has to be remembered here.
            UserRecord.love_notes_receive_enabled.is_(True),
        )).all()
        return [_user_payload(row, account=viewer) for row in rows]

async def get_all_cohorts_service(db: Database) -> List[Dict[str, Any]]:
    """Every ``(year, department)`` pair a love note recipient can be filtered by.

    Delegates to :func:`matchmaking_slot_service.list_participant_cohorts` rather than repeating
    the query. It used to be a second, identical copy of this function in this module, and the
    filter list and the matchmaking palette answering differently for the same database was a
    question nobody needed to be asking. One query, one answer.
    """
    return await list_participant_cohorts(db)


def _user_payload(user: UserRecord, *, account: AccountRef) -> dict:
    payload = {
        "id": str(user.id), "name": user.name, "emoji": user.emoji,
        "year": user.year, "department": user.department, "gender": user.gender,
        "interests": user.interests or [],
        "profile_picture_url": asset_delivery_service.profile_image_url(
            user.profile_picture_url, account=account
        ),
        "is_blocked": bool(user.is_blocked),
    }
    return payload

async def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """
    Service to create and save a new love note with 'pending_review' status.
    The base64 image is uploaded to Cloudinary and the URL is stored instead.
    """
    with db.sessions() as session:
        recipient = session.get(UserRecord, str(note_data.recipient_id))
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipient not found."
        )

    # Both parties are participants by construction: the recipient is read from ``users``
    # and the sender arrives through ``require_participant``. The role checks that used to
    # stand here are gone because there is no longer a value they could reject.
    try:
        # Upload the love note image to Cloudinary
        upload_result = storage_service.upload_love_note_image(
            image_base64=note_data.image_base64,
            sender_name=sender.name or sender.regno
        )
        
        # Use the Cloudinary URL instead of the base64 string
        cloudinary_url = upload_result["cloudinary_url"]
        
    except HTTPException:
        # Size, format and dimension rejections are the sender's problem and carry a
        # message worth showing. Re-raise as-is instead of collapsing them into a 500
        # that reports a server fault and hides which limit was hit.
        raise
    except Exception as e:
        logger.error(f"Failed to upload love note image to Cloudinary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload love note image"
        )
    
    # Create the love note with the Cloudinary URL
    love_note = LoveNote(
        sender_id=sender.id,
        recipient_id=str(note_data.recipient_id),
        image_url=cloudinary_url,
        body=note_data.body,
        anonymous=note_data.anonymous,
        status="pending_review"
    )

    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender.id, recipient_id=str(note_data.recipient_id),
            image_url=cloudinary_url, body=note_data.body,
            anonymous=note_data.anonymous, status="pending_review",
            created_at=love_note.created_at,
        )
        session.add(row)
        sender_row = session.get(UserRecord, sender.id)
        if sender_row:
            sender_row.love_notes_send_enabled = True
        session.flush()
        note_id = str(row.id)
        return {
            "message": "Love note sent successfully for review.",
            "note_id": note_id,
            # The sender is always entitled to their own note, in any review state.
            "image_url": asset_delivery_service.build_delivery_url(
                kind="love-notes", stored_identifier=cloudinary_url, account=AccountRef.of(sender)
            ) or cloudinary_url
        }
    
    logger.error("Failed to insert love note into the database.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not send the love note."
    )


# ============================================================================
# Admin-only Love Note Functions
# ============================================================================

async def get_all_love_notes_for_admin(db: Database) -> List[Dict[str, Any]]:
    """
    Fetch every love note for moderation, as the note's text and nothing else.

    A reviewer of an anonymous love note is judging a message, not the people who exchanged
    it, so this deliberately withholds every field that identifies either party -- sender and
    recipient ids, names, regnos and emails, the ``anonymous`` flag that says which of them is
    hidden, and the read receipt that says the recipient opened it. None of it is needed to
    decide whether a note is acceptable, and loading it at all would put the identities in
    the response where a future field could surface them.

    The image is withheld for the same reason. It is content in its own right, so a reviewer
    can only rule on the words the reviewer can read, and the asset proxy is not asked to
    mint a delivery URL for a review this payload no longer carries.

    What stays is what moderation needs to act and to be audited: the note id (every action
    takes it), the body, the moderation status, when it arrived, and the removal flags -- so
    a removed note is still listed and still shows who took it down and when.
    """
    items: List[Dict[str, Any]] = []
    with db.sessions() as session:
        rows = session.scalars(select(LoveNoteRecord).order_by(LoveNoteRecord.created_at.desc())).all()
    for doc in rows:
        items.append({
            "id": str(doc.id),
            "body": doc.body,
            "status": doc.status,
            "created_at": isoformat_datetime(doc.created_at),
            # Flagged, not filtered: staff see removed notes so a removal can be audited, and
            # ``status`` above keeps answering the separate question of whether the note was
            # judged acceptable.
            "content_removed": doc.removed_at is not None,
            "removed_at": isoformat_datetime(doc.removed_at),
            "removed_by_regno": doc.removed_by_regno,
        })
    return items


async def update_love_note_status_service(
    note_id: str, 
    status_value: str, 
    db: Database
) -> Dict[str, Any]:
    """
    Update the status of a love note and send appropriate notifications.

    A rejection is final. ``pending_review -> rejected`` is a judgement that the note was never
    acceptable, and a moderator reversing that would mean a note that broke the rules could be
    delivered by a second click, so the transition is refused here rather than merely not
    offered by the screen. ``approved -> rejected`` is a different act under the same status:
    the note was delivered and is being taken back out of the recipient's inbox. It is
    permitted, and it tells the sender so in different words.

    Args:
        note_id: The str of the love note
        status_value: New status (approved, rejected, pending_review)
        db: PostgreSQL database instance
        
    Returns:
        Dict with updated note_id and status
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid love note id"
        )

    # Get the love note before updating. The previous status is read inside the transaction and
    # decides which notice the sender gets, so it has to be the value the row actually held --
    # read outside, a second moderator clicking in between would produce the wrong notice.
    previous_status: Optional[str] = None
    with db.sessions.begin() as session:
        love_note = session.get(LoveNoteRecord, str(note_id))
        if love_note:
            previous_status = love_note.status
            if previous_status == "rejected" and status_value != "rejected":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This love note was rejected and cannot be approved later.",
                )
            love_note.status = status_value
    if not love_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    
    # Create notifications based on status change
    from ..services.notification_service import create_notification_service
    
    with db.sessions() as session:
        sender = session.get(UserRecord, love_note.sender_id)
        recipient = session.get(UserRecord, love_note.recipient_id)
    
    if status_value == "approved":
        # Notify recipient that they received a love note
        if recipient:
            sender_name = "Someone" if love_note.anonymous else (
                sender.name if sender else "Someone"
            )
            # The one notification here with a target. This notice is written at the exact
            # moment the note becomes readable in the recipient's inbox and nowhere else --
            # the inbox lists approved notes and nothing else -- so pointing at the note id is
            # the difference between a reader who has to go looking and one who lands on it.
            create_notification_service(
                recipient_regno=recipient.regno,
                heading="💌 You received a Love Note!",
                body=f"{sender_name} sent you a love note! Go to Love Notes → Inbox to view it.",
                db=db,
                target_kind="love_note",
                target_id=str(love_note.id),
            )
        
        # Notify sender that their love note was accepted. No target: a sender has no list of
        # notes they have sent to open, so there is nowhere on any page for this to land.
        if sender:
            create_notification_service(
                recipient_regno=sender.regno,
                heading="✅ Love Note Delivered!",
                body="Your love note was reviewed and successfully delivered to the recipient.",
                db=db
            )
    
    elif status_value == "rejected":
        # Two different things end in ``rejected``, and the sender is owed an accurate account
        # of which one happened to them. A note refused before it was ever delivered reads as
        # "could not be sent". A note that was delivered and has now been pulled back out of
        # the recipient's inbox reads as "taken down", because the sender has already seen the
        # delivered notice and the silent disappearance would be the confusing part. Telling
        # the second one "could not be sent" would state something untrue about a note that
        # demonstrably arrived. No target for either, same reason as above.
        if sender:
            if previous_status == "approved":
                create_notification_service(
                    recipient_regno=sender.regno,
                    heading="💔 Love Note Taken Down",
                    body="Your love note was taken down by a moderator and is no longer available to the recipient.",
                    db=db
                )
            else:
                create_notification_service(
                    recipient_regno=sender.regno,
                    heading="❌ Love Note Not Delivered",
                    body="Your love note could not be sent. It may have violated our community guidelines.",
                    db=db
                )
    
    return {"id": note_id, "status": status_value}


async def report_love_note_service(
    note_id: str,
    report_data: LoveNoteReportCreate,
    reporter: UserDetails,
    db: Database,
) -> Dict[str, Any]:
    """
    File a report against a love note the reporter received.

    The recipient is the only party entitled to report, for the same reason the receiver is
    the only party entitled to report a message: a note is private to two people, and the
    person who did not write it is the one who has standing to complain. A duplicate is
    refused rather than merged, so a report cannot be re-filed to reset its place in the
    queue.
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid love note id",
        )

    reason = (report_data.reason or "").strip()
    if not reason:
        # A blank reason is a report nobody can act on, and it is the kind of thing a
        # client sends by accident. Refusing it here keeps the queue free of rows that
        # exist only to be closed again.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A report needs a reason.",
        )

    with db.sessions.begin() as session:
        note = session.get(LoveNoteRecord, str(note_id))
        if note is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Love note not found",
            )
        if note.recipient_id != reporter.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You can only report love notes sent to you",
            )
        existing = session.scalar(select(LoveNoteReportRecord.id).where(
            LoveNoteReportRecord.love_note_id == str(note_id),
            LoveNoteReportRecord.reporter_regno == reporter.regno,
        ).limit(1))
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already reported this love note",
            )

        row = LoveNoteReportRecord(
            love_note_id=str(note_id),
            reporter_regno=reporter.regno,
            reason=reason,
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(row)
        session.flush()
        report_id = str(row.id)
        # The reporter is the recipient, so the person the complaint belongs to is the sender.
        # Counted here, in the same transaction as the report row, so a rolled-back filing
        # leaves no phantom complaint against them.
        record_report_filed(session, kind=LOVE_NOTE, author_user_id=note.sender_id)

    logger.info(f"Love note {note_id} reported by {reporter.regno} for: {reason}")

    return {"id": report_id, "message": "Love note reported successfully"}


async def get_love_note_reports_for_admin(
    db: Database,
    viewer: AccountRef,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    """
    The staff report queue, newest first, with the reported note joined in.

    The note is carried rather than referenced: the question a moderator opens this screen
    to answer is "what did this note actually say", and a row holding only a note id sends
    them to a second screen to find out. The sender comes with it, because a report that
    says *this note is unacceptable* is only actionable once staff can see who wrote it --
    the recipient may never have been told, which is the whole point of an anonymous note.

    The join is an inner join on purpose. The report's foreign key cascades, so a report
    cannot outlive its note; an outer join would only ever render a "[deleted]" row that no
    code path can produce.
    """
    filters = []
    if status_filter:
        filters.append(LoveNoteReportRecord.status.in_(resolve_status_filter(status_filter)))

    with db.sessions() as session:
        rows = session.execute(
            select(LoveNoteReportRecord, LoveNoteRecord)
            .join(LoveNoteRecord, LoveNoteRecord.id == LoveNoteReportRecord.love_note_id)
            .where(*filters)
            .order_by(LoveNoteReportRecord.reported_at.desc())
            .offset(skip).limit(limit)
        ).all()
        # Counted over the same join rather than over the report table alone, so ``total``
        # is the number of rows this endpoint can actually return instead of a number the
        # pager cannot reach.
        total = session.scalar(
            select(func.count())
            .select_from(LoveNoteReportRecord)
            .join(LoveNoteRecord, LoveNoteRecord.id == LoveNoteReportRecord.love_note_id)
            .where(*filters)
        ) or 0

        # The reporter is stored as a regno and the sender as a ``users.id``, so both
        # columns have to be resolved. Two bulk reads rather than two per row: this is the
        # N+1 mistake the confession admin list is called out for in the knowledge base,
        # and it is not worth repeating on a queue that grows with every report filed.
        reporter_regnos = {report.reporter_regno for report, _note in rows}
        sender_ids = {str(note.sender_id) for _report, note in rows}
        reporters = {
            user.regno: user
            for user in session.scalars(
                select(UserRecord).where(UserRecord.regno.in_(reporter_regnos))
            ).all()
        } if reporter_regnos else {}
        senders = {
            str(user.id): user
            for user in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(sender_ids))
            ).all()
        } if sender_ids else {}

    items: List[Dict[str, Any]] = []
    for report, note in rows:
        reporter = reporters.get(report.reporter_regno)
        sender = senders.get(str(note.sender_id))
        items.append({
            "id": str(report.id),
            "love_note_id": str(report.love_note_id),
            "body": note.body,
            "anonymous": bool(note.anonymous),
            "note_status": note.status,
            "note_created_at": isoformat_datetime(note.created_at),
            "image_url": (
                asset_delivery_service.build_delivery_url(
                    kind="love-notes", stored_identifier=note.image_url, account=viewer
                ) or note.image_url
            ),
            "reporter": {
                "regno": report.reporter_regno,
                "name": reporter.name if reporter else "Unknown",
            },
            "reported_user": {
                "id": str(note.sender_id),
                "name": sender.name if sender else "Unknown",
                "regno": sender.regno if sender else None,
            },
            "reason": report.reason,
            "reported_at": isoformat_datetime(report.reported_at),
            "status": report.status,
            "reviewed_at": isoformat_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            # Whether the note this report is about has been taken down, so a moderator sees
            # that before deciding. Kept beside ``status`` because the two answer different
            # questions and the screen needs both.
            "content_removed": bool(note.removed_at) if note else False,
        })

    return {"reports": items, "total": total, "skip": skip, "limit": limit}





async def delete_love_note_service(note_id: str, db: Database) -> Dict[str, Any]:
    """
    Delete a love note from the system.
    
    Args:
        note_id: The str of the love note
        db: PostgreSQL database instance
        
    Returns:
        Dict with deleted note_id
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid love note id"
        )

    with db.sessions.begin() as session:
        love_note = session.get(LoveNoteRecord, str(note_id))
        if love_note:
            session.delete(love_note)
    if not love_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    return {"deleted": note_id}
