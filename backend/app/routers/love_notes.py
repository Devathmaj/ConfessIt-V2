
from fastapi import APIRouter, Depends, HTTPException, status
from pymongo.database import Database
from typing import Annotated, List, Dict, Any
from bson import ObjectId
from datetime import datetime

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


@router.get("/inbox", response_model=List[Dict[str, Any]])
async def get_received_love_notes(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """
    Used to fetch all accepted love notes sent to the current user.
    """
    # Find all love notes sent to current user with status 'approved'
    love_notes = list(
        db["LoveNotes"].find({
            "recipient_id": current_user.id,
            "status": "approved"
        }).sort("created_at", -1)
    )
    
    # Enrich with sender information
    result = []
    for note in love_notes:
        sender_id = note.get("sender_id")
        sender = None
        
        if sender_id:
            sender = db["UserDetails"].find_one({"_id": sender_id})
        
        # Prepare sender name (handle anonymous)
        sender_name = "Anonymous"
        if not note.get("is_anonymous", False) and sender:
            sender_name = sender.get("Name", "Unknown")
        
        result.append({
            "id": str(note.get("_id")),
            "from": sender_name,
            "message": note.get("message_text", ""),
            "image_url": note.get("image_base64"),  # This is actually the Cloudinary URL
            "anonymous": note.get("is_anonymous", False),
            "created_at": note.get("created_at").isoformat() if note.get("created_at") else None,
            "read_at": note.get("read_at").isoformat() if note.get("read_at") else None,
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
    if not ObjectId.is_valid(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid love note ID."
        )
    
    # Verify the note belongs to the current user
    note = db["LoveNotes"].find_one({
        "_id": ObjectId(note_id),
        "recipient_id": current_user.id
    })
    
    if not note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Love note not found."
        )
    
    # Update read_at timestamp
    db["LoveNotes"].update_one(
        {"_id": ObjectId(note_id)},
        {"$set": {"read_at": datetime.utcnow()}}
    )
    
    return {"message": "Love note marked as read."}