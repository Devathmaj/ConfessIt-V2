
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.database import Database
from typing import Annotated, List
from bson import ObjectId

from ..dependencies import get_db
from ..models import UserDetails
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

class UserRecipient(UserDetails):
    """
    A simplified UserDetails model for the recipient list.
    """
    id: str

@router.get("/users", response_model=List[UserDetails])
async def get_all_users(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to fetch all users from the database, excluding the current user.
    """
    return await get_all_users_service(db, current_user.Regno)

@router.get("/classes", response_model=List[str])
async def get_all_classes(db: Database = Depends(get_db)):
    """
    Used to fetch all unique classes from the UserDetails collection.
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
    if current_user.isLovenotesSend:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have already sent your love note for this season."
        )

    # Basic validation
    if not note_data.message_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Message cannot be empty."
        )
    if not ObjectId.is_valid(note_data.recipient_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid recipient ID."
        )

    # Prevent user from sending a note to themselves
    if current_user.id == ObjectId(note_data.recipient_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot send a love note to yourself."
        )

    return await send_love_note_service(note_data, current_user, db)