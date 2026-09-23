"""Every admin route is gated by exactly one capability, and moderators get the right subset.

Rather than hardcoding a list of "moderator may" paths, these tests read the capability each
route declares from its own dependency graph. That way a route cannot be quietly added without
a gate, a gate cannot be quietly moved to a different capability, and the moderator split
cannot drift from :mod:`app.accounts` without a failure here.

The behavioural half then confirms the declared gate is what actually runs: a moderator is
refused on the admin-only routes and is not refused on the moderation ones, and a plain user
is refused everywhere.
"""

import re

import pytest

from app.main import app
from app.accounts import ADMIN_ONLY_CAPABILITIES, MODERATION_CAPABILITIES, Capability
from app.services.authorization import FORBIDDEN_DETAIL

# The dependency is named after the capability by require_capability, which makes the name a
# faithful reverse index from the route graph back to the enum.
DEPENDENCY_NAMES = {f"require_{capability.value.replace(':', '_')}": capability for capability in Capability}


def _declared_capability(route):
    """The single capability a route requires, or ``None`` if it declares none."""
    found = []
    for dependency in route.dependant.dependencies:
        name = getattr(dependency.call, "__name__", "")
        if name in DEPENDENCY_NAMES:
            found.append(DEPENDENCY_NAMES[name])
    return found


def _admin_routes():
    return [route for route in app.routes if getattr(route, "path", "").startswith("/admin")]


ADMIN_ROUTES = _admin_routes()

# A representative method and path template for each gated operation. These are
# the templates as declared, so a renamed route cannot slip past unnoticed.
SAMPLE_ROUTES = [
    ("GET", "/admin/confessions"),
    ("DELETE", "/admin/confessions/{confession_id}"),
    ("PATCH", "/admin/confessions/{confession_id}/hide"),
    ("PATCH", "/admin/comments/{comment_id}/hide"),
    ("GET", "/admin/confessions/reports"),
("PATCH", "/admin/confessions/reports/{report_id}"),
    ("GET", "/admin/users"),
    ("POST", "/admin/users"),
    ("PUT", "/admin/users/{user_id}/block"),
    ("PUT", "/admin/users/{user_id}"),
    # ``PUT /admin/users/{user_id}/picture`` used to be here. It was the admin's way to set an
    # account's profile picture, and it is gone: an admin mints an identity, and everything about
    # the profile itself -- the picture included -- is the account's to set from
    # ``POST /profile/upload-picture``. Its absence from this list is the assertion. The route
    # inventory is an exact match, so putting it back would fail ``test_every_admin_route_is_
    # discovered`` rather than passing quietly.
    ("GET", "/admin/stats"),
    # ``GET /admin/stats/active-sessions`` used to be here. It listed every account live in
    # the last 24 hours with their last login, browser and IP, and the dashboard's "Active
    # Sessions" card was the only thing that opened it. Both are gone: the card now reads
    # "Active Today" and shows the count alone, so there is nothing left to list a session
    # for, and ``sessions:read`` left the enum with it. The route inventory is an exact
    # match, so putting it back would fail ``test_every_admin_route_is_discovered`` rather
    # than pass quietly.
    ("GET", "/admin/config"),
    ("POST", "/admin/config/matchmaking-slot"),
    ("DELETE", "/admin/config/matchmaking-slot"),
    ("POST", "/admin/messages/archive"),
    ("GET", "/admin/messages/archives"),
    ("GET", "/admin/messages/archives/{date}"),
    ("GET", "/admin/messages/reports"),
    ("PATCH", "/admin/messages/reports/{report_id}"),
("GET", "/admin/love-notes"),
("PUT", "/admin/love-notes/{note_id}/status"),
("DELETE", "/admin/love-notes/{note_id}"),
("GET", "/admin/love-notes/reports"),
("PATCH", "/admin/love-notes/reports/{report_id}"),
    # Wordle configuration. ``GET /admin/wordle/preview`` is declared ahead of
    # ``GET /admin/wordle/{game_id}`` in the router so the word "preview" is never read as an
    # id, and the four together are the whole feature: draw, save, list, read one. Each is
    # gated on ``games:manage``, which is in ``ADMIN_ONLY_CAPABILITIES`` -- publishing a
    # puzzle is not a moderation action, so a moderator is refused on all four.
    ("GET", "/admin/wordle/preview"),
    ("POST", "/admin/wordle"),
    ("GET", "/admin/wordle"),
    ("GET", "/admin/wordle/{game_id}"),
    # Anagram configuration. Four routes like Wordle's four: the preview is declared ahead
    # of ``/{game_id}`` in the router so the word "preview" is never read as an id, and all
    # four are gated on ``games:manage`` for the same reason Wordle's are.
    ("GET", "/admin/anagram/preview"),
    ("POST", "/admin/anagram"),
    ("GET", "/admin/anagram"),
    ("GET", "/admin/anagram/{game_id}"),
]


def _concrete(path_template):
    """Fill the path parameters in, so the request reaches the handler.

    The value is deliberately nonsense: these tests are asserting on the authorisation
    layer, and a handler that dislikes the id is allowed to complain about it afterwards.
    """
    return re.sub(r"\{[^}]+\}", "1", path_template)


def _route_for(method, path_template):
    for route in ADMIN_ROUTES:
        if route.path == path_template and method in route.methods:
            return route
    raise AssertionError(f"no admin route for {method} {path_template}")


def test_every_admin_route_is_discovered():
    """Keeps the sample list honest: a renamed or removed route must fail loudly."""
    found = {(method, route.path) for route in ADMIN_ROUTES for method in route.methods - {"HEAD"}}
    expected = set(SAMPLE_ROUTES)
    assert expected <= found, f"missing from the app: {expected - found}"
    assert found == expected, f"ungathered admin routes: {found - expected}"


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=lambda route: f"{sorted(route.methods)[0]} {route.path}")
def test_admin_route_declares_exactly_one_capability(route):
    """The core invariant. Zero means an ungated route; two means an ambiguous one."""
    declared = _declared_capability(route)
    assert len(declared) == 1, f"{route.path} declares {declared or 'no capability'}"


def test_every_capability_is_guarded_by_at_least_one_route():
    """A capability nobody can exercise is either dead or a mistake in the enum."""
    guarded = {capability for route in ADMIN_ROUTES for capability in _declared_capability(route)}
    assert guarded == set(Capability), f"unreachable: {set(Capability) - guarded}"


@pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
def test_moderator_alignment_matches_the_declared_capability(method, path):
    """Moderators are allowed exactly where the declared capability is a moderation one."""
    capability = _declared_capability(_route_for(method, path))[0]
    moderator_allowed = capability.value in MODERATION_CAPABILITIES
    admin_only = capability.value in ADMIN_ONLY_CAPABILITIES
    assert moderator_allowed != admin_only, f"{capability} is in both sets or neither"


@pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
def test_moderator_is_refused_on_admin_only_routes(moderator_client, method, path):
    """Live check of the admin-only boundary.

    The handler may still reject the request for want of data, but it must never be the
    authorisation layer that lets it through, so only 403 is a failure here.
    """
    capability = _declared_capability(_route_for(method, path))[0]
    if capability.value in ADMIN_ONLY_CAPABILITIES:
        response = moderator_client.request(method, _concrete(path), json={})
        assert response.status_code == 403, f"{method} {path} let a moderator through"
        assert response.json()["detail"] == FORBIDDEN_DETAIL


@pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
def test_moderator_gets_past_the_gate_on_moderation_routes(moderator_client, method, path):
    """A 403 here would mean a moderator cannot do the job they exist to do."""
    capability = _declared_capability(_route_for(method, path))[0]
    if capability.value in MODERATION_CAPABILITIES:
        response = moderator_client.request(method, _concrete(path), json={})
        assert response.status_code != 403, f"{method} {path} refused a moderator"


@pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
def test_plain_user_is_refused_everywhere(client, method, path):
    """An unprivileged account gets nothing, including on the routes moderators can reach."""
    response = client.request(method, _concrete(path), json={})
    assert response.status_code == 403, f"{method} {path} is reachable by a plain user"
    assert response.json()["detail"] == FORBIDDEN_DETAIL


@pytest.mark.parametrize("method,path", SAMPLE_ROUTES)
def test_admin_is_never_refused(admin_client, method, path):
    """Admin holds every capability, so a 403 means the gate is wrong, not the caller."""
    response = admin_client.request(method, _concrete(path), json={})
    assert response.status_code != 403, f"{method} {path} refused an admin"
