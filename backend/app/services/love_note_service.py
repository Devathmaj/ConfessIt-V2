
import logging
from pymongo.database import Database
from pydantic import BaseModel
from typing import List
from bson import ObjectId
from fastapi import HTTPException, status

from ..models import UserDetails, LoveNote
from ..services.storage_service import storage_service

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
    Service to fetch all users available as love note recipients, excluding the
    active user and any administrator accounts.
    """
    users_cursor = db["UserDetails"].find(
        {
            "Regno": {"$ne": current_user_regno},
            "user_role": {"$ne": "admin"},
        }
    )
    # Used to synchronously iterate over the cursor and build the list
    users: List[UserDetails] = []
    for user_doc in users_cursor:
        enriched = storage_service.with_profile_signed_url(user_doc) or user_doc
        users.append(UserDetails(**enriched))
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
    The base64 image is uploaded to Cloudinary and the URL is stored instead.
    """
    recipient_doc = db["UserDetails"].find_one({"_id": ObjectId(note_data.recipient_id)})
    if not recipient_doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipient not found."
        )

    if recipient_doc.get("user_role") == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot receive love notes."
        )

    if sender.user_role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators are not permitted to send love notes."
        )

    try:
        # Upload the love note image to Cloudinary
        upload_result = storage_service.upload_love_note_image(
            image_base64=note_data.image_base64,
            sender_username=sender.Name or sender.Regno
        )
        
        # Use the Cloudinary URL instead of the base64 string
        cloudinary_url = upload_result["cloudinary_url"]
        
    except Exception as e:
        logger.error(f"Failed to upload love note image to Cloudinary: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload love note image: {str(e)}"
        )
    
    # Create the love note with the Cloudinary URL
    love_note = LoveNote(
        sender_id=sender.id,
        recipient_id=ObjectId(note_data.recipient_id),
        image_base64=cloudinary_url,  # Store the Cloudinary URL instead of base64
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
        return {
            "message": "Love note sent successfully for review.", 
            "note_id": str(result.inserted_id),
            "image_url": cloudinary_url
        }
    
    logger.error("Failed to insert love note into the database.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not send the love note."
    )