"""CSRF on the cookie-authenticated POSTs, and Secure cookies behind a proxy.

``POST /auth/refresh`` and ``POST /auth/logout`` act on ambient authority (the
HttpOnly refresh cookie), so a cross-site POST that somehow carries the cookie -- a
307 redirect chain replaying it, an old browser past SameSite -- must be refused by a
second layer. That layer is the Origin/Referer check: a browser-driven POST always
carries one of them, and it must name an origin from the CORS allowlist. Requests
carrying neither are non-browser clients and stay working.

The raw-token body alternative is debug-only: with ``ENABLE_DEBUG_ENDPOINTS`` off it
is ignored and the cookie flow applies, so production has exactly one credential per
endpoint. (The suite enables the flag globally for the rotation mechanics tests; the
gate itself is pinned here with the flag off.)

A separate half is the Secure flag behind a TLS proxy: the app sees plain http while
the browser sees https, so the flag follows the proxy-corrected scheme (or the
explicit ``COOKIE_SECURE`` override), and clearing a cookie repeats the flag -- a
deletion for a Secure cookie that arrives without it is ignored by browsers.
"""

import jwt
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.config import cookie_secure, settings
from app.db_models import UserRecord, find_account_in_table

VERIFY_ENDPOINT = "/auth/login/magic/verify"
REFRESH_ENDPOINT = "/auth/refresh"
LOGOUT_ENDPOINT = "/auth/logout"

ALLOWED_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "https://evil.example.com"


class _FromHost:
    """Presents every request as arriving from one fixed address (see test_refresh_sessions)."""

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        from app.main import app

        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser: a fresh client, so no cookie is shared between logins."""
    return TestClient(_FromHost(host))


def _seed_user(db, regno):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno,
            name=regno.title(),
            email=f"{regno}@example.com",
            year=2,
            department="Test_Class",
            gender="other",
        ))


def _mint_token_for(db, regno, account_type=AccountType.USERS.value):
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, account_type, regno)
    assert found is not None, f"no {account_type} account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "Test Agent"}

    token, _ = create_and_store_magic_token(db, str(row.id), resolved_type, _Request())
    return token


def _login(db, regno):
    """A browser holding a live session cookie for ``regno``."""
    _seed_user(db, regno)
    browser = _browser()
    plaintext = _mint_token_for(db, regno)
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    assert browser.cookies.get(settings.REFRESH_COOKIE_NAME)
    return browser


def _refresh_cookie(browser):
    return browser.cookies.get(settings.REFRESH_COOKIE_NAME)


def test_cookie_refresh_from_a_foreign_origin_is_refused_without_rotating(db):
    browser = _login(db, "csrf-user")
    before = _refresh_cookie(browser)

    refused = browser.post(REFRESH_ENDPOINT, headers={"Origin": EVIL_ORIGIN})

    assert refused.status_code == 403, refused.text
    # Nothing rotated: the refused call must not burn the presented token.
    assert _refresh_cookie(browser) == before

    # ... and the session is still live for a legitimate call.
    allowed = browser.post(REFRESH_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN})
    assert allowed.status_code == 200, allowed.text


def test_cookie_refresh_from_the_app_origin_succeeds(db):
    browser = _login(db, "same-origin-user")

    response = browser.post(REFRESH_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200, response.text
    assert response.json()["access_token"]


def test_cookie_refresh_without_origin_headers_still_works(db):
    # curl, tests and service clients send neither header: there is no ambient
    # authority to steal outside a browser, so these stay working.
    browser = _login(db, "script-user")

    response = browser.post(REFRESH_ENDPOINT)

    assert response.status_code == 200, response.text


def test_referer_only_request_is_checked_when_origin_is_absent(db):
    browser = _login(db, "referer-user")

    refused = browser.post(
        REFRESH_ENDPOINT, headers={"Referer": f"{EVIL_ORIGIN}/attacker-page"}
    )
    assert refused.status_code == 403, refused.text

    allowed = browser.post(
        REFRESH_ENDPOINT, headers={"Referer": f"{ALLOWED_ORIGIN}/dashboard"}
    )
    assert allowed.status_code == 200, allowed.text


def test_cookie_logout_from_a_foreign_origin_is_refused_without_revoking(db):
    browser = _login(db, "logout-csrf-user")

    refused = browser.post(LOGOUT_ENDPOINT, headers={"Origin": EVIL_ORIGIN})

    assert refused.status_code == 403, refused.text
    # The session survived: a legitimate refresh still works afterwards.
    assert browser.post(REFRESH_ENDPOINT).status_code == 200


def test_cookie_logout_from_the_app_origin_revokes(db):
    browser = _login(db, "logout-user")

    assert browser.post(LOGOUT_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN}).status_code == 200
    assert browser.post(REFRESH_ENDPOINT).status_code == 401


def test_body_flow_is_ignored_unless_debug_endpoints_are_on(db, monkeypatch):
    _seed_user(db, "body-user")
    browser = _browser()
    plaintext = _mint_token_for(db, "body-user")
    assert browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}").status_code == 200
    raw_session_token = _refresh_cookie(browser)
    assert raw_session_token is not None
    monkeypatch.setattr(settings, "ENABLE_DEBUG_ENDPOINTS", False)

    # No cookie on this client: the body alone must not authenticate when the gate is shut.
    assert (
        _browser()
        .post(REFRESH_ENDPOINT, json={"refresh_token": raw_session_token})
        .status_code
        == 401
    )

    # ... while the same body works with the gate open, proving the token is live and
    # the refusal above was the gate, not a dead credential.
    monkeypatch.setattr(settings, "ENABLE_DEBUG_ENDPOINTS", True)
    response = _browser().post(REFRESH_ENDPOINT, json={"refresh_token": raw_session_token})
    assert response.status_code == 200, response.text


def test_body_is_ignored_in_favour_of_the_cookie_when_the_gate_is_shut(db, monkeypatch):
    """With the flag off, a body cannot override (or smuggle past) the cookie flow."""
    _seed_user(db, "cookie-user")
    _seed_user(db, "body-other")
    monkeypatch.setattr(settings, "ENABLE_DEBUG_ENDPOINTS", False)

    cookie_browser = _browser()
    body_browser = _browser()
    for client, regno in ((cookie_browser, "cookie-user"), (body_browser, "body-other")):
        plaintext = _mint_token_for(db, regno)
        assert client.get(f"{VERIFY_ENDPOINT}?token={plaintext}").status_code == 200
    body_token = _refresh_cookie(body_browser)
    assert body_token is not None

    response = cookie_browser.post(
        REFRESH_ENDPOINT, json={"refresh_token": body_token}
    )

    assert response.status_code == 200, response.text
    claims = jwt.decode(
        response.json()["access_token"],
        settings.SECRET_KEY,
        algorithms=[settings.ALGORITHM],
    )
    with db.sessions() as session:
        cookie_owner = session.scalar(
            select(UserRecord).where(UserRecord.regno == "cookie-user")
        )
    assert claims["account_id"] == str(cookie_owner.id)


def test_secure_flag_follows_configuration_not_just_the_socket(db, monkeypatch):
    browser = _login(db, "secure-user")

    # Plain http here, but an explicit override wins over the socket.
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    response = browser.post(REFRESH_ENDPOINT)
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert "secure" in set_cookie.lower()
    assert "samesite" in set_cookie.lower()

    # ... and the deletion carries the flag too, or browsers ignore it.
    logout = browser.post(LOGOUT_ENDPOINT)
    assert logout.status_code == 200, logout.text
    assert "secure" in logout.headers.get("set-cookie", "").lower()


def test_cookie_secure_helper(monkeypatch):
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    assert cookie_secure("http") is False
    assert cookie_secure("https") is True
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    assert cookie_secure("http") is True
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    assert cookie_secure("https") is False
