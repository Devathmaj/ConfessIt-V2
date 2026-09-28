"""The CORS policy is least-privilege: exact origins, tight methods/headers, no wildcard.

Credentials ride on the refresh and client cookies, so a permissive preflight answer
would turn any website into a credentialed caller. These tests pin the contract through
real preflights (no auth needed -- the middleware answers before routing):

* an allowed origin gets exactly the method allowlist, the header allowlist,
  credentialed access and a cached preflight -- and nothing else;
* `Retry-After` is the one exposed response header, because the login page reads it;
* a disallowed origin gets no credentialed answer at all;
* a `*` in CORS_ORIGINS refuses to boot rather than opening the API to every site.
"""

import pytest

from app import main as main_module
from app.config import settings

ALLOWED_ORIGIN = "http://localhost:5173"
DISALLOWED_ORIGIN = "https://evil.example.com"

EXPECTED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
EXPECTED_HEADERS = {"authorization", "content-type", "accept"}


def _preflight(client, origin, method="DELETE", headers="Authorization, Content-Type"):
    return client.options(
        "/auth/login/users/magic",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


def test_preflight_from_an_allowed_origin_is_tightly_scoped(client):
    response = _preflight(client, ALLOWED_ORIGIN)

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert {
        method.strip()
        for method in response.headers["access-control-allow-methods"].split(",")
    } == EXPECTED_METHODS
    # A superset check: this Starlette also appends the safelisted Accept-Language and
    # Content-Language. What matters is that exactly our three are the granted ones and
    # no custom header (X-Requested-With and friends) is offered.
    assert EXPECTED_HEADERS <= {
        header.strip().lower()
        for header in response.headers["access-control-allow-headers"].split(",")
    }
    assert "x-requested-with" not in response.headers["access-control-allow-headers"].lower()
    assert int(response.headers["access-control-max-age"]) == 600


def test_preflight_refuses_a_method_outside_the_allowlist(client):
    # The preflight fails, so the browser never sends the real request -- which is the
    # whole of the guarantee. (This Starlette still echoes the allowed origin on the
    # 400; a non-2xx preflight grants nothing regardless.)
    response = _preflight(client, ALLOWED_ORIGIN, method="TRACE")

    assert response.status_code == 400, response.text
    assert "TRACE" not in response.headers["access-control-allow-methods"]


def test_preflight_from_a_disallowed_origin_gets_no_credentialed_answer(client):
    # 400 with no `access-control-allow-origin`: without that header -- and without a
    # 2xx -- the browser drops the real request, so the `allow-credentials` echo
    # alongside it authorises nothing.
    response = _preflight(client, DISALLOWED_ORIGIN)

    assert response.status_code == 400, response.text
    assert "access-control-allow-origin" not in response.headers


def test_actual_response_exposes_retry_after_to_allowed_origins(client):
    # The exposed header must also be present where the login page reads it, not just
    # advertised up front: this Starlette sends it on actual responses (where it takes
    # effect), not on the preflight.
    response = client.get("/api/time", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-expose-headers"] == "Retry-After"


def test_origins_are_de_duplicated_by_trailing_slash(monkeypatch):
    monkeypatch.setattr(
        settings, "CORS_ORIGINS", "http://localhost:5173,http://localhost:5173/"
    )
    assert main_module._cors_allow_origins() == ["http://localhost:5173"]


def test_wildcard_origin_refuses_to_boot(monkeypatch):
    monkeypatch.setattr(settings, "CORS_ORIGINS", "http://localhost:5173, *")
    with pytest.raises(RuntimeError):
        main_module._cors_allow_origins()
