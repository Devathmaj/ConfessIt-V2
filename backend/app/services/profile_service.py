# app/services/profile_service.py

from typing import List, Optional

from fastapi import File, HTTPException, UploadFile, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from ..db_models import UserRecord
from ..logger import get_logger
from ..postgres import Database

from ..models import Interest, UserDetails, clean_interests
from ..services.account_picture_service import (
    retire_previous_picture,
    upload_account_picture,
)
from ..accounts import AccountRef

# Logger
logger = get_logger(__name__)

class UserProfileUpdate(BaseModel):
    """Allowed fields for updating profile.

    ``interests`` is typed with the closed vocabulary rather than left as free text for the
    same reason the admin payload is: this is the route a participant uses to uncheck the boxes
    an admin ticked for them, so it is exactly the path through which a bad value -- "music",
    "Music", or a typo -- would otherwise get in and become a value nothing can match. The
    validator collapses duplicates and applies the cap.
    """
    emoji: Optional[str] = None
    bio: Optional[str] = None
    interests: Optional[List[Interest]] = None
    matchmaking_enabled: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    love_notes_receive_enabled: Optional[bool] = None

    @field_validator("interests")
    @classmethod
    def _validate_interests(cls, value: Optional[List[Interest]]) -> Optional[List[Interest]]:
        """De-duplicate and bound the list, sharing one rule with the admin write path.

        Returns ``None`` for ``None`` rather than coercing it to a list, because ``None`` is
        how ``exclude_unset`` distinguishes "left this field alone" from "set it to nothing";
        collapsing the two here would let an unrelated profile edit clear somebody's interests.
        """
        if value is None:
            return None
        return list(clean_interests(value))


async def update_user_profile_service(
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
            "emoji": "emoji", "bio": "bio", "interests": "interests",
            "matchmaking_enabled": "matchmaking_enabled", "notifications_enabled": "notifications_enabled",
            "love_notes_receive_enabled": "love_notes_receive_enabled",
        }
        for field, value in update_data.items():
            setattr(user, field_map[field], value)
        updated_user_doc = {
            "id": str(user.id), "regno": user.regno, "name": user.name,
            "email": user.email, "emoji": user.emoji, "bio": user.bio,
            "class_name": user.class_name, "profile_picture_url": user.profile_picture_url,
            "gender": user.gender, "interests": user.interests or [],
            "matchmaking_enabled": user.matchmaking_enabled, "notifications_enabled": user.notifications_enabled,
            "love_notes_receive_enabled": user.love_notes_receive_enabled, "love_notes_send_enabled": user.love_notes_send_enabled,
            "is_blocked": user.is_blocked,
            "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent,
            "last_matchmaking_at": user.last_matchmaking_at,
            "last_confession_at": user.last_confession_at,
            "account_type": "users",
        }

    # Profile images are delivered through the asset proxy.
    updated_user_doc["profile_picture_url"] = asset_delivery_service.profile_image_url(
        updated_user_doc.get("profile_picture_url"), account=AccountRef.of(current_user)
    )
    return UserDetails(**updated_user_doc)


async def upload_profile_picture_service(
    current_user: UserDetails,
    db: Database,
    file: UploadFile = File(...)
):
    """Service to upload and set a new profile picture for the signed-in participant."""
    # The upload itself -- capped read, validation, Cloudinary -- lives in
    # ``account_picture_service`` because an admin performing the same upload on a moderator's
    # row needs those rules too, and the row this needs is in the ``users`` table only. What
    # stays here is the part that *is* participant-specific: which row to write.
    result = await upload_account_picture(
        file=file,
        owner_regno=current_user.regno,
        account=AccountRef.of(current_user),
        previous_picture_url=current_user.profile_picture_url,
        actor_regno=current_user.regno,
    )

    # Written after the upload succeeded and committed, and only then is the old object retired.
    # The order is the whole point: a row that still names a deleted object is worse than a
    # leftover one, because nothing the owner does can bring the picture back.
    with db.sessions.begin() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
        if user:
            user.profile_picture_url = result["stored_identifier"]

    # Outside the transaction. If the write above rolled back, the row would be pointing at the old
    # object, and deleting it here is what would make that unrecoverable.
    retire_previous_picture(
        previous_picture_url=current_user.profile_picture_url,
        owner_regno=current_user.regno,
        new_stored_identifier=result["stored_identifier"],
        actor_regno=current_user.regno,
    )

    return result["response"]
