import logging
from ..postgres import Database
from pydantic import BaseModel
from typing import List, Dict, Any
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
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
    users_cursor = db.users.list(
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
    classes = db.users.distinct("which_class")
    return classes

async def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """
    Service to create and save a new love note with 'pending_review' status.
    The base64 image is uploaded to Cloudinary and the URL is stored instead.
    """
    recipient_doc = db.users.get_one({"_id": str(note_data.recipient_id)})
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
        recipient_id=str(note_data.recipient_id),
        image_base64=cloudinary_url,  # Store the Cloudinary URL instead of base64
        message_text=note_data.message_text,
        is_anonymous=note_data.is_anonymous,
        status="pending_review"
    )

    # Convert to a dict and insert into the database
    note_dict = love_note.model_dump(by_alias=True, exclude=["id"])
    
    result = db.love_notes.add(note_dict)
    
    if result.inserted_id:
        # Used to update the sender's status to indicate they have sent a love note
        db.users.update(
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


# ============================================================================
# Admin-only Love Note Functions
# ============================================================================

async def get_all_love_notes_for_admin(db: Database) -> List[Dict[str, Any]]:
    """
    Fetch all love notes with full sender and recipient details for admin review.
    """
    notes_repo = db.love_notes
    users_repo = db.users

    items: List[Dict[str, Any]] = []
    for doc in notes_repo.list().sort("created_at", -1):
        sender_doc = users_repo.get_one({"_id": doc.get("sender_id")})
        recipient_doc = users_repo.get_one({"_id": doc.get("recipient_id")})
        
        items.append({
            "id": str(doc.get("_id")),
            "sender": {
                "id": str(doc.get("sender_id")) if doc.get("sender_id") else None,
                "name": sender_doc.get("Name") if sender_doc else None,
                "regno": sender_doc.get("Regno") if sender_doc else None,
                "email": sender_doc.get("email") if sender_doc else None,
            },
            "recipient": {
                "id": str(doc.get("recipient_id")) if doc.get("recipient_id") else None,
                "name": recipient_doc.get("Name") if recipient_doc else None,
                "regno": recipient_doc.get("Regno") if recipient_doc else None,
                "email": recipient_doc.get("email") if recipient_doc else None,
            },
            "image_url": doc.get("image_base64"),
            "message_text": doc.get("message_text", ""),
            "is_anonymous": doc.get("is_anonymous", False),
            "status": doc.get("status", "pending_review"),
            "created_at": isoformat_datetime(doc.get("created_at")),
            "read_at": isoformat_datetime(doc.get("read_at")),
        })
    return items


async def update_love_note_status_service(
    note_id: str, 
    status_value: str, 
    db: Database
) -> Dict[str, Any]:
    """
    Update the status of a love note and send appropriate notifications.
    
    Args:
        note_id: The str of the love note
        status_value: New status (approved, rejected, pending_review)
        db: PostgreSQL database instance
        
    Returns:
        Dict with updated note_id and status
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid love note id"
        )

    # Get the love note before updating
    love_note = db.love_notes.get_one({"_id": str(note_id)})
    if not love_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    
    # Update the status
    result = db.love_notes.update(
        {"_id": str(note_id)}, 
        {"$set": {"status": status_value}}
    )
    if result.matched_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    
    # Create notifications based on status change
    from ..services.notification_service import create_notification_service
    
    sender_id_obj = love_note.get("sender_id")
    recipient_id_obj = love_note.get("recipient_id")
    
    # Get sender and recipient details
    sender = db.users.get_one({"_id": sender_id_obj}) if sender_id_obj else None
    recipient = db.users.get_one({"_id": recipient_id_obj}) if recipient_id_obj else None
    
    if status_value == "approved":
        # Notify recipient that they received a love note
        if recipient:
            sender_name = "Someone" if love_note.get("is_anonymous") else (
                sender.get("Name") if sender else "Someone"
            )
            create_notification_service(
                user_id=recipient.get("Regno"),
                heading="💌 You received a Love Note!",
                body=f"{sender_name} sent you a love note! Go to Love Notes → Inbox to view it.",
                db=db
            )
        
        # Notify sender that their love note was accepted
        if sender:
            create_notification_service(
                user_id=sender.get("Regno"),
                heading="✅ Love Note Delivered!",
                body="Your love note was reviewed and successfully delivered to the recipient.",
                db=db
            )
    
    elif status_value == "rejected":
        # Notify sender that their love note was rejected
        if sender:
            create_notification_service(
                user_id=sender.get("Regno"),
                heading="❌ Love Note Not Delivered",
                body="Your love note could not be sent. It may have violated our community guidelines.",
                db=db
            )
    
    return {"id": note_id, "status": status_value}


async def delete_love_note_service(note_id: str, db: Database) -> Dict[str, Any]:
    """
    Delete a love note from the system.
    
    Args:
        note_id: The str of the love note
        db: PostgreSQL database instance
        
    Returns:
        Dict with deleted note_id
    """
    if not is_valid_id(note_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Invalid love note id"
        )

    result = db.love_notes.delete({"_id": str(note_id)})
    if result.deleted_count == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    return {"deleted": note_id}
