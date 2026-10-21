# app/services/auth_service.py

import hashlib
import ipaddress
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import jwt
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import OperationalError
from ..db_models import (ACCOUNT_MODELS, AdminRecord, LoginTokenRecord,
                         WebSocketTicketRecord, find_account_in_table,
                         resolve_account)
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from ..postgres import Database, is_valid_id

from ..config import public_frontend_origin, settings
from ..dependencies import get_db
from ..limits import USER_REGNO_MAX
from ..models import AccountDetails, UserDetails
from ..accounts import (AccountRef, AccountType, Capability, capabilities_for,
                        has_capability, is_valid_account_type,
                        normalize_account_type)
from ..logger import get_console_only_logger, get_logger, log_identity_hash
from . import email_delivery_service
from .login_rate_limit import (SCOPE_ACCOUNT, SCOPE_ACCOUNT_LONG, SCOPE_CLIENT,
                               SCOPE_IP, RateLimitVerdict, evaluate_login_rate_limits,
                               resolve_client_id)
from .storage_service import storage_service

logger = get_logger(__name__)

# Console-only: the magic link is a live bearer credential and OTLP/Loki retains logs.
magic_link_logger = get_console_only_logger(__name__ + ".magic_link")

# One wording for every blocked-account rejection, whichever endpoint refuses it.
ACCOUNT_UNAVAILABLE_DETAIL = "Your account is currently not available. Please contact an administrator."

# One live session per account: older logins move to `superseded`; shared constants keep wording and checks aligned.
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_SUPERSEDED = "superseded"
SESSION_STATUS_REVOKED = "revoked"
SESSION_STATUS_EXPIRED = "expired"

# Uniform 401 so a random token cannot be probed for liveness; only superseded is distinguished.
def credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


class StaleRefreshTokenError(HTTPException):
    """A rotated-away refresh value from a still-live session; do not clear the cookie (another tab may hold the newer value)."""


def _stale_refresh_token_exception() -> HTTPException:
    return StaleRefreshTokenError(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

# Distinguishable from the generic refusal so the client can say "signed in elsewhere".
SESSION_SUPERSEDED_DETAIL = (
    "You have been signed out because a new session was opened on another device."
)
_SESSION_SUPERSEDED_WWW_AUTHENTICATE = 'Bearer error="session_superseded"'

# Uniform 200 for real, unknown and blocked regnos, so sweeping reveals no account state.
LOGIN_LINK_SENT_MESSAGE = (
    "If this registration number is registered, a login link has been generated. "
    "Check the backend console -- and double-check the number for typos."
)


def _uniform_link_response() -> Dict[str, Any]:
    """The uniform sign-in body; ``expires_at`` uses the same formula as a real mint."""
    return {
        "message": LOGIN_LINK_SENT_MESSAGE,
        "expires_at": (
            datetime.now(timezone.utc)
            + timedelta(minutes=settings.LOGIN_TOKEN_EXPIRE_MINUTES)
        ).isoformat(),
    }

_RATE_LIMIT_DETAILS: Dict[str, str] = {
    SCOPE_CLIENT: "Too many login requests from this browser. Please try again in a few minutes.",
    SCOPE_IP: "Too many login requests from this network. Please try again in a few minutes.",
    SCOPE_ACCOUNT: (
        "A login link for this registration number can be requested only once every few "
        "minutes. Please try again shortly."
    ),
    SCOPE_ACCOUNT_LONG: (
        "Too many login requests for this registration number. Please try again later."
    ),
}

# Log-only labels; callers are never told an account's category, so sign-in cannot probe for staff.
ACCOUNT_CATEGORY_LABELS: Dict[str, str] = {
    AccountType.USERS.value: "participant",
    AccountType.ADMIN.value: "admin",
    AccountType.MODERATOR.value: "moderator",
}

# Columns shared by all three account tables; users-only fields come from `_user_payload`.
_ACCOUNT_SERIALIZED_FIELDS: Tuple[str, ...] = (
    "id", "regno", "name", "email",
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
        "bio": row.bio,
        "year": row.year,
        "department": row.department,
        "gender": row.gender,
        # Derived from stored identity: a profile picture is a public CDN object that authorises nothing.
        "profile_picture_url": storage_service.profile_delivery_url(
            row.profile_picture_public_id, row.profile_picture_format
        ),
        "interests": row.interests or [],
        "matchmaking_enabled": row.matchmaking_enabled,
        "love_notes_receive_enabled": row.love_notes_receive_enabled,
        "love_notes_send_enabled": row.love_notes_send_enabled,
        "last_matchmaking_at": row.last_matchmaking_at,
        "last_confession_at": row.last_confession_at,
        "last_profile_picture_at": row.last_profile_picture_at,
    }


def account_from_row(account_type: str, row: Any):
    """Build the model for an account kind from a row; participant rows get UserDetails."""
    if account_type == AccountType.USERS.value:
        return UserDetails(**_user_payload(row))
    return AccountDetails(**_account_payload(row, account_type))


def _ensure_not_blocked(row: Any) -> None:
    if row.is_blocked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ACCOUNT_UNAVAILABLE_DETAIL,
        )


def retire_account_sessions(session, account_id: str, account_type: str) -> None:
    """Revoke live sessions and unredeemed magic links in the caller's transaction; shared by both block paths, while enforcement always re-checks ``is_blocked``."""
    session.execute(
        update(LoginTokenRecord)
        .where(
            LoginTokenRecord.account_id == str(account_id),
            LoginTokenRecord.account_type == account_type,
            LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE,
        )
        .values(session_status=SESSION_STATUS_REVOKED)
    )
    session.execute(
        update(LoginTokenRecord)
        .where(
            LoginTokenRecord.account_id == str(account_id),
            LoginTokenRecord.account_type == account_type,
            LoginTokenRecord.used.is_(False),
            LoginTokenRecord.revoked.is_(False),
        )
        .values(revoked=True)
    )


# HTTPBearer, not OAuth2PasswordBearer: there is no password grant, only the Bearer header.
bearer_scheme = HTTPBearer(auto_error=False)

def _deadline_passed(deadline: Optional[datetime], now: datetime) -> bool:
    """Whether an absolute session deadline is missing or passed; missing counts as passed."""
    if deadline is None:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now

#: Fixed HS256 on both sides: a configurable algorithm is how ``none``/cross-algorithm confusion gets in.
_JWT_ALGORITHM = "HS256"
_JWT_ALGORITHMS = (_JWT_ALGORITHM,)

#: Required claims; a token missing them is refused before any session lookup.
_JWT_REQUIRED_CLAIMS = ("exp", "account_id", "session_id")

#: Hash domains prevent replaying a value across token-type columns.
TOKEN_HASH_DOMAIN_MAGIC = "magic"
TOKEN_HASH_DOMAIN_REFRESH = "refresh"
TOKEN_HASH_DOMAIN_WEBSOCKET = "websocket"

#: Covers a slow handshake while bounding a leaked ticket's value.
WEBSOCKET_TICKET_EXPIRE_SECONDS = 60


def create_access_token(data: dict, expires_delta: timedelta | None = None):
    """Create a signed JWT access token."""
    to_encode = data.copy()
    # Aware UTC so a later edit cannot confuse it with local time.
    now = datetime.now(timezone.utc)
    if expires_delta:
        expire = now + expires_delta
    else:
        expire = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, settings.SECRET_KEY, algorithm=_JWT_ALGORITHM)
    return encoded_jwt

def hash_token(token: str, *, domain: str) -> str:
    """Hash a token for storage, peppered and domain-separated (pepper defeats offline dump search; domain prevents cross-column replay)."""
    return hashlib.sha256(
        f"{domain}:{settings.TOKEN_HASH_PEPPER}:{token}".encode()
    ).hexdigest()

def generate_refresh_token() -> str:
    """A fresh opaque 256-bit refresh token, shown once and stored only hashed."""
    return secrets.token_urlsafe(64)

def _session_advisory_lock_key(account_type: str, account_id: str) -> int:
    """Advisory-lock key that serialises logins for one account.

    Per-account, not per-table: two racing logins must not both see no active session and both go live.
    """
    digest = hashlib.sha256(f"{account_type}:{account_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFFFFFFFFFFFFFF


def _session_superseded_exception() -> HTTPException:
    """The 401 that tells the holder of a replaced session they were signed out elsewhere."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=SESSION_SUPERSEDED_DETAIL,
        headers={"WWW-Authenticate": _SESSION_SUPERSEDED_WWW_AUTHENTICATE},
    )

def _decode_access_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decode and signature-check an access JWT, or None if it is unreadable."""
    if token is None:
        return None
    try:
        return jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=list(_JWT_ALGORITHMS),
            options={"require": list(_JWT_REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        logger.error(f"JWT decode error: {exc}")
        return None


def _account_from_session(
    session,
    *,
    session_id: str,
    account_id: str,
    account_type: str,
    require_active_session: bool = True,
) -> Optional[Any]:
    """Resolve the account behind one login session, re-reading rows; bearer and WebSocket-ticket paths converge here under the same live-session/deadline/blocked rules."""
    session_row = session.get(LoginTokenRecord, session_id)
    if session_row is None:
        logger.error(f"Session {session_id} does not exist")
        return None
    if (
        str(session_row.account_id) != str(account_id)
        or normalize_account_type(session_row.account_type) != account_type
    ):
        logger.error(f"Session {session_id} belongs to a different account")
        return None
    if session_row.session_status == SESSION_STATUS_SUPERSEDED:
        if not require_active_session:
            return None
        logger.warning(
            f"Session superseded: session {session_id} was replaced by session "
            f"{session_row.superseded_by}"
        )
        raise _session_superseded_exception()
    if session_row.session_status != SESSION_STATUS_ACTIVE:
        logger.error(
            f"Session {session_id} is not active "
            f"(status={session_row.session_status!r})"
        )
        return None
    if _deadline_passed(session_row.session_expires_at, datetime.now(timezone.utc)):
        logger.warning(
            f"Session {session_id} has passed its absolute deadline "
            f"({session_row.session_expires_at})"
        )
        return None
    row = resolve_account(session, account_type, account_id)
    if row is None or row.is_blocked:
        return None
    return account_from_row(normalize_account_type(account_type), row)


def _account_from_bearer_token(
    token: Optional[str],
    db: Database,
    *,
    require_active_session: bool = True,
) -> Optional[Any]:
    """Decode an access token and resolve it to a live account, or None; privileges are never taken from the token because the row is re-read every request."""
    payload = _decode_access_token(token)
    if payload is None:
        return None
    account_type = payload.get("account_type")
    account_id = payload.get("account_id")
    session_id = payload.get("session_id")
    if not is_valid_account_type(account_type) or not account_id:
        logger.error("Access token is missing account_id/account_type claims")
        return None
    if not session_id or not is_valid_id(session_id):
        logger.error("Access token is missing session_id claim")
        return None
    with db.sessions() as session:
        return _account_from_session(
            session,
            session_id=str(session_id),
            account_id=str(account_id),
            account_type=account_type,
            require_active_session=require_active_session,
        )


def access_token_session_id(token: Optional[str]) -> Optional[str]:
    """The session id to bind a WebSocket ticket to, from an already-validated token."""
    payload = _decode_access_token(token)
    if payload is None:
        return None
    session_id = payload.get("session_id")
    if not session_id or not is_valid_id(session_id):
        return None
    return str(session_id)


def create_websocket_ticket(db: Database, account: Any, session_id: str) -> Dict[str, Any]:
    """Mint one short-lived, single-use WebSocket ticket; only its hash is stored."""
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=WEBSOCKET_TICKET_EXPIRE_SECONDS)
    raw_ticket = secrets.token_urlsafe(32)
    raw_account_type = getattr(account, "account_type", None)
    if not is_valid_account_type(raw_account_type) or not is_valid_id(session_id):
        raise ValueError("A WebSocket ticket needs a valid account and session")
    account_type = normalize_account_type(raw_account_type)

    with db.sessions.begin() as session:
        session.execute(
            delete(WebSocketTicketRecord).where(WebSocketTicketRecord.expires_at < now)
        )
        session.add(
            WebSocketTicketRecord(
                ticket_hash=hash_token(raw_ticket, domain=TOKEN_HASH_DOMAIN_WEBSOCKET),
                account_id=str(account.id),
                account_type=account_type,
                session_id=session_id,
                issued_at=now,
                expires_at=expires_at,
            )
        )
    return {
        "ticket": raw_ticket,
        "expires_at": expires_at.isoformat(),
        "expires_in": WEBSOCKET_TICKET_EXPIRE_SECONDS,
    }


def consume_websocket_ticket(
    db: Database, raw_ticket: Optional[str]
) -> Optional[Tuple[Any, str]]:
    """Burn a ticket and return ``(account, session_id)``, or None if unusable.

    The conditional UPDATE is the single-use gate; session state is re-read in the same transaction.
    """
    if not raw_ticket:
        return None
    now = datetime.now(timezone.utc)
    hashed = hash_token(raw_ticket, domain=TOKEN_HASH_DOMAIN_WEBSOCKET)
    with db.sessions.begin() as session:
        consumed = session.execute(
            update(WebSocketTicketRecord)
            .where(
                WebSocketTicketRecord.ticket_hash == hashed,
                WebSocketTicketRecord.consumed_at.is_(None),
                WebSocketTicketRecord.expires_at > now,
            )
            .values(consumed_at=now)
            .returning(
                WebSocketTicketRecord.account_id,
                WebSocketTicketRecord.account_type,
                WebSocketTicketRecord.session_id,
            )
        ).first()
        if consumed is None:
            return None
        account_id, account_type, session_id = consumed
        try:
            account = _account_from_session(
                session,
                session_id=str(session_id),
                account_id=str(account_id),
                account_type=normalize_account_type(account_type),
                require_active_session=True,
            )
        except HTTPException:
            account = None
        if account is None:
            return None
        return account, str(session_id)


def revalidate_websocket_session(
    db: Database,
    *,
    session_id: str,
    account_id: str,
    account_type: str,
) -> Optional[Any]:
    """Re-resolve a live socket's session per frame; any refusal becomes None so the socket closes."""
    try:
        with db.sessions() as session:
            return _account_from_session(
                session,
                session_id=session_id,
                account_id=account_id,
                account_type=account_type,
                require_active_session=True,
            )
    except HTTPException:
        return None


def verify_token_service(token: str, db: Database) -> AccountDetails:
    """Verify a JWT and return account details; kept for non-WebSocket callers and tests."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    account = _account_from_bearer_token(token, db)
    if account is None:
        raise credentials_exception
    return account


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Database = Depends(get_db),
) -> AccountDetails:
    """Decode the JWT and validate the account; participant rows become ``UserDetails``, staff the shared ``AccountDetails``."""
    token = credentials.credentials if credentials is not None else None
    account = _account_from_bearer_token(token, db)
    if account is None:
        raise credentials_exception()
    return account

def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Database = Depends(get_db),
) -> Optional[AccountDetails]:
    """Like ``get_current_user`` but returns None for a missing or invalid token."""
    token = credentials.credentials if credentials is not None else None
    # A superseded session counts as anonymous here, not a 401: a replaced token has no more claim than none.
    return _account_from_bearer_token(token, db, require_active_session=False)


# Log-safe stand-ins: these logs ship to retained OTLP/Loki, so raw identifiers must never appear.
def _log_identity_hash(value: str) -> str:
    """Shared log identifier hash; alias kept because auth tests import this name."""
    return log_identity_hash(value)


def _log_ip_network(ip: Any) -> str:
    """The caller's subnet for logs: /24 IPv4, /64 IPv6; unparseable logs as ``unparseable``."""
    try:
        parsed = ipaddress.ip_address(str(ip).strip())
    except ValueError:
        return "unparseable"
    width = 24 if isinstance(parsed, ipaddress.IPv4Address) else 64
    return str(ipaddress.ip_network(f"{parsed}/{width}", strict=False))


def _rate_limit_rejection(    verdict: RateLimitVerdict,
    *,
    account_type: str,
    regno: str,
    client_id: str,
    ip: str,
) -> HTTPException:
    """Log which limiter refused and build the 429; only hashed/truncated keys reach retained logs."""
    if verdict.scope == SCOPE_CLIENT:
        logger.warning(
            f"Client rate limit triggered: "
            f"client_hash={_log_identity_hash(client_id)} "
            f"ip_net={_log_ip_network(ip)} "
            f"retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_IP:
        logger.warning(
            f"IP rate limit triggered: ip_net={_log_ip_network(ip)} "
            f"client_hash={_log_identity_hash(client_id)} "
            f"retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_ACCOUNT:
        logger.warning(
            f"Account cooldown triggered: account_type={normalize_account_type(account_type)} "
            f"regno_hash={_log_identity_hash(regno)} retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_ACCOUNT_LONG:
        logger.warning(
            f"Account rate limit triggered (long window): "
            f"account_type={normalize_account_type(account_type)} "
            f"regno_hash={_log_identity_hash(regno)} retry_after={verdict.retry_after}s"
        )

    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_RATE_LIMIT_DETAILS.get(
            verdict.scope or "", "Too many login requests. Please try again later."
        ),
        headers={"Retry-After": str(verdict.retry_after)},
    )


# Regno shape, enforced before any lookup, rate-limit key or log line; matches USER_REGNO_MAX.
_REGNO_PATTERN = re.compile(rf"^[\w./-]{{1,{USER_REGNO_MAX}}}$")

# Conservative shape check before a legacy admin row is used as a contact address.
_ADMIN_EMAIL_PATTERN = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _validated_regno(value: Any) -> str:
    """Trim and validate the submitted regno or 400; runs before rate limits so invalid input costs no budget."""
    if not isinstance(value, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Registration number is required.",
        )
    candidate = value.strip()
    if not candidate or not _REGNO_PATTERN.fullmatch(candidate):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Registration number must be 1 to 100 characters long and may contain "
                "only letters, numbers and the punctuation . / - _."
            ),
        )
    return candidate


def _random_admin_contact_email(db: Database) -> Optional[str]:
    """One random administrator address for a blocked-account appeal; failures return None without stopping the notice."""
    try:
        with db.sessions() as session:
            emails = session.scalars(
                select(AdminRecord.email).where(
                    AdminRecord.is_blocked.is_not(True)
                )
            ).all()
    except Exception as exc:
        logger.warning(
            "Blocked-account email could not select an administrator contact "
            "(error_type=%s)",
            type(exc).__name__,
        )
        return None

    candidates = []
    for email in emails:
        text = str(email).strip() if email is not None else ""
        if text and _ADMIN_EMAIL_PATTERN.fullmatch(text):
            candidates.append(text)
    return secrets.choice(candidates) if candidates else None


async def generate_magic_link_service(
    request: Request,
    db: Database,
    account_type: str,
    client_id: Optional[str] = None,
):
    """
    Mint a magic link for an account in one named account table.

    ``account_type`` selects the table (a regno is unique only within one) and is recorded on the token. The answer is uniform for real, unknown or blocked regnos; rate limits run before the lookup, so 429s reveal no account state. ``client_id`` comes from the router when given, else resolved here.
    """
    if not is_valid_account_type(account_type):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown account type")
    category = ACCOUNT_CATEGORY_LABELS[normalize_account_type(account_type)]

    try:
        form_data = await request.json()
    except ValueError:  # a malformed body carries no fields to gate; refuse it cleanly
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The request body must be valid JSON.",
        ) from None
    if not isinstance(form_data, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The request body must be a JSON object.",
        )
    regno = _validated_regno(form_data.get("regno"))

    if client_id is None:
        client_id, _ = resolve_client_id(request)
    client = getattr(request, "client", None)
    ip = client.host if client is not None else "unknown"

    verdict = evaluate_login_rate_limits(
        db,
        account_type=account_type,
        regno=regno,
        client_id=client_id,
        ip=ip,
    )
    if not verdict.allowed:
        raise _rate_limit_rejection(
            verdict, account_type=account_type, regno=regno, client_id=client_id, ip=ip
        )

    with db.sessions() as session:
        found = find_account_in_table(session, account_type, regno)
    if not found:
        # Answered, not refused: same body as a mint, no token stored, hashed tags only in logs.
        logger.info(
            f"Login request for unknown {category} registration number "
            f"(regno_hash={_log_identity_hash(regno)}): answering as if a link was sent"
        )
        return _uniform_link_response()

    resolved_type, account = found

    # Blocked accounts mint nothing (redemption also re-checks) and answer like an unknown number, so state is not learnable.
    if account.is_blocked:
        logger.warning(
            f"Login request for blocked {category} account "
            f"(regno_hash={_log_identity_hash(regno)}): answering as if a link was sent"
        )
        # The notice is a sign-in outcome: ignores notifications_enabled, and the response stays uniform.
        await email_delivery_service.enqueue_account_blocked_email(
            email=account.email,
            name=account.name,
            regno=account.regno,
            admin_email=_random_admin_contact_email(db),
        )
        return _uniform_link_response()

    from ..create_token import create_and_store_magic_token
    token, expires_at = create_and_store_magic_token(db, str(account.id), resolved_type, request)

    magic_link = f"{public_frontend_origin()}/verify-login?token={token}"
    magic_link_logger.info(f"Magic Link for {account.name}: {magic_link}")
    logger.info(
        f"Login request accepted: magic link minted for {category} account "
        f"(regno_hash={_log_identity_hash(regno)})"
    )

    # Console copy is the dev fallback; a queueing failure is logged without changing the response.
    await email_delivery_service.enqueue_login_email(
        link=magic_link,
        email=account.email,
        name=account.name,
        regno=account.regno,
    )

    return {"message": LOGIN_LINK_SENT_MESSAGE, "expires_at": expires_at.isoformat()}


def _normalize_client_ip(value: Any) -> Optional[Any]:
    """Parse an address for comparison, or None; IPv4-mapped IPv6 unwraps to IPv4."""
    try:
        parsed = ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _redemption_continuity(
    *,
    minted_ip: Any,
    minted_user_agent: Optional[str],
    remote_ip: Any,
    remote_user_agent: str,
) -> bool:
    """Whether a redemption plausibly continues the session its mint started.

    Same /24 (IPv4) or /64 (IPv6) subnet or User-Agent; the token remains the actual authentication.
    """
    minted = _normalize_client_ip(minted_ip)
    remote = _normalize_client_ip(remote_ip)
    if minted is not None and remote is not None and type(minted) is type(remote):
        width = 24 if isinstance(minted, ipaddress.IPv4Address) else 64
        if remote in ipaddress.ip_network(f"{minted}/{width}", strict=False):
            return True
    if minted_user_agent and minted_user_agent == remote_user_agent:
        return True
    return False


def verify_magic_link_service(token: str, db: Database, request: Optional[Request] = None):
    """Redeem a magic link and return an access token plus the landing path; the account is read from the table the token recorded."""
    if not token:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Token is required")

    hashed_token = hash_token(token, domain=TOKEN_HASH_DOMAIN_MAGIC)

    now = datetime.now(timezone.utc)

    if request is None or request.client is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Request context is required")

    # Read-only classification; the conditional UPDATE below is the single-use
    # linearisation point, and every failure answers the same body (no timing oracle).
    with db.sessions() as session:
        login_token = session.scalar(select(LoginTokenRecord).where(
            LoginTokenRecord.token_hash == hashed_token,
        ))

    # Lookup by hash alone; one client message for every failure, the log records which.
    if login_token is None:
        # DEBUG hint is only 6 chars so retained logs cannot narrow an offline search.
        logger.debug(
            f"Invalid magic link token: no matching token "
            f"(hash_hint={hashed_token[:6]})"
        )
        logger.warning("Invalid magic link token: no matching token")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")
    if login_token.revoked:
        logger.warning(f"Invalid magic link token: token {login_token.id} is revoked")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")
    if login_token.used:
        logger.warning(
            f"Already-used magic link token: token {login_token.id} was consumed at "
            f"{login_token.consumed_at}"
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    expires_at = login_token.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        logger.warning(
            f"Expired magic link token: token {login_token.id} expired at {expires_at}"
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    login_token_id = str(login_token.id)
    account_id = str(login_token.account_id)
    account_type = str(login_token.account_type)
    request_user_agent = login_token.request_user_agent

    # Redemption is expected from the same neighbourhood (NAT/campus egress): same /24,
    # /64 or User-Agent is enough; mismatches are counted (bounded) and refused uniformly.
    if not _redemption_continuity(
        minted_ip=login_token.request_ip,
        minted_user_agent=request_user_agent,
        remote_ip=request.client.host,
        remote_user_agent=request.headers.get("user-agent", "unknown"),
    ):
        logger.warning(
            f"Magic link token {login_token_id} redeemed from an unrelated network "
            f"(minted at {_log_ip_network(login_token.request_ip)}, "
            f"redeemed from {_log_ip_network(request.client.host)})"
        )
        with db.sessions.begin() as session:
            session.execute(
                update(LoginTokenRecord)
                .where(LoginTokenRecord.id == login_token_id)
                # Bound the operand, not the sum: LEAST(x, MAX-1) + 1 saturates; LEAST(x+1, MAX) can overflow.
                .values(attempt_count=func.least(LoginTokenRecord.attempt_count, 2147483646) + 1)
            )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    consume_ip = request.client.host
    consume_user_agent = request.headers.get("user-agent", "unknown")

    # Store only the refresh token's hash; ``session_expires_at`` is an absolute login deadline, never extended.
    refresh_token = generate_refresh_token()
    session_expires_at = now + timedelta(hours=settings.LOGIN_SESSION_EXPIRE_HOURS)

    with db.sessions.begin() as session:
        login_token_row = session.get(LoginTokenRecord, login_token_id)
        # Read the table recorded on the token - minted for one account in one table.
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
                # Bounded like the refusal counter so no increment path can overflow.
                attempt_count=func.least(LoginTokenRecord.attempt_count, 2147483646) + 1,
                session_status="active",
                last_seen_at=now,
                device=consume_user_agent or request_user_agent or "unknown",
                session_expires_at=session_expires_at,
                refresh_token_hash=hash_token(refresh_token, domain=TOKEN_HASH_DOMAIN_REFRESH),
            )
        )
        if result.rowcount != 1:
            # Conditional UPDATE makes single-use true under concurrency (PostgreSQL re-checks the
            # predicate under the row lock); the loser re-reads to log which refusal happened.
            fresh = session.get(LoginTokenRecord, login_token_id)
            if fresh is None or fresh.revoked:
                logger.warning(f"Invalid magic link token during redemption: token {login_token_id}")
            elif fresh.used:
                logger.warning(
                    f"Already-used magic link token during redemption: token {login_token_id} "
                    f"was consumed at {fresh.consumed_at}"
                )
            else:
                logger.warning(
                    f"Expired magic link token during redemption: token {login_token_id} "
                    f"expired at {fresh.expires_at}"
                )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

        # One live session per account: older actives are superseded, recorded with their successor.
        # Runs under a per-account advisory lock so two racing logins cannot both go live.
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _session_advisory_lock_key(account_type, account_id)},
        )
        superseded_count = session.execute(
            update(LoginTokenRecord)
            .where(
                LoginTokenRecord.account_id == account_id,
                LoginTokenRecord.account_type == account_type,
                LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE,
                LoginTokenRecord.id != login_token_id,
            )
            .values(
                session_status=SESSION_STATUS_SUPERSEDED,
                superseded_at=now,
                superseded_by=login_token_id,
            )
        ).rowcount
        if superseded_count:
            logger.info(
                f"Magic link verification superseded {superseded_count} older active session(s): "
                f"account={account_id} account_type={normalize_account_type(account_type)} "
                f"new_session={login_token_id}"
            )

        account.last_login_at = now
        account.last_login_ip = consume_ip
        account.last_login_user_agent = consume_user_agent
        account_regno = account.regno

    logger.info(
        f"Magic link token verification success: token={login_token_id} "
        f"account={account_id} account_type={normalize_account_type(account_type)}"
    )


    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    # Table plus primary key (never regno alone), with `session_id` tying the JWT to the
    # session row re-read per request: a superseded session's JWT is refused even unexpired.
    access_token = create_access_token(
        data={
            "sub": account_regno,
            "account_id": str(account_id),
            "account_type": normalize_account_type(account_type),
            "session_id": login_token_id,
        },
        expires_delta=access_token_expires
    )

    # Landing path by capability; the client re-derives from /auth/me, so this only covers the redirect gap.
    if has_capability(account_type, Capability.STATS_READ):
        redirect_url = "/admin"
    elif capabilities_for(account_type):
        redirect_url = "/moderation"
    else:
        redirect_url = "/dashboard"

    # Carried out to the router for the HttpOnly cookie; the raw value never reaches the response body.
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "redirect_url": redirect_url,
        "refresh_token": refresh_token,
    }


# At most one replay notice per session per hour: racing tabs stay quiet, repeats still surface.
_REFRESH_REPLAY_NOTICE_TTL_SECONDS = 3600


# Bounded lock wait: a stuck transaction must not queue every later refresh until the pool exhausts.
_AUTH_ROW_LOCK_TIMEOUT = "5s"


def _bound_auth_row_lock_wait(session) -> None:
    """Cap this transaction's wait for a ``login_tokens`` row lock (``SET LOCAL``)."""
    session.execute(text(f"SET LOCAL lock_timeout = '{_AUTH_ROW_LOCK_TIMEOUT}'"))


def _is_lock_timeout(error: BaseException) -> bool:
    """Whether a SQLAlchemy error is PostgreSQL's ``lock_not_available`` (55P03)."""
    original = getattr(error, "orig", error)
    return (
        getattr(original, "sqlstate", None) == "55P03"
        or getattr(original, "pgcode", None) == "55P03"
    )


def _record_refresh_replay(db: Database, session_id: str, holder_regno: Optional[str]) -> None:
    """Count a proven refresh replay and, sparingly, notify the holder (SEC-008).

    Proven means the value matches ``refresh_token_replaced_hash`` (a rotated-away value). Best-effort and never raises. Runs after the caller's transaction closes -- opening it earlier deadlocks on the rotation's tuple lock; the hourly Redis gate fails open.
    """
    try:
        with db.sessions.begin() as session:
            _bound_auth_row_lock_wait(session)
            session.execute(
                update(LoginTokenRecord)
                .where(LoginTokenRecord.id == session_id)
                .values(attempt_count=func.least(LoginTokenRecord.attempt_count, 2147483646) + 1)
            )
    except Exception as exc:
        logger.warning(f"Refresh replay count failed for session {session_id}: {exc}")
    if not holder_regno:
        return
    try:
        from .redis_service import redis_service

        first = redis_service.client.set(
            f"replay-notify:{session_id}", "1",
            ex=_REFRESH_REPLAY_NOTICE_TTL_SECONDS, nx=True,
        )
    except Exception as exc:
        logger.debug(f"Replay notice gate unavailable for session {session_id}: {exc}")
        return
    if not first:
        return
    try:
        from .notification_service import create_notification_service

        create_notification_service(
            recipient_regno=holder_regno,
            heading="An old session credential was used",
            body=(
                "Someone -- possibly you on another tab or device -- tried to refresh "
                "this session with an outdated credential, so nothing changed. If that "
                "was you, no action is needed. If it was not, request a fresh magic link "
                "to replace this session."
            ),
            db=db,
        )
    except Exception as exc:
        logger.warning(f"Replay notice failed for session {session_id}: {exc}")


def rotate_refresh_token_service(raw_token: str, db: Database) -> Dict[str, str]:
    """Rotate a refresh token and mint a fresh access token for its session.

    One conditional UPDATE keyed on the incoming hash makes rotation single-use. A rotated-away value is refused and logged as replay, but the session is *not* revoked: the old value can never succeed again and killing the live token would punish a benign racing tab. Replay bookkeeping runs after the transaction closes; authority is re-read so revocation or a block applies now.
    """
    if not raw_token:
        raise credentials_exception()

    hashed = hash_token(raw_token, domain=TOKEN_HASH_DOMAIN_REFRESH)
    now = datetime.now(timezone.utc)

    # Set inside the rowcount branch and acted on after the transaction has closed.
    replay_proven = False
    replay_holder: Optional[str] = None
    refusal: Optional[HTTPException] = None

    try:
        with db.sessions.begin() as session:
            _bound_auth_row_lock_wait(session)
            row = session.scalar(
                select(LoginTokenRecord).where(LoginTokenRecord.refresh_token_hash == hashed)
            )
            if row is None:
                # A stale copy of the session's previous value (a racing tab can still hold it),
                # not an unknown value: its browser may hold the newer live value, so keep the cookie.
                rotated_away_from = session.scalar(
                    select(LoginTokenRecord.id)
                    .where(LoginTokenRecord.refresh_token_replaced_hash == hashed)
                    .limit(1)
                )
                if rotated_away_from is not None:
                    logger.warning(
                        f"Refresh rejected: the presented token was rotated away from "
                        f"session {rotated_away_from}"
                    )
                    raise _stale_refresh_token_exception()
                logger.debug(
                    f"Refresh rejected: no session holds refresh token "
                    f"(hash_hint={hashed[:6]})"
                )
                logger.warning("Refresh rejected: no session holds refresh token")
                raise credentials_exception()

            session_id = str(row.id)
            account_type = str(row.account_type)
            account_id = str(row.account_id)

            if row.session_status == SESSION_STATUS_SUPERSEDED:
                logger.warning(
                    f"Refresh rejected: session {session_id} was superseded by "
                    f"session {row.superseded_by}"
                )
                raise _session_superseded_exception()
            if row.session_status != SESSION_STATUS_ACTIVE:
                logger.warning(
                    f"Refresh rejected: session {session_id} is not active "
                    f"(status={row.session_status!r})"
                )
                raise credentials_exception()

            if _deadline_passed(row.session_expires_at, now):
                # Hard ceiling reached; the admin sweep marks expiry past the window, this refusal is enforcement.
                logger.warning(
                    f"Refresh rejected: session {session_id} passed its absolute deadline "
                    f"({row.session_expires_at})"
                )
                raise credentials_exception()

            account = resolve_account(session, account_type, account_id)
            if account is None:
                logger.warning(
                    f"Refresh rejected: session {session_id} account {account_id} does not exist"
                )
                raise credentials_exception()
            if account.is_blocked:
                logger.warning(
                    f"Refresh rejected: session {session_id} account {account_id} is blocked"
                )
                raise credentials_exception()

            new_raw = generate_refresh_token()
            result = session.execute(
                update(LoginTokenRecord)
                .where(
                    LoginTokenRecord.id == session_id,
                    LoginTokenRecord.refresh_token_hash == hashed,
                )
                .values(
                    refresh_token_hash=hash_token(new_raw, domain=TOKEN_HASH_DOMAIN_REFRESH),
                    refresh_token_replaced_hash=hashed,
                    last_seen_at=now,
                )
            )
            if result.rowcount != 1:
                # Lost a race or the session changed underneath; re-read to say which. The refusal
                # is raised after this transaction closes so replay bookkeeping gets its own connection.
                fresh = session.get(LoginTokenRecord, session_id)
                if fresh is not None and fresh.session_status == SESSION_STATUS_SUPERSEDED:
                    logger.warning(
                        f"Refresh raced with a login: session {session_id} superseded by "
                        f"session {fresh.superseded_by}"
                    )
                    refusal = _session_superseded_exception()
                elif fresh is not None and fresh.refresh_token_replaced_hash == hashed:
                    logger.debug(
                        f"Refresh token replayed: session {session_id} already rotated away "
                        f"this value (hash_hint={hashed[:6]})"
                    )
                    logger.warning(
                        f"Refresh token replayed: session {session_id} already rotated away "
                        f"this value"
                    )
                    # Proven second use of a consumed value (SEC-008): count it and notify at most hourly.
                    # The 401 is unchanged; killing the live session would punish a benign double-submit.
                    replay_proven = True
                    replay_holder = getattr(account, "regno", None)
                    # Stale, not dead: a racing rotation replaced this value; do not clear the cookie.
                    refusal = _stale_refresh_token_exception()
                else:
                    logger.warning(f"Refresh race lost for session {session_id}")
                    refusal = credentials_exception()
    except OperationalError as exc:
        if _is_lock_timeout(exc):
            logger.warning("Refresh rejected: timed out waiting for the session row lock")
            raise credentials_exception() from exc
        raise

    # Only after the rotation's transaction ends: its UPDATE holds a tuple lock until commit.
    if replay_proven:
        _record_refresh_replay(db, session_id, replay_holder)
    if refusal is not None:
        raise refusal

    new_access_token = create_access_token(
        data={
            "sub": account.regno,
            "account_id": str(account_id),
            "account_type": normalize_account_type(account_type),
            "session_id": session_id,
        },
    )
    return {
        "access_token": new_access_token,
        "token_type": "bearer",
        "refresh_token": new_raw,
    }


def revoke_refresh_token_service(raw_token: str, db: Database) -> bool:
    """Revoke the session holding a refresh token; returns whether anything was revoked.

    Logout is idempotent and silent about whether the token existed, so a caller cannot probe for live tokens. Only an *active* session is marked revoked; a value rotated away one refresh ago still names its session via ``refresh_token_replaced_hash``, keeping logout reliable when it races a refresh.
    """
    if not raw_token:
        return False
    hashed = hash_token(raw_token, domain=TOKEN_HASH_DOMAIN_REFRESH)
    with db.sessions.begin() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(LoginTokenRecord.refresh_token_hash == hashed)
        )
        if row is None:
            # Rotated away: the session that replaced this value is the one to end.
            row = session.scalar(
                select(LoginTokenRecord).where(
                    LoginTokenRecord.refresh_token_replaced_hash == hashed
                )
            )
        if row is None:
            return False
        if row.session_status != SESSION_STATUS_ACTIVE:
            return False
        session.execute(
            update(LoginTokenRecord)
            .where(
                LoginTokenRecord.id == row.id,
                LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE,
            )
            .values(session_status=SESSION_STATUS_REVOKED)
        )
        logger.info(
            f"Logout revoked session {row.id} for account {row.account_id}"
        )
        return True
