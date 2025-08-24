# app/services/matchmaking_service.py

import logging
from fastapi import HTTPException, status
from pymongo.database import Database
from datetime import datetime, timedelta, timezone

from ..models import Match, UserDetails

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
    active_match = db.matches.find_one({
        "$or": [{"user_1_regno": current_user.Regno}, {"user_2_regno": current_user.Regno}],
        "expires_at": {"$gt": datetime.utcnow()}
    })

    if active_match:
        match_data = Match(**active_match)
        other_user_regno = match_data.user_1_regno if match_data.user_2_regno == current_user.Regno else match_data.user_2_regno
        
        other_user_details = db.UserDetails.find_one({"Regno": other_user_regno})
        if not other_user_details:
            # This case should ideally not happen if data integrity is maintained
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Matched user not found.")

        # Return the Pydantic model instance directly.
        # FastAPI will correctly serialize it using the model's Config.
        return {
            "status": "matched",
            "matched_with": UserDetails(**other_user_details),
            "match_id": str(match_data.id),  # Include the match ID
            "expires_at": match_data.expires_at.isoformat()
        }

    # If no active match, check for cooldown
    if current_user.last_matchmaking_time:
        cooldown_period = timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
        current_time = datetime.now(timezone.utc)
        last_matchmaking_time = current_user.last_matchmaking_time
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if last_matchmaking_time.tzinfo is None:
            last_matchmaking_time = last_matchmaking_time.replace(tzinfo=timezone.utc)
        
        time_since_last_match = current_time - last_matchmaking_time

        if time_since_last_match < cooldown_period:
            remaining_time = cooldown_period - time_since_last_match
            hours, remainder = divmod(remaining_time.total_seconds(), 3600)
            minutes, _ = divmod(remainder, 60)
            
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"You are on a cooldown. Please try again in {int(hours)} hours and {int(minutes)} minutes."
            )
    
    return {"status": "eligible", "message": "You are eligible for matchmaking. Proceed?"}


def find_match_service(
    current_user: UserDetails,
    db: Database
) -> dict:
    """
    Used to find a random user, create a persistent match, and update the matchmaking timestamp.
    This should be called after the cooldown check is passed and the user confirms.
    """
    # Re-check the cooldown as a safeguard
    if current_user.last_matchmaking_time:
        cooldown_period = timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
        current_time = datetime.now(timezone.utc)
        last_matchmaking_time = current_user.last_matchmaking_time
        
        # If the stored datetime is timezone-naive, assume it's UTC
        if last_matchmaking_time.tzinfo is None:
            last_matchmaking_time = last_matchmaking_time.replace(tzinfo=timezone.utc)
        
        time_since_last_match = current_time - last_matchmaking_time
        if time_since_last_match < cooldown_period:
             raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Cooldown is still active."
            )

    # Define the query to find a potential match
    query = {
        "Regno": {"$ne": current_user.Regno},
        "gender": {"$ne": current_user.gender},
        "isMatchmaking": True
    }

    count = db["UserDetails"].count_documents(query)
    if count == 0:
        # Still consume the attempt by setting the timestamp
        db["UserDetails"].update_one(
            {"Regno": current_user.Regno},
            {"$set": {"last_matchmaking_time": datetime.now(timezone.utc)}}
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No potential matches found. Your attempt has been used. Try again later!"
        )

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
    
    # Create and store the match
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=MATCH_EXPIRY_HOURS)
    
    new_match = Match(
        user_1_regno=current_user.Regno,
        user_2_regno=matched_user.Regno,
        created_at=now,
        expires_at=expires_at
    )
    match_result = db.matches.insert_one(new_match.dict(by_alias=True))
    
    # Get the inserted match ID
    match_id = str(match_result.inserted_id)

    # Update timestamps for both users
    db["UserDetails"].update_one(
        {"Regno": current_user.Regno},
        {"$set": {"last_matchmaking_time": now}}
    )
    db["UserDetails"].update_one(
        {"Regno": matched_user.Regno},
        {"$set": {"last_matchmaking_time": now}}
    )

    # Return both the matched user and the match ID
    return {
        "matched_with": matched_user,
        "match_id": match_id,
        "expires_at": expires_at.isoformat()
    }
