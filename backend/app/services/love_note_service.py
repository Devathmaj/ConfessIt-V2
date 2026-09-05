import logging
from sqlalchemy import select
from ..postgres import Database
from ..db_models import LoveNoteRecord, UserRecord
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
    with db.sessions() as session:
        rows = session.scalars(select(UserRecord).where(
            UserRecord.Regno != current_user_regno, UserRecord.user_role != "admin"
        )).all()
        return [UserDetails(**_user_payload(row)) for row in rows]

async def get_all_classes_service(db: Database) -> List[str]:
    """
    Service to fetch all distinct 'which_class' values.
    """
    with db.sessions() as session:
        return list(session.scalars(select(UserRecord.which_class).distinct().where(UserRecord.which_class.is_not(None))))

def _user_payload(user: UserRecord) -> dict:
    payload = {
        "id": str(user.id), "Regno": user.Regno, "Name": user.Name, "email": user.email,
        "emoji": user.emoji, "bio": user.bio, "which_class": user.which_class,
        "profile_picture_id": user.profile_picture_id, "gender": user.gender,
        "interests": user.interests or [], "isMatchmaking": user.isMatchmaking,
        "isNotifications": user.isNotifications, "isLovenotesRecieve": user.isLovenotesRecieve,
        "isLovenotesSend": user.isLovenotesSend, "is_blocked": user.is_blocked,
        "reported_count": user.reported_count, "last_login_time": user.last_login_time,
        "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
        "last_login_user_agent": user.last_login_user_agent, "user_role": user.user_role,
        "last_matchmaking_time": user.last_matchmaking_time,
    }
    return storage_service.with_profile_signed_url(payload) or payload

async def send_love_note_service(note_data: LoveNoteCreate, sender: UserDetails, db: Database):
    """
    Service to create and save a new love note with 'pending_review' status.
    The base64 image is uploaded to Cloudinary and the URL is stored instead.
    """
    with db.sessions() as session:
        recipient = session.get(UserRecord, str(note_data.recipient_id))
    if not recipient:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Recipient not found."
        )

    if recipient.user_role == "admin":
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
            sender_name=sender.Name or sender.Regno
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
        image_url=cloudinary_url,
        message_text=note_data.message_text,
        is_anonymous=note_data.is_anonymous,
        status="pending_review"
    )

    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender.id, recipient_id=str(note_data.recipient_id),
            image_url=cloudinary_url, message_text=note_data.message_text,
            is_anonymous=note_data.is_anonymous, status="pending_review",
            created_at=love_note.created_at,
        )
        session.add(row)
        sender_row = session.get(UserRecord, sender.id)
        if sender_row:
            sender_row.isLovenotesSend = True
        session.flush()
        note_id = str(row.id)
        return {
            "message": "Love note sent successfully for review.",
            "note_id": note_id,
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
    items: List[Dict[str, Any]] = []
    with db.sessions() as session:
        rows = session.scalars(select(LoveNoteRecord).order_by(LoveNoteRecord.created_at.desc())).all()
        user_ids = {note.sender_id for note in rows} | {note.recipient_id for note in rows}
        users = {user.id: user for user in session.scalars(select(UserRecord).where(UserRecord.id.in_(user_ids))).all()}
    for doc in rows:
        sender_doc = users.get(doc.sender_id)
        recipient_doc = users.get(doc.recipient_id)
        
        items.append({
            "id": str(doc.id),
            "sender": {
                "id": str(doc.sender_id) if doc.sender_id else None,
                "name": sender_doc.Name if sender_doc else None,
                "regno": sender_doc.Regno if sender_doc else None,
                "email": sender_doc.email if sender_doc else None,
            },
            "recipient": {
                "id": str(doc.recipient_id) if doc.recipient_id else None,
                "name": recipient_doc.Name if recipient_doc else None,
                "regno": recipient_doc.Regno if recipient_doc else None,
                "email": recipient_doc.email if recipient_doc else None,
            },
            "image_url": doc.image_url,
            "message_text": doc.message_text,
            "is_anonymous": doc.is_anonymous,
            "status": doc.status,
            "created_at": isoformat_datetime(doc.created_at),
            "read_at": isoformat_datetime(doc.read_at),
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
    with db.sessions.begin() as session:
        love_note = session.get(LoveNoteRecord, str(note_id))
        if love_note:
            love_note.status = status_value
    if not love_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    
    # Create notifications based on status change
    from ..services.notification_service import create_notification_service
    
    with db.sessions() as session:
        sender = session.get(UserRecord, love_note.sender_id)
        recipient = session.get(UserRecord, love_note.recipient_id)
    
    if status_value == "approved":
        # Notify recipient that they received a love note
        if recipient:
            sender_name = "Someone" if love_note.is_anonymous else (
                sender.Name if sender else "Someone"
            )
            create_notification_service(
                user_id=recipient.Regno,
                heading="💌 You received a Love Note!",
                body=f"{sender_name} sent you a love note! Go to Love Notes → Inbox to view it.",
                db=db
            )
        
        # Notify sender that their love note was accepted
        if sender:
            create_notification_service(
                user_id=sender.Regno,
                heading="✅ Love Note Delivered!",
                body="Your love note was reviewed and successfully delivered to the recipient.",
                db=db
            )
    
    elif status_value == "rejected":
        # Notify sender that their love note was rejected
        if sender:
            create_notification_service(
                user_id=sender.Regno,
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

    with db.sessions.begin() as session:
        love_note = session.get(LoveNoteRecord, str(note_id))
        if love_note:
            session.delete(love_note)
    if not love_note:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, 
            detail="Love note not found"
        )
    return {"deleted": note_id}
