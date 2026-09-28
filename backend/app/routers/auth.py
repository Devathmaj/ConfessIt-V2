# app/routers/auth.py

from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ..postgres import Database
from typing import Annotated

# Corrected: Use direct imports from the project root
from ..config import settings
from ..dependencies import get_db
from ..accounts import AccountType
from ..models import AccountDetails, UserDetails
from ..services.auth_service import (
    credentials_exception,
    generate_magic_link_service,
    revoke_refresh_token_service,
    rotate_refresh_token_service,
    verify_magic_link_service,
    get_current_user
)
from ..services.login_rate_limit import client_cookie_options, resolve_client_id

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

class Token(BaseModel):
    access_token: str
    token_type: str
    redirect_url: str

class RefreshRequest(BaseModel):
    """An alternative to the refresh cookie: a caller (test, API client) can hand the
    raw refresh token in the body instead. The cookie path stays the primary one; the
    body exists so a client that captured the token out of band can use it without
    cookie plumbing."""
    refresh_token: str


def _refresh_cookie_options(request: Request) -> Dict[str, object]:
    """Keyword arguments for setting the session's refresh token as a cookie.

    ``HttpOnly`` keeps the raw token out of page scripts, ``SameSite=Lax`` keeps it off
    cross-site requests while still delivering it to the API from a sibling port of the
    same site, and ``Secure`` follows the scheme the request actually arrived on -- the
    same trade-off the client-id cookie makes in login_rate_limit. ``Path=/auth`` means
    the browser only ever attaches it to authentication endpoints, never to the rest of
    the API, so even an XSS cannot send it along with an ordinary request.
    """
    return {
        "key": settings.REFRESH_COOKIE_NAME,
        "max_age": settings.LOGIN_SESSION_EXPIRE_HOURS * 60 * 60,
        "httponly": True,
        "samesite": "lax",
        "secure": request.url.scheme == "https",
        "path": settings.REFRESH_COOKIE_PATH,
    }


def _clear_refresh_cookie(response: Response) -> None:
    """Drop the refresh cookie, matching the path it was set with."""
    response.delete_cookie(settings.REFRESH_COOKIE_NAME, path=settings.REFRESH_COOKIE_PATH)

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
        # FastAPI's own handler would build the error response from scratch and drop a
        # client id this request just issued, so the refusal is built here, where the
        # id is known. Only the rate limiter and the input gate refuse here -- the
        # lookup itself always answers 200 -- and a request refused by either has
        # already counted against this client, so unless the browser is given the id
        # back it will be treated as a brand-new client -- handed a fresh budget -- on
        # the very next request. That is what lets repeated sweeps of unknown
        # registration numbers dodge the per-browser limit.
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
async def verify_magic_link(
    token: str,
    request: Request,
    response: Response,
    db: Database = Depends(get_db),
):
    """
    Used to verify a magic link token and return a JWT access token.
    """
    payload = verify_magic_link_service(token, db, request)
    # The refresh token only ever leaves the server through this cookie. The response body
    # is the ``Token`` model (access token plus where to send the person); the raw refresh
    # value is carried out of the service separately and handed to the browser here.
    response.set_cookie(
        value=payload["refresh_token"],
        **_refresh_cookie_options(request),
    )
    return payload


@router.post("/refresh")
async def refresh_access_token(
    request: Request,
    response: Response,
    body: Optional[RefreshRequest] = None,
    db: Database = Depends(get_db),
):
    """
    Rotate the session's refresh token and mint a fresh access token.

    The refresh token is read from the HttpOnly cookie set at login; a body carrying
    ``refresh_token`` is accepted as an alternative for callers that captured the value
    out of band. The response sets a new cookie holding the rotated refresh token -- the
    old one is dead the moment this succeeds -- and answers with a fresh access token.
    The rotation inherits the session's original absolute deadline: refreshing extends
    the access token, never the session.

    Every refusal is a generic 401 except the superseded case, which keeps the dedicated
    ``session_superseded`` wording so the client can say the person was signed in
    elsewhere. A failed refresh also clears the cookie, so a dead token is not re-sent.
    """
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if body is not None and body.refresh_token:
        raw = body.refresh_token
    try:
        result = rotate_refresh_token_service(raw, db)
    except HTTPException:
        _clear_refresh_cookie(response)
        raise
    response.set_cookie(value=result["refresh_token"], **_refresh_cookie_options(request))
    return {"access_token": result["access_token"], "token_type": result["token_type"]}


@router.post("/logout")
async def logout(
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
    access-token copy regardless.
    """
    raw = request.cookies.get(settings.REFRESH_COOKIE_NAME)
    if body is not None and body.refresh_token:
        raw = body.refresh_token
    if raw:
        revoke_refresh_token_service(raw, db)
    _clear_refresh_cookie(response)
    return {"message": "Signed out"}

# Deliberately not a response_model. The response is a ``UserDetails`` for a participant and
# an ``AccountDetails`` for a staff account, and a single model would have to declare every
# participant field optional to be valid for both -- which would let a staff response
# quietly carry a year and department the row does not have, and would stop the client from
# being able to tell a missing field from a null one. Declaring no model keeps the response
# equal to what the account actually is.
@router.get("/me")
async def read_users_me(current_user: Annotated[AccountDetails, Depends(get_current_user)]):
    """
    Fetch the details of the currently authenticated account.

    The shape follows the table: a participant gets the full profile, staff get the shared
    account fields plus the capabilities their table grants. The client uses the same
    ``account_type`` to decide which landing path and which screens are reachable, and the
    server re-checks all of it per request regardless.
    """
    payload = current_user.model_dump(by_alias=True)
    # ``profile_picture_url`` is already correct here: ``auth_service._user_payload`` derives it
    # from the stored Cloudinary identity when a participant is resolved, so there is no URL in the
    # database to rewrite and nothing that could still hold a stale address from a previous build.
    if current_user.account_type == AccountType.USERS.value:
        return UserDetails(**payload)
    return AccountDetails(**payload)
