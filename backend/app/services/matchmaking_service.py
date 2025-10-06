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
    Used to check if the user is eligible for matchmaking based on the cooldown period,
    or if they have an active match.
    """
    # Check for an active match first
    current_time = datetime.now(timezone.utc)

    # First, check for active match with proper expiry check
    active_match = db.matches.find_one({
        "$or": [{"user_1_regno": current_user.Regno}, {"user_2_regno": current_user.Regno}],
        "expires_at": {"$gt": current_time}
    })

    if not active_match:
        # If no active match, check cooldown
        if current_user.last_matchmaking_time:
            cooldown_period = timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
            
            # Ensure last_matchmaking_time is timezone-aware
            last_matchmaking_time = current_user.last_matchmaking_time
            if last_matchmaking_time.tzinfo is None:
                last_matchmaking_time = last_matchmaking_time.replace(tzinfo=timezone.utc)
            
            time_since_last_match = current_time - last_matchmaking_time
            
            if time_since_last_match < cooldown_period:
                remaining_time = cooldown_period - time_since_last_match
                hours, remainder = divmod(remaining_time.total_seconds(), 3600)
                minutes, _ = divmod(remainder, 60)
                
                return {
                    "status": "cooldown",
                    "message": f"You are on a cooldown. Please try again in {int(hours)} hours and {int(minutes)} minutes.",
                    "remaining_hours": int(hours),
                    "remaining_minutes": int(minutes)
                }
        
        return {"status": "eligible", "message": "You are eligible for matchmaking. Proceed?"}

    # If there is an active match, return its details
    match_data = Match(**active_match)
    other_user_regno = match_data.user_1_regno if match_data.user_2_regno == current_user.Regno else match_data.user_2_regno
    
    other_user_details = db.UserDetails.find_one({"Regno": other_user_regno})
    if not other_user_details:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matched user not found.")

    enriched_other = storage_service.with_profile_signed_url(other_user_details) if other_user_details else None

    return {
        "status": "matched",
        "matched_with": UserDetails(**(enriched_other or other_user_details)),
        "match_id": str(match_data.id),
        "expires_at": match_data.expires_at.isoformat()
    }


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
    db["UserDetails"].update_many(
        {"Regno": {"$in": [current_user.Regno, matched_user.Regno]}},
        {"$set": {"last_matchmaking_time": now}}
    )

    # Automatically create a pending conversation request
    try:
        match_id_str = str(match_result.inserted_id)
        request_conversation_service(current_user=current_user, match_id_str=match_id_str, db=db)
        logger.info(f"Automatically created a pending conversation for match {match_id_str}")
    except Exception as e:
        # Log the error but don't fail the matchmaking process.
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