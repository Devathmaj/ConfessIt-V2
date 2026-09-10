# app/services/profile_service.py

from typing import List, Optional

from fastapi import File, HTTPException, UploadFile, status
from ..postgres import Database
from pydantic import BaseModel
from sqlalchemy import select
from ..db_models import UserRecord
from ..logger import get_logger

from ..models import UserDetails
from ..services.storage_service import storage_service
from ..services import asset_delivery_service

# Logger
logger = get_logger(__name__)

class UserProfileUpdate(BaseModel):
    """Allowed fields for updating profile."""
    emoji: Optional[str] = None
    bio: Optional[str] = None
    interests: Optional[List[str]] = None
    matchmaking_enabled: Optional[bool] = None
    notifications_enabled: Optional[bool] = None
    love_notes_receive_enabled: Optional[bool] = None


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
            "is_blocked": user.is_blocked, "reported_count": user.reported_count,
            "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent, "role": user.role,
            "last_matchmaking_at": user.last_matchmaking_at,
        }

    # Profile images are delivered through the asset proxy.
    updated_user_doc["profile_picture_url"] = asset_delivery_service.profile_image_url(
        updated_user_doc.get("profile_picture_url"), regno=current_user.regno
    )
    return UserDetails(**updated_user_doc)


async def upload_profile_picture_service(
    current_user: UserDetails,
    db: Database,
    file: UploadFile = File(...)
):
    """Service to upload and set a new profile picture."""
    # Read through the bounded reader rather than ``await file.read()``, which would
    # buffer the whole body before anything had looked at it.
    contents = await storage_service.read_upload_capped(file)

    try:
        storage_service.validate_image_file(file, contents, max_size_mb=storage_service.max_image_size_mb)
    except HTTPException as exc:

        raise exc
    except Exception as exc:
        logger.error(f"Unexpected error while validating profile image: {exc}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image file")

    upload_target_name = current_user.regno or "user"
    try:
        upload_result = storage_service.upload_profile_image(
            file=file,
            content=contents,
            user_folder=upload_target_name,
        )
        storage_service.verify_uploaded_asset("profile", upload_result["object_path"])
    except HTTPException as exc:
        logger.error(f"Cloudinary upload failed for user {current_user.regno}: {exc.detail}")
        raise exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Unexpected error uploading profile image for {current_user.regno}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to upload profile picture")


    # Store the full Cloudinary URL in the database (not just the object path)
    cloudinary_url = upload_result["cloudinary_url"]
    with db.sessions.begin() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == current_user.regno))
        if user:
            user.profile_picture_url = cloudinary_url

    # Hand back the proxy URL rather than the raw Cloudinary URL, so the frontend never
    # learns an address it could reuse outside the proxy's authorisation check.
    delivery_url = asset_delivery_service.build_delivery_url(
        kind="profile", stored_identifier=cloudinary_url, regno=current_user.regno
    )

    # Only now that the replacement is stored do we retire the old object. Deleting it
    # before the upload was attempted meant a failed upload left the account pointing at
    # an asset that had already been destroyed, which no retry could recover from.
    # ``delete_profile_asset`` re-checks that the path is under this account's folder, so
    # a stored value naming somebody else's object is refused rather than acted on.
    previous_identifier = current_user.profile_picture_url
    if previous_identifier:
        previous_path = storage_service.normalize_stored_path(previous_identifier, "profile")
        new_path = storage_service.normalize_stored_path(cloudinary_url, "profile")
        if previous_path and previous_path != new_path:
            try:
                storage_service.delete_profile_asset(
                    previous_identifier, owner_regno=current_user.regno
                )
            except HTTPException as exc:
                # The new picture is already saved, so a stale object is a cleanup
                # problem, not a reason to fail the request.
                logger.warning(
                    f"Failed to delete previous profile asset for {current_user.regno}: {exc.detail}"
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    f"Unexpected error deleting previous profile asset for {current_user.regno}: {exc}"
                )

    return {
        "profile_picture_url": delivery_url or cloudinary_url,
        "url": delivery_url or cloudinary_url,
        "message": "Profile picture uploaded successfully",
    }
