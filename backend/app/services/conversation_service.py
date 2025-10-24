# app/services/conversation_service.py

import logging
from fastapi import HTTPException, status
from pymongo.database import Database
from bson import ObjectId
from datetime import datetime, timezone

from ..models import UserDetails, Match, Conversation
from ..services.storage_service import storage_service
from ..services.supabase_service import supabase_service
from ..config import settings

# Logger
logger = logging.getLogger(__name__)

def request_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to initiate a conversation request based on a match.
    """
    try:
        match_id = ObjectId(match_id_str)
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Match ID format.")

    # 1. Fetch the match document
    match_doc = db.matches.find_one({"_id": match_id})
    if not match_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match not found.")
    
    try:
        match = Match(**match_doc)
    except Exception as e:
        logger.error(f"Error parsing match document: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error processing match data.")

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

    # 4. Check if a conversation already exists for this match
    existing_conversation = db.conversations.find_one({"matchId": match_id})
    if existing_conversation:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A conversation for this match already exists.")

    # 5. Determine initiator and receiver
    receiver_regno = match.user_2_regno if current_user.Regno == match.user_1_regno else match.user_1_regno

    # 6. Create the new conversation document
    try:
        new_conversation = Conversation(
            matchId=match_id,  # Use the ObjectId directly
            initiatorId=current_user.Regno,
            receiverId=receiver_regno,
            status="pending"
        )

        result = db.conversations.insert_one(new_conversation.dict(by_alias=True))
        
        # Sync to Supabase
        supabase_conversation_id = supabase_service.sync_conversation_to_supabase(
            mongo_conversation_id=str(result.inserted_id),
            match_id=str(match_id),
            initiator_id=current_user.Regno,
            receiver_id=receiver_regno,
            status="pending",
            created_at=new_conversation.createdAt
        )
        
        if not supabase_conversation_id:
            logger.warning(f"Failed to sync conversation to Supabase for match {match_id}")
        
        logger.info(f"Conversation request created from {current_user.Regno} to {receiver_regno} for match {match_id}")

        return {"status": "success", "message": "Conversation request sent."}
    except Exception as e:
        logger.error(f"Error creating conversation: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Error creating conversation.")


def get_conversation_status_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to get the status of a conversation for a given match.
    """
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


def accept_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to accept a pending conversation request.
    """
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
    
    # Sync status update to Supabase
    supabase_service.update_conversation_status_in_supabase(
        match_id=str(match_id),
        status="accepted",
        accepted_at=accepted_at
    )
    
    logger.info(f"Conversation {conversation.id} accepted by {current_user.Regno}")

    # Get Supabase conversation for token generation
    supabase_conversation = supabase_service.get_conversation_by_match_id(str(match_id))
    
    if supabase_conversation:
        # Generate ephemeral tokens for both users
        initiator_token = supabase_service.generate_ephemeral_token(
            user_id=conversation.initiatorId,
            conversation_id=supabase_conversation["id"]
        )
        receiver_token = supabase_service.generate_ephemeral_token(
            user_id=conversation.receiverId,
            conversation_id=supabase_conversation["id"]
        )
        
        return {
            "status": "success",
            "message": "Conversation accepted successfully.",
            "supabase_token": receiver_token,  # Token for the current user (receiver)
            "conversation_id": supabase_conversation["id"]
        }
    else:
        logger.error(f"Failed to get Supabase conversation for match {match_id}")
        return {"status": "success", "message": "Conversation accepted successfully."}


def reject_conversation_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to reject a pending conversation request.
    """
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
    if conversation.status != "pending":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Conversation is not in pending status.")

    # 7. Update the conversation status
    db.conversations.update_one(
        {"_id": conversation.id},
        {
            "$set": {
                "status": "rejected"
            }
        }
    )
    
    # Sync status update to Supabase
    supabase_service.update_conversation_status_in_supabase(
        match_id=str(match_id),
        status="rejected"
    )
    
    logger.info(f"Conversation {conversation.id} rejected by {current_user.Regno}")

    return {"status": "success", "message": "Conversation rejected successfully."}


def get_supabase_token_service(current_user: UserDetails, match_id_str: str, db: Database):
    """
    Used to get a Supabase ephemeral token for accessing messages in a conversation.
    This replaces send_message_service and get_conversation_messages_service.
    Frontend will use this token to directly interact with Supabase for messaging.
    """
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
    current_time = datetime.now(timezone.utc)
    match_expires_at = match.expires_at
    
    if match_expires_at.tzinfo is None:
        match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
    
    if current_time > match_expires_at:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail="This match has expired.")

    # 3. Verify the current user is part of the match
    if current_user.Regno not in [match.user_1_regno, match.user_2_regno]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not part of this match.")

    # 4. Get the conversation and check if it's accepted
    conversation_doc = db.conversations.find_one({"matchId": match_id})
    if not conversation_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No conversation found for this match.")

    conversation = Conversation(**conversation_doc)
    
    if conversation.status != "accepted":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Can only access messages in accepted conversations.")

    # 5. Get Supabase conversation and generate token
    supabase_conversation = supabase_service.get_conversation_by_match_id(str(match_id))
    
    if not supabase_conversation:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Conversation not synced to Supabase.")
    
    # Calculate time until match expires for token expiry
    time_until_expiry = (match_expires_at - current_time).total_seconds() / 3600  # in hours
    expires_in_hours = max(1, int(time_until_expiry) + 1)  # At least 1 hour, rounded up
    
    token = supabase_service.generate_ephemeral_token(
        user_id=current_user.Regno,
        conversation_id=supabase_conversation["id"],
        expires_in_hours=expires_in_hours
    )
    
    logger.info(f"Generated Supabase token for user {current_user.Regno} in match {match_id}")

    return {
        "status": "success",
        "supabase_token": token,
        "conversation_id": supabase_conversation["id"],
        "supabase_url": supabase_service.supabase_url,
        "expires_in_hours": expires_in_hours
    }


def get_current_conversation_service(current_user: UserDetails, db: Database):
    """
    Used to get the current active conversation for the authenticated user.
    If there is no active (non-expired) match, return the most recent match (expired)
    so the frontend can still display the inbox and conversation history.
    """
    current_time = datetime.now(timezone.utc)

    # Try to find an active (non-expired) match first
    current_match = db.matches.find_one({
        "$or": [{"user_1_regno": current_user.Regno}, {"user_2_regno": current_user.Regno}],
        "expires_at": {"$gt": current_time}
    })

    if not current_match:
        # No active match — try to find the most recent match (expired or not)
        current_match = db.matches.find_one(
            {"$or": [{"user_1_regno": current_user.Regno}, {"user_2_regno": current_user.Regno}]},
            sort=[("expires_at", -1)]
        )
        if not current_match:
            # No matches at all
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
        "is_initiator": conversation.initiatorId == current_user.Regno
    }
    
    # If conversation is accepted and not expired, include Supabase token
    if conversation.status == "accepted" and not is_expired:
        supabase_conversation = supabase_service.get_conversation_by_match_id(str(match.id))
        if supabase_conversation:
            match_expires_at = match.expires_at
            if match_expires_at.tzinfo is None:
                match_expires_at = match_expires_at.replace(tzinfo=timezone.utc)
            
            time_until_expiry = (match_expires_at - current_time).total_seconds() / 3600
            expires_in_hours = max(1, int(time_until_expiry) + 1)
            
            token = supabase_service.generate_ephemeral_token(
                user_id=current_user.Regno,
                conversation_id=supabase_conversation["id"],
                expires_in_hours=expires_in_hours
            )
            
            response_data["supabase_token"] = token
            response_data["supabase_anon_key"] = settings.SUPABASE_ANON_KEY
            response_data["conversation_id_supabase"] = supabase_conversation["id"]
            response_data["supabase_url"] = supabase_service.supabase_url
    
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

