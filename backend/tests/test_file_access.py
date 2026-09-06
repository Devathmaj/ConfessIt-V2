"""Tests for the authenticated file-delivery routes and the gated /db-test endpoint."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.dependencies import get_db
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user
from app.services.storage_service import storage_service


# A path shaped exactly like the ones storage_service.generate_object_path() produces.
OWNED_PATH = "testuser/AbCdEfGhIjKlMnOpQrStUv/20240101_120000_000000_photo.jpg"

# Two shapes are stored in users.profile_picture_url: a full Cloudinary secure_url
# (current uploads) and a bare object path (legacy rows).
OWNED_URL = f"https://res.cloudinary.com/demo/image/upload/v1700000000/profile_pics/{OWNED_PATH}"
OWNED_LEGACY_PATH = OWNED_PATH

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

FILE_ROUTES = ["profile", "events", "gallery", "committee"]


class _FakeSession:
    def __init__(self, value):
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def scalar(self, *args, **kwargs):
        return self._value


class _FakeDatabase:
    """Minimal stand-in exposing the stored profile_picture_url for one user."""

    def __init__(self, profile_picture_url):
        self._profile_picture_url = profile_picture_url

    def sessions(self):
        return _FakeSession(self._profile_picture_url)


@pytest.fixture
def owned_profile_user(monkeypatch):
    """Authenticate as a user whose stored asset is OWNED_PATH."""
    monkeypatch.setitem(
        app.dependency_overrides,
        get_current_user,
        lambda: UserDetails(
            id="11111111-1111-1111-1111-111111111111",
            regno="testuser",
            name="Test User",
            email="test@user.com",
            class_name="Test_Class",
            gender="male",
            role="user",
            matchmaking_enabled=True,
            notifications_enabled=True,
            profile_picture_url=OWNED_URL,
        ),
    )


@pytest.fixture
def stored_asset(monkeypatch):
    """Point the profile-ownership lookup at a specific stored URL."""

    def _apply(profile_picture_url):
        monkeypatch.setitem(app.dependency_overrides, get_db, lambda: _FakeDatabase(profile_picture_url))

    return _apply


@pytest.fixture
def stub_delivery(monkeypatch):
    """Stub Cloudinary delivery so a 307 proves the request passed validation + ownership."""
    monkeypatch.setattr(
        storage_service,
        "generate_temporary_view_url",
        lambda route_key, object_path: f"https://res.cloudinary.com/demo/{route_key}/{object_path}",
    )


@pytest.fixture
def anonymous_client():
    """Client with authentication genuinely disabled, so real 401 behaviour is exercised.

    conftest.py installs a global get_current_user override, so it is removed here and
    restored afterwards to keep this independent of the shared fixture setup. The client
    is used without a context manager so the app's startup event (which needs a live
    database) is not triggered.
    """
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved


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


def test_generate_temporary_view_url_rejects_traversal(monkeypatch):
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    monkeypatch.setattr(storage_service, "can_sign", True, raising=False)
    with pytest.raises(HTTPException) as exc_info:
        storage_service.generate_temporary_view_url("profile", "../../events/secret/photo.jpg")
    assert exc_info.value.status_code == 400


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------
@pytest.mark.parametrize("route", FILE_ROUTES)
def test_file_routes_require_authentication(anonymous_client, route):
    response = anonymous_client.get(f"/api/{route}/files/{OWNED_PATH}", follow_redirects=False)
    assert response.status_code == 401


# ----------------------------------------------------------------------
# Ownership (profile)
# ----------------------------------------------------------------------
def test_profile_route_allows_owned_asset(owned_profile_user, stored_asset, stub_delivery):
    stored_asset(OWNED_URL)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/api/profile/files/{OWNED_PATH}")

    assert response.status_code == 307
    assert OWNED_PATH in response.headers["location"]


def test_profile_route_rejects_asset_owned_by_someone_else(owned_profile_user, stored_asset):
    other_path = "someoneelse/ZzYyXxWwVvUuTtSsRrQqPp/20240101_120000_000000_photo.jpg"
    stored_asset(
        f"https://res.cloudinary.com/demo/image/upload/v1700000000/profile_pics/{other_path}"
    )

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/api/profile/files/{OWNED_PATH}")

    assert response.status_code == 404


def test_profile_route_accepts_legacy_bare_object_path(owned_profile_user, stored_asset, stub_delivery):
    stored_asset(OWNED_LEGACY_PATH)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/api/profile/files/{OWNED_PATH}")

    assert response.status_code == 307
    assert OWNED_PATH in response.headers["location"]


def test_profile_route_rejects_traversal_even_when_authenticated(owned_profile_user, stored_asset):
    stored_asset(OWNED_URL)

    client = TestClient(app, follow_redirects=False)
    response = client.get("/api/profile/files/../../events/AbCdEfGhIjKlMnOpQrStUv/x.jpg")

    assert response.status_code in (400, 404)


def test_profile_route_rejects_when_user_has_no_stored_asset(owned_profile_user, stored_asset):
    stored_asset(None)

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/api/profile/files/{OWNED_PATH}")

    assert response.status_code == 404


def test_profile_route_tolerates_non_ascii_stored_value(owned_profile_user, stored_asset):
    # Legacy rows can hold arbitrary text; this must fail closed, not raise a 500.
    stored_asset("prof\u00efle_pics/\u2603/photo.jpg")

    client = TestClient(app, follow_redirects=False)
    response = client.get(f"/api/profile/files/{OWNED_PATH}")

    assert response.status_code == 404


# ----------------------------------------------------------------------
# /db-test gating
# ----------------------------------------------------------------------
def test_db_test_is_not_registered_by_default():
    assert "/db-test" not in {route.path for route in app.routes}
