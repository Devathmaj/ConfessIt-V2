# app/services/love_note_service.py

import logging
from pymongo.database import Database
from pydantic import BaseModel
from typing import List
from bson import ObjectId
from fastapi import HTTPException, status

from ..models import UserDetails, LoveNote

# Logger
logger = logging.getLogger(__name__)

class LoveNoteCreate(BaseModel):
    """
    Pydantic model for creating a love note.
    """
    recipient_id: str
    template_name: str
    message: str
    stickers: List[str]
    is_anonymous: bool

async def get_all_users_service(db: Database, current_user_regno: str) -> List[UserDetails]:
    """
    Service to fetch all users, excluding the current user.
    """
    # Used to execute a synchronous database query
    users_cursor = db["UserDetails"].find({"Regno": {"$ne": current_user_regno}})
    # Used to synchronously iterate over the cursor and build the list
    users = [UserDetails(**user) for user in users_cursor]
    return users

async def get_all_classes_service(db: Database) -> List[str]:
    """
    Service to fetch all distinct 'which_class' values.
    """
    # Used to execute a synchronous 'distinct' query
    classes = db["UserDetails"].distinct("which_class")
    return classes

async def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """
    Service to create and save a new love note with 'pending_review' status.
    """
    # For now, we assume template_name is a string that can be resolved on the client.
    # In a real app, you might validate template_name against a templates collection.
    
    love_note = LoveNote(
        sender=sender.id,
        recipient=ObjectId(note_data.recipient_id),
        template_id=ObjectId(), # This should be linked to a real template ID in the future
        message=note_data.message,
        stickers=note_data.stickers,
        is_anonymous=note_data.is_anonymous,
        status="pending_review" # Important: Set status for review
    )

    # Convert to a dict and insert into the database
    note_dict = love_note.model_dump(by_alias=True, exclude=["id"])
    
    # Use sender and recipient ObjectIds
    note_dict["sender"] = sender.id
    note_dict["recipient"] = ObjectId(note_data.recipient_id)

    result = db["LoveNotes"].insert_one(note_dict)
    
    if result.inserted_id:
        return {"message": "Love note sent successfully for review.", "note_id": str(result.inserted_id)}
    
    logger.error("Failed to insert love note into the database.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not send the love note."
    )
