# app/services/matchmaking_service.py

import random
from fastapi import HTTPException, status
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from ..postgres import Database
from ..db_models import ConversationRecord, MatchRecord, UserRecord
from ..logger import get_logger
from datetime import datetime, timedelta, timezone

from ..models import UserDetails
from ..services.storage_service import storage_service
from ..services import asset_delivery_service

# Logger
logger = get_logger(__name__)

# Used to define the cooldown period in hours.
MATCHMAKING_COOLDOWN_HOURS = 4

# Used to define the match expiry period in hours.
MATCH_EXPIRY_HOURS = 4

# Used to define the chance (in percentage) of same-gender matches.
# Change this value to adjust the probability (0.1 = 0.1% chance)
SAME_GENDER_MATCH_CHANCE = 0.1

# How many times to redraw when a racing request took the pair that was drawn. Each retry
# is a fresh candidate list, so this only needs to cover realistic contention.
PAIR_COLLISION_RETRIES = 5


def _consume_matchmaking_attempt(session, regno: str, now: datetime) -> bool:
    """Atomically claim the user's single matchmaking attempt.

    The conditional UPDATE is the only thing serialising this action. Concurrent callers
    contend on the same row lock, and because PostgreSQL re-evaluates the predicate against
    the committed version of the row, only the winner sees a row to update. Reading
    ``last_matchmaking_at`` and writing it in a later transaction instead leaves a window in
    which every concurrent request observes "eligible".

    Returns True if this caller won the attempt.
    """
    cooldown_cutoff = now - timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)
    result = session.execute(
        update(UserRecord)
        .where(
            UserRecord.regno == regno,
            or_(
                UserRecord.last_matchmaking_at.is_(None),
                UserRecord.last_matchmaking_at < cooldown_cutoff,
            ),
        )
        .values(last_matchmaking_at=now)
    )
    return result.rowcount == 1


def _cooldown_rejection(seconds_remaining: float) -> HTTPException:
    hours, remainder = divmod(seconds_remaining, 3600)
    minutes = remainder // 60
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"You are on a cooldown. Please try again in {int(hours)} hours and {int(minutes)} minutes.",
    )


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


def _pair_key(first_regno: str, second_regno: str) -> str:
    """Canonical identity for a pair, independent of who initiated the match."""
    return f"{min(first_regno, second_regno)}|{max(first_regno, second_regno)}"


class _PairTaken(Exception):
    """Another request matched this pair first; the caller should draw a different one."""


def _create_match_attempt(current_user: UserDetails, db: Database, now: datetime) -> dict:
    """One pass: claim the attempt, pick a partner, and write the match atomically."""
    is_rare_same_gender_match = random.random() < (SAME_GENDER_MATCH_CHANCE / 100)

    # Define the query to find a potential match
    filters = [UserRecord.matchmaking_enabled.is_(True), UserRecord.role != "admin",
               UserRecord.is_blocked.is_(False), UserRecord.regno != current_user.regno,
               UserRecord.gender == current_user.gender if is_rare_same_gender_match else UserRecord.gender != current_user.gender]

    # Skip anyone this user is already matched with right now. This keeps the common case
    # from even attempting a pair that is taken; the unique pair_key below is what
    # actually settles the race when two requests read the same candidate list. Each arm
    # selects the *other* column, since that is the partner rather than this user.
    live_partners = select(MatchRecord.user_2_regno).where(
        MatchRecord.user_1_regno == current_user.regno,
        MatchRecord.expires_at > now,
    ).union(
        select(MatchRecord.user_1_regno).where(
            MatchRecord.user_2_regno == current_user.regno,
            MatchRecord.expires_at > now,
        )
    )
    filters.append(UserRecord.regno.not_in(live_partners))

    with db.sessions() as session:
        candidate_users = session.scalars(select(UserRecord).where(*filters)).all()

    if not candidate_users:
        # The attempt is still spent even though nothing was created, so the user cannot
        # poll the endpoint for candidates.
        with db.sessions.begin() as session:
            _consume_matchmaking_attempt(session, current_user.regno, now)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No potential matches found. Your attempt has been used. Try again later!"
        )

    # Find random match
    matched_user_row = random.choice(candidate_users)
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
    expires_at = now + timedelta(hours=MATCH_EXPIRY_HOURS)
    pair_key = _pair_key(current_user.regno, matched_user.regno)

    try:
        # One transaction: claim the attempt, free any lapsed key for this pair, insert the
        # match and insert its pending conversation. A losing race rolls back having
        # written nothing, so it can neither consume the attempt nor orphan a match.
        with db.sessions.begin() as session:
            if not _consume_matchmaking_attempt(session, current_user.regno, now):
                # Lost the race, or an earlier request already took the attempt. The
                # winner holds the row lock until it commits, so this read is current.
                row = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
                last = row.last_matchmaking_at if row is not None else None
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                eligible_at = (last + timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)) if last else now
                raise _cooldown_rejection((eligible_at - now).total_seconds())

            # A match of this pair that has already lapsed must not keep the pair locked.
            # Doing it here, for the exact pair about to be created, is what makes the
            # uniqueness constraint mean "no two live matches" without a sweeper.
            session.execute(
                update(MatchRecord)
                .where(MatchRecord.pair_key == pair_key, MatchRecord.expires_at <= now)
                .values(pair_key=None)
            )

            match_row = MatchRecord(user_1_regno=current_user.regno, user_2_regno=matched_user.regno,
                                    created_at=now, expires_at=expires_at, pair_key=pair_key)
            session.add(match_row)
            session.flush()
            match_id_str = str(match_row.id)

            # Create a pending conversation in the same transaction. It becomes a request
            # only after the initiator explicitly sends one.
            session.add(ConversationRecord(
                match_id=match_id_str, initiator_regno=current_user.regno,
                receiver_regno=matched_user.regno, status="pending", created_at=now,
            ))
    except IntegrityError as exc:
        # Another request matched this exact pair first. The transaction rolled back, so
        # the attempt is unspent and it is safe to draw again.
        if "pair_key" not in str(exc.orig):
            raise
        raise _PairTaken from exc

    logger.info(f"Automatically created a pending conversation (not requested) for match {match_id_str}")

    # Notify the matched user only after the match is committed, so a notification failure
    # cannot roll back a match the user was already told about.
    #
    # No absolute time is written into the body. `now` is UTC and the server does not know
    # where the recipient is, so a formatted wall-clock string is persisted once and then
    # reads wrong for everyone outside the server's assumed zone -- a user at UTC+5:30 saw
    # "12:00 PM" for a match made at 17:31. The row's own created_at is timezone-aware and
    # already renders in the reader's local time, so the body stays timezone-free.
    from ..services.notification_service import create_notification_service
    create_notification_service(
        recipient_regno=matched_user.regno,
        heading="You appeared in matchmaking",
        body=f"You were matched with {current_user.name}",
        db=db
    )

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


def find_match_service(current_user: UserDetails, db: Database) -> dict:
    """
    Used to find a random user, create a persistent match, update the matchmaking timestamp,
    and automatically create a 'pending' conversation.
    """
    if current_user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrators cannot participate in matchmaking."
        )

    # Re-check cooldown status for a friendly message before touching the database. This
    # is only a fast-fail: _consume_matchmaking_attempt is the authoritative check,
    # because this one runs in a different transaction than the write.
    cooldown_check = check_matchmaking_cooldown_service(current_user, db)
    if cooldown_check["status"] not in ["eligible"]:
        if cooldown_check["status"] == "matched":
            return cooldown_check  # Return existing match data
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=cooldown_check.get("message", "Cooldown is still active.")
        )

    for _ in range(PAIR_COLLISION_RETRIES):
        try:
            return _create_match_attempt(current_user, db, datetime.now(timezone.utc))
        except _PairTaken:
            # The partner we drew was matched to this user by a racing request. Draw again;
            # the rolled-back attempt is still unspent.
            continue

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not settle on a partner just now. Please try again.",
    )



def get_active_matches(db: Database):
    """
    Get all active (non-expired) matches for WebSocket expiry checking.
    """
    current_time = datetime.now(timezone.utc)
    
    # Find all matches that haven't expired yet
    with db.sessions() as session:
        return list(session.scalars(select(MatchRecord).where(MatchRecord.expires_at > current_time)).all())
