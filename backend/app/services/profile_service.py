# app/services/profile_service.py

import logging
from typing import List, Optional

from fastapi import File, HTTPException, UploadFile, status
from ..postgres import Database
from pydantic import BaseModel
from sqlalchemy import select
from ..db_models import UserRecord

from ..models import UserDetails
from ..services.storage_service import storage_service

# Logger
logger = logging.getLogger(__name__)

class UserProfileUpdate(BaseModel):
    """Allowed fields for updating profile."""
    emoji: Optional[str] = None
    bio: Optional[str] = None
    interests: Optional[List[str]] = None
    isMatchmaking: Optional[bool] = None
    isNotifications: Optional[bool] = None
    isLovenotesRecieve: Optional[bool] = None


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
        user = session.scalar(select(UserRecord).where(UserRecord.Regno == current_user.Regno).with_for_update())
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        for field, value in update_data.items():
            setattr(user, field, value)
        updated_user_doc = {
            "_id": str(user.id), "Regno": user.Regno, "Name": user.Name,
            "email": user.email, "emoji": user.emoji, "bio": user.bio,
            "which_class": user.which_class, "profile_picture_id": user.profile_picture_id,
            "gender": user.gender, "interests": user.interests or [],
            "isMatchmaking": user.isMatchmaking, "isNotifications": user.isNotifications,
            "isLovenotesRecieve": user.isLovenotesRecieve, "isLovenotesSend": user.isLovenotesSend,
            "is_blocked": user.is_blocked, "reported_count": user.reported_count,
            "last_login_time": user.last_login_time, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
            "last_login_user_agent": user.last_login_user_agent, "user_role": user.user_role,
            "last_matchmaking_time": user.last_matchmaking_time,
        }

    enriched_doc = storage_service.with_profile_signed_url(updated_user_doc) or updated_user_doc
    return UserDetails(**enriched_doc)


async def upload_profile_picture_service(
    current_user: UserDetails,
    db: Database,
    file: UploadFile = File(...)
):
    """Service to upload and set a new profile picture."""
    contents = await file.read()

    try:
        storage_service.validate_image_file(file, contents, max_size_mb=storage_service.max_image_size_mb)
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        logger.error(f"Unexpected error while validating profile image: {exc}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image file")

    previous_identifier = current_user.profile_picture_id
    if previous_identifier:
        try:
            storage_service.delete_profile_asset(previous_identifier)
        except HTTPException as exc:
            logger.warning(f"Failed to delete previous profile asset for {current_user.Regno}: {exc.detail}")
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Unexpected error deleting previous profile asset for {current_user.Regno}: {exc}")

    upload_target_name = current_user.Regno or "user"
    try:
        upload_result = storage_service.upload_profile_image(
            file=file,
            content=contents,
            user_folder=upload_target_name,
        )
        storage_service.verify_uploaded_asset("profile", upload_result["object_path"])
    except HTTPException as exc:
        logger.error(f"Cloudinary upload failed for user {current_user.Regno}: {exc.detail}")
        raise exc
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(f"Unexpected error uploading profile image for {current_user.Regno}: {exc}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Failed to upload profile picture")

    # Store the full Cloudinary URL in the database (not just the object path)
    cloudinary_url = upload_result["cloudinary_url"]
    with db.sessions.begin() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.Regno == current_user.Regno))
        if user:
            user.profile_picture_id = cloudinary_url

    return {
        "profile_picture_id": cloudinary_url,
        "url": cloudinary_url,
        "message": "Profile picture uploaded successfully",
    }
