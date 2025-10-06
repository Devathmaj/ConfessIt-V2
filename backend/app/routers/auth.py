# app/routers/auth.py

from fastapi import APIRouter, Depends, Request
from pymongo.database import Database
from typing import Annotated

# Corrected: Use direct imports from the project root
from ..dependencies import get_db
from ..models import UserDetails
from ..services.auth_service import (
    generate_magic_link_service,
    verify_magic_link_service,
    get_current_user
)
from ..services.storage_service import storage_service
from pydantic import BaseModel

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

class Token(BaseModel):
    access_token: str
    token_type: str
    redirect_url: str

@router.post("/login/magic")
async def generate_magic_link(request: Request, db: Database = Depends(get_db)):
    """
    Used to generate a magic link for a user if they exist in the database.
    """
    return await generate_magic_link_service(request, db)

@router.get("/login/magic/verify", response_model=Token)
async def verify_magic_link(token: str, db: Database = Depends(get_db)):
    """
    Used to verify a magic link token and return a JWT access token.
    """
    return verify_magic_link_service(token, db)

@router.get("/me", response_model=UserDetails)
async def read_users_me(current_user: Annotated[UserDetails, Depends(get_current_user)]):
    """
    Used to fetch the details of the currently authenticated user.
    """
    user_payload = current_user.model_dump(by_alias=True)
    enriched = storage_service.with_profile_signed_url(user_payload) or user_payload
    return UserDetails(**enriched)
