# app/services/profile_service.py

import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import File, HTTPException, UploadFile, status
from pymongo import ReturnDocument
from pymongo.database import Database
from pydantic import BaseModel

from ..models import UserDetails

# Logger
logger = logging.getLogger(__name__)

# Profile pictures directory
BASE_DIR = Path(__file__).resolve().parent.parent  # app/
PROFILE_PICTURES_DIR = BASE_DIR / "profile_pictures"
PROFILE_PICTURES_DIR.mkdir(parents=True, exist_ok=True)

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

    updated_user_doc = db["UserDetails"].find_one_and_update(
        {"Regno": current_user.Regno},
        {"$set": update_data},
        return_document=ReturnDocument.AFTER
    )

    if not updated_user_doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return UserDetails(**updated_user_doc)


async def upload_profile_picture_service(
    current_user: UserDetails,
    db: Database,
    file: UploadFile = File(...)
):
    """Service to upload and set a new profile picture."""
    # Enforce 2MB file size limit
    contents = await file.read()
    if len(contents) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="File is too large. Maximum size is 2MB."
        )
    await file.seek(0)  # Reset file pointer

    # Generate filename
    file_extension = os.path.splitext(file.filename)[1]
    file_name = f"{current_user.Regno}_profile{file_extension}"
    file_path = PROFILE_PICTURES_DIR / file_name

    # Remove old picture
    for ext in ['.png', '.jpg', '.jpeg', '.gif']:
        old_file = PROFILE_PICTURES_DIR / f"{current_user.Regno}_profile{ext}"
        if old_file.exists() and old_file != file_path:
            try:
                os.remove(old_file)
                logger.info(f"Removed old profile picture: {old_file}")
            except OSError as e:
                logger.error(f"Error removing old profile picture {old_file}: {e}")

    # Save new file
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        logger.error(f"Failed to save profile picture: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Could not save file.")

    # Update database
    db["UserDetails"].update_one(
        {"Regno": current_user.Regno},
        {"$set": {"profile_picture_id": file_name}}
    )

    return {
        "filename": file_name,
        "url": f"/profile_pictures/{file_name}"
    }
