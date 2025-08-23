# app/routes/profile.py

from fastapi import APIRouter, Depends, UploadFile, File
from pymongo.database import Database
from typing import Annotated

from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import get_current_user
from ..services.profile_service import (
    UserProfileUpdate,
    update_user_profile_service,
    upload_profile_picture_service
)

router = APIRouter(
    prefix="/profile",
    tags=["Profile"]
)

@router.put("/update", response_model=UserDetails)
async def update_user_profile(
    profile_data: UserProfileUpdate,
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db)
):
    """Update the profile of the currently authenticated user."""
    return await update_user_profile_service(profile_data, current_user, db)


@router.post("/upload-picture")
async def upload_profile_picture(
    current_user: Annotated[UserDetails, Depends(get_current_user)],
    db: Database = Depends(get_db),
    file: UploadFile = File(...)
):
    """
    Used to upload and set a new profile picture for the user.
    """
    return await upload_profile_picture_service(current_user, db, file)
