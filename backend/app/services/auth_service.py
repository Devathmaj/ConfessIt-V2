# app/services/auth_service.py

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from sqlalchemy import select, update
from ..db_models import UserRecord, LoginTokenRecord
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from ..postgres import Database

from ..config import settings
from ..dependencies import get_db
from ..models import UserDetails
from ..logger import get_logger

logger = get_logger(__name__)

# Claim set by the asset delivery proxy on its own short-lived tokens. Access tokens must
# never be minted from one, and asset tokens must not be accepted as access tokens: they
# travel in <img src> query strings and therefore leak into logs, referrers and history.
ASSET_TOKEN_TYPE = "asset_delivery"

# Single wording for every blocked-account rejection so the frontend can show
# one message regardless of which endpoint refused the request.
ACCOUNT_UNAVAILABLE_DETAIL = "Your account is currently not available. Please contact an administrator."

def _user_payload(user: UserRecord) -> dict:
    return {
        "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
        "emoji": user.emoji, "bio": user.bio, "class_name": user.class_name,
        "profile_picture_url": user.profile_picture_url, "gender": user.gender,
        "interests": user.interests or [], "matchmaking_enabled": user.matchmaking_enabled,
        "notifications_enabled": user.notifications_enabled, "love_notes_receive_enabled": user.love_notes_receive_enabled,
        "love_notes_send_enabled": user.love_notes_send_enabled, "is_blocked": user.is_blocked,
        "reported_count": user.reported_count, "last_login_at": user.last_login_at,
        "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
        "last_login_user_agent": user.last_login_user_agent, "role": user.role,
        "last_matchmaking_at": user.last_matchmaking_at,
    }

def _ensure_not_blocked(user: UserRecord) -> None:
    if user.is_blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACCOUNT_UNAVAILABLE_DETAIL,
        )

def _reject_asset_token(payload: dict) -> None:
    """Refuse an asset delivery token presented as an API access token."""
    if payload.get("typ") == ASSET_TOKEN_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

def load_active_user_service(db: Database, regno: str) -> UserDetails:
    """
    Load a user by registration number and reject unknown or blocked accounts.

    Used when a request is authenticated by an asset delivery token rather than by the
    normal bearer flow, so the account is re-validated against the database.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not regno:
        raise credentials_exception
    with db.sessions() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    if user is None:
        raise credentials_exception
    _ensure_not_blocked(user)
    return UserDetails(**_user_payload(user))


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """
    Used to create a JWT access token.
    """
    to_encode = data.copy()
    # Aware UTC, not utcnow(): the claim value is identical, but an aware datetime
    # cannot be silently confused with local time by a later edit.
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
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
        _reject_asset_token(payload)
    except jwt.PyJWTError as e:
        logger.error(f"JWT decode error: {e}")
        raise credentials_exception

    with db.sessions() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        if user is None:
            raise credentials_exception
        _ensure_not_blocked(user)
        return UserDetails(**_user_payload(user))

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
        _reject_asset_token(payload)
    except jwt.PyJWTError:
        raise credentials_exception

    with db.sessions() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        if user is None:
            raise credentials_exception
        _ensure_not_blocked(user)
        return UserDetails(**_user_payload(user))

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
        if payload.get("typ") == ASSET_TOKEN_TYPE:
            return None
        with db.sessions() as session:
            user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
            if user is None or user.is_blocked:
                return None
            return UserDetails(**_user_payload(user))
    except jwt.PyJWTError:
        return None


def admin_required(current_user: UserDetails = Depends(get_current_user)):
    """
    Used to verify that the current user is an admin based on their role.
    """
    if current_user.role != "admin":
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

    with db.sessions() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    if not user:
        logger.warning(f"No user found with registration number: {regno}")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Reject blocked accounts before a token is minted, so no magic link is ever
    # generated or logged for an account that could not use it.
    _ensure_not_blocked(user)

    # This function is not defined in the provided code, assuming it exists elsewhere
    from ..create_token import create_and_store_magic_token
    token = create_and_store_magic_token(db, str(user.id), request)

    magic_link = f"http://localhost:5173/verify-login?token={token}"
    logger.info(f"Magic Link for {user.name}: {magic_link}")

    return {"message": "Magic link generated. Check the backend console."}


def verify_magic_link_service(token: str, db: Database, request: Optional[Request] = None):
    """
    Used to verify a magic link token and return a JWT access token and a redirect URL based on user role.
    """
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required")

    hashed_token = hash_token(token)

    now = datetime.now(timezone.utc)

    if request is None or request.client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request context is required")

    with db.sessions.begin() as session:
        login_token = session.scalar(select(LoginTokenRecord).where(
            LoginTokenRecord.token_hash == hashed_token,
            LoginTokenRecord.used.is_(False),
            LoginTokenRecord.revoked.is_(False),
            LoginTokenRecord.expires_at > now,
        ))

    if not login_token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    login_token_id = str(login_token.id)
    user_id = str(login_token.user_id)
    request_user_agent = login_token.request_user_agent

    if str(login_token.request_ip) != request.client.host:
        with db.sessions.begin() as session:
            session.execute(
                update(LoginTokenRecord)
                .where(LoginTokenRecord.id == login_token_id)
                .values(attempt_count=LoginTokenRecord.attempt_count + 1)
            )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Token can only be used from the same IP address")

    consume_ip = request.client.host
    consume_user_agent = request.headers.get("user-agent", "unknown")

    with db.sessions.begin() as session:
        login_token_row = session.get(LoginTokenRecord, login_token_id)
        user = session.get(UserRecord, user_id) if login_token_row else None
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User associated with token not found")
        _ensure_not_blocked(user)
        result = session.execute(
            update(LoginTokenRecord)
            .where(
                LoginTokenRecord.id == login_token_id,
                LoginTokenRecord.used.is_(False),
                LoginTokenRecord.revoked.is_(False),
                LoginTokenRecord.expires_at > now,
            )
            .values(
                used=True,
                consumed_at=now,
                consume_ip=consume_ip,
                consume_user_agent=consume_user_agent,
                attempt_count=LoginTokenRecord.attempt_count + 1,
                session_status="active",
                last_seen_at=now,
                device=consume_user_agent or request_user_agent or "unknown",
            )
        )
        if result.rowcount != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")
        user.last_login_at = now
        user.last_login_ip = consume_ip
        user.last_login_user_agent = consume_user_agent
        user_regno = user.regno
        role = user.role

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user_regno},
        expires_delta=access_token_expires
    )

    # Used to determine the redirect URL based on the user's role.
    redirect_url = "/admin" if role == "admin" else "/dashboard"

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
