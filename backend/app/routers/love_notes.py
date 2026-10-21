
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select
from ..db_models import LoveNoteRecord, UserRecord
from ..postgres import Database
from typing import Annotated, Any, Dict, List, Optional
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from datetime import datetime, timezone

from ..dependencies import get_db
from ..limits import PAGE_OFFSET_MAX, USER_DEPARTMENT_MAX, USER_YEAR_MAX, USER_YEAR_MIN
from ..models import Cohort, UserDetails
from ..services.authorization import require_participant
from ..services.love_note_service import (
    LOVE_NOTE_RECIPIENTS_MAX_LIMIT,
    LoveNoteCreate,
    LoveNoteReportCreate,
    SEASON_ALREADY_CLAIMED,
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
    # Keep year and department separate for cohort filters; year matches the DB integer type.
    year: int
    department: str
    gender: str
    interests: Optional[List[str]] = None
    profile_picture_url: Optional[str] = None
    # Blocked accounts stay in the list on purpose: a note may still be sent to them,
    # the sender just has to confirm it first. The flag is what lets the client warn.
    is_blocked: bool = False

class RecipientListPage(BaseModel):
    """
    One page of the recipient list, with the total behind it.

    An envelope rather than a bare list because the picker pages: ``total``
    tells the screen how many people are left below the page it already holds.
    """
    users: List[UserRecipient]
    total: int
    skip: int
    limit: int

@router.get("/users", response_model=RecipientListPage)
def get_all_users(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching participants to skip."),
    limit: int = Query(50, ge=1, le=LOVE_NOTE_RECIPIENTS_MAX_LIMIT, description="How many matching participants to return. Clamped so one request can never materialise the whole participant table."),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the participant's name, run in SQL."),
    include_same_gender: bool = Query(False, description="Widen the list to the viewer's own gender. Off by default, so the picker opens on the genders the sender is not in."),
    year: Optional[int] = Query(None, ge=USER_YEAR_MIN, le=USER_YEAR_MAX, description="Exact year of study."),
    department: Optional[str] = Query(None, max_length=USER_DEPARTMENT_MAX, description="Exact department, case-insensitive."),
):
    """
    Fetch one page of users available as note recipients, excluding the current user.

    Every filter is a SQL WHERE and the page a SQL LIMIT/OFFSET, so the backend
    reads and serialises at most ``limit`` rows, and the search matches names
    in the database rather than over a loaded list.
    """
    return get_all_users_service(
        db,
        current_user,
        skip=skip,
        limit=limit,
        query=q,
        include_same_gender=include_same_gender,
        year=year,
        department=department,
    )

@router.get("/cohorts", response_model=List[Cohort])
def get_all_cohorts(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Every ``(year, department)`` pair a recipient on this screen belongs to.

    Renamed from ``/classes`` along with the column it was derived from; year
    and department are now chosen separately rather than by picking one
    pre-joined string that contained both.
    """
    return get_all_cohorts_service(db)

@router.post("/send", status_code=status.HTTP_201_CREATED)
def send_love_note(
    note_data: LoveNoteCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    response: Response = None
):
    """
    Used to send a love note. The note is marked as 'pending_review'.
    """
    # Signed URLs never expire, so disable caching.
    response.headers["Cache-Control"] = "no-store"
    # Prevent the URL from leaking to later navigations through the Referer header.
    response.headers["Referrer-Policy"] = "no-referrer"
    # Fast-fail before upload; the service's conditional update enforces the limit.
    if current_user.love_notes_send_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=SEASON_ALREADY_CLAIMED
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

    return send_love_note_service(note_data, current_user, db)


@router.get("/inbox", response_model=Dict[str, Any])
def get_received_love_notes(
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db),
    response: Response = None,
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many of the reader's notes to skip."),
    limit: int = Query(50, ge=1, le=100, description="How many of the reader's notes to return. Clamped so one request can never mint delivery URLs for the whole inbox at once."),
):
    """
    Fetch one page of accepted love notes sent to the current user.

    The page is a SQL LIMIT/OFFSET and ``total`` a COUNT under the same filter,
    so a much-loved reader loads downwards instead of all at once -- which
    matters doubly here, because every row carries a freshly minted image URL.
    """
    # Same reasoning as the send response: these signed URLs do not expire, so
    # nothing downstream may cache them -- and the inbox is exactly where the
    # URLs cluster.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Find all love notes sent to current user with status 'approved'
    filters = [
        LoveNoteRecord.recipient_id == current_user.id,
        LoveNoteRecord.status == "approved",
        # A note taken down for a report leaves the inbox: removal is a separate
        # decision from approval, and a note can be approved and then removed
        # later.
        LoveNoteRecord.removed_at.is_(None),
    ]
    with db.sessions() as session:
        total = session.scalar(
            select(func.count()).select_from(LoveNoteRecord).where(*filters)
        ) or 0
        love_notes = session.scalars(select(LoveNoteRecord).where(
            *filters,
            # ``id`` seconds the order so two notes stamped in the same instant still
            # page without repeating or skipping one.
        ).order_by(LoveNoteRecord.created_at.desc(), LoveNoteRecord.id.asc()).offset(skip).limit(limit)).all()
        sender_ids = {note.sender_id for note in love_notes if note.sender_id}
        senders = {user.id: user for user in session.scalars(select(UserRecord).where(UserRecord.id.in_(sender_ids))).all()} if sender_ids else {}
        result = []
        for note in love_notes:
            sender = senders.get(note.sender_id)
            sender_name = "Anonymous" if note.anonymous or not sender else sender.name or "Unknown"
            result.append({
                "id": str(note.id), "from": sender_name, "body": note.body,
    # Mint only for the approved recipient; this signed URL is a non-expiring bearer credential.
                "image_url": love_note_image_url(
                    note,
                    viewer_id=current_user.id,
                    viewer_account_type=current_user.account_type,
                ),
                "anonymous": note.anonymous,
                "created_at": isoformat_datetime(note.created_at),
                "read_at": isoformat_datetime(note.read_at),
            })

    return {"notes": result, "total": total, "skip": skip, "limit": limit}


@router.post("/inbox/{note_id}/mark-read", status_code=status.HTTP_200_OK)
def mark_love_note_read(
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
def report_received_love_note(
    note_id: str,
    report_data: LoveNoteReportCreate,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    db: Database = Depends(get_db)
):
    """
    Report a love note the current user received.

    Only the recipient can report, and only once. The report is not itself a
    moderation action: the note stays exactly as it was until staff act, so
    filing one does not silently retract a note somebody was shown.
    """
    return report_love_note_service(note_id, report_data, current_user, db)
