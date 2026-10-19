
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

class RecipientListPage(BaseModel):
    """
    One page of the recipient list, with the total behind it.

    An envelope rather than a bare list because the picker pages: ``total`` is what tells
    the screen how many people are left below the page it already holds, and the rows
    themselves are still validated one by one as ``UserRecipient``.
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
    Used to fetch one page of users available as note recipients, excluding the current user.

    Every filter is a SQL WHERE and the page a SQL LIMIT/OFFSET: with thousands of
    participants the backend reads and serialises at most ``limit`` rows, never the whole
    table, and the search matches names in the database rather than over a loaded list.
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

    Renamed from ``/classes`` along with the column it was derived from. The old name described a
    single column that no longer exists, and the filter on the client is now genuinely over two
    fields -- the year and "CSE" are chosen separately rather than by picking one pre-joined
    string that happened to contain both.
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
    # A signed image URL does not expire, so the response that carries it must not be cached:
    # a copy in a browser or proxy cache would keep the capability alive long after the user
    # closed the page. ``no-store`` is the point, not ``no-cache`` -- revalidation only helps
    # if the revalidation is against a server that can *retract* the URL, and this one cannot.
    response.headers["Cache-Control"] = "no-store"
    # The URL is also a bearer credential that must not leak sideways: a ``Referer`` header
    # sent with a later navigation would deposit it in a third party's logs, so responses
    # that mint one ask for no referrer (the ``referrerpolicy`` attributes on the love-note
    # images themselves are what enforce this on the actual image fetches).
    response.headers["Referrer-Policy"] = "no-referrer"
    # A fast-fail, not the rule. The season is claimed by a conditional update in the
    # service, inside the note's own transaction, because a read on the way in is a
    # read-then-write: two sends in flight both see an unspent season and both believe
    # they hold it. This check exists only so that the ordinary second send of the
    # season is turned away before it uploads an image -- the service refuses it either
    # way, and refuses it authoritatively.
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
    Used to fetch one page of accepted love notes sent to the current user.

    The page is a SQL LIMIT/OFFSET and ``total`` a COUNT under the same filter, so a
    much-loved reader loads downwards instead of all at once -- which matters doubly
    here, because every row carries a freshly minted image URL.
    """
    # Same reasoning as the send response: the signed URLs in this body do not expire, so
    # nothing downstream may cache them. The inbox is also exactly where the URLs *cluster*,
    # which is why the header matters as much here as on the response that first issued one.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Find all love notes sent to current user with status 'approved'
    filters = [
        LoveNoteRecord.recipient_id == current_user.id,
        LoveNoteRecord.status == "approved",
        # A note taken down for a report leaves the inbox. ``status == "approved"`` is
        # not enough on its own: the removal is a separate decision from approval, and a
        # note can be approved and then removed later, which is the case this catches.
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

    A note can be reported by the recipient only, and only once. The report is not a
    moderation action on the note: the note stays exactly as it was until staff act on the
    report, so filing one does not silently retract a note somebody was shown.
    """
    return report_love_note_service(note_id, report_data, current_user, db)
