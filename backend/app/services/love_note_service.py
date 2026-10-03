from sqlalchemy import func, or_, select
from ..postgres import Database
from ..db_models import LoveNoteRecord, LoveNoteReportRecord, UserRecord
from ..logger import get_logger
from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime, timezone
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from ..utils import ilike_contains
from ..limits import LOVE_NOTE_BODY_MAX, REPORT_REASON_MAX
from fastapi import HTTPException, status

from ..models import UserDetails, LoveNote, LoveNoteImageIdentity
from ..services.storage_service import storage_service
from ..services.report_resolution_service import resolve_status_filter
from ..accounts import AccountRef, Capability, has_capability
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
    # Matches the ``ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}`` constraint on
    # db_models.LoveNoteRecord, and is the legibility limit described there rather than a storage
    # one: the note is rasterised onto a fixed card, so a body longer than this could only be
    # rendered at a size nobody reads. Without the cap here this field is just as unbounded as the
    # image was.
    body: str = Field(max_length=LOVE_NOTE_BODY_MAX)
    anonymous: bool

class LoveNoteReportCreate(BaseModel):
    """The body of a report filed by the recipient of a love note.

    The same reason field, and the same cap, as the confession and comment reports. A note
    may have been sent anonymously, which changes what the recipient knows and nothing
    about what staff see: the sender is withheld from the recipient and named here.
    """

    reason: str = Field(max_length=REPORT_REASON_MAX)


def get_all_users_service(db: Database, viewer: AccountRef) -> List[dict]:
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

def get_all_cohorts_service(db: Database) -> List[Dict[str, Any]]:
    """Every ``(year, department)`` pair a love note recipient can be filtered by.

    Delegates to :func:`matchmaking_slot_service.list_participant_cohorts` rather than repeating
    the query. It used to be a second, identical copy of this function in this module, and the
    filter list and the matchmaking palette answering differently for the same database was a
    question nobody needed to be asking. One query, one answer.
    """
    return list_participant_cohorts(db)


def _user_payload(user: UserRecord, *, account: AccountRef) -> dict:
    payload = {
        "id": str(user.id), "name": user.name,
        "year": user.year, "department": user.department, "gender": user.gender,
        "interests": user.interests or [],
        # A profile picture is public on Cloudinary's CDN, so this is a plain derivation from the
        # stored identity. The recipient list is shown to every participant who may send a note,
        # which is exactly the set of people who are allowed to see a profile picture.
        "profile_picture_url": storage_service.profile_delivery_url(
            user.profile_picture_public_id, user.profile_picture_format
        ),
        "is_blocked": bool(user.is_blocked),
    }
    return payload


def may_view_love_note_image(
    note: LoveNoteRecord, *, viewer_id: str | None, viewer_account_type: str | None
) -> bool:
    """Whether this viewer may be handed a working address for ``note``'s image.

    A love note's delivery URL is a durable bearer capability, not a convenience: it
    authorises Cloudinary to serve that one image to whoever holds it, indefinitely --
    the signature does not expire (see ``storage_service.love_note_delivery_url``). So
    this question is answered
    before any token is minted, and it is answered from the row rather than from anything the
    request carried. The rules are unchanged from when the backend proxied the bytes, and each
    one exists for a reason:

    * A staff account holding ``love_notes:review`` may open any note, in any state. Moderation
      means ruling on notes that are pending, rejected and already removed -- a reviewer who could
      not see the image of the note they are ruling on would be reviewing it blind.
    * Anyone else is refused once the note has been taken down (``removed_at``). Removal is the
      platform withdrawing a note, and it has to withdraw the picture with it: leaving the image
      reachable for a few more minutes because the sender has the URL would make the moderation
      action reversible by anyone who already saw it.
    * The sender may always see their own note, in any review state. They wrote it, and the
      "was it delivered?" screen is the whole point of the sent list.
    * The recipient may see it only once it is ``approved``, and only while it has not been
      removed. A pending note is exactly one the recipient must not see yet.
    """
    if viewer_account_type is not None and has_capability(
        viewer_account_type, Capability.LOVE_NOTES_REVIEW
    ):
        return True
    if note.removed_at is not None:
        return False
    if viewer_id is None:
        return False
    viewer_id = str(viewer_id)
    if str(note.sender_id) == viewer_id:
        return True
    return str(note.recipient_id) == viewer_id and note.status == "approved"


def love_note_image_url(
    note: LoveNoteRecord, *, viewer_id: str | None, viewer_account_type: str | None
) -> Optional[str]:
    """The signed delivery URL for ``note``'s image, or ``None`` if the viewer is not entitled.

    The single place a love note image address is created. Keeping authorisation and signing in
    one function is what makes it hard to add a second call site that hands out an address without
    asking first -- which is the only way this control could be got wrong now that the bytes are
    served by somebody else's server and nothing on our side re-checks a URL after it is issued.

    That single choke point is load-bearing here for a reason specific to signed URLs: they do not
    expire, so there is no later moment at which a mistake can be corrected. Every check has to
    happen here, every time.
    """
    if not may_view_love_note_image(
        note, viewer_id=viewer_id, viewer_account_type=viewer_account_type
    ):
        return None
    return storage_service.love_note_delivery_url(note.image_public_id, note.image_format)


def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """
    Service to create and save a new love note with 'pending_review' status.

    The note's id is minted *before* the image is uploaded, because the object's public_id is
    derived from it (``love_notes/{note_id}/image``). That ordering is what makes the object
    one-to-one with the row: the image cannot be named after anything the sender supplied, it
    cannot be re-pointed at another note, and deleting the note gives the deletion code a public_id
    it can read straight out of the database instead of parsing it back out of a stored URL.
    """
    with db.sessions() as session:
        recipient = session.get(UserRecord, str(note_data.recipient_id))
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipient not found."
        )

    note_id = new_id()

    # Both parties are participants by construction: the recipient is read from ``users``
    # and the sender arrives through ``require_participant``. The role checks that used to
    # stand here are gone because there is no longer a value they could reject.
    try:
        stored_image = storage_service.upload_love_note_image(
            image_base64=note_data.image_base64,
            love_note_id=note_id,
        )
    except HTTPException:
        # Size, format and dimension rejections are the sender's problem and carry a
        # message worth showing. Re-raise as-is instead of collapsing them into a 500
        # that reports a server fault and hides which limit was hit.
        raise
    except Exception:
        logger.error("Failed to upload love note image to Cloudinary", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload love note image"
        )

    love_note = LoveNote(
        id=note_id,
        sender_id=sender.id,
        recipient_id=str(note_data.recipient_id),
        image=LoveNoteImageIdentity(
            public_id=stored_image["public_id"], format=stored_image["format"]
        ),
        body=note_data.body,
        anonymous=note_data.anonymous,
        status="pending_review"
    )

    try:
        with db.sessions.begin() as session:
            row = LoveNoteRecord(
                id=note_id,
                sender_id=sender.id, recipient_id=str(note_data.recipient_id),
                image_public_id=stored_image["public_id"], image_format=stored_image["format"],
                body=note_data.body,
                anonymous=note_data.anonymous, status="pending_review",
                created_at=love_note.created_at,
            )
            session.add(row)
            sender_row = session.get(UserRecord, sender.id)
            if sender_row:
                sender_row.love_notes_send_enabled = True
            session.flush()
    except Exception:
        # The image is already in the bucket with nothing pointing at it. Best-effort cleanup
        # rather than a leak: the note was never created, so the object is unreachable through
        # any URL this application will ever produce.
        logger.error("Failed to insert love note into the database; discarding uploaded image", exc_info=True)
        try:
            storage_service.delete_love_note_asset(stored_image["public_id"])
        except Exception:
            logger.error("Failed to discard image for unsaved love note %s", note_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send the love note."
        )

    # Staff-facing notice (role-based, best-effort): a note awaiting approval is
    # news to every moderator and admin, and a failed write here must never
    # unwind the note that was just committed.
    try:
        from .staff_notification_service import notify_love_note_pending

        notify_love_note_pending(db, note_id=note_id)
    except Exception:
        logger.warning("Failed to queue staff notification for love note %s", note_id)

    return {
        "message": "Love note sent successfully for review.",
        "note_id": note_id,
        # The sender is always entitled to their own note, in any review state.
        "image_url": storage_service.love_note_delivery_url(
            stored_image["public_id"], stored_image["format"]
        ),
    }


# ============================================================================
# Admin-only Love Note Functions
# ============================================================================

# Hard ceiling for one page of the admin love-note list. The endpoint clamps anything
# larger, so no client -- present or future, well-meaning or not -- can ask for every note
# and make the backend materialise them all at once.
LOVE_NOTES_MAX_LIMIT = 100

LOVE_NOTE_STATUSES = frozenset({"pending_review", "approved", "rejected"})

LOVE_NOTE_ORDERS = frozenset({"newest", "oldest"})


def _serialise_admin_love_note(doc: Any) -> Dict[str, Any]:
    """One love note as the moderation list carries it: text and audit fields, no people."""
    return {
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
    }


def get_admin_love_notes_data(
    db: Database,
    *,
    skip: int = 0,
    limit: int = 50,
    status: str = "all",
    order: str = "newest",
) -> Dict[str, Any]:
    """One page of love notes for moderation, filtered, ordered and counted in SQL.

    Every filter here is a WHERE, the timeline order is an ORDER BY, and every page is a
    LIMIT/OFFSET on the database itself: with thousands of notes the backend reads and
    serialises at most ``limit`` rows, never the whole table, and ``total`` is a COUNT
    under the same WHERE so the screen can keep loading more downwards without ever
    holding the notes it is not showing.

    Each note travels as its text and nothing else. A reviewer of an anonymous love note
    is judging a message, not the people who exchanged it, so sender and recipient ids,
    names, regnos and emails, the ``anonymous`` flag and the read receipt are withheld --
    none of it is needed to decide whether a note is acceptable, and loading it at all
    would put the identities in the response where a future field could surface them.
    The image is withheld for the same reason: a reviewer can only rule on the words the
    reviewer can read. What stays is what moderation needs to act and to be audited: the
    note id (every action takes it), the body, the moderation status, when it arrived,
    and the removal flags -- so a removed note is still listed and still shows who took
    it down and when.
    """
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), LOVE_NOTES_MAX_LIMIT))
    if status not in LOVE_NOTE_STATUSES and status != "all":
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid status")
    if order not in LOVE_NOTE_ORDERS:
        from fastapi import HTTPException, status as _status

        raise HTTPException(status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid order")

    filters: list = []
    if status != "all":
        filters.append(LoveNoteRecord.status == status)
    # ``id`` second so two notes stamped in the same instant still come back in the same
    # order on every page: an unordered tail reshuffles between the count and the read,
    # and a load-more list that reorders under the reader shows some notes twice.
    ordering = (
        [LoveNoteRecord.created_at.desc(), LoveNoteRecord.id.asc()]
        if order == "newest"
        else [LoveNoteRecord.created_at.asc(), LoveNoteRecord.id.asc()]
    )

    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(LoveNoteRecord).where(*filters)) or 0
        rows = session.scalars(
            select(LoveNoteRecord).where(*filters).order_by(*ordering).offset(skip).limit(limit)
        ).all()
    return {
        "notes": [_serialise_admin_love_note(doc) for doc in rows],
        "total": total,
        "skip": skip,
        "limit": limit,
    }


def update_love_note_status_service(
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


def report_love_note_service(
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

    # Staff-facing notice (role-based, best-effort): a failed write here must
    # never unwind the report that was just committed.
    try:
        from .staff_notification_service import notify_report_filed

        notify_report_filed(
            db, kind="love note", report_id=report_id,
            summary=f"A love note was reported by {reporter.regno}. Open Reports review to rule on it.",
        )
    except Exception:
        logger.warning("Failed to queue staff notification for love note report %s", note_id)

    return {"id": report_id, "message": "Love note reported successfully"}


def get_love_note_reports_for_admin(
    db: Database,
    viewer: AccountRef,
    status_filter: Optional[str] = None,
    skip: int = 0,
    limit: int = 50,
    query: Optional[str] = None,
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

    ``query`` narrows in SQL to reports whose reason, note body, reporter regno or sender
    name/regno matches, with the total counted under the same filter so the screen pages
    the question it asked. The sender is searchable here -- unlike a confession author --
    because this queue names them on every row: searching cannot reveal what the response
    does not already say.
    """
    filters = []
    if status_filter:
        filters.append(LoveNoteReportRecord.status.in_(resolve_status_filter(status_filter)))
    cleaned = (query or "").strip()
    search_senders = bool(cleaned)
    if cleaned:
        filters.append(or_(
            ilike_contains(LoveNoteReportRecord.reason, cleaned),
            ilike_contains(LoveNoteReportRecord.reporter_regno, cleaned),
            ilike_contains(LoveNoteRecord.body, cleaned),
            ilike_contains(UserRecord.name, cleaned),
            ilike_contains(UserRecord.regno, cleaned),
        ))

    def _joined(statement):
        statement = statement.join(
            LoveNoteRecord, LoveNoteRecord.id == LoveNoteReportRecord.love_note_id
        )
        # Many-to-one on unique keys both ways, so neither the page nor the COUNT can fan
        # out. Joined only when the search needs it, so the unfiltered queue pays nothing.
        if search_senders:
            statement = statement.outerjoin(UserRecord, UserRecord.id == LoveNoteRecord.sender_id)
        return statement

    with db.sessions() as session:
        rows = session.execute(
            _joined(select(LoveNoteReportRecord, LoveNoteRecord))
            .where(*filters)
            .order_by(LoveNoteReportRecord.reported_at.desc())
            .offset(skip).limit(limit)
        ).all()
        # Counted over the same join rather than over the report table alone, so ``total``
        # is the number of rows this endpoint can actually return instead of a number the
        # pager cannot reach.
        total = session.scalar(
            _joined(select(func.count()).select_from(LoveNoteReportRecord))
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
            "image_url": love_note_image_url(
                note, viewer_id=None, viewer_account_type=viewer.account_type
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





def delete_love_note_service(note_id: str, db: Database) -> Dict[str, Any]:
    """
    Delete a love note, permanently, and destroy the image it points at.

    The order is deliberate and is the same rule the profile picture path follows: the object is
    destroyed *before* the row goes, never after. The row is what names the object, so destroying
    it last would mean an interrupted deletion left a live row whose image no longer exists --
    which is a broken note nobody can repair, and is indistinguishable from data loss. Destroying
    it first means the worst case is an orphaned object that costs storage and nothing else.

    The public_id is read from the authoritative row, never reconstructed from a request or a URL,
    so the deletion is provably about the image this note had. An object Cloudinary has never heard
    of is not an error: the note is being removed either way, and refusing because the image was
    already gone would leave a note that can never be deleted. An *unexpected* failure is logged
    and raised rather than swallowed, because that is the case where an orphan is being created
    and somebody has to know.
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid love note id"
        )

    with db.sessions() as session:
        existing = session.get(LoveNoteRecord, str(note_id))
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Love note not found"
            )
        image_public_id = existing.image_public_id

    try:
        storage_service.delete_love_note_asset(image_public_id)
    except Exception:
        # Logged with the note id so the orphan can be found and cleaned up later. Raising here
        # leaves the row intact, which is the recoverable half of the trade.
        logger.error(
            "Deleted love note %s but failed to destroy its image %s; the object is orphaned",
            note_id, image_public_id, exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not delete the love note's image. The note was left in place.",
        )

    with db.sessions.begin() as session:
        love_note = session.get(LoveNoteRecord, str(note_id))
        if love_note:
            session.delete(love_note)
    return {"deleted": note_id}


def rotate_sender_love_note_images(db: Database, sender_id: str) -> Dict[str, int]:
    """Rotate every live image of one sender's love notes, killing old signed URLs.

    Called when the sender's account is blocked: the notes themselves stay (blocking
    takes away sign-in, not content), but every bearer URL minted while the account
    was live must stop resolving. Rotation -- destroy plus re-upload of the same bytes
    under the same public_id -- bumps the object version, so old signatures no longer
    verify while entitled viewers keep working through freshly minted URLs.

    Only notes that are still visible are rotated: a removed note's image was already
    destroyed when it came down, and a note with no image has nothing to rotate. Each
    asset is best-effort -- rotation never raises -- and the returned counts say how
    many were replaced and how many need manual cleanup.

    Returns ``{"rotated": <n>, "failed": <m>}``.
    """
    with db.sessions() as session:
        rows = session.execute(
            select(
                LoveNoteRecord.image_public_id, LoveNoteRecord.image_format
            ).where(
                LoveNoteRecord.sender_id == str(sender_id),
                LoveNoteRecord.removed_at.is_(None),
                LoveNoteRecord.image_public_id.is_not(None),
            )
        ).all()
    targets = [(public_id, image_format) for public_id, image_format in rows]

    rotated = 0
    failed = 0
    for public_id, image_format in targets:
        try:
            ok = storage_service.rotate_love_note_asset(public_id, image_format)
        except Exception as exc:
            logger.error(
                "Love note asset rotation raised for %s during sender rotation: %s",
                public_id, exc, exc_info=True,
            )
            ok = False
        if ok:
            rotated += 1
        else:
            failed += 1
            logger.error(
                "Love note asset rotation failed for %s; previously minted URLs for "
                "sender %s may still resolve",
                public_id, sender_id,
            )

    if targets:
        logger.info(
            "Rotated %d of %d love note images for sender %s",
            rotated, len(targets), sender_id,
        )
    return {"rotated": rotated, "failed": failed}
