"""Route contract for the PostgreSQL-backed API.

This file used to be a smoke test written before the data layer was migrated: it requested
route names the service no longer registers, and accepted any of 200/404/405/422/401 for
each one. It could therefore not fail -- it was pinning the absence of the API rather than
its behaviour, and it hid the fact that the client and the server had drifted apart.

Every route named below is one the service actually registers, and each test pins the exact
status that route returns. The last test closes the loop by checking the paths the frontend
calls against the paths the backend registers, so a rename cannot land on one side only.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.auth_service import get_current_user

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_API_MODULE = BACKEND_DIR.parent / "frontend" / "src" / "services" / "api.ts"

# Readable without credentials. The confession feed is the app's public surface, so these
# must keep answering to a signed-out visitor.
PUBLIC_READ_ROUTES = [
    "/confessions",
    "/confessions/total_count",
]

# Readable only by a signed-in account. Each is a real path, so a rename shows up here as
# a 404 rather than as a pass.
SIGNED_IN_READ_ROUTES = [
    "/auth/me",
    "/matchmaking/check",
    "/love-notes/users",
    "/love-notes/cohorts",
    "/love-notes/inbox",
    "/conversations/current",
    "/conversations/received",
    "/notifications",
]

# Closed to a normal account, open to an admin one.
ADMIN_READ_ROUTES = [
    "/admin/users",
    "/admin/confessions",
    "/admin/confessions/reports",
    "/admin/love-notes",
    "/admin/love-notes/reports",
    "/admin/messages/reports",
    "/admin/stats",
    "/admin/wordle",
    "/admin/wordle/preview",
    "/admin/wordle/scheduled",
    "/admin/anagram",
    "/admin/anagram/preview",
    "/admin/anagram/scheduled",
    "/admin/couple",
    "/admin/games/config",
    "/admin/featured",
]


@pytest.fixture
def anonymous_client():
    """A client with the auth dependency override removed, to exercise the real 401 path."""
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


def _normalise_path(path: str) -> str:
    """Reduce a path to its shape, so ``/comments/{id}/like`` and ``/comments/{}`` match.

    The two sides name and interpolate their parameters independently (FastAPI uses the
    route function's own parameter name, the frontend a JavaScript variable), and that
    naming is not part of the contract -- only the position of the segment and the verb.
    """
    path = re.sub(r"\$\{[^}]*\}", "{}", path)
    segments = [segment for segment in re.sub(r"\{[^}]*\}", "{}", path).split("/") if segment]
    return "/" + "/".join(segments)


def registered_routes() -> set[tuple[str, str]]:
    """(method, normalised path) pairs the app actually exposes."""
    found = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        for method in getattr(route, "methods", None) or ():
            found.add((method, _normalise_path(path)))
    return found


def frontend_api_calls() -> set[tuple[str, str]]:
    """(method, normalised path) pairs the frontend's API client issues.

    Parsed out of ``api.ts`` rather than restated here, so this stays true as the client
    changes and does not become a second list somebody has to update by hand.
    """
    source = FRONTEND_API_MODULE.read_text(encoding="utf-8")
    calls = set()
    pattern = r"axios\.(get|post|put|patch|delete)\(\s*`\$\{API_URL\}(/[^`]*)`"
    for method, raw_path in re.findall(pattern, source):
        # Any query string is not part of the route the backend registers.
        calls.add((method.upper(), _normalise_path(raw_path.split("?", 1)[0])))
    return calls


def test_profile_update_requires_authentication(client: TestClient):
    # Remove the dependency override for this specific test, then put it back. Leaving it
    # removed silently makes every test that runs later an unauthenticated one.
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.put("/profile/update", json={"bio": "New bio"})
        assert response.status_code == 401
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


@pytest.mark.parametrize("path", PUBLIC_READ_ROUTES + SIGNED_IN_READ_ROUTES)
def test_a_signed_in_account_can_read_its_own_data(client: TestClient, path):
    """Exact status, not a permissive set: these routes exist and answer on empty tables."""
    response = client.get(path)

    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"


@pytest.mark.parametrize("path", PUBLIC_READ_ROUTES)
def test_the_confession_feed_stays_public(anonymous_client: TestClient, path):
    response = anonymous_client.get(path)

    assert response.status_code == 200, f"{path} answered {response.status_code} to a signed-out visitor"


@pytest.mark.parametrize("path", SIGNED_IN_READ_ROUTES)
def test_unauthenticated_requests_are_refused(anonymous_client: TestClient, path):
    response = anonymous_client.get(path)

    assert response.status_code == 401, f"{path} answered {response.status_code} without credentials"


@pytest.mark.parametrize("path", ADMIN_READ_ROUTES)
def test_admin_routes_are_closed_to_a_normal_account(client: TestClient, path):
    response = client.get(path)

    assert response.status_code == 403, f"{path} answered {response.status_code} to a non-admin"


@pytest.mark.parametrize("path", ADMIN_READ_ROUTES)
def test_admin_routes_answer_for_an_admin(admin_client: TestClient, path):
    response = admin_client.get(path)

    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text}"


@pytest.mark.skipif(
    not FRONTEND_API_MODULE.is_file(),
    reason="frontend sources are not present in this checkout (e.g. the backend image mounts only ./backend)",
)
def test_frontend_only_calls_routes_the_backend_registers():
    """A path the frontend calls must exist on the backend, and for the same verb.

    Both sides carry their own copy of every URL, so renaming a route on one side stays
    silent until a user hits it. The direction that matters is frontend -> backend: a call
    the frontend makes that the backend does not serve is a 404 in production, and the
    permissive status assertions this file used to carry are what let such a mismatch go
    unnoticed.
    """
    registered = registered_routes()
    missing = sorted(
        f"{method} {path}"
        for method, path in frontend_api_calls()
        if (method, path) not in registered
    )

    assert not missing, f"frontend calls routes the backend does not register: {missing}"
