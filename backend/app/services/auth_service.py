# app/services/auth_service.py

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from pymongo.database import Database
from pymongo import ReturnDocument

from ..config import settings
from ..dependencies import get_db
from ..models import UserDetails
from ..logger import get_logger

logger = get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """
    Used to create a JWT access token.
    """
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)
    return encoded_jwt

def hash_token(token: str) -> str:
    """
    Used to hash a token using SHA-256.
    """
    return hashlib.sha256(token.encode()).hexdigest()

def verify_token_service(token: str, db: Database) -> UserDetails:
    """
    Verify JWT token and return user details.
    Used for WebSocket authentication where Depends() cannot be used.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        regno: str = payload.get("sub")
        if regno is None:
            raise credentials_exception
    except jwt.PyJWTError as e:
        logger.error(f"JWT decode error: {e}")
        raise credentials_exception

    user_doc = db["UserDetails"].find_one({"Regno": regno})
    if user_doc is None:
        raise credentials_exception
    
    return UserDetails(**user_doc)

def get_current_user(token: str = Depends(oauth2_scheme), db: Database = Depends(get_db)) -> UserDetails:
    """
    Used to decode the JWT, validate the user, and return the user's details.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        regno: str = payload.get("sub")
        if regno is None:
            raise credentials_exception
    except jwt.PyJWTError:
        raise credentials_exception

    user_doc = db["UserDetails"].find_one({"Regno": regno})
    if user_doc is None:
        raise credentials_exception
    return UserDetails(**user_doc)

def get_current_user_optional(token: Optional[str] = Depends(oauth2_scheme), db: Database = Depends(get_db)) -> Optional[UserDetails]:
    """
    Used to decode the JWT and return user details if the token is valid,
    but does not raise an error if the token is missing or invalid.
    """
    if token is None:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        regno: str = payload.get("sub")
        if regno is None:
            return None
        user_doc = db["UserDetails"].find_one({"Regno": regno})
        if user_doc is None:
            return None
        return UserDetails(**user_doc)
    except jwt.PyJWTError:
        return None


def admin_required(current_user: UserDetails = Depends(get_current_user)):
    """
    Used to verify that the current user is an admin based on their role.
    """
    if current_user.user_role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to access this resource",
        )
    return current_user

async def generate_magic_link_service(request: Request, db: Database):
    """
    Used to generate a magic link for a user.
    """
    form_data = await request.json()
    regno = form_data.get("regno")
    if not regno:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Registration number is required")

    user = db["UserDetails"].find_one({"Regno": regno})
    if not user:
        logger.warning(f"No user found with registration number: {regno}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # This function is not defined in the provided code, assuming it exists elsewhere
    from ..create_token import create_and_store_magic_token
    token = create_and_store_magic_token(db, str(user["_id"]), request)

    magic_link = f"http://localhost:5173/verify-login?token={token}"
    logger.info(f"Magic Link for {user['Name']}: {magic_link}")

    return {"message": "Magic link generated. Check the backend console."}


def verify_magic_link_service(token: str, db: Database, request: Optional[Request] = None):
    """
    Used to verify a magic link token and return a JWT access token and a redirect URL based on user role.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required")

    hashed_token = hash_token(token)

    now = datetime.utcnow()

    login_token = db["LoginTokens"].find_one_and_update(
        {
            "token_hash": hashed_token,
            "used": False,
            "revoked": False,
            "expires_at": {"$gt": datetime.utcnow()},
        },
        {
            "$set": {
                "used": True,
                "consumed_at": now,
            }
        },
        return_document=ReturnDocument.BEFORE
    )

    if not login_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    # Check if the consumer IP matches the request IP from token generation
    if login_token.get("request_ip") != request.client.host:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token can only be used from the same IP address")

    # Update the token as used
    db["LoginTokens"].update_one(
        {"_id": login_token["_id"]},
        {"$set": {
            "used": True,
            "consumed_at": datetime.utcnow(),
            "consume_ip": request.client.host,
            "consume_user_agent": request.headers.get("user-agent", "unknown"),
            "attempt_count": 1
        }}
    )

    user = db["UserDetails"].find_one({"_id": ObjectId(login_token["user_id"])})
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User associated with token not found")

    consume_ip = request.client.host if request else login_token.get("request_ip")
    consume_user_agent = request.headers.get("user-agent") if request else login_token.get("request_user_agent")

    metadata = login_token.get("metadata") or {}
    metadata.update({
        "status": "active",
        "last_seen": now,
        "device": consume_user_agent or login_token.get("request_user_agent") or "unknown",
    })

    db["LoginTokens"].update_one(
        {"_id": login_token["_id"]},
        {
            "$set": {
                "consume_ip": consume_ip,
                "consume_user_agent": consume_user_agent,
                "metadata": metadata,
            }
        }
    )

    db["UserDetails"].update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "last_login_time": now,
                "last_login_ip": consume_ip,
                "last_login_user_agent": consume_user_agent,
            }
        }
    )

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["Regno"]},
        expires_delta=access_token_expires
    )

    # Used to determine the redirect URL based on the user's role.
    redirect_url = "/admin" if user.get("user_role") == "admin" else "/dashboard"

    return {"access_token": access_token, "token_type": "bearer", "redirect_url": redirect_url}

def decode_token(token: str) -> dict:
    """
    Used to decode and validate a JWT token.
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        return payload
    except jwt.PyJWTError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Could not validate token: {str(e)}"
        )
