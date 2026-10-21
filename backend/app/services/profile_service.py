# app/services/profile_service.py

import datetime
from datetime import timezone
from typing import List, Optional

from fastapi import File, HTTPException, UploadFile, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import or_, select, update

from ..db_models import UserRecord
from ..limits import USER_BIO_MAX
from ..logger import get_logger, log_identity_hash, redact_log_identifiers
from ..postgres import Database

from ..models import Interest, UserDetails, clean_interests
from ..services.account_picture_service import (
    read_and_prepare_account_picture,
    retire_previous_picture,
    store_account_picture,
)
from ..services.storage_service import storage_service

# Logger
logger = get_logger(__name__)

# Limit profile-picture changes to reduce churn and keep participant identities recognizable.
PROFILE_PICTURE_COOLDOWN_DAYS = 3


def _picture_cooldown_message(seconds_remaining: float) -> str:
    """The sentence a participant is shown, from the number of seconds left.

    The wait is quoted, not a wall-clock instant, for the same reason the
    confession cooldown quotes it: "try again in 2 days and 4 hours" is the
    answer to "why was I refused", a timestamp is not. The rule itself is
    stated in every copy, because a refusal is also where most participants
    first learn the rule exists.
    """
    days, remainder = divmod(int(seconds_remaining), 86400)
    hours = remainder // 3600
    minutes = (remainder % 3600) // 60
    rule = (
        f"You can change your profile picture once every "
        f"{PROFILE_PICTURE_COOLDOWN_DAYS} days."
    )
    if days > 0:
        wait = f"{days} day{'s' if days != 1 else ''}"
        if hours:
            wait += f" and {hours} hour{'s' if hours != 1 else ''}"
        return f"{rule} Please try again in {wait}."
    if hours > 0:
        return (
            f"{rule} Please try again in {hours} hour{'s' if hours != 1 else ''} "
            f"and {minutes} minute{'s' if minutes != 1 else ''}."
        )
    return f"{rule} Please try again in {minutes} minute{'s' if minutes != 1 else ''}."


def _consume_picture_attempt(session, regno: str, now: datetime.datetime) -> bool:
    """Atomically claim the caller's one picture change for this window.

    Same conditional UPDATE as ``confession_service``/``matchmaking_service``:
    concurrent uploads contend on the row lock, PostgreSQL re-evaluates the
    predicate against the committed row, and only the winner sees a row to
    update. Reading the column and writing it in a later transaction would
    let every racing request observe "eligible".

    Returns True if this caller won the window.
    """
    cooldown_cutoff = now - datetime.timedelta(days=PROFILE_PICTURE_COOLDOWN_DAYS)
    result = session.execute(
        update(UserRecord)
        .where(
            UserRecord.regno == regno,
            or_(
                UserRecord.last_profile_picture_at.is_(None),
                UserRecord.last_profile_picture_at < cooldown_cutoff,
            ),
        )
        .values(last_profile_picture_at=now)
    )
    return result.rowcount == 1


def _seconds_until_picture_eligible(
    session, regno: str, now: datetime.datetime
) -> float:
    """How long this caller still has to wait. Zero or less means they may change it now."""
    last = session.scalar(
        select(UserRecord.last_profile_picture_at).where(UserRecord.regno == regno)
    )
    if last is None:
        return 0.0
    # TIMESTAMPTZ can still arrive naive from a script or a hand-edited fixture, and
    # cooldown arithmetic across a naive and an aware datetime raises. Refusing a picture
    # change with a 500 because of a timezone would be the wrong way to be strict.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    eligible_at = last + datetime.timedelta(days=PROFILE_PICTURE_COOLDOWN_DAYS)
    return (eligible_at - now).total_seconds()


def _seconds_left_before_change(db: Database, regno: str, now: datetime.datetime) -> float:
    """The same question, asked of a session of our own, before anything is uploaded."""
    with db.sessions() as session:
        return _seconds_until_picture_eligible(session, regno, now)


def _picture_cooldown_rejection(seconds_remaining: float) -> HTTPException:
    """429 with the wait in the message, and ``Retry-After`` so a client can count down.

    429 rather than 403: nothing is wrong with the caller's right to change their picture,
    only with the time, and a 403 would be indistinguishable from being refused for good.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_picture_cooldown_message(max(seconds_remaining, 0)),
        headers={"Retry-After": str(max(int(seconds_remaining), 0))},
    )


def _discard_uploaded_picture(public_id: str, owner_regno: str) -> None:
    """Delete an object that never became anybody's picture.

    Only reached when the claim was refused *after* the upload succeeded, so the object
    exists and no row names it. Best effort, like every other retirement: the alternative
    is answering a 429 with a 500 because cleanup failed, and an orphaned object is a
    cleanup problem rather than a user-visible one.
    """
    try:
        storage_service.delete_profile_asset(public_id, owner_regno=owner_regno)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to discard refused profile upload for regno_hash=%s: %s",
            log_identity_hash(owner_regno),
            redact_log_identifiers(exc, public_id, owner_regno),
        )


class UserProfileUpdate(BaseModel):
    """Allowed fields for updating profile.

    ``interests`` is typed with the closed vocabulary rather than left as
    free text: it is a list something has to match on, so a bad value becomes
    a value nothing can match. ``bio`` is capped against the column width
    here because this is now the only path that writes it -- without it the
    participant gets a 500 from the ``String(500)`` column instead of a 422
    naming the field, and "Internal Server Error" for a 501st character is
    not a useful response. ``extra="forbid"`` makes an unknown key a 422
    instead of a KeyError in the field_map below (or a silently dropped
    edit): the map is an allowlist, and anything outside it must fail
    loudly.
    """

    model_config = ConfigDict(extra="forbid")

    bio: Optional[str] = Field(default=None, max_length=USER_BIO_MAX)
    interests: Optional[List[Interest]] = None
    matchmaking_enabled: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    love_notes_receive_enabled: Optional[bool] = None

    @field_validator("interests")
    @classmethod
    def _validate_interests(cls, value: Optional[List[Interest]]) -> Optional[List[Interest]]:
        """De-duplicate and bound the list, sharing one rule with the admin write path.

        Returns ``None`` for ``None`` rather than coercing it to a list:
        ``None`` is how ``exclude_unset`` distinguishes "left this field
        alone" from "set it to nothing"; collapsing the two would let an
        unrelated profile edit clear somebody's interests.
        """
        if value is None:
            return None
        return list(clean_interests(value))


def update_user_profile_service(
    profile_data: UserProfileUpdate,
    current_user: UserDetails,
    db: Database
) -> UserDetails:
    """Service to update user profile."""
    update_data = profile_data.model_dump(exclude_unset=True)

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No update data provided"
        )

    with db.sessions.begin() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno).with_for_update())
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        field_map = {
            "bio": "bio", "interests": "interests",
            "matchmaking_enabled": "matchmaking_enabled", "notifications_enabled": "notifications_enabled",
            "love_notes_receive_enabled": "love_notes_receive_enabled",
        }
        for field, value in update_data.items():
            if field not in field_map:
                # Unreachable with extra="forbid" above, but fail loudly (422)
                # rather than KeyError (500) if the model ever widens.
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Unknown profile field: {field}",
                )
            setattr(user, field_map[field], value)
        updated_user_doc = {
            "id": str(user.id), "regno": user.regno, "name": user.name,
            "email": user.email, "bio": user.bio,
            "year": user.year, "department": user.department,
            # The row holds the picture's identity; the URL the client receives is derived from
            # it below, so nothing durable ever depends on an address that could be regenerated
            # differently by a later build.
            "profile_picture_url": storage_service.profile_delivery_url(
                user.profile_picture_public_id, user.profile_picture_format
            ),
            "gender": user.gender, "interests": user.interests or [],
            "matchmaking_enabled": user.matchmaking_enabled, "notifications_enabled": user.notifications_enabled,
            "love_notes_receive_enabled": user.love_notes_receive_enabled, "love_notes_send_enabled": user.love_notes_send_enabled,
            "is_blocked": user.is_blocked,
            "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent,
            "last_matchmaking_at": user.last_matchmaking_at,
            "last_confession_at": user.last_confession_at,
            "last_profile_picture_at": user.last_profile_picture_at,
            "account_type": "users",
        }

    return UserDetails(**updated_user_doc)


async def upload_profile_picture_service(
    current_user: UserDetails,
    db: Database,
    file: UploadFile = File(...)
):
    """Upload a participant picture after validation and moderation.

    The cooldown is checked early to avoid unnecessary uploads and claimed
    atomically with the database write to handle races. Refused or failed writes
    discard the uploaded object; moderation failures spend the cooldown only
    for violating content, not provider outages.
    """
    # Shared upload validation lives in account_picture_service; this function
    # handles participant eligibility and writes the users row.
    contents = await read_and_prepare_account_picture(
        file=file,
        owner_regno=current_user.regno,
        actor_regno=current_user.regno,
    )

    now = datetime.datetime.now(datetime.timezone.utc)

    seconds_left = _seconds_left_before_change(db, current_user.regno, now)
    if seconds_left > 0:
        raise _picture_cooldown_rejection(seconds_left)

    # Moderate prepared bytes before storage. Provider failures abort before the
    # cooldown is spent; keep the import local for service-level patching.
    from ..services.content_safety_service import (
        CONTENT_PROFILE_PICTURE,
        ContentSafetyError,
        moderate_profile_picture,
        moderation_error_to_http,
    )

    try:
        decision = moderate_profile_picture(contents)
    except ContentSafetyError as exc:
        logger.error(
            "Content Safety moderation failed (%s); aborting profile picture upload",
            exc.code,
        )
        raise moderation_error_to_http(exc, content_type=CONTENT_PROFILE_PICTURE) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Content Safety moderation failed unexpectedly: %s",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Content moderation is temporarily unavailable. Please retry shortly.",
        ) from exc

    if decision.should_block:
    # A moderation refusal spends the change window. The conditional update
    # prevents racing uploads; no image or report row is stored.
        with db.sessions.begin() as session:
            if not _consume_picture_attempt(session, current_user.regno, now):
                raise _picture_cooldown_rejection(
                    _seconds_until_picture_eligible(session, current_user.regno, now)
                )
        from ..services.staff_notification_service import (
            notify_profile_picture_rejected,
        )

        notify_profile_picture_rejected(db, regno=current_user.regno, occurred_at=now)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Your profile picture was rejected because it violated our "
                "content policies."
            ),
        )

    result = store_account_picture(
        content=contents,
        owner_regno=current_user.regno,
        actor_regno=current_user.regno,
    )

    # Lock and read the current object in the write transaction. Commit the replacement
    # before retiring the old object so racing uploads cannot delete the winner's image.
    previous_public_id: Optional[str] = None
    try:
        with db.sessions.begin() as session:
            user = session.scalar(
                select(UserRecord).where(UserRecord.regno == current_user.regno).with_for_update()
            )
            # The authenticated account may have been deleted since sign-in; refuse and
            # discard the uploaded object instead of reporting a no-op success.
            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Not authorized to change this profile picture",
                )
            if not _consume_picture_attempt(session, user.regno, now):
                raise _picture_cooldown_rejection(
                    _seconds_until_picture_eligible(session, user.regno, now)
                )
            previous_public_id = user.profile_picture_public_id
            user.profile_picture_public_id = result["public_id"]
            user.profile_picture_format = result["format"]
    except HTTPException:
        # The transaction above rolled back, so the row still names the previous picture and
        # the object just uploaded is named by nothing at all. Dropping it here is what keeps
        # a refused change from leaving a stray object behind.
        _discard_uploaded_picture(result["public_id"], current_user.regno)
        raise
    except Exception:
        # The transaction rolled back, so discard the orphaned upload and preserve the
        # original database error for the caller.
        logger.error(
            "Profile picture upload for regno_hash=%s failed after the object was stored; "
            "discarding the orphaned object",
            log_identity_hash(current_user.regno),
            exc_info=True,
        )
        _discard_uploaded_picture(result["public_id"], current_user.regno)
        raise

    # Outside the transaction. If the write above rolled back, the row would be pointing at the old
    # object, and deleting it here is what would make that unrecoverable.
    retire_previous_picture(
        previous_public_id=previous_public_id,
        new_public_id=result["public_id"],
        owner_regno=current_user.regno,
        actor_regno=current_user.regno,
    )

    return result["response"]
