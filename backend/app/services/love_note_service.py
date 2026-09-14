from sqlalchemy import select
from ..postgres import Database
from ..db_models import LoveNoteRecord, UserRecord
from ..logger import get_logger
from pydantic import BaseModel, Field
from typing import List, Dict, Any
from ..postgres import new_id, is_valid_id
from ..utils import isoformat_datetime
from fastapi import HTTPException, status

from ..models import UserDetails, LoveNote
from ..services.storage_service import storage_service
from ..services import asset_delivery_service
from ..accounts import AccountRef

# Logger
logger = get_logger(__name__)

class LoveNoteCreate(BaseModel):
    """
    Pydantic model for creating a love note.
    """
    recipient_id: str
    # Capped here so an oversized body is rejected during validation, before the
    # service (or base64.b64decode) allocates anything. This is the same budget
    # storage_service applies to the encoded string, so a payload that validates is
    # still within what the decoder will accept once the data URI prefix and any
    # embedded whitespace are folded out.
    image_base64: str = Field(max_length=storage_service.max_base64_chars)
    # Matches the String(2000) column in db_models.LoveNoteRecord; without a cap here
    # this field is just as unbounded as the image was.
    body: str = Field(max_length=2000)
    anonymous: bool

async def get_all_users_service(db: Database, viewer: AccountRef) -> List[dict]:
    """
    Service to fetch all users available as love note recipients, excluding the
    active user, any administrator accounts, and users who opted out of
    receiving love notes.

    Accounts a staff member has blocked are deliberately still listed: love notes are
    moderated independently of matchmaking, so a note may still reach a blocked
    recipient. The ``is_blocked`` flag travels with each row so the sender is asked
    to confirm before the note is sent.
    """
    with db.sessions() as session:
        rows = session.scalars(select(UserRecord).where(
            UserRecord.regno != viewer.regno,
            # No staff exclusion filter is needed: staff have no row in ``users``, so they
            # cannot appear as candidates in the first place. The rule is now a property of
            # the schema rather than a clause that has to be remembered here.
            UserRecord.love_notes_receive_enabled.is_(True),
        )).all()
        return [_user_payload(row, account=viewer) for row in rows]

async def get_all_classes_service(db: Database) -> List[str]:
    """
    Service to fetch all distinct 'class_name' values.
    """
    with db.sessions() as session:
        return list(session.scalars(select(UserRecord.class_name).distinct().where(UserRecord.class_name.is_not(None))))

def _user_payload(user: UserRecord, *, account: AccountRef) -> dict:
    payload = {
        "id": str(user.id), "name": user.name, "emoji": user.emoji,
        "class_name": user.class_name, "gender": user.gender,
        "interests": user.interests or [],
        "profile_picture_url": asset_delivery_service.profile_image_url(
            user.profile_picture_url, account=account
        ),
        "is_blocked": bool(user.is_blocked),
    }
    return payload

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

    # Both parties are participants by construction: the recipient is read from ``users``
    # and the sender arrives through ``require_participant``. The role checks that used to
    # stand here are gone because there is no longer a value they could reject.
    try:
        # Upload the love note image to Cloudinary
        upload_result = storage_service.upload_love_note_image(
            image_base64=note_data.image_base64,
            sender_name=sender.name or sender.regno
        )
        
        # Use the Cloudinary URL instead of the base64 string
        cloudinary_url = upload_result["cloudinary_url"]
        
    except HTTPException:
        # Size, format and dimension rejections are the sender's problem and carry a
        # message worth showing. Re-raise as-is instead of collapsing them into a 500
        # that reports a server fault and hides which limit was hit.
        raise
    except Exception as e:
        logger.error(f"Failed to upload love note image to Cloudinary: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload love note image"
        )
    
    # Create the love note with the Cloudinary URL
    love_note = LoveNote(
        sender_id=sender.id,
        recipient_id=str(note_data.recipient_id),
        image_url=cloudinary_url,
        body=note_data.body,
        anonymous=note_data.anonymous,
        status="pending_review"
    )

    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender.id, recipient_id=str(note_data.recipient_id),
            image_url=cloudinary_url, body=note_data.body,
            anonymous=note_data.anonymous, status="pending_review",
            created_at=love_note.created_at,
        )
        session.add(row)
        sender_row = session.get(UserRecord, sender.id)
        if sender_row:
            sender_row.love_notes_send_enabled = True
        session.flush()
        note_id = str(row.id)
        return {
            "message": "Love note sent successfully for review.",
            "note_id": note_id,
            # The sender is always entitled to their own note, in any review state.
            "image_url": asset_delivery_service.build_delivery_url(
                kind="love-notes", stored_identifier=cloudinary_url, account=AccountRef.of(sender)
            ) or cloudinary_url
        }
    
    logger.error("Failed to insert love note into the database.")
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Could not send the love note."
    )


# ============================================================================
# Admin-only Love Note Functions
# ============================================================================

async def get_all_love_notes_for_admin(db: Database, viewer: AccountRef) -> List[Dict[str, Any]]:
    """
    Fetch all love notes with full sender and recipient details for admin review.
    """
    items: List[Dict[str, Any]] = []
    with db.sessions() as session:
        rows = session.scalars(select(LoveNoteRecord).order_by(LoveNoteRecord.created_at.desc())).all()
        user_ids = {note.sender_id for note in rows} | {note.recipient_id for note in rows}
        users = {user.id: user for user in session.scalars(select(UserRecord).where(UserRecord.id.in_(user_ids))).all()} if user_ids else {}
    for doc in rows:
        sender_doc = users.get(doc.sender_id)
        recipient_doc = users.get(doc.recipient_id)
        
        items.append({
            "id": str(doc.id),
            "sender": {
                "id": str(doc.sender_id) if doc.sender_id else None,
                "name": sender_doc.name if sender_doc else None,
                "regno": sender_doc.regno if sender_doc else None,
                "email": sender_doc.email if sender_doc else None,
            },
            "recipient": {
                "id": str(doc.recipient_id) if doc.recipient_id else None,
                "name": recipient_doc.name if recipient_doc else None,
                "regno": recipient_doc.regno if recipient_doc else None,
                "email": recipient_doc.email if recipient_doc else None,
            },
            # Admins moderate notes in every state, so the proxy serves them the image.
            "image_url": asset_delivery_service.build_delivery_url(
                kind="love-notes", stored_identifier=doc.image_url, account=viewer
            ) or doc.image_url,
            "body": doc.body,
            "anonymous": doc.anonymous,
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
            sender_name = "Someone" if love_note.anonymous else (
                sender.name if sender else "Someone"
            )
            create_notification_service(
                recipient_regno=recipient.regno,
                heading="💌 You received a Love Note!",
                body=f"{sender_name} sent you a love note! Go to Love Notes → Inbox to view it.",
                db=db
            )
        
        # Notify sender that their love note was accepted
        if sender:
            create_notification_service(
                recipient_regno=sender.regno,
                heading="✅ Love Note Delivered!",
                body="Your love note was reviewed and successfully delivered to the recipient.",
                db=db
            )
    
    elif status_value == "rejected":
        # Notify sender that their love note was rejected
        if sender:
            create_notification_service(
                recipient_regno=sender.regno,
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
