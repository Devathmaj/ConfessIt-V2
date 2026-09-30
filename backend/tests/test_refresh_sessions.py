"""Refresh tokens, the 24-hour session, and logout.

The access token is a 30-minute JWT; the refresh token keeps a signed-in person from
being interrupted every half hour. These tests pin the contract around it:

* redeeming a magic link establishes a session with a hard deadline
  (``consumed_at + LOGIN_SESSION_EXPIRE_HOURS``) and hands the refresh token to the
  browser as an HttpOnly cookie only;
* a refresh rotates to a fresh refresh token, minting a fresh access token, and the
  old refresh token stops working -- exactly one rotation can win a race;
* rotating extends the *access* token, never the session: the absolute deadline is
  immutable across any number of refreshes, and both access and refresh refuse it once
  passed;
* a superseded session cannot refresh (it gets the dedicated ``session_superseded``
  401, same as its access token), and a replay of a rotated-away token is refused
  without disturbing the account's still-live session;
* a blocked or deleted account stops refreshing on the next call, answered identically
  to an invalid token;
* logout revokes the session server-side, works after the access token has expired,
  and is idempotent.

The tests drive the real HTTP endpoints. Like ``test_login_sessions`` they pop the
conftest auth override, because redemption and the access path are where the real token,
the real session row and the real deadline live.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoginTokenRecord, UserRecord, find_account_in_table
from app.main import app
from app.services import auth_service as auth_service_module
from app.services.auth_service import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_REVOKED,
    SESSION_STATUS_SUPERSEDED,
    SESSION_SUPERSEDED_DETAIL,
    TOKEN_HASH_DOMAIN_REFRESH,
    get_current_user,
    hash_token,
)

VERIFY_ENDPOINT = "/auth/login/magic/verify"
REFRESH_ENDPOINT = "/auth/refresh"
LOGOUT_ENDPOINT = "/auth/logout"


class _FromHost:
    """Presents every request as arriving from one fixed address (see test_login_sessions)."""

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser: a fresh client, so no cookie is shared between logins."""
    return TestClient(_FromHost(host))


def _seed_user(db, regno, name=None):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno,
            name=name or regno.title(),
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


def _session_row(db, session_id):
    with db.sessions() as session:
        return session.get(LoginTokenRecord, session_id)


def _redeem_token(browser, plaintext):
    """Redeem a magic link through the real endpoint and return the access token."""
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _refresh_cookie(browser):
    return browser.cookies.get(settings.REFRESH_COOKIE_NAME)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _set_deadline(db, session_id, when):
    with db.sessions.begin() as session:
        session.execute(
            update(LoginTokenRecord)
            .where(LoginTokenRecord.id == session_id)
            .values(session_expires_at=when)
        )


@pytest.fixture
def no_auth_override():
    """Run the real ``get_current_user`` instead of the conftest's fixed account."""
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved


@pytest.fixture
def auth_log(monkeypatch):
    """Captures the structured application log lines, as strings."""
    captured = {"info": [], "warning": []}
    monkeypatch.setattr(
        auth_service_module.logger, "info",
        lambda message, *args, **kwargs: captured["info"].append(message),
    )
    monkeypatch.setattr(
        auth_service_module.logger, "warning",
        lambda message, *args, **kwargs: captured["warning"].append(message),
    )
    return captured


def test_redemption_establishes_a_session_with_a_deadline_and_a_refresh_cookie(
    db, no_auth_override
):
    _seed_user(db, "fresh-user")
    browser = _browser()

    response = browser.get(f"{VERIFY_ENDPOINT}?token={_mint_token_for(db, 'fresh-user')}")
    assert response.status_code == 200
    # The raw refresh token is a cookie-only credential: the response body is the Token
    # model and must not carry it, or HttpOnly would be theatre.
    assert "refresh_token" not in response.json()
    assert _refresh_cookie(browser) is not None

    with db.sessions() as session:
        sessions = session.scalars(
            select(LoginTokenRecord).where(LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE)
        ).all()
    assert len(sessions) == 1
    session_row = sessions[0]
    assert session_row.session_expires_at is not None
    remaining = session_row.session_expires_at - datetime.now(timezone.utc)
    assert timedelta(hours=settings.LOGIN_SESSION_EXPIRE_HOURS - 1) < remaining
    assert session_row.refresh_token_hash is not None
    assert session_row.refresh_token_replaced_hash is None


def test_refresh_returns_a_fresh_access_token_and_rotates_the_cookie(db, no_auth_override):
    _seed_user(db, "rotate-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "rotate-user"))
    old_cookie = _refresh_cookie(browser)
    assert old_cookie is not None
    assert browser.get("/auth/me", headers=_bearer(access_token)).status_code == 200

    refreshed = browser.post(REFRESH_ENDPOINT)
    assert refreshed.status_code == 200, refreshed.text
    assert "refresh_token" not in refreshed.json()
    new_access = refreshed.json()["access_token"]
    new_cookie = _refresh_cookie(browser)
    assert new_cookie is not None and new_cookie != old_cookie

    assert browser.get("/auth/me", headers=_bearer(new_access)).status_code == 200


def test_the_rotated_away_refresh_token_stops_working(db, no_auth_override):
    _seed_user(db, "old-token-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "old-token-user"))
    old_cookie = _refresh_cookie(browser)

    response = browser.post(REFRESH_ENDPOINT)
    assert response.status_code == 200
    fresh_cookie = _refresh_cookie(browser)

    # The old value is refused the moment it was rotated away, with the same generic 401
    # an unknown token gets, so a reuse cannot be told apart from noise by a caller.
    # Presented as a cookie (not the debug-only body): the replay must fail on the
    # production credential path.
    replay = _browser().post(
        REFRESH_ENDPOINT, cookies={settings.REFRESH_COOKIE_NAME: old_cookie}
    )
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Could not validate credentials"

    # The new value carries the session through a further rotation.
    assert browser.post(REFRESH_ENDPOINT).status_code == 200
    latest_cookie = _refresh_cookie(browser)
    assert latest_cookie != fresh_cookie

    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(latest_cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert row is not None
    # Each rotation records the value it rotated away from, so a replay of that value is
    # recognisable in the logs.
    assert row.refresh_token_replaced_hash == hash_token(fresh_cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)


def test_refresh_never_extends_the_session_deadline(db, no_auth_override):
    _seed_user(db, "bounded-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "bounded-user"))
    cookie = _refresh_cookie(browser)

    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    first_deadline = row.session_expires_at

    for _ in range(2):
        assert browser.post(REFRESH_ENDPOINT).status_code == 200
        cookie = _refresh_cookie(browser)

    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert row.session_expires_at == first_deadline


def test_a_refresh_after_the_absolute_deadline_is_refused(db, no_auth_override):
    _seed_user(db, "deadline-refresh-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "deadline-refresh-user"))
    cookie = _refresh_cookie(browser)

    with db.sessions() as session:
        session_row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    _set_deadline(db, session_row.id, datetime.now(timezone.utc) - timedelta(seconds=1))

    refused = browser.post(REFRESH_ENDPOINT)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Could not validate credentials"
    # The deadline is the hard ceiling for the access token too.
    rejected = browser.get("/auth/me", headers=_bearer(access_token))
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "Could not validate credentials"


def test_an_access_token_cannot_outlive_the_sessions_deadline(db, no_auth_override):
    _seed_user(db, "straddle-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "straddle-user"))
    cookie = _refresh_cookie(browser)

    with db.sessions() as session:
        session_row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    # A 30-minute JWT can straddle the 24-hour mark; the row the token names is what cuts
    # it off exactly at the deadline.
    _set_deadline(db, session_row.id, datetime.now(timezone.utc) - timedelta(seconds=1))

    rejected = browser.get("/auth/me", headers=_bearer(access_token))
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == "Could not validate credentials"
    assert "session_superseded" not in rejected.headers.get("WWW-Authenticate", "")


def test_reusing_a_rotated_away_token_is_refused_without_revoking_the_session(
    db, no_auth_override
):
    _seed_user(db, "replay-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "replay-user"))
    old_cookie = _refresh_cookie(browser)
    assert browser.post(REFRESH_ENDPOINT).status_code == 200
    fresh_cookie = _refresh_cookie(browser)

    replay = _browser().post(
        REFRESH_ENDPOINT, cookies={settings.REFRESH_COOKIE_NAME: old_cookie}
    )
    assert replay.status_code == 401
    assert replay.json()["detail"] == "Could not validate credentials"

    # The replay rotated nothing and revoked nothing: the session still holds the fresh
    # value, still active, and the fresh value still works.
    row = _session_row(db, session_id_of(db, fresh_cookie))
    assert row.session_status == SESSION_STATUS_ACTIVE
    assert row.refresh_token_hash == hash_token(fresh_cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
    assert browser.post(REFRESH_ENDPOINT).status_code == 200


def session_id_of(db, raw_refresh):
    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(raw_refresh, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert row is not None
    return row.id


def test_two_concurrent_refreshes_of_one_token_leave_exactly_one_winner(db, no_auth_override):
    _seed_user(db, "race-refresh-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "race-refresh-user"))
    cookie = _refresh_cookie(browser)
    assert cookie is not None

    def refresh_with(client):
        return client, client.post(
            REFRESH_ENDPOINT, cookies={settings.REFRESH_COOKIE_NAME: cookie}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(refresh_with, [_browser(), _browser()]))

    statuses = sorted(response.status_code for _, response in outcomes)
    assert statuses == [200, 401], statuses

    winner_client, winner_response = next(
        (client, response) for client, response in outcomes if response.status_code == 200
    )
    loser_response = next(
        response for _, response in outcomes if response.status_code == 401
    )
    assert loser_response.json()["detail"] == "Could not validate credentials"

    # The winner's rotated refresh cookie is still live, and the loser's replay did not
    # revoke the session: a refresh with the winner's new value, and the access token from
    # the original login, both still work.
    winner_cookie = winner_client.cookies.get(settings.REFRESH_COOKIE_NAME)
    assert winner_cookie is not None and winner_cookie != cookie
    assert winner_client.post(REFRESH_ENDPOINT).status_code == 200
    assert browser.get("/auth/me", headers=_bearer(access_token)).status_code == 200


def test_a_superseded_session_cannot_refresh(db, no_auth_override):
    _seed_user(db, "supersede-refresh-user")
    first_browser = _browser()
    other_browser = _browser()

    first_token = _redeem_token(first_browser, _mint_token_for(db, "supersede-refresh-user"))
    second_token = _redeem_token(other_browser, _mint_token_for(db, "supersede-refresh-user"))
    assert other_browser.get("/auth/me", headers=_bearer(second_token)).status_code == 200

    # The replaced session's access token gets the dedicated 401...
    rejected = first_browser.get("/auth/me", headers=_bearer(first_token))
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == SESSION_SUPERSEDED_DETAIL
    assert "session_superseded" in rejected.headers["WWW-Authenticate"]

    # ...and so does its refresh: the session cannot be revived, and the client must see
    # the same "signed in elsewhere" reason rather than a generic expiry.
    refused = first_browser.post(REFRESH_ENDPOINT)
    assert refused.status_code == 401
    assert refused.json()["detail"] == SESSION_SUPERSEDED_DETAIL
    assert "session_superseded" in refused.headers.get("WWW-Authenticate", "")

    # The live session refreshes normally.
    assert other_browser.post(REFRESH_ENDPOINT).status_code == 200


def test_a_blocked_account_cannot_refresh(db, no_auth_override):
    _seed_user(db, "blocked-refresh-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "blocked-refresh-user"))

    with db.sessions.begin() as session:
        session.execute(
            update(UserRecord).where(UserRecord.regno == "blocked-refresh-user")
            .values(is_blocked=True)
        )

    # Same generic refusal as an invalid token: nothing here confirms the account exists.
    access = browser.get("/auth/me", headers=_bearer(access_token))
    assert access.status_code == 401
    assert access.json()["detail"] == "Could not validate credentials"

    refused = browser.post(REFRESH_ENDPOINT)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Could not validate credentials"
    assert "session_superseded" not in refused.headers.get("WWW-Authenticate", "")


def test_a_deleted_account_cannot_refresh(db, no_auth_override):
    _seed_user(db, "deleted-refresh-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "deleted-refresh-user"))

    with db.sessions.begin() as session:
        session.execute(
            delete(UserRecord).where(UserRecord.regno == "deleted-refresh-user")
        )

    refused = browser.post(REFRESH_ENDPOINT)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Could not validate credentials"


def test_logout_revokes_the_session_on_the_server(db, no_auth_override):
    _seed_user(db, "logout-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "logout-user"))
    cookie = _refresh_cookie(browser)
    assert cookie is not None

    response = browser.post(LOGOUT_ENDPOINT)
    assert response.status_code == 200
    assert _refresh_cookie(browser) is None

    row = _session_row(db, session_id_of(db, cookie))
    assert row.session_status == SESSION_STATUS_REVOKED

    assert browser.post(REFRESH_ENDPOINT, cookies={settings.REFRESH_COOKIE_NAME: cookie}).status_code == 401
    assert browser.get("/auth/me", headers=_bearer(access_token)).status_code == 401


def test_logout_is_idempotent_and_works_after_the_access_token_has_lapsed(db, no_auth_override):
    _seed_user(db, "late-logout-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "late-logout-user"))

    # Logout is authorised by the refresh cookie alone, so it works with no valid access
    # token attached -- the exact situation of a person who returns after 30 minutes.
    assert browser.post(LOGOUT_ENDPOINT).status_code == 200
    second = browser.post(LOGOUT_ENDPOINT)
    assert second.status_code == 200
    assert second.json()["message"] == "Signed out"


def test_refresh_without_any_token_is_refused(db, no_auth_override):
    _seed_user(db, "no-token-user")
    _redeem_token(_browser(), _mint_token_for(db, "no-token-user"))

    refused = _browser().post(REFRESH_ENDPOINT)
    assert refused.status_code == 401
    assert refused.json()["detail"] == "Could not validate credentials"


def test_refresh_with_an_unknown_token_is_refused_without_touching_the_session(
    db, no_auth_override
):
    _seed_user(db, "phantom-refresh-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "phantom-refresh-user"))
    cookie = _refresh_cookie(browser)

    forged = _browser().post(
        REFRESH_ENDPOINT, cookies={settings.REFRESH_COOKIE_NAME: "not-a-real-token"}
    )
    assert forged.status_code == 401
    assert forged.json()["detail"] == "Could not validate credentials"

    # The account's own session is untouched by a stranger presenting a random token.
    assert browser.get("/auth/me", headers=_bearer(access_token)).status_code == 200
    assert browser.post(REFRESH_ENDPOINT).status_code == 200