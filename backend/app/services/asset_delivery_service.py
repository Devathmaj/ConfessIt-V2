# app/services/asset_delivery_service.py

"""Backend-authorized delivery of user-uploaded images.

Every image the backend hands out is addressed through a proxy route rather than by its
raw Cloudinary URL. Each proxy URL carries a short-lived token that is scoped to exactly
one object, and the request is then re-checked against the database before any bytes are
served. A token therefore only proves "this account was entitled a moment ago"; it can
never grant access to a different asset, and revoking a love note immediately stops
delivery even to a URL that was minted while the note was live.
"""

import hashlib
import hmac
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import jwt
from fastapi import HTTPException
from sqlalchemy import or_, select

from ..config import settings
from ..db_models import LoveNoteRecord, UserRecord
from ..models import AccountDetails
from ..accounts import (AccountRef, Capability, has_capability, is_valid_account_type,
                        normalize_account_type)
from ..postgres import Database
from .storage_service import storage_service
from .auth_service import ASSET_TOKEN_TYPE

# Public route prefix per asset kind. These are the only paths images are served from.
ROUTE_PREFIXES: Dict[str, str] = {
    "profile": "/api/profile/files/",
    "love-notes": "/api/love-notes/files/",
}

# Bounds for the server-side fetch. Images are small, so anything larger is a mistake or
# an attempt to make the backend relay an arbitrary payload.
MAX_FETCH_BYTES = 10 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 15

_TOKEN_SIGNING_CONTEXT = b"confessit-asset-delivery-v1"


def asset_token_ttl_seconds() -> int:
    """Lifetime of a minted delivery token.

    Defaults to the access-token lifetime so a delivery URL can never outlive the session
    that requested it, and the SPA necessarily re-mints on its next ``/auth/me`` fetch.
    """
    configured = getattr(settings, "ASSET_TOKEN_TTL_SECONDS", None)
    if configured:
        return max(30, int(configured))
    return max(60, int(settings.ACCESS_TOKEN_EXPIRE_MINUTES) * 60)


def _signing_key() -> str:
    """Derive a signing key that is distinct from the API access-token key.

    Asset tokens travel in query strings, so they must not be verifiable as access tokens.
    Deriving a separate key means an asset token fails signature validation against the
    access-token key even if the ``typ`` claim check were ever removed.
    """
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), _TOKEN_SIGNING_CONTEXT, hashlib.sha256).hexdigest()


def _credentials_exception() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ----------------------------------------------------------------------
# Token minting / verification
# ----------------------------------------------------------------------
def mint_asset_token(*, account: AccountRef, kind: str, object_path: str, ttl_seconds: Optional[int] = None) -> str:
    """Mint a token that authorises exactly one (kind, object_path) pair for one account.

    The account travels as a table and a primary key, not as a regno. The proxy re-reads the
    account on every delivery, and a regno can identify a row in more than one account
    table, so a regno here would leave the proxy guessing which account the token was for.
    """
    issued_at = datetime.now(timezone.utc)
    ttl = asset_token_ttl_seconds() if ttl_seconds is None else int(ttl_seconds)
    payload: Dict[str, Any] = {
        "sub": account.regno,
        "account_id": account.account_id,
        "account_type": account.account_type,
        "typ": ASSET_TOKEN_TYPE,
        "kind": kind,
        "path": object_path,
        "iat": int(issued_at.timestamp()),
        "exp": int((issued_at + timedelta(seconds=ttl)).timestamp()),
    }
    return jwt.encode(payload, _signing_key(), algorithm=settings.ALGORITHM)


def verify_asset_token(token: str, *, kind: str, object_path: str) -> AccountRef:
    """Validate a delivery token and return the account it was minted for.

    Every failure mode raises the same 401 so a caller cannot use error codes to learn
    whether an object exists or whether a token was minted for a different asset.
    """
    try:
        payload = jwt.decode(token, _signing_key(), algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        raise _credentials_exception()

    if payload.get("typ") != ASSET_TOKEN_TYPE:
        raise _credentials_exception()
    if not hmac.compare_digest(str(payload.get("kind") or ""), str(kind)):
        raise _credentials_exception()
    if not hmac.compare_digest(str(payload.get("path") or ""), str(object_path)):
        raise _credentials_exception()

    regno = payload.get("sub")
    account_id = payload.get("account_id")
    account_type = payload.get("account_type")
    if not regno or not account_id or not is_valid_account_type(account_type):
        raise _credentials_exception()
    return AccountRef(
        regno=str(regno),
        account_id=str(account_id),
        account_type=normalize_account_type(account_type),
    )


# ----------------------------------------------------------------------
# URL construction
# ----------------------------------------------------------------------
def normalize_asset_path(kind: str, stored_identifier: Optional[str]) -> Optional[str]:
    """Turn a stored identifier into a validated object path, or None if it is not one.

    Rows can hold a full Cloudinary URL, a legacy bare object path, or an arbitrary
    administrator-entered URL. Anything that is not a servable object in this asset kind
    returns None so the caller can fall back to the original value.
    """
    if not stored_identifier:
        return None
    object_path = storage_service.normalize_stored_path(stored_identifier, kind)
    if not object_path:
        return None
    try:
        return storage_service.validate_object_path(object_path)
    except HTTPException:
        return None


def _delivery_base() -> str:
    """Absolute base for delivery URLs.

    These URLs are fetched by the browser, so they must address whichever origin serves the
    proxy routes (the API), which is not necessarily the frontend's PUBLIC_BASE_URL.
    """
    configured = getattr(settings, "ASSET_BASE_URL", None) or settings.PUBLIC_BASE_URL or ""
    return str(configured).rstrip("/")


def build_delivery_url(*, kind: str, stored_identifier: Optional[str], account: AccountRef) -> Optional[str]:
    """Build the proxy URL the account that owns this object should use to load it.

    Returns None when the stored value is not a servable object of this kind, in which
    case the caller should keep whatever value it already had.
    """
    if kind not in ROUTE_PREFIXES:
        return None
    object_path = normalize_asset_path(kind, stored_identifier)
    if not object_path:
        return None

    token = mint_asset_token(account=account, kind=kind, object_path=object_path)
    return f"{_delivery_base()}{ROUTE_PREFIXES[kind]}{quote(object_path)}?t={quote(token)}"


def profile_image_url(stored_identifier: Optional[str], *, account: AccountRef) -> Optional[str]:
    """Proxy URL for a profile image, falling back to the stored value.

    Every response that shows a profile picture should call this so there is a single
    delivery mechanism. Values that are not a servable object in the profile bucket (a
    data URL, a placeholder, nothing at all) are passed through untouched.
    """
    proxied = build_delivery_url(
        kind="profile", stored_identifier=stored_identifier, account=account
    )
    return proxied or storage_service.get_signed_profile_url(stored_identifier)


# ----------------------------------------------------------------------
# Entitlement
# ----------------------------------------------------------------------
def _matches_stored_asset(stored_identifier: Optional[str], kind: str, object_path: str) -> bool:
    candidate = normalize_asset_path(kind, stored_identifier)
    if not candidate:
        return False
    return hmac.compare_digest(candidate, object_path)


def _profile_asset_is_published(db: Database, object_path: str) -> bool:
    """Profile images are visible to any signed-in account, not just the owner.

    Profiles surface in matchmaking, confessions and chat, so restricting these to the
    owner would break those screens. What still has to hold is that the object is a
    profile picture some user currently references, so the proxy cannot be used to pull
    arbitrary or already-replaced objects out of the bucket.

    Profile objects are stored as ``{regno}/{token}/{filename}``, so the first segment
    identifies the owning account without scanning every row.
    """
    owner_regno = object_path.split("/", 1)[0]
    if not owner_regno:
        return False
    with db.sessions() as session:
        stored_url = session.scalar(
            select(UserRecord.profile_picture_url).where(UserRecord.regno == owner_regno)
        )
    return _matches_stored_asset(stored_url, "profile", object_path)


def _may_view_love_note_asset(db: Database, user: AccountDetails, object_path: str) -> bool:
    """Authorise a love note image for the accounts allowed to see it.

    - the recipient may only view a note once a moderator has approved it
    - the sender may always view the note they sent
    - staff may view any note, in any state, in order to moderate it
    """
    with db.sessions() as session:
        query = select(
            LoveNoteRecord.sender_id,
            LoveNoteRecord.recipient_id,
            LoveNoteRecord.image_url,
            LoveNoteRecord.status,
        )
        # Widening the query to every note is what lets a moderator inspect an image on a
        # note that is still pending, which is the point of the review screen. Keyed on the
        # capability that guards the love-note review endpoint, so the image proxy and the
        # review list grant the same people access.
        if not has_capability(user.account_type, Capability.LOVE_NOTES_REVIEW):
            query = query.where(
                or_(
                    LoveNoteRecord.sender_id == user.id,
                    LoveNoteRecord.recipient_id == user.id,
                )
            )
        rows = session.execute(query).all()

    for sender_id, recipient_id, image_url, status in rows:
        is_recipient = str(recipient_id or "") == str(user.id)
        # A recipient must never see a note that has not been approved; a rejected or
        # still-pending note stays visible only to its sender and to staff.
        if is_recipient and status != "approved":
            continue
        if _matches_stored_asset(image_url, "love-notes", object_path):
            return True
    return False


def is_entitled_to_asset(db: Database, *, kind: str, object_path: str, user: AccountDetails) -> bool:
    """Authorisation gate applied to every proxy request.

    Both kinds require a signed-in account. Profile images are then visible to anyone
    signed in; love note images stay restricted to the note's parties.
    """
    if kind == "profile":
        return _profile_asset_is_published(db, object_path)
    if kind == "love-notes":
        return _may_view_love_note_asset(db, user, object_path)
    return False


# ----------------------------------------------------------------------
# Byte delivery
# ----------------------------------------------------------------------
def fetch_asset_bytes(origin_url: str) -> Tuple[bytes, str]:
    """Fetch the object server-side so the raw Cloudinary URL never reaches the client.

    Any failure is reported as a 404 so the proxy cannot be used to distinguish a missing
    object from an unreachable origin.
    """
    request = urllib.request.Request(
        origin_url,
        headers={"User-Agent": "ConfessIt-Asset-Proxy/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type") or "application/octet-stream"
            payload = response.read(MAX_FETCH_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found")

    if not payload or len(payload) > MAX_FETCH_BYTES:
        raise HTTPException(status_code=404, detail="File not found")

    # Both proxy routes only ever serve images. Refusing anything else keeps an unexpected
    # origin response from being re-served same-origin as an active type.
    if not content_type.split(";")[0].strip().lower().startswith("image/"):
        raise HTTPException(status_code=404, detail="File not found")

    return payload, content_type
