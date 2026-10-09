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
from types import SimpleNamespace

import pytest

from app.main import app
from app.accounts import ADMIN_ONLY_CAPABILITIES, MODERATION_CAPABILITIES, Capability
from app.services.authorization import FORBIDDEN_DETAIL

# The dependency is named after the capability by require_capability, which makes the name a
# faithful reverse index from the route graph back to the enum.
DEPENDENCY_NAMES = {f"require_{capability.value.replace(':', '_')}": capability for capability in Capability}

# A syntactically valid but certainly absent UUID, used to fill path parameters
# so a request reaches the authorisation layer without tripping a database cast.
_SAMPLE_UUID = "00000000-0000-0000-0000-000000000000"


def _declared_capability(route):
    """The single capability a route requires, or ``None`` if it declares none."""
    found = []
    for dependency in route.dependant.dependencies:
        name = getattr(dependency.call, "__name__", "")
        if name in DEPENDENCY_NAMES:
            found.append(DEPENDENCY_NAMES[name])
    return found


def _iter_routes(router, prefix=""):
    """Yield ``(full_path, route)`` for every leaf route under ``router``.

    ``include_router`` keeps each included router as a nested node on
    ``app.routes`` instead of copying its routes onto the parent, so a single
    pass over ``app.routes`` no longer reaches the leaves. Walk the tree and
    fold each include prefix into the path so the leaves look exactly like the
    flat list the assertions were written against.
    """
    for route in getattr(router, "routes", []):
        if type(route).__name__ == "_IncludedRouter":
            context = getattr(route, "include_context", None)
            nested_prefix = prefix + (getattr(context, "prefix", "") or "")
            yield from _iter_routes(route.original_router, nested_prefix)
        else:
            yield prefix, route


def _admin_routes():
    collected = []
    for prefix, route in _iter_routes(app):
        path = prefix + getattr(route, "path", "")
        if path.startswith("/admin"):
            collected.append(
                SimpleNamespace(
                    path=path,
                    methods=getattr(route, "methods", None) or set(),
                    dependant=route.dependant,
                )
            )
    return collected


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
    ("GET", "/admin/messages/reports"),
    ("PATCH", "/admin/messages/reports/{report_id}"),
("GET", "/admin/love-notes"),
("PUT", "/admin/love-notes/{note_id}/status"),
("DELETE", "/admin/love-notes/{note_id}"),
    # Regenerating a note's missing moderation preview is part of review, not a
    # ruling on the note, so it carries the review capability like the list does.
    ("POST", "/admin/love-notes/{note_id}/redact"),
("GET", "/admin/love-notes/reports"),
("PATCH", "/admin/love-notes/reports/{report_id}"),
    # Wordle sessions. ``POST /admin/wordle`` is the Update action (end the current
    # session, open a new one); ``/current`` reads the live session, and
    # ``/schedule`` + ``/scheduled`` own the horizon-bounded future words. The
    # static paths are declared ahead of ``/{game_id}`` so none is read as an
    # id. All are gated on ``games:manage``, which is in
    # ``ADMIN_ONLY_CAPABILITIES`` -- publishing a puzzle is not a moderation
    # action, so a moderator is refused on every one.
    ("GET", "/admin/wordle/preview"),
    ("POST", "/admin/wordle"),
    ("GET", "/admin/wordle"),
    ("GET", "/admin/wordle/current"),
    ("POST", "/admin/wordle/schedule"),
    ("GET", "/admin/wordle/scheduled"),
    ("DELETE", "/admin/wordle/scheduled/{target_date}"),
    ("GET", "/admin/wordle/{game_id}"),
    # Anagram sessions. The same surface as Wordle's: the preview is declared
    # ahead of ``/{game_id}`` so no static path is read as an id, and all are
    # gated on ``games:manage`` for the same reason Wordle's are.
    ("GET", "/admin/anagram/preview"),
    ("POST", "/admin/anagram"),
    ("GET", "/admin/anagram"),
    ("GET", "/admin/anagram/current"),
    ("POST", "/admin/anagram/schedule"),
    ("GET", "/admin/anagram/scheduled"),
    ("DELETE", "/admin/anagram/scheduled/{target_date}"),
    ("GET", "/admin/anagram/{game_id}"),
    # Connections sessions. The same surface as Wordle's minus the preview: the
    # static paths are declared ahead of ``/{game_id}`` so none is read as an id,
    # and all are gated on ``games:manage`` for the same reason Wordle's are.
    ("GET", "/admin/connections"),
    ("POST", "/admin/connections"),
    ("GET", "/admin/connections/current"),
    ("POST", "/admin/connections/schedule"),
    ("GET", "/admin/connections/scheduled"),
    ("DELETE", "/admin/connections/scheduled/{target_date}"),
    ("GET", "/admin/connections/{game_id}"),
    # Trivia Drop questions. Publishing a question is not a moderation action:
    # creating, editing, deploying, scheduling, rescheduling and cancelling are
    # all gated on ``games:manage``, and the static paths are declared ahead of
    # ``/{question_id}`` so none is read as an id.
    ("POST", "/admin/trivia-drop"),
    ("GET", "/admin/trivia-drop"),
    ("GET", "/admin/trivia-drop/current"),
    ("GET", "/admin/trivia-drop/{question_id}"),
    ("PUT", "/admin/trivia-drop/{question_id}"),
    ("POST", "/admin/trivia-drop/{question_id}/deploy"),
    ("POST", "/admin/trivia-drop/{question_id}/schedule"),
    ("POST", "/admin/trivia-drop/{question_id}/reschedule"),
    ("POST", "/admin/trivia-drop/{question_id}/cancel"),
    # Couple Prediction. Featuring a day only pins the spotlight (the sessions
    # roll on their own cadence with no Update action), and ``/session`` reads
    # the live global session. All are gated on ``games:manage`` for the same
    # reason the puzzles are.
    ("POST", "/admin/couple"),
    ("GET", "/admin/couple"),
    ("GET", "/admin/couple/session"),
    ("GET", "/admin/couple/{game_id}"),
    # Reaction Time. Featuring a day stores the day and nothing else -- the
    # mechanics are fixed by the application, so there is no preview,
    # schedule or session read. All are gated on ``games:manage`` for the
    # same reason the other games are.
    ("POST", "/admin/reaction-time"),
    ("GET", "/admin/reaction-time"),
    ("GET", "/admin/reaction-time/session"),
    ("GET", "/admin/reaction-time/{game_id}"),
    # League leaderboards for the games console. Reads of the individual, couple
    # and per-game boards plus the summary, and the finalize/reprocess repair
    # paths the scheduler does not run on its own -- all gated on
    # ``games:manage``, since awarding and correcting league results is not a
    # moderation action.
    ("GET", "/admin/leaderboards/summary"),
    ("GET", "/admin/leaderboards/individual"),
    ("GET", "/admin/leaderboards/couples"),
    ("GET", "/admin/leaderboards/games/{game_id}"),
    ("POST", "/admin/leaderboards/finalize"),
    ("POST", "/admin/leaderboards/reprocess"),
    # Game configuration, the featured spotlight, and the manual reset trigger.
    # Configuration owns per-game availability and cadence, the featured
    # schedule pins at most one game per UTC date without touching sessions,
    # and ``run-resets`` is what a 00:01 UTC cron calls. All gated on
    # ``games:manage`` like the per-game routers.
    ("GET", "/admin/games/config"),
    ("PUT", "/admin/games/config/{game_id}"),
    ("GET", "/admin/featured"),
    ("POST", "/admin/featured"),
    ("PUT", "/admin/featured"),
    ("POST", "/admin/featured/range"),
    ("PUT", "/admin/featured/range"),
    ("DELETE", "/admin/featured/{game_date}"),
    ("POST", "/admin/games/run-resets"),
    # Confession search and the un-hide repair paths. Search is a confessions
    # review read; restoring a hidden confession or comment is a delete-action
    # reversal, and restoring an Azure-hidden chat message is a moderation act.
    ("GET", "/admin/confessions/search"),
    ("PATCH", "/admin/confessions/{confession_id}/unhide"),
    ("PATCH", "/admin/comments/{comment_id}/unhide"),
    ("PATCH", "/admin/messages/{message_id}/unhide"),
    # Case Closed content studio. Blueprints and packages are authored material,
    # so every read and write on the surface is gated on ``games:manage`` like
    # the other game consoles.
    ("GET", "/admin/case-closed/blueprints"),
    ("POST", "/admin/case-closed/blueprints"),
    ("GET", "/admin/case-closed/blueprints/{blueprint_id}"),
    ("POST", "/admin/case-closed/blueprints/{blueprint_id}/generate"),
    ("POST", "/admin/case-closed/blueprints/{blueprint_id}/approve"),
    ("POST", "/admin/case-closed/blueprints/{blueprint_id}/packages"),
    ("GET", "/admin/case-closed/packages"),
    ("GET", "/admin/case-closed/packages/{package_id}"),
    ("POST", "/admin/case-closed/packages/{package_id}/publish"),
    ("GET", "/admin/case-closed/session"),
    # Plot Twist content studio. Same shape as Case Closed's: blueprint and
    # package authoring, generation, approval and publishing, all gated on
    # ``games:manage``.
    ("GET", "/admin/plot-twist/blueprints"),
    ("POST", "/admin/plot-twist/blueprints"),
    ("GET", "/admin/plot-twist/blueprints/{blueprint_id}"),
    ("POST", "/admin/plot-twist/blueprints/{blueprint_id}/generate"),
    ("POST", "/admin/plot-twist/blueprints/{blueprint_id}/approve"),
    ("POST", "/admin/plot-twist/blueprints/{blueprint_id}/packages"),
    ("GET", "/admin/plot-twist/packages"),
    ("GET", "/admin/plot-twist/packages/{package_id}"),
    ("PUT", "/admin/plot-twist/packages/{package_id}"),
    ("POST", "/admin/plot-twist/packages/{package_id}/publish"),
    ("POST", "/admin/plot-twist/packages/{package_id}/simulate"),
    (
        "POST",
        "/admin/plot-twist/packages/{package_id}/scenes/{scene_id}/regenerate",
    ),
    ("GET", "/admin/plot-twist/session"),
]


def _concrete(path_template):
    """Fill the path parameters in, so the request reaches the handler.

    The value is deliberately nonsense but must be well formed: ids in this
    application are UUID columns, so a bare "1" would raise inside the query
    and surface as a 500 instead of letting the request reach -- and be
    refused by -- the authorisation layer. Dates keep the old "1" because the
    validation layer rejects it before the handler is ever entered.
    """
    def replacement(match):
        return "1" if match.group(1).endswith("date") else _SAMPLE_UUID

    return re.sub(r"\{([^}]+)\}", replacement, path_template)


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
