from sqlalchemy import func, or_, select, update
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
from .pii_detection_service import PiiDetectionError, redact_love_note_body
from ..services.report_resolution_service import resolve_status_filter
from ..accounts import AccountRef, Capability, has_capability
from ..services.matchmaking_slot_service import list_participant_cohorts
from .report_counters import LOVE_NOTE, record_report_filed

# Logger
logger = get_logger(__name__)

# One definition each for the season-spent and opt-out refusals: the client renders the
# server's ``detail`` verbatim, so two spellings of one refusal would read as two refusals.
SEASON_ALREADY_CLAIMED = "You have already sent your love note for this season."
RECIPIENT_NOT_ACCEPTING = "This participant is not accepting love notes."


def _discard_uploaded_image(note_id: str, public_id: str) -> None:
    """Delete an image that was uploaded for a note which will never exist.

    Housekeeping rather than a decision about the note -- logged, not raised, since the
    note is already lost and failed cleanup must not turn a refusal into another error.
    """
    try:
        storage_service.delete_love_note_asset(public_id)
    except Exception:
        logger.error("Failed to discard image for unsaved love note %s", note_id)


class LoveNoteCreate(BaseModel):
    """Pydantic model for creating a love note."""
    recipient_id: str
    # Capped so an oversized body is rejected during validation, before the service
    # allocates anything; the same budget storage_service applies to the encoded string.
    image_base64: str = Field(max_length=storage_service.max_base64_chars)
    # Matches the ``ck_love_notes_body_length_{LOVE_NOTE_BODY_MAX}`` constraint, and is
    # the legibility limit rather than a storage one: the note rasterises onto a fixed card.
    body: str = Field(max_length=LOVE_NOTE_BODY_MAX)
    anonymous: bool

class LoveNoteReportCreate(BaseModel):
    """The body of a report filed by the recipient of a love note.

    Same reason field and cap as the confession/comment reports; the sender is withheld
    from the recipient and named to staff here.
    """

    reason: str = Field(max_length=REPORT_REASON_MAX)


# Ceiling for one page of the recipient list; the endpoint clamps anything larger.
LOVE_NOTE_RECIPIENTS_MAX_LIMIT = 100


def get_all_users_service(
    db: Database,
    viewer: UserDetails,
    *,
    skip: int = 0,
    limit: int = 50,
    query: Optional[str] = None,
    include_same_gender: bool = False,
    year: Optional[int] = None,
    department: Optional[str] = None,
) -> Dict[str, Any]:
    """One page of users available as love note recipients, filtered and counted in SQL.

    Filters exclude the viewer, opted-out accounts and (unless ``include_same_gender``)
    the viewer's own gender; ``query`` is a case-insensitive SQL substring. Blocked
    accounts are still listed (notes are moderated independently of matchmaking) with
    ``is_blocked`` shown; staff have no ``users`` row so need no exclusion filter.
    """
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), LOVE_NOTE_RECIPIENTS_MAX_LIMIT))

    filters: list = [
        UserRecord.regno != viewer.regno,
        UserRecord.love_notes_receive_enabled.is_(True),
    ]
    cleaned_query = (query or "").strip()
    if cleaned_query:
        filters.append(ilike_contains(UserRecord.name, cleaned_query))
    if not include_same_gender:
        filters.append(UserRecord.gender != viewer.gender)
    if year is not None:
        filters.append(UserRecord.year == year)
    cleaned_department = (department or "").strip()
    if cleaned_department:
        filters.append(func.lower(UserRecord.department) == cleaned_department.lower())

    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(UserRecord).where(*filters)) or 0
        rows = session.scalars(
            select(UserRecord).where(*filters)
            # ``id`` seconds the name so shared names order identically on every page; an
            # unordered tail reshuffles between count and read, showing some people twice.
            .order_by(UserRecord.name.asc(), UserRecord.id.asc())
            .offset(skip)
            .limit(limit)
        ).all()
    return {
        "users": [_user_payload(row) for row in rows],
        "total": total,
        "skip": skip,
        "limit": limit,
    }

def get_all_cohorts_service(db: Database) -> List[Dict[str, Any]]:
    """Every ``(year, department)`` pair a love note recipient can be filtered by.

    Delegates to ``matchmaking_slot_service.list_participant_cohorts``: one query,
    one answer, instead of a second copy that could drift.
    """
    return list_participant_cohorts(db)


def _user_payload(user: UserRecord) -> dict:
    payload = {
        "id": str(user.id), "name": user.name,
        "year": user.year, "department": user.department, "gender": user.gender,
        "interests": user.interests or [],
        # The picture is public on Cloudinary's CDN, so a plain derivation; the recipient
        # list is shown to exactly the set of people allowed to see a profile picture.
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

    The delivery URL is a durable bearer capability (the signature never expires), so this
    is answered before any token is minted: staff with ``love_notes:review`` may open any
    state; once removed, only staff; the sender always; the recipient only when approved.
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

    The single place a love note image address is created, keeping authorisation and
    signing together so a call site cannot skip the check; signed URLs never expire, so
    every check happens here, every time.
    """
    if not may_view_love_note_image(
        note, viewer_id=viewer_id, viewer_account_type=viewer_account_type
    ):
        return None
    return storage_service.love_note_delivery_url(note.image_public_id, note.image_format)


def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """Create and save a new love note with 'pending_review' status.

    The note id is minted before the image upload because the object's public_id derives
    from it, making the image one-to-one with the row. One note per season is claimed by
    a conditional update in the note's own transaction; the recipient's opt-out is
    enforced here as well as in the picker.
    """
    with db.sessions() as session:
        recipient = session.get(UserRecord, str(note_data.recipient_id))
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipient not found."
        )

    # The recipient's own switch, enforced here and not only in the picker: a client that
    # knows a recipient id talks to the API. This is the fast-fail keeping a refused note
    # from uploading an image at all; the authoritative reading happens in the transaction.
    if not recipient.love_notes_receive_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=RECIPIENT_NOT_ACCEPTING,
        )

    note_id = new_id()

    # Both parties are participants by construction (recipient read from ``users``; sender
    # via ``require_participant``), so the old role checks had nothing left to reject.
    try:
        stored_image = storage_service.upload_love_note_image(
            image_base64=note_data.image_base64,
            love_note_id=note_id,
        )
    except HTTPException:
        # Size, format and dimension rejections are the sender's problem and carry a
        # message worth showing; re-raise as-is rather than collapsing into a 500 that
        # hides which limit was hit.
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

    # The moderator-facing preview is produced exactly once, here, and stored with the row:
    # redacting on read would re-run the provider on every reload, making an outage a
    # moderation outage. Failure stores *nothing* (never the raw body); nothing is logged
    # but the failure code.
    moderation_body: Optional[str] = None
    try:
        moderation_body = redact_love_note_body(note_data.body).moderation_body
    except PiiDetectionError as exc:
        logger.warning(
            "PII redaction failed for love note %s (%s); no moderation preview stored",
            note_id,
            exc.code,
        )
    except Exception:
        logger.error(
            "Unexpected PII redaction failure for love note %s; no moderation preview stored",
            note_id,
            exc_info=True,
        )

    try:
        with db.sessions.begin() as session:
            # The season is *claimed* by the conditional update: PostgreSQL re-checks the
            # predicate after the row lock is taken, so of two racers exactly one sees
            # ``rowcount == 1``. The old read-then-write let two sends both claim it.
            claimed = session.execute(
                update(UserRecord)
                .where(
                    UserRecord.id == str(sender.id),
                    UserRecord.love_notes_send_enabled.is_(False),
                )
                .values(love_notes_send_enabled=True)
                # The in-session sender copy is not kept in step; the flag is read from
                # the row the update itself matched.
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                # A claim fails two ways with different answers: the season is spent, or
                # the sender row is missing -- a missing row must not be told their season
                # is gone.
                if session.scalar(
                    select(UserRecord.id).where(UserRecord.id == str(sender.id))
                ) is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Sender not found.",
                    )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=SEASON_ALREADY_CLAIMED,
                )

            # The recipient's switch, re-read where the note is written: the pre-upload
            # check is only a fast-fail; this one catches an opt-out made in between.
            recipient_row = session.get(UserRecord, str(note_data.recipient_id))
            if recipient_row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Recipient not found.",
                )
            if not recipient_row.love_notes_receive_enabled:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=RECIPIENT_NOT_ACCEPTING,
                )

            row = LoveNoteRecord(
                id=note_id,
                sender_id=sender.id, recipient_id=str(note_data.recipient_id),
                image_public_id=stored_image["public_id"], image_format=stored_image["format"],
                body=note_data.body,
                moderation_body=moderation_body,
                anonymous=note_data.anonymous, status="pending_review",
                created_at=love_note.created_at,
            )
            session.add(row)
            session.flush()
    except HTTPException:
        # The note was refused, so the uploaded image points at nothing and is discarded;
        # the refusal is re-raised rather than reported as the 500 below.
        logger.info("Love note %s refused; discarding the uploaded image", note_id)
        _discard_uploaded_image(note_id, stored_image["public_id"])
        raise
    except Exception:
        # Best-effort cleanup so the image already in the bucket does not become an orphan.
        logger.error("Failed to insert love note into the database; discarding uploaded image", exc_info=True)
        _discard_uploaded_image(note_id, stored_image["public_id"])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not send the love note."
        )

    # Staff-facing notice (role-based, best-effort): a failed write must never unwind the
    # note that was just committed.
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

# Ceiling for one page of the admin love-note list; the endpoint clamps anything larger.
LOVE_NOTES_MAX_LIMIT = 100

LOVE_NOTE_STATUSES = frozenset({"pending_review", "approved", "rejected"})

LOVE_NOTE_ORDERS = frozenset({"newest", "oldest"})


def _serialise_admin_love_note(doc: Any) -> Dict[str, Any]:
    """One love note as the moderation list carries it: preview text and audit fields, no people.

    ``moderation_body`` is the PII-redacted preview produced at creation; the original
    ``body`` is deliberately absent so no future edit to this screen can leak it.
    ``None`` renders as "preview unavailable", never as the body.
    """
    return {
        "id": str(doc.id),
        "moderation_body": doc.moderation_body,
        "status": doc.status,
        "created_at": isoformat_datetime(doc.created_at),
        # Flagged, not filtered: staff see removed notes so a removal can be audited;
        # ``status`` answers whether the note was judged acceptable.
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

    Each note travels as moderation data only: identities, image and original body are
    withheld so the reviewer judges a message, not the people (and no field can surface
    them). What stays is the id, redacted preview, status and removal flags -- a removed
    note is still listed with who took it down and when.
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
    # ``id`` second so notes stamped in the same instant order identically on every page;
    # an unordered tail reshuffles between count and read, showing some notes twice.
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
    """Update the status of a love note and send appropriate notifications.

    A rejection is final: reversing ``pending_review -> rejected`` would deliver a note
    that broke the rules by a second click. ``approved -> rejected`` is a permitted,
    different act -- pulling a delivered note back out of the inbox, with different words.
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid love note id"
        )

    # The transition is claimed under a row lock, and the claim is what makes it
    # idempotent: the status decides both the guard and the notice, so reading it outside
    # the transaction would let two clicks both read ``pending_review`` and deliver twice.
    previous_status: Optional[str] = None
    with db.sessions.begin() as session:
        love_note = session.scalar(
            select(LoveNoteRecord)
            .where(LoveNoteRecord.id == str(note_id))
            .with_for_update()
        )
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

    # A move to the status already held is not a move and is not announced: one decision
    # is one notice, which is what makes the endpoint idempotent.
    if previous_status == status_value:
        return {"id": note_id, "status": status_value}
    
    from ..services.notification_service import create_notification_service
    
    with db.sessions() as session:
        sender = session.get(UserRecord, love_note.sender_id)
        recipient = session.get(UserRecord, love_note.recipient_id)
    
    if status_value == "approved":
        if recipient:
            sender_name = "Someone" if love_note.anonymous else (
                sender.name if sender else "Someone"
            )
            # The one notification with a target: written exactly when the note becomes
            # readable in the recipient's inbox, so pointing at the note id lands the
            # reader on it instead of making them go looking.
            create_notification_service(
                recipient_regno=recipient.regno,
                heading="💌 You received a Love Note!",
                body=f"{sender_name} sent you a love note! Go to Love Notes → Inbox to view it.",
                db=db,
                target_kind="love_note",
                target_id=str(love_note.id),
            )
        
        # No target for the sender: there is no sent-notes list for this to land on.
        if sender:
            create_notification_service(
                recipient_regno=sender.regno,
                heading="✅ Love Note Delivered!",
                body="Your love note was reviewed and successfully delivered to the recipient.",
                db=db
            )
    
    elif status_value == "rejected":
        # Two things end in ``rejected`` and the sender is owed an accurate account of
        # which: a pre-delivery refusal reads as "could not be sent"; a note pulled after
        # delivery reads as "taken down", since the sender already saw the delivered
        # notice. No target for either.
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


def regenerate_love_note_moderation_body(note_id: str, db: Database) -> Dict[str, Any]:
    """Re-run PII redaction for one note and persist the refreshed moderation preview.

    Escape hatch for a note whose preview was never stored (provider down, or the note
    predates the column); a no-op with no provider call when a preview exists, so a retry
    cannot re-run over answered text. A failed retry raises ``PiiDetectionError`` (mapped
    by the router to 503); the body is never returned.
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid love note id",
        )

    with db.sessions() as session:
        note = session.get(LoveNoteRecord, str(note_id))
    if note is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Love note not found",
        )
    if note.moderation_body is not None:
        return {"id": str(note.id), "moderation_body": note.moderation_body}

    # Provider call outside any open transaction: the Language request can take
    # seconds, and holding a pool connection across it would trade a slow provider
    # for exhausted connections on every other endpoint.
    result = redact_love_note_body(note.body)

    with db.sessions.begin() as session:
        row = session.get(LoveNoteRecord, str(note_id))
        if row is None:
            # Deleted between the read and the write; the caller asked to refresh a
            # note that no longer exists, which is the same 404 as above.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Love note not found",
            )
        row.moderation_body = result.moderation_body

    logger.info(
        "Stored moderation preview for love note %s (passes=%s, entities=%s)",
        note_id,
        ",".join(result.languages),
        result.entity_count,
    )
    return {"id": str(note_id), "moderation_body": result.moderation_body}


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

    The note is carried rather than referenced, because the moderator's question on
    this screen is "what did this note actually say" -- a row holding only an id sends
    them to a second screen. The sender comes with it, because a report saying *this
    note is unacceptable* is only actionable once staff can see who wrote it -- the
    recipient may never have been told, which is the whole point of an anonymous note.

    The join is an inner join on purpose: the report's foreign key cascades, so a
    report cannot outlive its note and an outer join could only ever render a
    "[deleted]" row no code path can produce.

    ``query`` narrows in SQL (reason, redacted preview, reporter regno, sender
    name/regno) with the total counted under the same filter. The sender is searchable
    here -- unlike a confession author -- because this queue names them on every row;
    the searchable note text is the redacted preview, never the original body.
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
            ilike_contains(LoveNoteRecord.moderation_body, cleaned),
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
            # The redacted preview, for the same reason the moderation list carries it:
            # this queue is another moderator-facing view of the same note, and the
            # original body must have no path out through it either. ``None`` renders
            # as "preview unavailable" on the client, never as the raw note.
            "moderation_body": note.moderation_body,
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
    Delete a love note and its image, destroying the object before removing its row.

    The public ID comes from the database. A missing Cloudinary object is already
    effectively deleted; unexpected storage failures are raised so orphan risks surface.
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

    Invalidate signed URLs when an account is blocked while keeping its notes available.

    Visible images are re-uploaded under the same public ID to change their version;
    failures are best-effort and counted in the result.
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
