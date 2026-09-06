"""Tests for backend-authorized image delivery and the gated /db-test endpoint.

The proxy is the only path images are served from, so these tests cover the three layers
that make it safe: path validation, the scoped delivery token, and the per-request
database entitlement check.
"""

import time

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.db_models import UserRecord
from app.dependencies import get_db
from app.main import app
from app.models import UserDetails
from app.services import asset_delivery_service
from app.services.asset_delivery_service import (
    asset_token_ttl_seconds,
    build_delivery_url,
    is_entitled_to_asset,
    mint_asset_token,
    verify_asset_token,
)
from app.services.auth_service import (
    ASSET_TOKEN_TYPE,
    get_current_user,
    load_active_user_service,
    oauth2_scheme,
)
from app.services.storage_service import storage_service


# Captured before the autouse stub below replaces it, so the origin-fetch unit tests can
# exercise the real implementation.
_REAL_FETCH_ASSET_BYTES = asset_delivery_service.fetch_asset_bytes


OWNED_PATH = "testuser/AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo.jpg"
NOTE_PATH = "Alice/BcDeFgHiJkLmNoPqRsTuVwX/20240101_120000_000000_love_note.png"
OTHER_PATH = "someoneelse/ZzYyXxWwVvUuTtSsRrQqPp/20240101_120000_000000_photo.jpg"

# Traversal / encoding / shape payloads that must never reach a Cloudinary public ID.
MALICIOUS_PATHS = [
    "../../events/somefolder/secret.jpg",
    "testuser/../../../etc/passwd",
    "/etc/passwd",
    "..%2f..%2fevents%2fsecret.jpg",
    "%2e%2e%2f%2e%2e%2fsecret.jpg",
    "testuser\\..\\..\\events\\secret.jpg",
    "testuser//AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo.jpg",
    "testuser/./AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo.jpg",
    "testuser/AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo",
    "testuser/AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo.txt",
    "AbCdEfGhIjKlMnOpQrStUv",
    "profile_pics/../../secret.jpg",
    "a/b\x00c.jpg",
]

FILE_ROUTES = ["profile", "love-notes"]

OWNER_ID = "11111111-1111-1111-1111-111111111111"
RECIPIENT_ID = "22222222-2222-2222-2222-222222222222"
STRANGER_ID = "33333333-3333-3333-3333-333333333333"


def stored_url(kind: str, object_path: str) -> str:
    """Build a stored Cloudinary URL for the bucket this app is actually configured with."""
    bucket = storage_service._route_bucket_map.get(kind)
    assert bucket, f"bucket for {kind} must be configured for this test"
    return (
        f"https://res.cloudinary.com/{storage_service.cloud_name}"
        f"/image/upload/v1700000000/{bucket}/{object_path}"
    )


def make_user_row(regno: str, user_id: str, role: str = "user", **overrides) -> UserRecord:
    values = dict(
        id=user_id, regno=regno, name=regno.title(), email=f"{regno}@example.com",
        emoji=None, bio=None, class_name="Test_Class", profile_picture_url=None,
        gender="male", interests=[], matchmaking_enabled=True, notifications_enabled=True,
        love_notes_receive_enabled=True, love_notes_send_enabled=False, is_blocked=False,
        reported_count=0, last_login_at=None, last_login_ip=None, last_login_user_agent=None,
        role=role, last_matchmaking_at=None,
    )
    values.update(overrides)
    return UserRecord(**values)


def user_details(regno: str, user_id: str, role: str = "user") -> UserDetails:
    return UserDetails(
        id=user_id, regno=regno, name=regno.title(), email=f"{regno}@e.com",
        class_name="C", gender="male", matchmaking_enabled=True,
        notifications_enabled=True, role=role,
    )


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def scalar(self, query):
        return self._db.resolve(query)

    def execute(self, query):
        return _FakeResult(self._db.filter_love_notes(query))


class _FakeDatabase:
    """Answers the queries the delivery path makes, scoped to the requested account.

    Ownership rules are per-registration-number, so the fake has to honour the bound
    ``regno`` rather than returning one canned row; otherwise "only the owner may fetch
    this" would pass even if the query ignored the account entirely.
    """

    def __init__(self, *, users=None, profile_pictures=None, love_note_rows=()):
        self.users = dict(users or {})
        self.profile_pictures = dict(profile_pictures or {})
        self.love_note_rows = list(love_note_rows)

    def sessions(self):
        return _FakeSession(self)

    def resolve(self, query):
        sql = str(query)
        regno = self._bound_regno(query)
        if "users.id," in sql or sql.strip().startswith("SELECT users.id"):
            return self.users.get(regno)
        if "users.profile_picture_url" in sql:
            return self.profile_pictures.get(regno)
        return None

    @staticmethod
    def _bound_regno(query):
        try:
            params = query.compile().params
        except Exception:
            return None
        for value in params.values():
            if isinstance(value, str):
                return value
        return None

    def filter_love_notes(self, query):
        """Honour the sender/recipient restriction the service applies for non-admins.

        Without this the fake would hand back every note regardless of who asked, and the
        "only the sender, the approved recipient, or an admin" rule would pass vacuously.
        An unrestricted query (the admin path) binds no account and so returns everything.
        """
        try:
            bound = {
                value for value in query.compile().params.values() if isinstance(value, str)
            }
        except Exception:
            bound = set()
        if not bound:
            return list(self.love_note_rows)
        return [row for row in self.love_note_rows if row[0] in bound or row[1] in bound]


def profile_db(actor_regno: str = "testuser", actor_id: str = OWNER_ID) -> _FakeDatabase:
    return _FakeDatabase(
        users={actor_regno: make_user_row(actor_regno, actor_id)},
        profile_pictures={"testuser": stored_url("profile", OWNED_PATH)},
    )


def love_note_db(status: str, actor_regno: str, actor_id: str) -> _FakeDatabase:
    return _FakeDatabase(
        users={actor_regno: make_user_row(actor_regno, actor_id)},
        love_note_rows=[(OWNER_ID, RECIPIENT_ID, stored_url("love-notes", NOTE_PATH), status)],
    )


@pytest.fixture(autouse=True)
def stub_origin_fetch(monkeypatch):
    """Replace the outbound Cloudinary fetch so no network call is made."""
    monkeypatch.setattr(
        asset_delivery_service,
        "fetch_asset_bytes",
        lambda url: (b"\x89PNG\r\n\x1a\nfake", "image/png"),
    )


@pytest.fixture(autouse=True)
def stub_origin_url(monkeypatch):
    monkeypatch.setattr(
        storage_service, "get_origin_url",
        lambda kind, object_path: f"https://cdn.test/{kind}/{object_path}",
    )


@pytest.fixture
def serve(monkeypatch):
    """Return a client bound to the given database. Tokens travel in the query string."""

    def _serve(db):
        monkeypatch.setitem(app.dependency_overrides, get_db, lambda: db)
        return TestClient(app, follow_redirects=False)

    return _serve


@pytest.fixture
def anonymous_client():
    """No auth at all, so the routes' own 401 behaviour is exercised."""
    saved = {
        key: app.dependency_overrides.pop(key)
        for key in (get_db, oauth2_scheme)
        if key in app.dependency_overrides
    }
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.update(saved)


# ----------------------------------------------------------------------
# Path validation
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", MALICIOUS_PATHS)
def test_validate_object_path_rejects_unsafe_paths(path):
    with pytest.raises(HTTPException) as exc_info:
        storage_service.validate_object_path(path)
    assert exc_info.value.status_code == 400


def test_validate_object_path_accepts_generated_path():
    assert storage_service.validate_object_path(OWNED_PATH) == OWNED_PATH


def test_validate_object_path_accepts_love_note_path():
    assert storage_service.validate_object_path(NOTE_PATH) == NOTE_PATH


# ----------------------------------------------------------------------
# Delivery token scoping
# ----------------------------------------------------------------------
def test_token_is_scoped_to_one_path():
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    assert verify_asset_token(token, kind="profile", object_path=OWNED_PATH) == "testuser"
    with pytest.raises(HTTPException) as exc_info:
        verify_asset_token(token, kind="profile", object_path=OTHER_PATH)
    assert exc_info.value.status_code == 401


def test_token_is_scoped_to_one_kind():
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    with pytest.raises(HTTPException) as exc_info:
        verify_asset_token(token, kind="love-notes", object_path=OWNED_PATH)
    assert exc_info.value.status_code == 401


def test_expired_token_is_rejected():
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH, ttl_seconds=-10)
    with pytest.raises(HTTPException) as exc_info:
        verify_asset_token(token, kind="profile", object_path=OWNED_PATH)
    assert exc_info.value.status_code == 401


def test_token_signed_with_another_key_is_rejected():
    forged = jwt.encode(
        {"sub": "testuser", "typ": ASSET_TOKEN_TYPE, "kind": "profile", "path": OWNED_PATH,
         "exp": int(time.time()) + 600},
        "attacker-secret",
        algorithm=settings.ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_asset_token(forged, kind="profile", object_path=OWNED_PATH)
    assert exc_info.value.status_code == 401


def test_token_without_typ_claim_is_rejected():
    """A normal access token must not be usable as a delivery token."""
    access_token = jwt.encode(
        {"sub": "testuser", "exp": int(time.time()) + 600},
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    with pytest.raises(HTTPException) as exc_info:
        verify_asset_token(access_token, kind="profile", object_path=OWNED_PATH)
    assert exc_info.value.status_code == 401


def test_token_ttl_does_not_outlive_access_token():
    assert asset_token_ttl_seconds() <= settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60


# ----------------------------------------------------------------------
# Account re-validation
# ----------------------------------------------------------------------
def test_asset_token_is_rejected_as_an_access_token():
    """Token confusion guard: a delivery token must not authenticate the REST API."""
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    with pytest.raises(HTTPException) as exc_info:
        get_current_user(token=token, db=profile_db())
    assert exc_info.value.status_code == 401


def test_load_active_user_rejects_unknown_account():
    with pytest.raises(HTTPException) as exc_info:
        load_active_user_service(_FakeDatabase(), "ghost")
    assert exc_info.value.status_code == 401


def test_load_active_user_rejects_blocked_account():
    db = _FakeDatabase(users={"blocked": make_user_row("blocked", OWNER_ID, is_blocked=True)})
    with pytest.raises(HTTPException) as exc_info:
        load_active_user_service(db, "blocked")
    assert exc_info.value.status_code == 403


# ----------------------------------------------------------------------
# Entitlement: profile
# ----------------------------------------------------------------------
def test_profile_entitled_to_owner():
    assert is_entitled_to_asset(
        profile_db(), kind="profile", object_path=OWNED_PATH, user=user_details("testuser", OWNER_ID)
    ) is True


def test_profile_visible_to_another_signed_in_account():
    """Profiles are not owner-only: matchmaking, confessions and chat all show them."""
    assert is_entitled_to_asset(
        profile_db(), kind="profile", object_path=OWNED_PATH, user=user_details("stranger", STRANGER_ID)
    ) is True


def test_profile_denied_when_stored_value_is_someone_elses_asset():
    """The row for the path's own regno must be the asset actually being requested."""
    db = _FakeDatabase(
        users={"testuser": make_user_row("testuser", OWNER_ID)},
        profile_pictures={"testuser": stored_url("profile", OTHER_PATH)},
    )
    assert is_entitled_to_asset(
        db, kind="profile", object_path=OWNED_PATH, user=user_details("testuser", OWNER_ID)
    ) is False


def test_profile_denied_when_owner_has_no_picture():
    db = _FakeDatabase(users={"testuser": make_user_row("testuser", OWNER_ID)}, profile_pictures={})
    assert is_entitled_to_asset(
        db, kind="profile", object_path=OWNED_PATH, user=user_details("testuser", OWNER_ID)
    ) is False


def test_profile_denied_for_superseded_upload():
    """An object the owner has since replaced is no longer a published profile picture.

    Both paths sit under the same ``regno`` prefix, so the row decides which one is live.
    """
    db = profile_db()  # testuser's current picture is OWNED_PATH
    stale = "testuser/ZzYyXxWwVvUuTtSsRrQqPp/20230101_120000_000000_photo.jpg"
    assert is_entitled_to_asset(
        db, kind="profile", object_path=stale, user=user_details("stranger", STRANGER_ID)
    ) is False
    assert is_entitled_to_asset(
        db, kind="profile", object_path=OWNED_PATH, user=user_details("stranger", STRANGER_ID)
    ) is True


# ----------------------------------------------------------------------
# Entitlement: love notes (admin approval gate)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("status", ["pending_review", "rejected"])
def test_recipient_blocked_until_approved(status):
    db = love_note_db(status, "recipient", RECIPIENT_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=NOTE_PATH, user=user_details("recipient", RECIPIENT_ID)
    ) is False


def test_recipient_allowed_once_approved():
    db = love_note_db("approved", "recipient", RECIPIENT_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=NOTE_PATH, user=user_details("recipient", RECIPIENT_ID)
    ) is True


@pytest.mark.parametrize("status", ["pending_review", "approved", "rejected"])
def test_sender_always_allowed(status):
    db = love_note_db(status, "sender", OWNER_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=NOTE_PATH, user=user_details("sender", OWNER_ID)
    ) is True


@pytest.mark.parametrize("status", ["pending_review", "rejected"])
def test_admin_allowed_to_moderate_unapproved_note(status):
    db = love_note_db(status, "admin", STRANGER_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=NOTE_PATH, user=user_details("admin", STRANGER_ID, role="admin")
    ) is True


def test_unrelated_user_blocked_even_for_approved_note():
    db = love_note_db("approved", "stranger", STRANGER_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=NOTE_PATH, user=user_details("stranger", STRANGER_ID)
    ) is False


def test_unrelated_note_image_not_served():
    db = love_note_db("approved", "recipient", RECIPIENT_ID)
    assert is_entitled_to_asset(
        db, kind="love-notes", object_path=OTHER_PATH, user=user_details("recipient", RECIPIENT_ID)
    ) is False


def test_unknown_kind_is_never_entitled():
    assert is_entitled_to_asset(
        profile_db(), kind="events", object_path=OWNED_PATH, user=user_details("testuser", OWNER_ID)
    ) is False


# ----------------------------------------------------------------------
# Delivery URL construction
# ----------------------------------------------------------------------
def test_build_delivery_url_points_at_proxy_and_hides_origin():
    url = build_delivery_url(
        kind="profile", stored_identifier=stored_url("profile", OWNED_PATH), regno="testuser"
    )
    assert "/api/profile/files/" in url
    assert "?t=" in url
    assert "res.cloudinary.com" not in url


def test_build_delivery_url_for_love_notes():
    url = build_delivery_url(
        kind="love-notes", stored_identifier=stored_url("love-notes", NOTE_PATH), regno="recipient"
    )
    assert "/api/love-notes/files/" in url
    assert "res.cloudinary.com" not in url


@pytest.mark.parametrize("stored", [None, "", "https://placehold.co/128x128", "data:image/png;base64,AAAA"])
def test_build_delivery_url_declines_non_asset_values(stored):
    """Admin-entered and placeholder values are not servable, so callers keep them as-is."""
    assert build_delivery_url(kind="profile", stored_identifier=stored, regno="testuser") is None


# ----------------------------------------------------------------------
# Unified profile delivery (matchmaking / conversations / love note picker)
# ----------------------------------------------------------------------
def test_profile_image_url_rewrites_a_servable_profile_object():
    url = asset_delivery_service.profile_image_url(
        stored_url("profile", OWNED_PATH), regno="testuser"
    )
    assert "/api/profile/files/" in url
    assert "res.cloudinary.com" not in url


@pytest.mark.parametrize("stored", [None, "https://placehold.co/128x128", "data:image/png;base64,AAAA"])
def test_profile_image_url_passes_through_non_asset_values(stored):
    """Placeholders and data URLs are not proxyable, so the stored value is kept."""
    assert asset_delivery_service.profile_image_url(stored, regno="testuser") == stored


def test_profile_image_url_normalises_empty_to_none():
    """Matches the helper this replaced, which treats any falsy value as no picture."""
    assert asset_delivery_service.profile_image_url("", regno="testuser") is None


def test_profile_delivery_token_is_bound_to_the_requester_not_the_owner():
    """A viewer's link must not be replayable by a different account.

    The picture belongs to ``testuser`` but the link is minted for ``stranger``, because
    ``stranger`` is the one who will present it. The token therefore has to carry the
    requester's identity, otherwise anyone could reuse a link copied out of a payload.
    """
    url = asset_delivery_service.profile_image_url(
        stored_url("profile", OWNED_PATH), regno="stranger"
    )
    token = url.split("?t=", 1)[1]
    assert verify_asset_token(token, kind="profile", object_path=OWNED_PATH) == "stranger"


def test_viewer_can_load_a_profile_picture_minted_for_them(serve):
    """End to end: the viewer presents their own link and gets the bytes."""
    db = _FakeDatabase(
        users={"stranger": make_user_row("stranger", STRANGER_ID)},
        profile_pictures={"testuser": stored_url("profile", OWNED_PATH)},
    )
    client = serve(db)
    url = asset_delivery_service.profile_image_url(
        stored_url("profile", OWNED_PATH), regno="stranger"
    )
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nfake"


# ----------------------------------------------------------------------
# Route behaviour
# ----------------------------------------------------------------------
@pytest.mark.parametrize("route", FILE_ROUTES)
def test_asset_routes_require_credentials(anonymous_client, route):
    path = OWNED_PATH if route == "profile" else NOTE_PATH
    assert anonymous_client.get(f"/api/{route}/files/{path}").status_code == 401


def test_profile_route_streams_owned_asset(serve):
    client = serve(profile_db())
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    response = client.get(f"/api/profile/files/{OWNED_PATH}?t={token}")
    assert response.status_code == 200
    assert response.content == b"\x89PNG\r\n\x1a\nfake"
    assert response.headers["cache-control"].startswith("private")
    assert response.headers["x-content-type-options"] == "nosniff"


def test_profile_route_serves_another_account(serve):
    """A signed-in account may load a profile image it does not own."""
    client = serve(profile_db(actor_regno="stranger", actor_id=STRANGER_ID))
    token = mint_asset_token(regno="stranger", kind="profile", object_path=OWNED_PATH)
    assert client.get(f"/api/profile/files/{OWNED_PATH}?t={token}").status_code == 200


def test_profile_route_still_rejects_an_unpublished_object(serve):
    """Relaxing ownership must not turn the proxy into a reader for the whole bucket."""
    db = _FakeDatabase(
        users={"stranger": make_user_row("stranger", STRANGER_ID)}, profile_pictures={}
    )
    client = serve(db)
    token = mint_asset_token(regno="stranger", kind="profile", object_path=OWNED_PATH)
    assert client.get(f"/api/profile/files/{OWNED_PATH}?t={token}").status_code == 404


def test_profile_route_rejects_token_minted_for_another_path(serve):
    client = serve(profile_db())
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OTHER_PATH)
    assert client.get(f"/api/profile/files/{OWNED_PATH}?t={token}").status_code == 401


def test_profile_route_rejects_double_encoded_traversal(serve):
    """The client normalises "../", so the double-encoded form is the one that reaches us."""
    client = serve(profile_db())
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    response = client.get(f"/api/profile/files/%252e%252e%252f%252e%252e%252fsecret%252fphoto.jpg?t={token}")
    assert response.status_code in (400, 404)


def test_love_note_route_blocks_recipient_before_approval(serve):
    client = serve(love_note_db("pending_review", "recipient", RECIPIENT_ID))
    token = mint_asset_token(regno="recipient", kind="love-notes", object_path=NOTE_PATH)
    assert client.get(f"/api/love-notes/files/{NOTE_PATH}?t={token}").status_code == 404


def test_love_note_route_serves_recipient_after_approval(serve):
    client = serve(love_note_db("approved", "recipient", RECIPIENT_ID))
    token = mint_asset_token(regno="recipient", kind="love-notes", object_path=NOTE_PATH)
    assert client.get(f"/api/love-notes/files/{NOTE_PATH}?t={token}").status_code == 200


def test_love_note_route_blocks_unrelated_user(serve):
    client = serve(love_note_db("approved", "stranger", STRANGER_ID))
    token = mint_asset_token(regno="stranger", kind="love-notes", object_path=NOTE_PATH)
    assert client.get(f"/api/love-notes/files/{NOTE_PATH}?t={token}").status_code == 404


def test_revoking_a_note_stops_an_already_issued_token(serve):
    """Entitlement is re-checked per request, so a status flip takes effect immediately."""
    db = love_note_db("approved", "recipient", RECIPIENT_ID)
    client = serve(db)
    token = mint_asset_token(regno="recipient", kind="love-notes", object_path=NOTE_PATH)
    assert client.get(f"/api/love-notes/files/{NOTE_PATH}?t={token}").status_code == 200

    db.love_note_rows = [(OWNER_ID, RECIPIENT_ID, stored_url("love-notes", NOTE_PATH), "rejected")]
    assert client.get(f"/api/love-notes/files/{NOTE_PATH}?t={token}").status_code == 404


def test_origin_failure_is_reported_as_404(serve, monkeypatch):
    def _boom(_url):
        raise HTTPException(status_code=404, detail="File not found")

    monkeypatch.setattr(asset_delivery_service, "fetch_asset_bytes", _boom)
    client = serve(profile_db())
    token = mint_asset_token(regno="testuser", kind="profile", object_path=OWNED_PATH)
    assert client.get(f"/api/profile/files/{OWNED_PATH}?t={token}").status_code == 404


# ----------------------------------------------------------------------
# Origin content type
# ----------------------------------------------------------------------
class _FakeOriginResponse:
    def __init__(self, payload, content_type):
        self._payload = payload
        self.headers = {"Content-Type": content_type}

    def read(self, size):
        return self._payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_fetch_refuses_non_image_content_from_origin(monkeypatch):
    """A proxy that only ever serves images must not relay an active content type."""
    monkeypatch.setattr(
        asset_delivery_service.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeOriginResponse(b"<html>hi</html>", "text/html; charset=utf-8"),
    )
    with pytest.raises(HTTPException) as exc_info:
        _REAL_FETCH_ASSET_BYTES("https://res.cloudinary.com/x/image/upload/a/b.jpg")
    assert exc_info.value.status_code == 404


def test_fetch_allows_image_content_from_origin(monkeypatch):
    monkeypatch.setattr(
        asset_delivery_service.urllib.request,
        "urlopen",
        lambda *a, **k: _FakeOriginResponse(b"\xff\xd8\xffbinary", "image/jpeg"),
    )
    payload, content_type = _REAL_FETCH_ASSET_BYTES(
        "https://res.cloudinary.com/x/image/upload/a/b.jpg"
    )
    assert content_type == "image/jpeg"
    assert payload == b"\xff\xd8\xffbinary"


# ----------------------------------------------------------------------
# Removed surfaces
# ----------------------------------------------------------------------
@pytest.mark.parametrize("route", ["events", "gallery", "committee"])
def test_unbacked_asset_routes_are_removed(route):
    assert f"/api/{route}/files/{{path:path}}" not in {getattr(r, "path", "") for r in app.routes}


def test_db_test_is_not_registered_by_default():
    assert "/db-test" not in {getattr(r, "path", "") for r in app.routes}
