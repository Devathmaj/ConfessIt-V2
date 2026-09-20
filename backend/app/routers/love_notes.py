
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from ..db_models import LoveNoteRecord, UserRecord
from ..postgres import Database
from typing import Annotated, Any, Dict, List, Optional
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from datetime import datetime, timezone

from ..dependencies import get_db
from ..accounts import AccountRef
from ..models import Cohort, UserDetails
from ..services.authorization import require_participant
from ..services.love_note_service import (
    LoveNoteCreate,
    LoveNoteReportCreate,
    get_all_users_service,
    get_all_cohorts_service,
    love_note_image_url,
    send_love_note_service,
    report_love_note_service
)

router = APIRouter(
    prefix="/love-notes",
    tags=["Love Notes"]
)

class UserRecipient(BaseModel):
    """
    A PII-free, minimal profile used for the love note recipient list.
    """
    id: str
    name: str
    emoji: Optional[str] = None
    # Both NOT NULL on ``users``. Kept as separate fields rather than one display string so the
    # cohort filter on this screen can compare either half independently -- which it could not do
    # when they were one ``class_name`` column.
    # ``year`` is an ``int`` because that is what ``users.year`` holds. Declaring it ``str`` here
    # does not quietly stringify it -- Pydantic v2 refuses int->str -- so it fails response
    # validation and takes the whole recipient list down with a 500.
    year: int
    department: str
    gender: str
    interests: Optional[List[str]] = None
    profile_picture_url: Optional[str] = None
    # Blocked accounts stay in the list on purpose: a note may still be sent to them,
    # the sender just has to confirm it first. The flag is what lets the client warn.
    is_blocked: bool = False

@router.get("/users", response_model=List[UserRecipient])
async def get_all_users(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Used to fetch all users from the database, excluding the current user.
    """
    return await get_all_users_service(db, AccountRef.of(current_user))

@router.get("/cohorts", response_model=List[Cohort])
async def get_all_cohorts(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Every ``(year, department)`` pair a recipient on this screen belongs to.

    Renamed from ``/classes`` along with the column it was derived from. The old name described a
    single column that no longer exists, and the filter on the client is now genuinely over two
    fields -- the year and "CSE" are chosen separately rather than by picking one pre-joined
    string that happened to contain both.
    """
    return await get_all_cohorts_service(db)

@router.post("/send", status_code=status.HTTP_201_CREATED)
async def send_love_note(
    note_data: LoveNoteCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    response: Response = None
):
    """
    Used to send a love note. The note is marked as 'pending_review'.
    """
    # A signed image URL does not expire, so the response that carries it must not be cached:
    # a copy in a browser or proxy cache would keep the capability alive long after the user
    # closed the page. ``no-store`` is the point, not ``no-cache`` -- revalidation only helps
    # if the revalidation is against a server that can *retract* the URL, and this one cannot.
    response.headers["Cache-Control"] = "no-store"
    # Used to check if the user has already sent a love note
    if current_user.love_notes_send_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have already sent your love note for this season."
        )

    # Basic validation
    if not note_data.body.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty."
        )
    if not is_valid_id(note_data.recipient_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid recipient ID."
        )

    # Prevent user from sending a note to themselves
    if current_user.id == str(note_data.recipient_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot send a love note to yourself."
        )

    return await send_love_note_service(note_data, current_user, db)


@router.get("/inbox", response_model=List[Dict[str, Any]])
async def get_received_love_notes(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    response: Response = None
):
    """
    Used to fetch all accepted love notes sent to the current user.
    """
    # Same reasoning as the send response: the signed URLs in this body do not expire, so
    # nothing downstream may cache them. The inbox is also exactly where the URLs *cluster*,
    # which is why the header matters as much here as on the response that first issued one.
    response.headers["Cache-Control"] = "no-store"
    # Find all love notes sent to current user with status 'approved'
    with db.sessions() as session:
        love_notes = session.scalars(select(LoveNoteRecord).where(
            LoveNoteRecord.recipient_id == current_user.id,
            LoveNoteRecord.status == "approved",
            # A note taken down for a report leaves the inbox. ``status == "approved"`` is
            # not enough on its own: the removal is a separate decision from approval, and a
            # note can be approved and then removed later, which is the case this catches.
            LoveNoteRecord.removed_at.is_(None),
        ).order_by(LoveNoteRecord.created_at.desc())).all()
        sender_ids = {note.sender_id for note in love_notes if note.sender_id}
        senders = {user.id: user for user in session.scalars(select(UserRecord).where(UserRecord.id.in_(sender_ids))).all()} if sender_ids else {}
        result = []
        for note in love_notes:
            sender = senders.get(note.sender_id)
            sender_name = "Anonymous" if note.anonymous or not sender else sender.name or "Unknown"
            result.append({
                "id": str(note.id), "from": sender_name, "body": note.body,
                # The address is minted here, per reader, from the note's stored identity -- and
                # only because this query has already established that the caller is the recipient
                # of an approved, not-removed note. The signature inside it covers this one image's
                # path and cannot be moved onto another, but it does **not** expire, which is why
                # the response carrying it is ``Cache-Control: no-store`` and why minting a URL for
                # an unentitled viewer would be a permanent leak, not a bounded one.
                "image_url": love_note_image_url(
                    note,
                    viewer_id=current_user.id,
                    viewer_account_type=current_user.account_type,
                ),
                "anonymous": note.anonymous,
                "created_at": isoformat_datetime(note.created_at),
                "read_at": isoformat_datetime(note.read_at),
            })
    
    return result


@router.post("/inbox/{note_id}/mark-read", status_code=status.HTTP_200_OK)
async def mark_love_note_read(
    note_id: str,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Mark a love note as read by the recipient.
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid love note ID."
        )
    
# Verify the note belongs to the current user
    with db.sessions.begin() as session:
        note = session.scalar(select(LoveNoteRecord).where(
            LoveNoteRecord.id == str(note_id),
            LoveNoteRecord.recipient_id == current_user.id,
            LoveNoteRecord.removed_at.is_(None),
        ).with_for_update())
        if note:
            note.read_at = datetime.now(timezone.utc)

    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Love note not found."
        )
    
    return {"message": "Love note marked as read."}


@router.post("/inbox/{note_id}/report", status_code=status.HTTP_201_CREATED)
async def report_received_love_note(
    note_id: str,
    report_data: LoveNoteReportCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Report a love note the current user received.

    A note can be reported by the recipient only, and only once. The report is not a
    moderation action on the note: the note stays exactly as it was until staff act on the
    report, so filing one does not silently retract a note somebody was shown.
    """
    return await report_love_note_service(note_id, report_data, current_user, db)
