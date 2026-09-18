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
from ..services.matchmaking_slot_service import (
    candidate_shares_a_slot_predicate,
    cohort_is_configured,
    cohort_label,
    slots_are_configured,
)
from ..accounts import AccountRef

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


COHORT_NOT_CONFIGURED = "matchmaking_cohort_not_configured"
NO_CANDIDATES = "matchmaking_no_candidates"


class MatchmakingRefusal(HTTPException):
    """A draw that was refused, carrying a code the client can branch on.

    ``detail`` is the human sentence the toast shows and stays a plain string, because that is
    what ``describeApiError`` reads and it should not have to change. ``code`` is what the client
    branches on to decide what the *page* should say.

    The two need to be separate because the two refusals ask different things of the reader. "No
    potential matches found" is bad luck, and the honest response is that another attempt may
    succeed. "Your cohort is not configured" is not bad luck at all -- no future attempt can succeed
    until an operator adds a slot -- and telling that user to try again would be the same mistake as
    telling them they are on a cooldown. The server is the only place that knows which happened, and
    a client that inferred it from the wording would break the day a sentence is reworded.
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

    **Three answers, not two.** A cooldown and a cohort with no slot both stop a draw, but they are
    different problems for different people and the page has to be able to tell them apart: one is
    the user's own timer, the other is an operator's configuration. Answering ``cohort_not_configured``
    here rather than letting the draw discover it is what lets the page stop *before* the animation
    instead of after it, so the user is not made to watch five seconds of matching to be told what
    they could have been told by a click.

    Ordered so the cheaper and more common answer wins: a user who is on a cooldown is told so
    without the slot table being read, and a user with no slot is told that even if they would also
    have been on a cooldown -- because the slot is the one that will still be true in four hours.
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

    # A cohort no slot names cannot be matched, and no amount of waiting changes that. Only a
    # configured deployment can refuse a cohort this way; with no slots at all every cohort may
    # match with every other, so there is nobody to refuse.
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
    # Whether this cohort can be matched at all, decided before anything is written.
    #
    # This is the one refusal that costs the user nothing. Spending an attempt on it would be the
    # worst of the three outcomes: the user watches five seconds of animation, is told their cohort
    # is not configured, and is then locked out for four hours from a draw that could never have
    # succeeded anyway. Nothing about waiting changes the answer -- only an operator adding a slot
    # does -- so the attempt is left unspent and the user may click again the moment they are
    # configured, without losing anything.
    #
    # Read per draw, because an operator can add the missing slot between two attempts, and the
    # user should not have to wait out a cooldown to benefit from it.
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

    # Staff are excluded as candidates by the schema rather than by a filter: they have no
    # row in ``users``, so a moderator cannot be paired off as a stranger while holding a
    # review queue.
    filters = [UserRecord.matchmaking_enabled.is_(True),
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

    # Which cohorts may match is a rule about the deployment rather than about this user, and it is
    # read per draw because an operator changes it from the admin screen between two draws. Empty
    # means all-to-all and skips the predicate; non-empty means the rows are the whole rule, so a
    # cohort missing from every one of them cannot match anybody. Both branches are the same query
    # with a different WHERE clause rather than two queries, because "no candidates" has to mean the
    # same thing on both paths for the message below to be honest.
    #
    # ``slots_exist`` was resolved above, where the cohort was checked against it, rather than
    # queried again here.
    with db.sessions() as session:
        candidate_query = select(UserRecord).where(*filters)
        if slots_exist:
            candidate_query = candidate_query.where(
                candidate_shares_a_slot_predicate(current_user.year, current_user.department)
            )
        candidate_users = session.scalars(candidate_query).all()

    if not candidate_users:
        # The cohort is configured -- it was checked at the top of this function -- so this is
        # nobody being available rather than a rule that excludes everyone, and that is bad luck
        # rather than a permanent condition. The attempt is spent even though nothing was created,
        # so the user cannot poll the endpoint for candidates.
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
        "email": matched_user_row.email, "emoji": matched_user_row.emoji, "bio": matched_user_row.bio,
        "year": matched_user_row.year, "department": matched_user_row.department,
        "profile_picture_url": matched_user_row.profile_picture_url,
        "gender": matched_user_row.gender, "interests": matched_user_row.interests or [],
        "matchmaking_enabled": matched_user_row.matchmaking_enabled, "notifications_enabled": matched_user_row.notifications_enabled,
        "love_notes_receive_enabled": matched_user_row.love_notes_receive_enabled, "love_notes_send_enabled": matched_user_row.love_notes_send_enabled,
        "is_blocked": matched_user_row.is_blocked,
        "last_login_at": matched_user_row.last_login_at, "last_login_ip": str(matched_user_row.last_login_ip) if matched_user_row.last_login_ip else None,
        "last_login_user_agent": matched_user_row.last_login_user_agent,
        "last_matchmaking_at": matched_user_row.last_matchmaking_at,
        "last_confession_at": matched_user_row.last_confession_at,
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
    # No target. A match is a fact about two people, not a thing on a page: the recipient has
    # no list of their matches to open, and the match id means nothing until someone asks for
    # the conversation it opened. So this notice says what happened and stops there.
    create_notification_service(
        recipient_regno=matched_user.regno,
        heading="You appeared in matchmaking",
        body=f"You were matched with {current_user.name}",
        db=db
    )

    # Return match details immediately with full user information
    matched_user_picture_url = asset_delivery_service.profile_image_url(
        matched_user.profile_picture_url, account=AccountRef.of(current_user)
    )

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
    # Re-check cooldown status for a friendly message before touching the database. This
    # is only a fast-fail: _consume_matchmaking_attempt is the authoritative check,
    # because this one runs in a different transaction than the write.
    #
    # Only a cooldown is answered here. The check now also reports a cohort that no slot names, and
    # that must not be answered with a 429: it is not a rate limit, nothing is waiting, and a
    # cooldown status would tell the client to come back in four hours for a draw that cannot
    # succeed. It is left to _create_match_attempt, which refuses it with the right code and --
    # unlike a cooldown -- without spending the attempt. That costs a second read of the slot table
    # on a draw that is going to be refused anyway, which is cheaper than teaching two places how to
    # describe the same refusal and having one of them drift.
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
