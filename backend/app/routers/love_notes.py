
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from ..db_models import LoveNoteRecord, UserRecord
from ..postgres import Database
from typing import Annotated, Any, Dict, List, Optional
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from datetime import datetime, timezone

from ..dependencies import get_db
from ..models import UserDetails
from ..services import asset_delivery_service
from ..services.auth_service import get_current_user
from ..services.love_note_service import (
    LoveNoteCreate,
    get_all_users_service,
    get_all_classes_service,
    send_love_note_service
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
    class_name: str
    gender: str
    interests: Optional[List[str]] = None
    profile_picture_url: Optional[str] = None

@router.get("/users", response_model=List[UserRecipient])
async def get_all_users(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to fetch all users from the database, excluding the current user.
    """
    return await get_all_users_service(db, current_user.regno)

@router.get("/classes", response_model=List[str])
async def get_all_classes(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Fetch all unique classes from the users table.
    """
    return await get_all_classes_service(db)

@router.post("/send", status_code=status.HTTP_201_CREATED)
async def send_love_note(
    note_data: LoveNoteCreate,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to send a love note. The note is marked as 'pending_review'.
    """
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
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to fetch all accepted love notes sent to the current user.
    """
    # Find all love notes sent to current user with status 'approved'
    with db.sessions() as session:
        love_notes = session.scalars(select(LoveNoteRecord).where(
            LoveNoteRecord.recipient_id == current_user.id,
            LoveNoteRecord.status == "approved",
        ).order_by(LoveNoteRecord.created_at.desc())).all()
        sender_ids = {note.sender_id for note in love_notes if note.sender_id}
        senders = {user.id: user for user in session.scalars(select(UserRecord).where(UserRecord.id.in_(sender_ids))).all()} if sender_ids else {}
        result = []
        for note in love_notes:
            sender = senders.get(note.sender_id)
            sender_name = "Anonymous" if note.anonymous or not sender else sender.name or "Unknown"
            result.append({
                "id": str(note.id), "from": sender_name, "body": note.body,
                # Only approved notes reach this list, so the recipient is entitled to
                # the image and the proxy will serve it.
                "image_url": asset_delivery_service.build_delivery_url(
                    kind="love-notes",
                    stored_identifier=note.image_url,
                    regno=current_user.regno,
                ) or note.image_url,
                "anonymous": note.anonymous,
                "created_at": isoformat_datetime(note.created_at),
                "read_at": isoformat_datetime(note.read_at),
            })
    
    return result


@router.post("/inbox/{note_id}/mark-read", status_code=status.HTTP_200_OK)
async def mark_love_note_read(
    note_id: str,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
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
        ).with_for_update())
        if note:
            note.read_at = datetime.now(timezone.utc)
    
    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Love note not found."
        )
    
    return {"message": "Love note marked as read."}
