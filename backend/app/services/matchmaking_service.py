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
from ..services.matchmaking_slot_service import (
    candidate_shares_a_slot_predicate,
    cohort_is_configured,
    cohort_label,
    slots_are_configured,
)

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
    """Claim the attempt with one conditional update; return whether this caller won."""
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


COHORT_NOT_CONFIGURED = "matchmaking_cohort_not_configured"
NO_CANDIDATES = "matchmaking_no_candidates"


class MatchmakingRefusal(HTTPException):
    """A draw that was refused, carrying a code the client can branch on.

    ``detail`` is the human toast sentence and stays a plain string; ``code`` is what the page
    branches on. They are separate because "no potential matches found" is bad luck and another
    attempt may succeed, while "cohort not configured" needs an operator to add a slot -- and
    telling that user to try again would repeat the cooldown mistake. Only the server knows
    which happened; a client inferring from wording would break when the sentence is reworded.
    """

    def __init__(self, detail: str, code: str):
        super().__init__(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        self.code = code


def _cooldown_rejection(seconds_remaining: float) -> HTTPException:
    hours, remainder = divmod(seconds_remaining, 3600)
    minutes = remainder // 60
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=f"You are on a cooldown. Please try again in {int(hours)} hours and {int(minutes)} minutes.",
    )


def check_matchmaking_cooldown_service(current_user: UserDetails, db: Database):
    """
    Used to check if a user is eligible for matchmaking based on the cooldown period.
    The matchmaking page should only show matchmaking UI or cooldown, not conversation status.
    Conversation status is handled by the inbox.

    The caller is a ``UserDetails``, which the router only produces for an account in
    ``users``, so no staff check is needed here.

    **Three answers, not two**: a cooldown is the user's own timer, an unconfigured cohort is
    an operator's problem, and answering the latter here is what lets the page stop *before*
    the animation. Ordered so the cheaper, more common answer wins: a cooldown answer skips
    the slot table entirely, and a user with no slot is told that even if they would also have
    been on a cooldown -- the misconfiguration is what will still be true in four hours.
    """
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

    # Refuse unconfigured cohorts only when slots are in use; without slots, all cohorts can match.
    if slots_are_configured(db) and not cohort_is_configured(
        db, current_user.year, current_user.department
    ):
        return {
            "status": "cohort_not_configured",
            "message": (
                f"Your cohort ({cohort_label(current_user.year, current_user.department)}) is not "
                "in any matchmaking slot. An administrator needs to add it."
            ),
            "cohort": cohort_label(current_user.year, current_user.department),
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
    # This refusal does not consume an attempt; recheck slots each draw so new configuration applies immediately.
    slots_exist = slots_are_configured(db)
    if slots_exist and not cohort_is_configured(
        db, current_user.year, current_user.department
    ):
        raise MatchmakingRefusal(
            detail=(
                f"Your cohort ({cohort_label(current_user.year, current_user.department)}) is not "
                "configured for matchmaking. Your attempt has not been used."
            ),
            code=COHORT_NOT_CONFIGURED,
        )

    is_rare_same_gender_match = random.random() < (SAME_GENDER_MATCH_CHANCE / 100)

    # Staff have no ``users`` row, so they cannot enter the participant candidate pool.
    filters = [UserRecord.matchmaking_enabled.is_(True),
               UserRecord.is_blocked.is_(False), UserRecord.regno != current_user.regno,
               UserRecord.gender == current_user.gender if is_rare_same_gender_match else UserRecord.gender != current_user.gender]

    # Exclude live partners; the unique pair key still settles races between requests.
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

    # Slot settings are read per draw so operator changes apply immediately.
    # No slots means all-to-all; otherwise candidates must share a configured slot.
    with db.sessions() as session:
        candidate_query = select(UserRecord).where(*filters)
        if slots_exist:
            candidate_query = candidate_query.where(
                candidate_shares_a_slot_predicate(current_user.year, current_user.department)
            )
        candidate_users = session.scalars(candidate_query).all()

    if not candidate_users:
        # A configured cohort with no candidates spends the attempt to prevent polling.
        with db.sessions.begin() as session:
            _consume_matchmaking_attempt(session, current_user.regno, now)

        raise MatchmakingRefusal(
            detail="No potential matches found. Your attempt has been used. Try again later!",
            code=NO_CANDIDATES,
        )

    # Find random match
    matched_user_row = random.choice(candidate_users)
    matched_user = UserDetails(**{
        "id": str(matched_user_row.id), "regno": matched_user_row.regno, "name": matched_user_row.name,
        "email": matched_user_row.email, "bio": matched_user_row.bio,
        "year": matched_user_row.year, "department": matched_user_row.department,
        "profile_picture_url": storage_service.profile_delivery_url(
            matched_user_row.profile_picture_public_id, matched_user_row.profile_picture_format
        ),
        "gender": matched_user_row.gender, "interests": matched_user_row.interests or [],
        "matchmaking_enabled": matched_user_row.matchmaking_enabled, "notifications_enabled": matched_user_row.notifications_enabled,
        "love_notes_receive_enabled": matched_user_row.love_notes_receive_enabled, "love_notes_send_enabled": matched_user_row.love_notes_send_enabled,
        "is_blocked": matched_user_row.is_blocked,
        "last_login_at": matched_user_row.last_login_at, "last_login_ip": str(matched_user_row.last_login_ip) if matched_user_row.last_login_ip else None,
        "last_login_user_agent": matched_user_row.last_login_user_agent,
        "last_matchmaking_at": matched_user_row.last_matchmaking_at,
        "last_confession_at": matched_user_row.last_confession_at,
        "last_profile_picture_at": matched_user_row.last_profile_picture_at,
    })

    # Create the match with current UTC time
    expires_at = now + timedelta(hours=MATCH_EXPIRY_HOURS)
    pair_key = _pair_key(current_user.regno, matched_user.regno)

    try:
        # Claim the attempt, release an expired pair key, and create both rows atomically.
        with db.sessions.begin() as session:
            if not _consume_matchmaking_attempt(session, current_user.regno, now):
                # The winning request holds this row lock until its timestamp is committed.
                row = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
                last = row.last_matchmaking_at if row is not None else None
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                eligible_at = (last + timedelta(hours=MATCHMAKING_COOLDOWN_HOURS)) if last else now
                raise _cooldown_rejection((eligible_at - now).total_seconds())

            # Release this expired pair key so the uniqueness constraint covers live matches only.
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

            # Create a pending conversation; it becomes a request after the initiator sends one.
            session.add(ConversationRecord(
                match_id=match_id_str, initiator_regno=current_user.regno,
                receiver_regno=matched_user.regno, status="pending", created_at=now,
            ))
    except IntegrityError as exc:
        # The pair race rolled back the transaction, leaving the attempt available for a redraw.
        if "pair_key" not in str(exc.orig):
            raise
        raise _PairTaken from exc

    logger.info(f"Automatically created a pending conversation (not requested) for match {match_id_str}")

    # Notify after commit; the timezone-aware creation time is rendered in the recipient's locale.
    from ..services.notification_service import create_notification_service
    # Best-effort notice: a delivery failure must not turn a committed match into an HTTP error.
    try:
        create_notification_service(
            recipient_regno=matched_user.regno,
            heading="You appeared in matchmaking",
            body=f"You were matched with {current_user.name}",
            db=db
        )
    except Exception:
        logger.warning(
            "Failed to write the matchmaking notification for match %s; the match is "
            "already committed",
            match_id_str,
            exc_info=True,
        )

    # ``matched_user.profile_picture_url`` was already derived as the public CDN address when
    # built from the matched row; re-deriving it here would compute the same answer twice.
    matched_user_picture_url = matched_user.profile_picture_url

    matched_user_details = {
        "regno": matched_user.regno,
        "name": matched_user.name,
        "profile_picture_url": matched_user_picture_url,
        "profile_picture": matched_user_picture_url,
        "year": matched_user.year,
        "department": matched_user.department,
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

    As with the cooldown check, the participant-only caller is guaranteed by the route's
    dependency, so no staff check remains here.
    """
    # Check cooldown early for a clear response; the write transaction rechecks it authoritatively.
    # Cohort configuration is handled in the attempt path, not as a rate-limit response.
    cooldown_check = check_matchmaking_cooldown_service(current_user, db)
    if cooldown_check["status"] == "cooldown":
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=cooldown_check.get("message", "Cooldown is still active.")
        )
    if cooldown_check["status"] == "matched":
        return cooldown_check  # Return existing match data

    for _ in range(PAIR_COLLISION_RETRIES):
        try:
            return _create_match_attempt(current_user, db, datetime.now(timezone.utc))
        except _PairTaken:
            # A competing request took this pair; redraw because the transaction rolled back.
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
