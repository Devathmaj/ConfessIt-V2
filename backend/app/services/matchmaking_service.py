# app/services/matchmaking_service.py

import logging
from fastapi import HTTPException, status
from pymongo.database import Database
from datetime import datetime, timedelta, timezone

from ..models import Match, UserDetails
from ..services.storage_service import storage_service
# Removed top-level import to prevent circular dependency
# from ..services.conversation_service import request_conversation_service 

# Logger
logger = logging.getLogger(__name__)

# Used to define the cooldown period in hours.
MATCHMAKING_COOLDOWN_HOURS = 4

# Used to define the match expiry period in hours.
MATCH_EXPIRY_HOURS = 4


def check_matchmaking_cooldown_service(current_user: UserDetails, db: Database):
    """
    Used to check if the user is eligible for matchmaking based on the cooldown period.
    The matchmaking page should only show matchmaking UI or cooldown, not conversation status.
    Conversation status is handled by the inbox.
    """
    current_time = datetime.now(timezone.utc)

    # Check if user has matchmaking time within cooldown period
    if current_user.last_matchmaking_time:
        cooldown_period = timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
        
        # Ensure last_matchmaking_time is timezone-aware
        last_matchmaking_time = current_user.last_matchmaking_time
        if last_matchmaking_time.tzinfo is None:
            last_matchmaking_time = last_matchmaking_time.replace(tzinfo=timezone.utc)
        
        time_since_last_matchmaking = current_time - last_matchmaking_time
        
        if time_since_last_matchmaking < cooldown_period:
            # Within cooldown period - show cooldown
            remaining_time = cooldown_period - time_since_last_matchmaking
            hours, remainder = divmod(remaining_time.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            
            return {
                "status": "cooldown",
                "message": f"You are on a cooldown. Please try again in {int(hours)} hours and {int(minutes)} minutes.",
                "remaining_hours": int(hours),
                "remaining_minutes": int(minutes)
            }
    
    # No last_matchmaking_time or past cooldown period - eligible for matchmaking
    return {"status": "eligible", "message": "You are eligible for matchmaking. Proceed?"}


def find_match_service(current_user: UserDetails, db: Database) -> dict:
    """
    Used to find a random user, create a persistent match, update the matchmaking timestamp,
    and automatically create a 'pending' conversation.
    """
    # Import moved here to break the circular dependency
    from ..services.conversation_service import request_conversation_service

    # Re-check cooldown status
    cooldown_check = check_matchmaking_cooldown_service(current_user, db)
    if cooldown_check["status"] not in ["eligible"]:
        if cooldown_check["status"] == "matched":
            return cooldown_check  # Return existing match data
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=cooldown_check.get("message", "Cooldown is still active.")
        )

    # Define the query to find a potential match
    query = {
        "Regno": {"$ne": current_user.Regno},
        "gender": {"$ne": current_user.gender},
        "isMatchmaking": True
    }

    # Get count of potential matches
    count = db["UserDetails"].count_documents(query)
    if count == 0:
        # Update timestamp before returning
        db["UserDetails"].update_one(
            {"Regno": current_user.Regno},
            {"$set": {"last_matchmaking_time": datetime.now(timezone.utc)}}
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No potential matches found. Your attempt has been used. Try again later!"
        )

    # Find random match
    random_pipeline = [
        {"$match": query},
        {"$sample": {"size": 1}}
    ]
    match_cursor = db["UserDetails"].aggregate(random_pipeline)
    matched_user_doc = next(match_cursor, None)

    if not matched_user_doc:
        db["UserDetails"].update_one(
            {"Regno": current_user.Regno},
            {"$set": {"last_matchmaking_time": datetime.now(timezone.utc)}}
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find a match at this moment. Your attempt has been used. Please try again."
        )
    
    matched_user = UserDetails(**matched_user_doc)
    
    # Create the match with current UTC time
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=MATCH_EXPIRY_HOURS)
    
    new_match = Match(
        user_1_regno=current_user.Regno,
        user_2_regno=matched_user.Regno,
        created_at=now,
        expires_at=expires_at
    )
    match_result = db.matches.insert_one(new_match.dict(by_alias=True))
    
    # Update timestamps for both users
    db["UserDetails"].update_one(
        {"Regno": current_user.Regno},
        {"$set": {"last_matchmaking_time": now}}
    )

    # Create notification for the matched user
    from ..services.notification_service import create_notification_service
    human_readable_time = now.strftime("%B %d, %Y at %I:%M %p")
    create_notification_service(
        user_id=matched_user.Regno,
        heading="You appeared in matchmaking",
        body=f"You were matched with {current_user.Name} at {human_readable_time}",
        db=db
    )

    # Automatically create a pending conversation (NOT requested yet - no notification)
    # The conversation will be updated to 'requested' when initiator clicks "Send Message Request"
    try:
        match_id_str = str(match_result.inserted_id)
        match_id = match_result.inserted_id
        
        # Import moved here to avoid circular dependency issues
        from ..models import Conversation
        
        new_conversation = Conversation(
            matchId=match_id,
            initiatorId=current_user.Regno,
            receiverId=matched_user.Regno,
            status="pending"  # Not yet requested
        )
        
        conversation_result = db.conversations.insert_one(new_conversation.dict(by_alias=True))
        
        # Sync to Supabase
        from ..services.supabase_service import supabase_service
        supabase_conversation_id = supabase_service.sync_conversation_to_supabase(
            mongo_conversation_id=str(conversation_result.inserted_id),
            match_id=match_id_str,
            initiator_id=current_user.Regno,
            receiver_id=matched_user.Regno,
            status="pending",
            created_at=new_conversation.createdAt
        )
        
        if not supabase_conversation_id:
            logger.warning(f"Failed to sync conversation to Supabase for match {match_id_str}")
        
        logger.info(f"Automatically created a pending conversation (not requested) for match {match_id_str}")
    except Exception as e:
        # Log the error but don't fail the matchmaking process
        logger.error(f"Failed to automatically create a pending conversation for match {match_id_str}: {e}")

    # Return match details immediately with full user information
    matched_user_signed_url = storage_service.get_signed_profile_url(getattr(matched_user, "profile_picture_id", None))

    matched_user_details = {
        "regno": matched_user.Regno,
        "Regno": matched_user.Regno,
        "name": matched_user.Name,
        "Name": matched_user.Name,
        "username": getattr(matched_user, "username", None) or matched_user.Name,
        "profile_picture_id": matched_user_signed_url,
        "profile_picture": matched_user_signed_url,
        "which_class": getattr(matched_user, "which_class", ""),
        "interests": getattr(matched_user, "interests", []) or []
    }

    return {
        "status": "matched",
        "matched_with": matched_user_details,
        "match_id": str(match_result.inserted_id),
        "expires_at": expires_at.isoformat()
    }


def get_active_matches(db: Database):
    """
    Get all active (non-expired) matches for WebSocket expiry checking.
    """
    current_time = datetime.now(timezone.utc)
    
    # Find all matches that haven't expired yet
    active_matches = db.matches.find({
        "expires_at": {"$gt": current_time}
    })
    
    return list(active_matches)