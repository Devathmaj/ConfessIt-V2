# app/services/auth_service.py

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import jwt
from sqlalchemy import select, update
from ..db_models import (ACCOUNT_MODELS, LoginTokenRecord, find_account_in_table,
                         resolve_account)
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from ..postgres import Database

from ..config import settings
from ..dependencies import get_db
from ..models import AccountDetails, UserDetails
from ..accounts import (AccountRef, AccountType, capabilities_for,
                        is_valid_account_type, normalize_account_type)
from ..logger import get_console_only_logger, get_logger

logger = get_logger(__name__)

# Deliberately separate from the module logger above. The magic link is a live bearer
# credential, so it goes to stdout only: `docker compose logs backend` shows it while
# debugging, but the OTLP handler is never attached, so it is not exported to the
# collector and never lands in Loki (or any log store with a retention period).
magic_link_logger = get_console_only_logger(__name__ + ".magic_link")

# Claim set by the asset delivery proxy on its own short-lived tokens. Access tokens must
# never be minted from one, and asset tokens must not be accepted as access tokens: they
# travel in <img src> query strings and therefore leak into logs, referrers and history.
ASSET_TOKEN_TYPE = "asset_delivery"

# Single wording for every blocked-account rejection so the frontend can show
# one message regardless of which endpoint refused the request.
ACCOUNT_UNAVAILABLE_DETAIL = "Your account is currently not available. Please contact an administrator."

# What each sign-in page calls the kind of account it is for. Used in the one message a
# person sees when their regno is not in that category, so that "no account for that
# registration number here" names the page they are actually on instead of leaving them to
# guess which of the three they used. It deliberately does not say whether the regno exists
# in another table -- that would turn the sign-in form into a probe for whether a given
# staff account exists.
ACCOUNT_CATEGORY_LABELS: Dict[str, str] = {
    AccountType.USERS.value: "participant",
    AccountType.ADMIN.value: "admin",
    AccountType.MODERATOR.value: "moderator",
}

# The columns every account table shares. Read generically so one serialiser serves all
# three, and so a field added to the mixin cannot be added to only one of the tables.
_ACCOUNT_SERIALIZED_FIELDS: Tuple[str, ...] = (
    "id", "regno", "name", "email", "profile_picture_url", "gender",
    "notifications_enabled", "is_blocked", "last_login_at",
    "last_login_ip", "last_login_user_agent", "created_at", "updated_at",
)


def _account_payload(row: Any, account_type: str) -> Dict[str, Any]:
    """Build the shared account shape from a row in any of the three account tables."""
    payload: Dict[str, Any] = {name: getattr(row, name) for name in _ACCOUNT_SERIALIZED_FIELDS}
    payload["id"] = str(row.id)
    payload["last_login_ip"] = str(row.last_login_ip) if row.last_login_ip else None
    payload["account_type"] = account_type
    return payload


def _user_payload(row: Any) -> Dict[str, Any]:
    """Build the full participant shape from a ``users`` row."""
    return {
        **_account_payload(row, AccountType.USERS.value),
        "emoji": row.emoji,
        "bio": row.bio,
        "class_name": row.class_name,
        "interests": row.interests or [],
        "matchmaking_enabled": row.matchmaking_enabled,
        "love_notes_receive_enabled": row.love_notes_receive_enabled,
        "love_notes_send_enabled": row.love_notes_send_enabled,
        "last_matchmaking_at": row.last_matchmaking_at,
    }


def account_from_row(account_type: str, row: Any):
    """Build the model an account of this kind is exposed as.

    A participant row becomes a ``UserDetails``; a staff row becomes the shared
    ``AccountDetails``. Both carry the shared columns, email included, so the difference is
    exactly the participant fields -- which is the same distinction the schema makes.
    """
    if account_type == AccountType.USERS.value:
        return UserDetails(**_user_payload(row))
    return AccountDetails(**_account_payload(row, account_type))


def _ensure_not_blocked(row: Any) -> None:
    if row.is_blocked:
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

def load_active_user_service(db: Database, account: AccountRef):
    """
    Load the account a delivery token was minted for, and reject it if unknown or blocked.

    Used when a request is authenticated by an asset delivery token rather than by the
    normal bearer flow. The token names the table and primary key of the account it was
    issued to, so this reads one row in one table: re-resolving by regno would let a token
    for one account stand in for a different account that happens to share its regno.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not account or not is_valid_account_type(account.account_type):
        raise credentials_exception
    with db.sessions() as session:
        row = resolve_account(session, account.account_type, account.account_id)
    if row is None:
        raise credentials_exception
    _ensure_not_blocked(row)
    return account_from_row(account.account_type, row)



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

def _account_from_bearer_token(token: Optional[str], db: Database) -> Optional[Any]:
    """Decode an access token and resolve it to a live account, or None.

    Shared by the three entry points so the re-read cannot drift between them. The token
    carries the account's table and primary key, so the lookup is a primary-key read in one
    named table -- never a search for the regno, which would be a search for *an* account
    rather than *this* one once a regno can exist in more than one table.

    Nothing about an account's privileges is ever taken from the token. The table decides
    what the caller may do, and it is re-read from the row on every request, so a revoked or
    demoted account is refused on its very next call, and a token minted for one table
    cannot be replayed as another.

    A token without those claims is rejected rather than resolved by regno. Such a token
    was minted before the account's table was recorded in it, and honouring it by searching
    is exactly the ambiguity this design removes; the cost is that sessions issued by an
    older build end at their next request and their users sign in again.
    """
    if token is None:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError as exc:
        logger.error(f"JWT decode error: {exc}")
        return None
    if payload.get("typ") == ASSET_TOKEN_TYPE:
        return None
    account_type = payload.get("account_type")
    account_id = payload.get("account_id")
    if not is_valid_account_type(account_type) or not account_id:
        logger.error("Access token is missing account_id/account_type claims")
        return None
    with db.sessions() as session:
        row = resolve_account(session, account_type, account_id)
    if row is None or row.is_blocked:
        return None
    return account_from_row(normalize_account_type(account_type), row)


def verify_token_service(token: str, db: Database) -> AccountDetails:
    """
    Verify JWT token and return account details.
    Used for WebSocket authentication where Depends() cannot be used.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    account = _account_from_bearer_token(token, db)
    if account is None:
        raise credentials_exception
    return account

def get_current_user(token: str = Depends(oauth2_scheme), db: Database = Depends(get_db)) -> AccountDetails:
    """
    Used to decode the JWT, validate the account, and return its details.

    Returns a ``UserDetails`` for a row in ``users`` and the shared ``AccountDetails`` for a
    row in ``admin`` or ``moderator``, which is what lets a participant route depend on
    ``require_participant`` and be handed a fully-populated participant model.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    account = _account_from_bearer_token(token, db)
    if account is None:
        raise credentials_exception
    return account

def get_current_user_optional(token: Optional[str] = Depends(oauth2_scheme), db: Database = Depends(get_db)) -> Optional[AccountDetails]:
    """
    Used to decode the JWT and return account details if the token is valid,
    but does not raise an error if the token is missing or invalid.
    """
    return _account_from_bearer_token(token, db)


async def generate_magic_link_service(request: Request, db: Database, account_type: str):
    """
    Mint a magic link for an account in one named account table.

    ``account_type`` is the table the person said they were signing in to, which is the only
    thing that makes the request answerable: a regno is unique per table, so the same regno
    can exist as a participant and as an admin at the same time, and the page the person
    chose is what says which of them they are. Searching all three tables here would put
    that decision back to an arbitrary ordering, and the person would be sent a link for an
    account they did not ask for.

    The table is recorded on the token as well, so redemption reads the same one table
    rather than searching for the regno again.
    """
    if not is_valid_account_type(account_type):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown account type")
    category = ACCOUNT_CATEGORY_LABELS[normalize_account_type(account_type)]

    form_data = await request.json()
    regno = form_data.get("regno")
    if not regno:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Registration number is required")

    with db.sessions() as session:
        found = find_account_in_table(session, account_type, regno)
    if not found:
        logger.warning(f"No {category} account found with registration number: {regno}")
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No {category} account with that registration number.",
        )

    resolved_type, account = found

    # Reject blocked accounts before a token is minted, so no magic link is ever
    # generated or logged for an account that could not use it.
    _ensure_not_blocked(account)

    from ..create_token import create_and_store_magic_token
    token = create_and_store_magic_token(db, str(account.id), resolved_type, request)

    magic_link = f"http://localhost:5173/verify-login?token={token}"
    magic_link_logger.info(f"Magic Link for {account.name}: {magic_link}")

    return {"message": "Magic link generated. Check the backend console."}


def verify_magic_link_service(token: str, db: Database, request: Optional[Request] = None):
    """
    Redeem a magic link and return an access token plus the landing path for the account.

    The account is read from the table the token recorded at mint time, so a staff link and
    a participant link take different paths through here without either of them naming the
    other in code.
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
    account_id = str(login_token.account_id)
    account_type = str(login_token.account_type)
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
        # Read from the table recorded on the token rather than searching all three: the
        # token was minted for one specific account in one specific table, and that is the
        # only interpretation of it that is correct.
        account = (
            resolve_account(session, account_type, account_id)
            if login_token_row
            else None
        )
        if not account:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User associated with token not found")
        _ensure_not_blocked(account)
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
        account.last_login_at = now
        account.last_login_ip = consume_ip
        account.last_login_user_agent = consume_user_agent
        account_regno = account.regno

    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    # The account's table and primary key, not just its regno. Every later request resolves
    # the caller from these two claims with a primary-key read, so a regno shared by a
    # participant and a moderator cannot move the session from one account to the other
    # between two requests. ``sub`` is kept alongside as the human-readable identity for
    # logs and for anything that wants to know who a token was for.
    access_token = create_access_token(
        data={
            "sub": account_regno,
            "account_id": str(account_id),
            "account_type": normalize_account_type(account_type),
        },
        expires_delta=access_token_expires
    )

    # Staff land on the moderation area; participants on their dashboard. Decided by the
    # account table, via the capabilities that table grants, so a moderator is not bounced
    # to a dashboard it has no business seeing and a table added later does not have to be
    # remembered here. The client re-derives the same destination from /auth/me once it
    # loads, so this only decides what the browser shows in the moment between the redirect
    # and that first fetch.
    redirect_url = "/admin" if capabilities_for(account_type) else "/dashboard"

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
