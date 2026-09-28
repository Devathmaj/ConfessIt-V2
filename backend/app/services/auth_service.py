# app/services/auth_service.py

import hashlib
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import jwt
from sqlalchemy import select, text, update
from ..db_models import (ACCOUNT_MODELS, LoginTokenRecord, find_account_in_table,
                         resolve_account)
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from ..postgres import Database, is_valid_id

from ..config import settings
from ..dependencies import get_db
from ..limits import USER_REGNO_MAX
from ..models import AccountDetails, UserDetails
from ..accounts import (AccountRef, AccountType, Capability, capabilities_for,
                        has_capability, is_valid_account_type,
                        normalize_account_type)
from ..logger import get_console_only_logger, get_logger
from .login_rate_limit import (SCOPE_ACCOUNT, SCOPE_ACCOUNT_LONG, SCOPE_CLIENT,
                               SCOPE_IP, RateLimitVerdict, evaluate_login_rate_limits,
                               resolve_client_id)
from .storage_service import storage_service

logger = get_logger(__name__)

# Deliberately separate from the module logger above. The magic link is a live bearer
# credential, so it goes to stdout only: `docker compose logs backend` shows it while
# debugging, but the OTLP handler is never attached, so it is not exported to the
# collector and never lands in Loki (or any log store with a retention period).
magic_link_logger = get_console_only_logger(__name__ + ".magic_link")

# Single wording for every blocked-account rejection so the frontend can show
# one message regardless of which endpoint refused the request.
ACCOUNT_UNAVAILABLE_DETAIL = "Your account is currently not available. Please contact an administrator."

# The one live-session-per-account rule: exactly one of an account's sessions is `active`,
# and every older login is `superseded` by the newest one. Read here so the wording of the
# response, the comparison in the token check, and the state written at login cannot drift
# apart.
SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_SUPERSEDED = "superseded"
SESSION_STATUS_REVOKED = "revoked"
SESSION_STATUS_EXPIRED = "expired"

# The one body a refused credential gets back. Every refresh/logout failure -- a token
# that never existed, one that was rotated away, a session past its absolute deadline, an
# account that was blocked or deleted -- answers identically, so nothing about the
# response tells a caller whether a random refresh token belongs to a live account. Only
# the superseded case is told apart, for the same reason an access token is: the person's
# session is genuinely over and the frontend needs the "signed in elsewhere" wording.
def credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

# What the superseded holder is told on their very next request, and what the frontend
# surfaces as the reason they were signed out. Kept distinguishable from the generic
# credentials wording so the client can show "signed in elsewhere" rather than "try again".
# The `WWW-Authenticate` error is the machine-readable half, the detail the human one.
SESSION_SUPERSEDED_DETAIL = (
    "You have been signed out because a new session was opened on another device."
)
_SESSION_SUPERSEDED_WWW_AUTHENTICATE = 'Bearer error="session_superseded"'

# The sign-in form answers uniformly. Every answered request -- a real, unblocked
# account, a registration number with no account, a blocked account -- gets the same
# 200 and the same body shape, so sweeping registration numbers across the three doors
# learns nothing: not existence, not blocked state, not which table holds the number.
# A link is minted (and the mint logged) only for a real, unblocked account; the other
# outcomes mint nothing and answer a synthetic deadline computed by the same formula
# as a real mint, so even the body is indistinguishable. None of these answers echoes
# the submitted registration number or any account field -- the only thing a caller
# can observe is timing noise. A request the rate limiter refuses gets its own 429
# instead of any of them, and a request the validation gate refuses gets a 400;
# neither of those varies with account state either.
LOGIN_LINK_SENT_MESSAGE = (
    "If this registration number is registered, a login link has been generated. "
    "Check the backend console -- and double-check the number for typos."
)


def _uniform_link_response() -> Dict[str, Any]:
    """The body every answered sign-in request gets, eligible or not.

    `expires_at` is synthesized by the same `now + LOGIN_TOKEN_EXPIRE_MINUTES` formula
    `create_and_store_magic_token` stores on a real row, so a refused caller sees the
    same keys, the same message and a same-shaped deadline as a minted one. The sign-in
    page counts this down exactly as it would a real mint.
    """
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

# What each sign-in page calls the kind of account it is for. Used only in
# server-side log lines: the caller is never told which category a regno belongs to --
# or whether it belongs to one at all -- so the sign-in form cannot be used as a probe
# for whether a given staff account exists.
ACCOUNT_CATEGORY_LABELS: Dict[str, str] = {
    AccountType.USERS.value: "participant",
    AccountType.ADMIN.value: "admin",
    AccountType.MODERATOR.value: "moderator",
}

# The columns every account table shares. Read generically so one serialiser serves all
# three, and so a field added to the mixin cannot be added to only one of the tables.
#
# ``gender`` and the two profile-picture columns are deliberately absent from this list, for
# the same reason the participant fields below are: they exist on ``users`` only, so a staff row
# has nothing to read and ``AccountDetails`` does not declare them. They are added by
# ``_user_payload``, alongside the rest of the participant shape.
#
# What an API response carries for a picture is an address the browser can fetch -- not the
# internal identity of the object behind it -- so that derivation lives with the participant
# payload too.
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
        # A profile picture is a public object on Cloudinary's CDN, so the address is derived
        # from the stored identity rather than stored itself. Nothing here authorises anything:
        # the same URL goes to every account that can see the profile, which is why it is cheap
        # to build and why building it must not be confused with a permission check.
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


# ``HTTPBearer`` rather than ``OAuth2PasswordBearer``: there is no password grant and no
# ``/auth/token`` endpoint (the tokenUrl in the old scheme's constructor advertised one
# that does not exist). The bearer scheme extracts the ``Authorization: Bearer`` header,
# which is exactly the shape every authenticated endpoint already uses.
bearer_scheme = HTTPBearer(auto_error=False)

def _deadline_passed(deadline: Optional[datetime], now: datetime) -> bool:
    """Whether an absolute session deadline is missing or already behind us.

    A missing deadline is treated as passed. The boot path backfills a deadline for every
    *redeemed* session, so a NULL here means a row that either never became a session or
    belongs to a build before the deadline existed -- and a session without a ceiling is
    precisely the unbounded-renewal case the deadline exists to forbid.
    """
    if deadline is None:
        return True
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return deadline <= now

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

def generate_refresh_token() -> str:
    """A fresh opaque refresh token.

    The raw value is secrets.token_urlsafe(64) -- 256 bits of entropy, so guessing one is
    not a threat worth defending against. It is shown exactly once (the router sets it as
    an HttpOnly cookie on the way through) and stored only as ``hash_token``.
    """
    return secrets.token_urlsafe(64)

def _session_advisory_lock_key(account_type: str, account_id: str) -> int:
    """The advisory-lock key that serialises logins for *one* account.

    ``account_cap`` serialises per account *table* with a fixed key; one-live-session needs
    the same serialisation per account, because two logins racing for the same account would
    otherwise both observe "no other active session" from the same snapshot and both go
    active. The key is derived from the two identity facts a login already has (the table and
    the row), masked to the positive bigint range advisory locks accept, so two logins for
    different accounts never block each other.
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

def _account_from_bearer_token(
    token: Optional[str],
    db: Database,
    *,
    require_active_session: bool = True,
) -> Optional[Any]:
    """Decode an access token and resolve it to a live account, or None.

    Shared by the three entry points so the re-read cannot drift between them. The token
    carries the account's table and primary key, so the lookup is a primary-key read in one
    named table -- never a search for the regno, which would be a search for *an* account
    rather than *this* one once a regno can exist in more than one table.

    Nothing about an account's privileges is ever taken from the token. The table decides
    what the caller may do, and it is re-read from the row on every request, so a revoked or
    demoted account is refused on its very next call, and a token minted for one table
    cannot be replayed as another.

    The token also carries the id of the login-token row that became the session. That row
    is re-read on every request too, which is what enforces one live session per account:
    a session replaced by a newer login is ``superseded`` in the database, and its token is
    refused here even though the JWT itself is still valid and unexpired. When
    ``require_active_session`` is set (the mandatory dependencies), a superseded session is
    refused with the dedicated 401 so its holder can be told they were signed in elsewhere;
    the optional dependency treats it as no account at all, like an absent token.

    A token without those claims is rejected rather than resolved by regno. Such a token
    was minted before the account's table -- or the session it belongs to -- was recorded in
    it, and honouring it by searching is exactly the ambiguity this design removes; the cost
    is that sessions issued by an older build end at their next request and their users sign
    in again.
    """
    if token is None:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError as exc:
        logger.error(f"JWT decode error: {exc}")
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
        session_row = session.get(LoginTokenRecord, session_id)
        if session_row is None:
            logger.error(f"Access token session {session_id} does not exist")
            return None
        if (
            str(session_row.account_id) != str(account_id)
            or normalize_account_type(session_row.account_type) != account_type
        ):
            logger.error(f"Access token session {session_id} belongs to a different account")
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
                f"Access token session {session_id} is not active "
                f"(status={session_row.session_status!r})"
            )
            return None
        if _deadline_passed(session_row.session_expires_at, datetime.now(timezone.utc)):
            logger.warning(
                f"Access token session {session_id} has passed its absolute deadline "
                f"({session_row.session_expires_at})"
            )
            return None
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

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Database = Depends(get_db),
) -> AccountDetails:
    """
    Used to decode the JWT, validate the account, and return its details.

    Returns a ``UserDetails`` for a row in ``users`` and the shared ``AccountDetails`` for a
    row in ``admin`` or ``moderator``, which is what lets a participant route depend on
    ``require_participant`` and be handed a fully-populated participant model.
    """
    token = credentials.credentials if credentials is not None else None
    account = _account_from_bearer_token(token, db)
    if account is None:
        raise credentials_exception()
    return account

def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Database = Depends(get_db),
) -> Optional[AccountDetails]:
    """
    Used to decode the JWT and return account details if the token is valid,
    but does not raise an error if the token is missing or invalid.
    """
    token = credentials.credentials if credentials is not None else None
    # A replaced session is "no account" here rather than a 401: this dependency is for the
    # one surface that works while signed out, and a superseded token has lost the right to
    # claim an identity on it exactly like a token that never had one.
    return _account_from_bearer_token(token, db, require_active_session=False)


def _rate_limit_rejection(
    verdict: RateLimitVerdict,
    *,
    account_type: str,
    regno: str,
    client_id: str,
    ip: str,
) -> HTTPException:
    """Log which limiter refused a login request, and build the 429 that says so.

    The log lines are the observability contract: one per scope, named the way an
    operator would search for them. They carry the key that was counted -- a client id
    or an ip, neither of which grants anything -- and never the token, because no token
    exists yet at this point in the request and none ever needs to be in a log line.
    """
    if verdict.scope == SCOPE_CLIENT:
        logger.warning(
            f"Client rate limit triggered: client_id={client_id} ip={ip} "
            f"retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_IP:
        logger.warning(
            f"IP rate limit triggered: ip={ip} client_id={client_id} "
            f"retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_ACCOUNT:
        logger.warning(
            f"Account cooldown triggered: account_type={normalize_account_type(account_type)} "
            f"regno={regno!r} retry_after={verdict.retry_after}s"
        )
    elif verdict.scope == SCOPE_ACCOUNT_LONG:
        logger.warning(
            f"Account rate limit triggered (long window): "
            f"account_type={normalize_account_type(account_type)} regno={regno!r} "
            f"retry_after={verdict.retry_after}s"
        )

    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_RATE_LIMIT_DETAILS.get(
            verdict.scope or "", "Too many login requests. Please try again later."
        ),
        headers={"Retry-After": str(verdict.retry_after)},
    )


# The only characters a registration number may be made of, enforced at sign-in so no
# unvalidated string reaches a lookup, a rate-limit key or a log line. ``\w`` is Unicode
# in a str pattern: letters and digits from any script, plus ``_``. The punctuation
# ``. / -`` is what real regnos carry (e.g. ``22/CS0455``). Everything else -- whitespace,
# control characters, quotes, angle brackets, ``&``, ``=``, ``;``, backslashes and the
# rest -- is refused with a 400 before it can do anything. The length is ``USER_REGNO_MAX``,
# the same bound the ``regno`` column and the admin account payloads enforce.
_REGNO_PATTERN = re.compile(rf"^[\w./-]{{1,{USER_REGNO_MAX}}}$")


def _validated_regno(value: Any) -> str:
    """The submitted registration number, narrowed to what a regno may be, or a 400.

    Runs before the rate limits so invalid input is refused without spending any budget,
    and before the lookup so no unvalidated string ever reaches a query, a key or a log
    line. Surrounding whitespace is trimmed first -- a pasted value with a stray space
    still signs in -- and the trimmed value is what is searched and what keys the limits.
    A value that is not a string (a list, a dict, a number) is invalid, never coerced.
    """
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


async def generate_magic_link_service(
    request: Request,
    db: Database,
    account_type: str,
    client_id: Optional[str] = None,
):
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

    The answer is uniform by design: a link for a real, unblocked account and the
    identical 200 body for an unknown registration number or a blocked one. The rate
    limits run before the lookup for a reason: they are keyed on the submitted
    registration number, so being cooled down -- and paying the cooldown down --
    happens for an invented registration number exactly as it does for a real one,
    and the 429s never vary with account state either.

    ``client_id`` is the caller's browser identity for the client-scoped limit. The
    router resolves it from the request cookie and passes it in so the same value can be
    written back into the response; a direct caller that supplies none gets one resolved
    here instead.
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
        # Answered, not refused: the caller gets the same body as a mint, and no
        # token is stored. The log names what happened; the response does not.
        logger.info(
            f"Login request for unknown {category} registration number {regno!r}: "
            "answering as if a link was sent"
        )
        return _uniform_link_response()

    resolved_type, account = found

    # A blocked account mints nothing -- while redemption re-checks the block for a
    # link issued before the block was set -- and is answered exactly like an
    # unknown number, so blocked state is not learnable from this endpoint.
    if account.is_blocked:
        logger.warning(
            f"Login request for blocked {category} account {regno!r}: "
            "answering as if a link was sent"
        )
        return _uniform_link_response()

    from ..create_token import create_and_store_magic_token
    token, expires_at = create_and_store_magic_token(db, str(account.id), resolved_type, request)

    magic_link = f"http://localhost:5173/verify-login?token={token}"
    magic_link_logger.info(f"Magic Link for {account.name}: {magic_link}")
    logger.info(f"Login request accepted: magic link minted for {category} account {regno!r}")

    return {"message": LOGIN_LINK_SENT_MESSAGE, "expires_at": expires_at.isoformat()}


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
        ))

    # The lookup reads by hash alone and the state is classified here, deliberately
    # separate from the consume below: the client is told one thing ("invalid or
    # expired") for all of these, while the log says which one actually happened, so
    # an operator can tell a link that aged out from one an attacker is replaying
    # without the answer to either becoming a signal to the caller.
    if login_token is None:
        logger.warning(
            f"Invalid magic link token: no matching token "
            f"(hash_hint={hashed_token[:12]}, ip={request.client.host})"
        )
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

    # The refresh token is minted here, once, and hashed before storage. The raw value is
    # returned to the caller a single time (the router hands it to the browser as an
    # HttpOnly cookie); every later refresh rotates to a fresh value under the same
    # immutable deadline. ``session_expires_at`` is the absolute lifetime of the session,
    # written now from the login time and never moved forwards by any number of refreshes.
    refresh_token = generate_refresh_token()
    session_expires_at = now + timedelta(hours=settings.LOGIN_SESSION_EXPIRE_HOURS)

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
                session_expires_at=session_expires_at,
                refresh_token_hash=hash_token(refresh_token),
            )
        )
        if result.rowcount != 1:
            # The conditional UPDATE is the only thing that makes single-use true under
            # concurrency: two requests can both pass the classification read above, but
            # they serialise on this row and PostgreSQL re-evaluates the predicate
            # against the committed version, so exactly one of them updates it. Whoever
            # loses re-reads the row to say which of the three refusals actually
            # happened -- a loss to a racing request reads back as used.
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

        # One live session per account. This login is now this account's session, so any
        # older one is replaced -- recorded as superseded, with this session named as the
        # replacement, so the old session's next request can tell its holder why it stopped
        # working. This runs inside the same transaction as the consume above, behind an
        # advisory lock keyed to *this* account, for the same reason account creation is
        # locked: two logins racing for one account would otherwise both read "no other
        # active session" from the same snapshot and both go active, and neither would have
        # superseded the other. The lock serialises the pair, so whichever commits second
        # sees the first's active session and replaces it -- exactly one stays live.
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
    # The account's table and primary key, not just its regno. Every later request resolves
    # the caller from these two claims with a primary-key read, so a regno shared by a
    # participant and a moderator cannot move the session from one account to the other
    # between two requests. ``sub`` is kept alongside as the human-readable identity for
    # logs and for anything that wants to know who a token was for.
    #
    # ``session_id`` is the login-token row this session became. It is what ties the JWT to
    # the live-session state that is re-read on every request, so a JWT whose session has
    # been replaced by a newer login is refused even though the JWT itself is unexpired.
    access_token = create_access_token(
        data={
            "sub": account_regno,
            "account_id": str(account_id),
            "account_type": normalize_account_type(account_type),
            "session_id": login_token_id,
        },
        expires_delta=access_token_expires
    )

# Staff land on the moderation area; participants on their dashboard. Decided by the
    # account table, via the capabilities that table grants, so a moderator is not bounced
    # to a dashboard it has no business seeing and a table added later does not have to be
    # remembered here. The client re-derives the same destination from /auth/me once it
    # loads, so this only decides what the browser shows in the moment between the redirect
    # and that first fetch.
    #
    # Both kinds of staff get a landing page and they are not the same one. An admin can read
    # the platform census and goes to the console that shows it; a moderator cannot, and goes
    # to the moderation hub, which counts the backlog of the queues they can rule on. Both
    # destinations are reached through `/admin/*` except the hub, which is its own page
    # precisely because it is not the admin's.
    if has_capability(account_type, Capability.STATS_READ):
        redirect_url = "/admin"
    elif capabilities_for(account_type):
        redirect_url = "/moderation"
    else:
        redirect_url = "/dashboard"

    # ``refresh_token`` is carried out to the router so it can be set as the HttpOnly
    # cookie; the ``Token`` response model does not declare it, so the raw value never
    # reaches the response body. The stored copy is its SHA-256 hash, and this is the last
    # moment the plaintext is in this process before the router's cookie transition.
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "redirect_url": redirect_url,
        "refresh_token": refresh_token,
    }


def rotate_refresh_token_service(raw_token: str, db: Database) -> Dict[str, str]:
    """Rotate a refresh token and mint a fresh access token for its session.

    Returns ``{"access_token", "token_type", "refresh_token"}`` (the new refresh token
    still being the session's one live value). Raises the generic 401 for any refusal,
    and the dedicated superseded 401 when the session was replaced by a newer login.

    The transformation is one conditional UPDATE, keyed on the incoming token's hash, in a
    single transaction, and it is the only thing that makes rotation single-use under
    concurrency: two refresh calls that both present the same value serialise on this row
    and PostgreSQL re-evaluates the ``refresh_token_hash == :incoming`` predicate against
    the committed version, so exactly one of them updates it and the other re-reads the row
    to say what happened. There is no lock to hold and no path by which a second chain can
    start from the same token.

    A token that was already rotated away is refused generically and logged as a replay:
    a second use of a consumed value means someone may be holding a stale copy of it. The
    session itself is *not* revoked -- the value can never succeed again anyway, and killing
    the freshly rotated token too would punish the legitimate user (or a second tab of
    theirs that lost a concurrent refresh race) rather than the replay.

    Nothing about the account's authority is taken from the token. The session row is
    re-read, the account is re-resolved from the current table, and the blocked/deleted
    states are applied on this request, so a revoked or demoted account stops refreshing on
    the very next call.
    """
    if not raw_token:
        raise credentials_exception()

    hashed = hash_token(raw_token)
    now = datetime.now(timezone.utc)

    with db.sessions.begin() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(LoginTokenRecord.refresh_token_hash == hashed)
        )
        if row is None:
            logger.warning(
                f"Refresh rejected: no session holds refresh token "
                f"(hash_hint={hashed[:12]})"
            )
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
            # The session has hit its hard ceiling. No write is needed here: the admin
            # "active sessions" sweep already moves a session past its 24-hour window to
            # `expired`, and this refusal is the enforcement.
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
                refresh_token_hash=hash_token(new_raw),
                refresh_token_replaced_hash=hashed,
                last_seen_at=now,
            )
        )
        if result.rowcount != 1:
            # Lost the race against a concurrent rotation of the same value, or the session
            # changed underneath us. Re-read the row to say which.
            fresh = session.get(LoginTokenRecord, session_id)
            if fresh is not None and fresh.session_status == SESSION_STATUS_SUPERSEDED:
                logger.warning(
                    f"Refresh raced with a login: session {session_id} superseded by "
                    f"session {fresh.superseded_by}"
                )
                raise _session_superseded_exception()
            if fresh is not None and fresh.refresh_token_replaced_hash == hashed:
                logger.warning(
                    f"Refresh token replayed: session {session_id} already rotated away "
                    f"this value (hash_hint={hashed[:12]})"
                )
            else:
                logger.warning(f"Refresh race lost for session {session_id}")
            raise credentials_exception()

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
    """Revoke the session that holds a refresh token. Returns whether anything was revoked.

    A logout is idempotent and deliberately silent about whether the token existed: the
    browser is clearing its own cookie either way, and answering differently would let a
    caller probe whether an arbitrary refresh token is live. Only an *active* session is
    marked revoked -- a superseded one keeps its superseded state, because that state
    records which newer session replaced it, and an unredeemed magic-link row is untouched
    (its pre-redemption `revoked` column is the mechanism for that, not the session status).
    The raw value is looked up by its hash and never logged.
    """
    if not raw_token:
        return False
    hashed = hash_token(raw_token)
    with db.sessions.begin() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(LoginTokenRecord.refresh_token_hash == hashed)
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
