# app/routers/auth.py

from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel

from ..postgres import Database
from typing import Annotated

from ..config import cookie_secure, cors_allow_origins, settings
from ..dependencies import get_db
from ..accounts import AccountType
from ..models import AccountDetails, UserDetails
from ..services.auth_service import (
    StaleRefreshTokenError,
    access_token_session_id,
    bearer_scheme,
    create_websocket_ticket,
    credentials_exception,
    generate_magic_link_service,
    revoke_refresh_token_service,
    rotate_refresh_token_service,
    verify_magic_link_service,
    get_current_user
)
from ..services.authorization import require_participant
from ..services.login_rate_limit import client_cookie_options, resolve_client_id

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

class Token(BaseModel):
    access_token: str
    token_type: str
    redirect_url: str


class WebSocketTicket(BaseModel):
    """A one-use credential for the next WebSocket handshake."""

    ticket: str
    expires_at: datetime
    expires_in: int

class RefreshRequest(BaseModel):
    """An alternative to the refresh cookie, for non-browser callers only.

    Honoured only when ``ENABLE_DEBUG_ENDPOINTS`` is on; otherwise a body is ignored and
    the cookie flow applies. Optional rather than required because browsers POST an empty
    ``{}`` body on refresh/logout, and a required field caused a 422 right after login.
    """
    refresh_token: Optional[str] = None


def _request_origin(value: str) -> Optional[str]:
    """Reduce an Origin or Referer header to ``scheme://authority``.

    Returns None when the value is not shaped like an origin; the caller treats that as a
    refusal, since a present-but-unparseable attestation is worse than none.
    """
    if "://" not in value:
        return None
    scheme, rest = value.split("://", 1)
    authority = rest.split("/", 1)[0]
    if not scheme or not authority:
        return None
    return f"{scheme.lower()}://{authority.lower()}"


def _enforce_cookie_origin(request: Request) -> None:
    """Refuse a cookie-authenticated refresh/logout that arrived cross-site.

    ``SameSite=Lax`` already blocks most of this, but 307 chains and old browsers can
    bypass it. A browser-driven POST carries Origin or Referer; either must name a CORS
    allowlist origin. No header means a non-browser caller, which has no ambient
    authority to steal, so it is allowed. A 403 here reveals no account state.
    """
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    candidate = origin or referer
    if candidate is None:
        return
    parsed = _request_origin(candidate)
    allowed = {o.lower() for o in cors_allow_origins()}
    if parsed is None or parsed not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-site request refused",
        )


def _refresh_cookie_options(request: Request) -> Dict[str, object]:
    """Keyword arguments for setting the session's refresh token as a cookie.

    Contract source of truth: tests/test_refresh_cookie_contract.py (the frontend's
    comments restate it). ``HttpOnly`` hides the token from scripts, ``SameSite=Lax``
    keeps it off cross-site requests, ``Secure`` follows the client-facing scheme or
    ``COOKIE_SECURE``, and ``Path=/auth`` limits it to auth endpoints.

    Not ``__Host-``-prefixed (that requires ``Path=/``, widening the cookie to the whole
    API) and not ``SameSite=Strict`` (that breaks cross-site refresh). The CSRF posture
    is Lax plus the Origin/Referer check plus rotation on every use.
    """
    return {
        "key": settings.REFRESH_COOKIE_NAME,
        "max_age": settings.LOGIN_SESSION_EXPIRE_HOURS * 60 * 60,
        "httponly": True,
        "samesite": "lax",
        "secure": cookie_secure(request.url.scheme),
        "path": settings.REFRESH_COOKIE_PATH,
    }


def _clear_refresh_cookie(response: Response, *, secure: bool) -> None:
    """Drop the refresh cookie, matching the path it was set with.

    The ``secure`` flag must match how it was set: browsers ignore deletion of a Secure
    cookie that arrives without the flag.
    """
    response.delete_cookie(
        settings.REFRESH_COOKIE_NAME,
        path=settings.REFRESH_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite="lax",
    )


def _refresh_refusal(
    request: Request, exc: HTTPException, *, cookie_presented: bool
) -> JSONResponse:
    """The answer to a refused refresh, clearing the cookie when it is dead.

    FastAPI builds a fresh response for a raised ``HTTPException``, discarding any
    ``Set-Cookie`` on the injected response, so the refusal is built here instead.

    Delete only when the cookie was presented and is dead. A rotated-away value from a
    live session may coexist with the newer value in another tab; clearing it would sign
    that browser out. The Origin/Referer refusal is not routed here: it says nothing about
    the cookie, and destroying a session over it would help an attacker.
    """
    refusal = JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )
    stale = isinstance(exc, StaleRefreshTokenError)
    if cookie_presented and not stale:
        _clear_refresh_cookie(refusal, secure=cookie_secure(request.url.scheme))
    return refusal

@router.post("/login/{account_type}/magic")
async def generate_magic_link(
    account_type: AccountType,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
):
    """
    Mint a magic link for an account in the named account table.

    One endpoint per account kind -- ``/auth/login/users/magic``,
    ``/auth/login/admin/magic``, ``/auth/login/moderator/magic`` -- and the path is what
    selects the table. A regno is unique only within a table, so the sign-in page a person
    used is the only thing that can say which account they mean when one regno exists as a
    participant and as staff at the same time. An unrecognised account type is a 404 from
    the path parser, not a default to some other table.

    The client id lives here rather than in the service because it is an HTTP concern:
    the service needs the value as a rate-limit key, and this layer is what writes it
    back into the cookie when it was just created. Resolving it once and passing it down
    keeps the key that was counted and the key that is stored the same value.
    """
    client_id, is_new_client = resolve_client_id(request)
    try:
        payload = await generate_magic_link_service(
            request, db, account_type.value, client_id=client_id
        )
    except HTTPException as exc:
        # Build the refusal here so a client id issued by this request is not lost.
        # Otherwise the next request looks like a new client and gets a fresh budget,
        # letting sweeps of unknown regnos dodge the per-browser limit.
        refusal = JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )
        if is_new_client:
            refusal.set_cookie(value=client_id, **client_cookie_options(request))
        return refusal
    if is_new_client:
        response.set_cookie(value=client_id, **client_cookie_options(request))
    return payload

@router.get("/login/magic/verify", response_model=Token)
def verify_magic_link(
    token: str,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
):
    """
    Used to verify a magic link token and return a JWT access token.
    """
    payload = verify_magic_link_service(token, db, request)
    # The refresh token only leaves via this cookie; the body carries the Token model,
    # and the raw value is passed out of the service separately.
    response.set_cookie(
        value=payload["refresh_token"],
        **_refresh_cookie_options(request),
    )
    return payload


@router.post("/refresh")
def refresh_access_token(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = None,
    db: Database = Depends(get_db),
):
    """
    Rotate the session's refresh token and mint a fresh access token.

    The refresh token is read from the HttpOnly cookie set at login; a body carrying
    ``refresh_token`` is accepted as an alternative only when ``ENABLE_DEBUG_ENDPOINTS``
    is on, for non-browser callers without cookie plumbing. The response sets a new
    cookie holding the rotated refresh token -- the old one is dead the moment this
    succeeds -- and answers with a fresh access token. The rotation inherits the
    session's original absolute deadline: refreshing extends the access token, never
    the session.

    A cookie-authenticated call must also pass the Origin/Referer check: the cookie is
    ambient authority, and a cross-site POST that somehow carries it (a 307 chain, an
    old browser past SameSite) is refused here. Body-authenticated calls carry no
    ambient authority and skip the check.

    Every refusal is a generic 401 except the superseded case, which keeps the dedicated
    ``session_superseded`` wording so the client can say the person was signed in
    elsewhere. A failed refresh also clears the cookie, so a dead token is not re-sent --
    except when the presented value is a stale copy of a live session's previous one,
    where the browser may still be holding the newer value (see ``_refresh_refusal``).
    """
    cookie_raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    raw = cookie_raw
    using_cookie = raw is not None
    if body is not None and body.refresh_token and settings.ENABLE_DEBUG_ENDPOINTS:
        raw = body.refresh_token
        using_cookie = False
    if using_cookie:
        _enforce_cookie_origin(request)
    try:
        result = rotate_refresh_token_service(raw, db)
    except HTTPException as exc:
        # Answered, not raised: FastAPI discards Set-Cookie on a raised HTTPException,
        # losing the cookie deletion this refusal owes the browser.
        return _refresh_refusal(request, exc, cookie_presented=using_cookie)
    response.set_cookie(value=result["refresh_token"], **_refresh_cookie_options(request))
    return {"access_token": result["access_token"], "token_type": result["token_type"]}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = None,
    db: Database = Depends(get_db),
):
    """
    Sign the current session out on the server and clear the refresh cookie.

    Works even after the 30-minute access token has lapsed, because it is authorised by
    the refresh cookie rather than the access token -- which is what makes "log out after
    the access token expired" possible. Revocation is silent about whether the token was
    ever live, so a logout cannot probe a refresh token; the client clears its own
    access-token copy regardless. Like refresh, a cookie-authenticated call must pass the
    Origin/Referer check, and the body alternative is debug-only.
    """
    cookie_raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    raw = cookie_raw
    using_cookie = raw is not None
    if body is not None and body.refresh_token and settings.ENABLE_DEBUG_ENDPOINTS:
        raw = body.refresh_token
        using_cookie = False
    if using_cookie:
        _enforce_cookie_origin(request)
    if raw:
        revoke_refresh_token_service(raw, db)
    _clear_refresh_cookie(response, secure=cookie_secure(request.url.scheme))
    return {"message": "Signed out"}


@router.post("/ws-ticket", response_model=WebSocketTicket)
def mint_websocket_ticket(
    response: Response,
    current_user: Annotated[UserDetails, Depends(require_participant)],
    credentials: Annotated[
        Optional[HTTPAuthorizationCredentials], Depends(bearer_scheme)
    ],
    db: Database = Depends(get_db),
):
    """Mint the short-lived, single-use ticket a WebSocket presents in its URL.

    The access token is deliberately not accepted as a WebSocket credential: Uvicorn logs
    the handshake path and query string, so putting a 30-minute bearer there writes a live
    API credential into stdout. This endpoint is the authenticated, non-URL exchange; the
    ticket it returns is bound to the caller's login session, expires in under a minute,
    and can be redeemed once. It is a participant-only surface because both live-message
    endpoints are participant-only.
    """
    if credentials is None:
        raise credentials_exception()
    session_id = access_token_session_id(credentials.credentials)
    if session_id is None:
        raise credentials_exception()
    try:
        ticket = create_websocket_ticket(db, current_user, session_id)
    except ValueError:
        # The dependency already resolved a participant; this only refuses a malformed
        # session id or an account shape that cannot own a socket.
        raise credentials_exception()
    # A ticket is a credential in a response body; keep it out of caches.
    response.headers["Cache-Control"] = "no-store"
    return ticket


# No response_model: a participant gets UserDetails and staff get AccountDetails. One
# model would force every participant field optional, blurring missing versus null and
# letting staff responses carry fields their row lacks.
@router.get("/me")
def read_users_me(current_user: Annotated[AccountDetails, Depends(get_current_user)]):
    """
    Fetch the details of the currently authenticated account.

    The shape follows the table: a participant gets the full profile, staff get the shared
    account fields plus the capabilities their table grants. The client uses the same
    ``account_type`` to decide which landing path and which screens are reachable, and the
    server re-checks all of it per request regardless.
    """
    payload = current_user.model_dump(by_alias=True)
    # ``profile_picture_url`` is already derived by auth_service._user_payload from the
    # stored Cloudinary identity, so there is no stale URL to rewrite here.
    if current_user.account_type == AccountType.USERS.value:
        return UserDetails(**payload)
    return AccountDetails(**payload)
