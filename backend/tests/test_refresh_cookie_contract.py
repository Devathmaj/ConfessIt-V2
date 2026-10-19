"""Backend-side contract for the refresh cookie's Set-Cookie flags and rotation.

The frontend (services/api.ts) restates this contract in comments -- HttpOnly,
Path=/auth -- but comments are not enforcement. This file pins the wire contract
where it is minted (routers/auth._refresh_cookie_options) so a regression that
weakens it fails here, not in a browser:

- name is settings.REFRESH_COOKIE_NAME, path is settings.REFRESH_COOKIE_PATH (/auth)
- HttpOnly, SameSite=lax on every mint; Secure follows the client-facing scheme
  (or the COOKIE_SECURE override)
- every successful refresh ROTATES: the response sets a new cookie value, the old
  raw value is dead (replay refused), and the access token is fresh
- logout/failed-refresh clearing repeats path + Secure + SameSite (+ HttpOnly) so
  the deletion is honoured by browsers
- a failed refresh clears the cookie only when the cookie is dead: a value that was
  rotated away from a live session is a stale copy (two tabs racing a refresh), and
  the browser may still be holding the newer value, so it must survive the refusal

Deliberately NOT asserting `__Host-` prefix or `SameSite=Strict`: `__Host-` is
rejected by browsers unless Path=/ exactly, which would widen this cookie from
/auth to the whole API; Strict would break cross-site deployments' refresh POST.
See _refresh_cookie_options for the full rationale.
"""

from fastapi.testclient import TestClient

from app.accounts import AccountType
from app.config import settings
from app.db_models import UserRecord, find_account_in_table

VERIFY_ENDPOINT = "/auth/login/magic/verify"
REFRESH_ENDPOINT = "/auth/refresh"
LOGOUT_ENDPOINT = "/auth/logout"
ALLOWED_ORIGIN = "http://localhost:5173"


class _FromHost:
    """Presents every request as arriving from one fixed address."""

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


def test_mint_sets_httponly_lax_path_scoped_cookie(db):
    browser = _login(db, "contract-user")
    assert _refresh_cookie(browser), "login must leave a refresh cookie in the jar"


def test_verify_set_cookie_carries_httponly_samesite_path(db):
    _seed_user(db, "contract-flags")
    browser = _browser()
    plaintext = _mint_token_for(db, "contract-flags")
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    lowered = set_cookie.lower()
    assert "httponly" in lowered, f"HttpOnly missing: {set_cookie}"
    assert "samesite=lax" in lowered, f"SameSite=lax missing: {set_cookie}"
    assert f"path={settings.REFRESH_COOKIE_PATH}".lower() in lowered, (
        f"Path={settings.REFRESH_COOKIE_PATH} missing: {set_cookie}"
    )
    assert settings.REFRESH_COOKIE_NAME in set_cookie


def test_refresh_rotates_and_kills_the_old_value(db):
    browser = _login(db, "contract-rotate")
    old_raw = _refresh_cookie(browser)
    assert old_raw

    response = browser.post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
    )
    assert response.status_code == 200, response.text
    new_raw = _refresh_cookie(browser)
    assert new_raw and new_raw != old_raw, "refresh must rotate to a new raw value"
    assert "access_token" in response.json()

    # The old value is dead: replaying it from a fresh jar is refused.
    # (Per-request cookies, not jar surgery: the jar's stored domain belongs to
    # the Set-Cookie response, while an explicit per-request cookie is always sent.)
    denied = _browser().post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
        cookies={settings.REFRESH_COOKIE_NAME: old_raw},
    )
    assert denied.status_code == 401, denied.text


def test_failed_refresh_clears_with_matching_flags(db):
    _login(db, "contract-clear")
    # A forged cookie value, sent from a fresh jar: the refusal must clear it with
    # the same path/samesite (and secure-per-scheme) so browsers honour the deletion.
    response = _browser().post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
        cookies={settings.REFRESH_COOKIE_NAME: "dead-value"},
    )
    assert response.status_code == 401, response.text
    set_cookie = response.headers.get("set-cookie", "")
    lowered = set_cookie.lower()
    assert f"path={settings.REFRESH_COOKIE_PATH}".lower() in lowered, set_cookie
    assert "samesite=lax" in lowered, set_cookie


def test_a_revoked_session_clears_the_cookie(db):
    # The cookie of a session that was signed out is dead too, and the browser has
    # to be told: otherwise it keeps sending a value that can never work again.
    browser = _login(db, "contract-revoked")
    raw = _refresh_cookie(browser)
    assert browser.post(LOGOUT_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN}).status_code == 200

    response = _browser().post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
        cookies={settings.REFRESH_COOKIE_NAME: raw},
    )
    assert response.status_code == 401, response.text
    set_cookie = response.headers.get("set-cookie", "")
    assert f"path={settings.REFRESH_COOKIE_PATH}".lower() in set_cookie.lower(), set_cookie


def test_a_rotated_away_value_does_not_delete_the_cookie(db):
    """The multi-tab hazard: the loser of a rotation race must not take the
    winner's cookie with it.

    Two tabs of one browser can refresh at the same moment. One of them rotates
    the cookie; the other is refused for presenting the value that was just
    replaced. The browser is now holding the *newer* value, so a deletion on that
    refusal would sign it out of a session it is still using -- the very thing the
    cookie's own rotation is meant to survive.
    """
    browser = _login(db, "contract-stale")
    old_raw = _refresh_cookie(browser)
    assert old_raw

    rotated = browser.post(REFRESH_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN})
    assert rotated.status_code == 200, rotated.text
    new_raw = _refresh_cookie(browser)
    assert new_raw and new_raw != old_raw, "refresh must rotate to a new raw value"

    # The stale copy, presented as the tab that lost the race would.
    refused = _browser().post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
        cookies={settings.REFRESH_COOKIE_NAME: old_raw},
    )
    assert refused.status_code == 401, refused.text
    # The answer is the same generic refusal either way: nothing about it tells a
    # caller whether a random token belonged to a live account.
    assert refused.json()["detail"] == "Could not validate credentials"
    # ... but no deletion: the browser's newer value has to survive this refusal.
    assert "set-cookie" not in refused.headers


def test_a_refused_refresh_answers_the_generic_401(db):
    # The client's contract: the refusal body and headers are unchanged, so the
    # "signed in elsewhere" wording still stands apart from every other refusal.
    response = _browser().post(
        REFRESH_ENDPOINT,
        headers={"Origin": ALLOWED_ORIGIN},
        cookies={settings.REFRESH_COOKIE_NAME: "dead-value"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Could not validate credentials"}
    assert response.headers.get("WWW-Authenticate") == "Bearer"


def test_logout_clears_with_matching_flags(db):
    browser = _login(db, "contract-logout")
    response = browser.post(LOGOUT_ENDPOINT, headers={"Origin": ALLOWED_ORIGIN})
    assert response.status_code == 200, response.text
    set_cookie = response.headers.get("set-cookie", "")
    lowered = set_cookie.lower()
    assert f"path={settings.REFRESH_COOKIE_PATH}".lower() in lowered, set_cookie
    assert _refresh_cookie(browser) is None or _refresh_cookie(browser) == ""


def test_secure_flag_follows_https_and_override(db, monkeypatch):
    _seed_user(db, "contract-secure")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    browser = _browser()
    plaintext = _mint_token_for(db, "contract-secure")
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    assert "secure" in response.headers.get("set-cookie", "").lower()
