# app/services/conversation_service.py

import logging
from fastapi import HTTPException, status
from pymongo.database import Database
from bson import ObjectId
from datetime import datetime, timezone

from ..models import UserDetails, Match, Conversation
from ..services.storage_service import storage_service
from ..config import settings

# Logger
logger = logging.getLogger(__name__)

def request_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to update a conversation from 'pending' to 'requested' and notify the receiver.
    This is called when the initiator clicks "Send Message Request".
    """
    try: # <--- FIX: Added main try block
        try: # Indented this block
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        try: # Indented this block
            match = Match(**match_doc)
        except Exception as e:
            logger.error(f"Error parsing match document: {e}")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing match data.")

        # 2. Check if the match has expired
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 4. Get the existing conversation (should exist with status 'pending')
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)
        
        # 5. Verify current user is the initiator
        if conversation.initiatorId != current_user.Regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the initiator can send the message request.")

        # 6. Check if already requested
        if conversation.status != "pending":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Message request has already been sent.")

        # 7. Update conversation status to 'requested'
        requested_at = datetime.now(timezone.utc)
        db.conversations.update_one(
            {"_id": conversation.id},
            {
                "$set": {
                    "status": "requested",
                    "requestedAt": requested_at
                }
            }
        )
        
        # 8. Create notification for the receiver
        from ..services.notification_service import create_notification_service
        receiver_user_doc = db.UserDetails.find_one({"Regno": conversation.receiverId})
        if receiver_user_doc:
            receiver_user = UserDetails(**receiver_user_doc)
            create_notification_service(
                user_id=conversation.receiverId,
                heading=f"Message request from {current_user.Name}",
                body=f"{current_user.Name} wants to start a conversation with you!",
                db=db
            )
        
        logger.info(f"Conversation request sent from {current_user.Regno} to {conversation.receiverId} for match {match_id}")

        return {"status": "success", "message": "Message request sent."}
    except Exception as e: # <--- FIX: This except block is now correctly placed
        logger.error(f"Error creating conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error creating conversation.")


def get_conversation_status_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to get the status of a conversation for a given match.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 4. Get conversation status
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            return {"status": "no_conversation", "message": "No conversation exists for this match."}

        conversation = Conversation(**conversation_doc)
        
        return {
            "status": "success",
            "conversation_status": conversation.status,
            "initiator_id": conversation.initiatorId,
            "receiver_id": conversation.receiverId,
            "created_at": conversation.createdAt,
            "accepted_at": conversation.acceptedAt
        }
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in get_conversation_status_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error getting conversation status.")


def accept_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to accept a pending conversation request.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 4. Get the conversation
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)

        # 5. Check if the current user is the receiver
        if conversation.receiverId != current_user.Regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can accept the conversation.")

        # 6. Check if the conversation is already accepted
        if conversation.status == "accepted":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is already accepted.")
        
        # Check if the conversation is in 'requested' status (not 'pending')
        if conversation.status != "requested":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Conversation must be in 'requested' status to accept.")

        # 7. Update the conversation status
        accepted_at = datetime.now(timezone.utc)
        db.conversations.update_one(
            {"_id": conversation.id},
            {
                "$set": {
                    "status": "accepted",
                    "acceptedAt": accepted_at
                }
            }
        )
        
        # Create notification for the initiator
        from ..services.notification_service import create_notification_service
        initiator_user_doc = db.UserDetails.find_one({"Regno": conversation.initiatorId})
        receiver_user_doc = db.UserDetails.find_one({"Regno": current_user.Regno})
        if initiator_user_doc and receiver_user_doc:
            create_notification_service(
                user_id=conversation.initiatorId,
                heading=f"{current_user.Name} accepted your message request",
                body=f"{current_user.Name} accepted your message request. You can now start chatting!",
                db=db
            )
        
        logger.info(f"Conversation {conversation.id} accepted by {current_user.Regno}")

        return {
            "status": "success",
            "message": "Conversation accepted successfully.",
            "conversation_id": str(conversation.id)
        }
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in accept_conversation_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error accepting conversation.")


def reject_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to reject a pending conversation request.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # 1. Fetch the match document
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # 2. Check if the match has expired
        # Ensure both datetimes are timezone-aware for comparison
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        
        if current_time > match_expires_at:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

        # 3. Verify the current user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # 4. Get the conversation
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

        conversation = Conversation(**conversation_doc)

        # 5. Check if the current user is the receiver
        if conversation.receiverId != current_user.Regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the receiver can reject the conversation.")

        # 6. Check if the conversation is already accepted or rejected
        if conversation.status != "requested":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is not in requested status.")

        # 7. Update the conversation status
        db.conversations.update_one(
            {"_id": conversation.id},
            {
                "$set": {
                    "status": "rejected"
                }
            }
        )
        
        # Create notification for the initiator
        from ..services.notification_service import create_notification_service
        initiator_user_doc = db.UserDetails.find_one({"Regno": conversation.initiatorId})
        if initiator_user_doc:
            create_notification_service(
                user_id=conversation.initiatorId,
                heading=f"{current_user.Name} rejected your message request",
                body=f"{current_user.Name} rejected your message request.",
                db=db
            )
        
        logger.info(f"Conversation {conversation.id} rejected by {current_user.Regno}")

        return {"status": "success", "message": "Conversation rejected successfully."}
    except Exception as e: # <--- FIX: Added missing except block
        logger.error(f"Error in reject_conversation_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error rejecting conversation.")


def get_current_conversation_service(current_user: UserDetails, db: Database):
    """
    Used to get the current active conversation for the authenticated user.
    ONLY returns matches where the current user is the INITIATOR (user_1).
    The matched user (user_2/receiver) should NOT see the match until they get a notification
    and should be able to continue matchmaking.
    """
    current_time = datetime.now(timezone.utc)

    # Only find matches where current user is user_1 (initiator)
    current_match = db.matches.find_one({
        "user_1_regno": current_user.Regno,
        "expires_at": {"$gt": current_time}
    })

    if not current_match:
        # No active match where user is initiator — try to find the most recent one (expired or not)
        current_match = db.matches.find_one(
            {"user_1_regno": current_user.Regno},
            sort=[("expires_at", -1)]
        )
        if not current_match:
            # No matches where user is initiator
            return {"status": "no_active_match", "message": "No active match found."}
        # mark as expired if expires_at <= now
        match = Match(**current_match)
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
    else:
        match = Match(**current_match)
        is_expired = False

    # Get the conversation for this match (may or may not exist)
    conversation_doc = db.conversations.find_one({"matchId": match.id})
    if not conversation_doc:
        # No conversation created yet; return match info and empty conversation placeholder
        # Return success so frontend shows the match in inbox even if expired
        other_user_regno = match.user_2_regno if current_user.Regno == match.user_1_regno else match.user_1_regno
        other_user_doc = db.UserDetails.find_one({"Regno": other_user_regno})
        other_user = UserDetails(**other_user_doc) if other_user_doc else None

        profile_picture_url = storage_service.get_signed_profile_url(other_user.profile_picture_id if other_user else None)

        return {
            "status": "success",
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": None,
                "status": "none",
                "initiator_id": None,
                "receiver_id": None,
                "created_at": None,
                "accepted_at": None
            },
            "other_user": {
                "regno": other_user.Regno if other_user else None,
                "Regno": other_user.Regno if other_user else None,
                "name": other_user.Name if other_user else None,
                "Name": other_user.Name if other_user else None,
                "username": other_user.username if other_user else None,
                "profile_picture_id": profile_picture_url,
                "profile_picture": profile_picture_url,
                "which_class": other_user.which_class if other_user else None,
                "bio": other_user.bio if other_user else None,
                "interests": other_user.interests if other_user else []
            },
            "is_initiator": False
        }

    # If conversation exists, return it (allow expired)
    conversation = Conversation(**conversation_doc)
    other_user_regno = match.user_2_regno if current_user.Regno == match.user_1_regno else match.user_1_regno
    other_user_doc = db.UserDetails.find_one({"Regno": other_user_regno})
    other_user = UserDetails(**other_user_doc) if other_user_doc else None

    profile_picture_url = storage_service.get_signed_profile_url(other_user.profile_picture_id if other_user else None)

    response_data = {
        "status": "success",
        "match": {
            "id": str(match.id),
            "expires_at": match.expires_at.isoformat(),
            "created_at": match.created_at.isoformat(),
            "is_expired": is_expired
        },
        "conversation": {
            "id": str(conversation.id),
            "status": conversation.status,
            "initiator_id": conversation.initiatorId,
            "receiver_id": conversation.receiverId,
            "created_at": conversation.createdAt.isoformat(),
            "accepted_at": conversation.acceptedAt.isoformat() if conversation.acceptedAt else None
        },
        "other_user": {
            "regno": other_user.Regno if other_user else None,
            "Regno": other_user.Regno if other_user else None,
            "name": other_user.Name if other_user else None,
            "Name": other_user.Name if other_user else None,
            "username": other_user.username if other_user else None,
            "profile_picture_id": profile_picture_url,
            "profile_picture": profile_picture_url,
            "which_class": other_user.which_class if other_user else None,
            "bio": other_user.bio if other_user else None,
            "interests": other_user.interests if other_user else []
        },
        "is_initiator": conversation.initiatorId == current_user.Regno,
        "is_blocked": conversation_doc.get("isBlocked", False),
        "blocked_by": conversation_doc.get("blockedBy", None)
    }
    
    return response_data


def get_conversation_by_match_id(match_id_str: str, db: Database):
    """
    Get conversation by match ID (kept for compatibility).
    """
    try:
        match_id = ObjectId(match_id_str)
    except Exception:
        return None
    
    conversation_doc = db.conversations.find_one({"matchId": match_id})
    if not conversation_doc:
        return None
    
    return conversation_doc


def update_conversation_status(match_id_str: str, status: str, db: Database):
    """
    Update conversation status (kept for compatibility, also syncs to Supabase).
    """
    try:
        match_id = ObjectId(match_id_str)
    except Exception:
        return False
    
    result = db.conversations.update_one(
        {"matchId": match_id},
        {"$set": {"status": status}}
    )
    
    return result.modified_count > 0


def get_received_conversations_service(current_user: UserDetails, db: Database):
    """
    Get conversations where the current user is the RECEIVER (user_2).
    This shows message requests that the user has received.
    """
    current_time = datetime.now(timezone.utc)
    
    # Find matches where current user is user_2 (receiver)
    received_matches = list(db.matches.find({
        "user_2_regno": current_user.Regno,
        "expires_at": {"$gt": current_time}
    }))
    
    if not received_matches:
        return {"status": "no_received_conversations", "conversations": []}
    
    conversations = []
    
    for match_doc in received_matches:
        match = Match(**match_doc)
        
        # Get conversation for this match
        conversation_doc = db.conversations.find_one({"matchId": match.id})
        if not conversation_doc:
            continue
        
        conversation = Conversation(**conversation_doc)
        
        # Only include if status is 'requested' or later (not 'pending')
        if conversation.status == 'pending':
            continue
        
        # Get the initiator's info
        initiator_user_doc = db.UserDetails.find_one({"Regno": match.user_1_regno})
        if not initiator_user_doc:
            continue
        
        initiator_user = UserDetails(**initiator_user_doc)
        profile_picture_url = storage_service.get_signed_profile_url(initiator_user.profile_picture_id)
        
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
        
        conversation_data = {
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": str(conversation.id),
                "status": conversation.status,
                "initiator_id": conversation.initiatorId,
                "receiver_id": conversation.receiverId,
                "created_at": conversation.createdAt.isoformat(),
                "accepted_at": conversation.acceptedAt.isoformat() if conversation.acceptedAt else None
            },
            "other_user": {
                "regno": initiator_user.Regno,
                "Regno": initiator_user.Regno,
                "name": initiator_user.Name,
                "Name": initiator_user.Name,
                "username": initiator_user.username,
                "profile_picture_id": profile_picture_url,
                "profile_picture": profile_picture_url,
                "which_class": initiator_user.which_class,
                "bio": initiator_user.bio,
                "interests": initiator_user.interests or []
            },
            "is_initiator": False,
            "is_blocked": conversation_doc.get("isBlocked", False),
            "blocked_by": conversation_doc.get("blockedBy", None)
        }
        
        conversations.append(conversation_data)
    
    return {"status": "success", "conversations": conversations}


def get_conversation_by_match_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Get a specific conversation by match_id.
    Works for both initiators and receivers.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

        # Fetch the match document
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)

        # Verify the current user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

        # Get conversation for this match
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            return {"status": "no_conversation", "message": "No conversation exists for this match."}
        
        conversation = Conversation(**conversation_doc)
        
        # Determine if current user is initiator
        is_initiator = current_user.Regno == match.user_1_regno
        
        # Get the other user's info
        other_user_regno = match.user_2_regno if is_initiator else match.user_1_regno
        other_user_doc = db.UserDetails.find_one({"Regno": other_user_regno})
        if not other_user_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Other user not found.")
        
        other_user = UserDetails(**other_user_doc)
        profile_picture_url = storage_service.get_signed_profile_url(other_user.profile_picture_id)
        
        # Check if match is expired
        current_time = datetime.now(timezone.utc)
        match_expires_at = match.expires_at
        if match_expires_at.tzinfo is None:
            match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
        is_expired = current_time > match_expires_at
        
        conversation_data = {
            "status": "success",
            "match": {
                "id": str(match.id),
                "expires_at": match.expires_at.isoformat(),
                "created_at": match.created_at.isoformat(),
                "is_expired": is_expired
            },
            "conversation": {
                "id": str(conversation.id),
                "status": conversation.status,
                "initiator_id": conversation.initiatorId,
                "receiver_id": conversation.receiverId,
                "created_at": conversation.createdAt.isoformat(),
                "accepted_at": conversation.acceptedAt.isoformat() if conversation.acceptedAt else None
            },
            "other_user": {
                "regno": other_user.Regno,
                "name": other_user.Name,
                "username": other_user.username,
                "profile_picture_id": profile_picture_url,
                "which_class": other_user.which_class,
                "bio": other_user.bio,
                "interests": other_user.interests or []
            },
            "is_initiator": is_initiator,
            "is_blocked": conversation_doc.get("isBlocked", False),
            "blocked_by": conversation_doc.get("blockedBy", None)
        }
        
        return conversation_data
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in get_conversation_by_match_service: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error getting conversation.")


def block_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Block a conversation. Updates MongoDB only.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match ID.")
        
        # Get the match
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)
        
        # Verify user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")
        
        # Get the conversation
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        
        # Update conversation to blocked status
        db.conversations.update_one(
            {"_id": conversation_doc["_id"]},
            {
                "$set": {
                    "isBlocked": True,
                    "blockedBy": current_user.Regno,
                    "blockedAt": datetime.now(timezone.utc)
                }
            }
        )
        
        logger.info(f"User {current_user.Regno} blocked conversation for match {match_id}")
        
        return {
            "status": "success",
            "message": "Conversation blocked successfully.",
            "blocked_by": current_user.Regno
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error blocking conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error blocking conversation.")


def unblock_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Unblock a conversation. Only the user who blocked can unblock.
    """
    try:
        try:
            match_id = ObjectId(match_id_str)
        except Exception:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid match ID.")
        
        # Get the match
        match_doc = db.matches.find_one({"_id": match_id})
        if not match_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
        
        match = Match(**match_doc)
        
        # Verify user is part of the match
        if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")
        
        # Get the conversation
        conversation_doc = db.conversations.find_one({"matchId": match_id})
        if not conversation_doc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found.")
        
        # Check if user is the one who blocked
        if conversation_doc.get("blockedBy") != current_user.Regno:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only unblock if you were the one who blocked.")
        
        # Update conversation to unblocked
        db.conversations.update_one(
            {"_id": conversation_doc["_id"]},
            {
                "$set": {
                    "isBlocked": False,
                    "blockedBy": None,
                    "unblockedAt": datetime.now(timezone.utc)
                }
            }
        )
        
        logger.info(f"User {current_user.Regno} unblocked conversation for match {match_id}")
        
        return {
            "status": "success",
            "message": "Conversation unblocked successfully."
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error unblocking conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error unblocking conversation.")
