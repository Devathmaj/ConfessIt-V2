
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
    image_base64: str
    message_text: str
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
    love_note = LoveNote(
        sender_id=sender.id,
        recipient_id=ObjectId(note_data.recipient_id),
        image_base64=note_data.image_base64,
        message_text=note_data.message_text,
        is_anonymous=note_data.is_anonymous,
        status="pending_review"
    )

    # Convert to a dict and insert into the database
    note_dict = love_note.model_dump(by_alias=True, exclude=["id"])
    
    result = db["LoveNotes"].insert_one(note_dict)
    
    if result.inserted_id:
        # Used to update the sender's status to indicate they have sent a love note
        db["UserDetails"].update_one(
            {"_id": sender.id},
            {"$set": {"isLovenotesSend": True}}
        )
        return {"message": "Love note sent successfully for review.", "note_id": str(result.inserted_id)}
    
    logger.error("Failed to insert love note into the database.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not send the love note."
    )