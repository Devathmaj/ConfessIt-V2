"""One live session per account.

Signing in again -- on another device, another browser, a freshly clicked link -- replaces
the account's previous session. These tests pin the contract:

* a new login supersedes the existing active session, and the superseded session's very next
  request is answered with a dedicated 401 explaining that it was signed out by a session
  opened elsewhere;
* only sessions of the same account (same table, same row) are replaced, never another
  account's;
* when two logins for one account race, exactly one session stays active (the last to
  commit), because both redemption transactions serialise behind an account-scoped advisory
  lock rather than each reading "no other active session" from the same snapshot;
* an access token whose session is gone, forged, or was issued before sessions were recorded
  in the token is refused rather than re-resolved.

The tests drive the real HTTP endpoints and the real ``get_current_user`` dependency -- the
conftest override is popped -- because the enforcement lives in the token a real login mints
and in the dependency a real request resolves through.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import uuid

import jwt
import pytest
from fastapi import HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoginTokenRecord, UserRecord, find_account_in_table
from app.main import app
from app.services import auth_service as auth_service_module
from app.services.auth_service import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_SUPERSEDED,
    SESSION_SUPERSEDED_DETAIL,
    create_access_token,
    get_current_user,
    hash_token,
    verify_token_service,
)

VERIFY_ENDPOINT = "/auth/login/magic/verify"


class _FromHost:
    """Presents every request as arriving from one fixed address.

    ``login_tokens.request_ip`` is an ``inet`` column, which the default ``testclient``
    hostname is not storable as, and redemption pins a token to the address it was minted
    from (127.0.0.1, the same host this wrapper presents).
    """

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser: a fresh client, so no client id or header is shared between logins."""
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
    """Mint a token the way the endpoint does, but hand the plaintext to the test only."""
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, account_type, regno)
    assert found is not None, f"no {account_type} account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "Test Agent"}

    return create_and_store_magic_token(db, str(row.id), resolved_type, _Request())


def _token_row(db, plaintext):
    with db.sessions() as session:
        return session.scalar(
            select(LoginTokenRecord).where(LoginTokenRecord.token_hash == hash_token(plaintext))
        )


def _account_id(db, regno):
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    assert row is not None, f"no user with regno {regno!r}"
    return str(row.id)


def _session_count(db, regno, session_status, account_type=AccountType.USERS.value):
    """How many login tokens of one status an account holds."""
    account_id = _account_id(db, regno)
    with db.sessions() as session:
        return len(session.scalars(
            select(LoginTokenRecord).where(
                LoginTokenRecord.account_id == account_id,
                LoginTokenRecord.account_type == account_type,
                LoginTokenRecord.session_status == session_status,
            )
        ).all())


def _redeem_token(browser, plaintext):
    """Redeem a magic link through the real endpoint and return the access token."""
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


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
    """Captures the structured application log lines, as strings.

    The service loggers do not propagate, so ``caplog`` alone would see nothing; patching
    the two methods captures exactly what an operator would be able to search for later.
    """
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


def test_a_new_login_supersedes_the_existing_session(db, no_auth_override, auth_log):
    _seed_user(db, "single-user")
    first_browser = _browser()
    other_browser = _browser()

    first_plaintext = _mint_token_for(db, "single-user")
    first_token = _redeem_token(first_browser, first_plaintext)
    assert first_browser.get("/auth/me", headers=_bearer(first_token)).status_code == 200

    second_plaintext = _mint_token_for(db, "single-user")
    second_token = _redeem_token(other_browser, second_plaintext)
    assert other_browser.get("/auth/me", headers=_bearer(second_token)).status_code == 200

    # The replaced session's very next request is told why, with a message distinct from
    # the generic "credentials" refusal so the client can say "signed in elsewhere".
    rejected = first_browser.get("/auth/me", headers=_bearer(first_token))
    assert rejected.status_code == 401
    assert rejected.json()["detail"] == SESSION_SUPERSEDED_DETAIL
    assert "session_superseded" in rejected.headers["WWW-Authenticate"]

    first_row = _token_row(db, first_plaintext)
    second_row = _token_row(db, second_plaintext)
    assert first_row.session_status == SESSION_STATUS_SUPERSEDED
    assert first_row.superseded_at is not None
    assert first_row.superseded_by == str(second_row.id)
    assert second_row.session_status == SESSION_STATUS_ACTIVE
    assert any(
        "superseded 1 older active session(s)" in line for line in auth_log["info"]
    ), auth_log["info"]


def test_only_sessions_of_the_same_account_are_superseded(db, no_auth_override):
    _seed_user(db, "alice-user")
    _seed_user(db, "bob-user")

    alice_token = _redeem_token(_browser(), _mint_token_for(db, "alice-user"))
    bob_token = _redeem_token(_browser(), _mint_token_for(db, "bob-user"))
    assert _browser().get("/auth/me", headers=_bearer(alice_token)).status_code == 200
    assert _browser().get("/auth/me", headers=_bearer(bob_token)).status_code == 200

    # Alice signs in again; only her own first session is replaced. Bob's is untouched.
    alice_again = _redeem_token(_browser(), _mint_token_for(db, "alice-user"))
    assert _browser().get("/auth/me", headers=_bearer(alice_token)).status_code == 401
    assert _browser().get("/auth/me", headers=_bearer(alice_again)).status_code == 200
    assert _browser().get("/auth/me", headers=_bearer(bob_token)).status_code == 200
    assert _session_count(db, "alice-user", SESSION_STATUS_ACTIVE) == 1
    assert _session_count(db, "bob-user", SESSION_STATUS_ACTIVE) == 1


def test_several_logins_leave_exactly_one_active_session(db, no_auth_override):
    _seed_user(db, "multi-user")
    tokens = [_redeem_token(_browser(), _mint_token_for(db, "multi-user")) for _ in range(3)]

    assert _session_count(db, "multi-user", SESSION_STATUS_ACTIVE) == 1
    assert _session_count(db, "multi-user", SESSION_STATUS_SUPERSEDED) == 2

    # The newest session is the survivor; the older ones are all told they were replaced.
    assert _browser().get("/auth/me", headers=_bearer(tokens[-1])).status_code == 200
    for older in tokens[:-1]:
        response = _browser().get("/auth/me", headers=_bearer(older))
        assert response.status_code == 401
        assert response.json()["detail"] == SESSION_SUPERSEDED_DETAIL


def test_two_concurrent_logins_for_one_account_leave_exactly_one_active_session(
    db, no_auth_override
):
    _seed_user(db, "race-user")
    plaintexts = [_mint_token_for(db, "race-user") for _ in range(2)]

    def redeem(plaintext):
        return _redeem_token(_browser(), plaintext)

    with ThreadPoolExecutor(max_workers=2) as pool:
        tokens = list(pool.map(redeem, plaintexts))

    assert len(tokens) == 2
    assert _session_count(db, "race-user", SESSION_STATUS_ACTIVE) == 1
    assert _session_count(db, "race-user", SESSION_STATUS_SUPERSEDED) == 1

    # Exactly one of the two tokens still works; the other was replaced by its own login.
    statuses = [
        _browser().get("/auth/me", headers=_bearer(token)).status_code for token in tokens
    ]
    assert sorted(statuses) == [200, 401], statuses


def test_an_access_token_without_a_session_claim_is_refused(db, no_auth_override):
    _seed_user(db, "legacy-user")
    legacy = create_access_token({
        "sub": "legacy-user",
        "account_id": _account_id(db, "legacy-user"),
        "account_type": AccountType.USERS.value,
    })

    response = _browser().get("/auth/me", headers=_bearer(legacy))

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_an_access_token_for_an_unknown_session_is_refused(db, no_auth_override):
    _seed_user(db, "phantom-user")
    plaintext = _mint_token_for(db, "phantom-user")
    token = _redeem_token(_browser(), plaintext)
    claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    forged = create_access_token({
        "sub": claims["sub"],
        "account_id": claims["account_id"],
        "account_type": claims["account_type"],
        "session_id": str(uuid.uuid4()),
    })

    response = _browser().get("/auth/me", headers=_bearer(forged))

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_a_session_that_is_no_longer_active_is_refused(db, no_auth_override):
    _seed_user(db, "idle-user")
    plaintext = _mint_token_for(db, "idle-user")
    token = _redeem_token(_browser(), plaintext)

    # What the admin "Active Today" sweep does to a session past its window.
    with db.sessions.begin() as session:
        session.execute(
            update(LoginTokenRecord)
            .where(LoginTokenRecord.token_hash == hash_token(plaintext))
            .values(session_status="expired")
        )

    response = _browser().get("/auth/me", headers=_bearer(token))

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials"


def test_the_optional_dependency_treats_a_superseded_token_as_signed_out(db, no_auth_override):
    """The confession feed is readable while signed out, so it must not 401 a replaced session."""
    _seed_user(db, "feed-user")
    old_token = _redeem_token(_browser(), _mint_token_for(db, "feed-user"))
    new_token = _redeem_token(_browser(), _mint_token_for(db, "feed-user"))

    anonymous = _browser().get("/confessions")
    superseded = _browser().get("/confessions", headers=_bearer(old_token))
    fresh = _browser().get("/confessions", headers=_bearer(new_token))

    assert anonymous.status_code == 200, anonymous.text
    assert superseded.status_code == 200, superseded.text
    assert fresh.status_code == 200, fresh.text


def test_a_superseded_session_is_refused_by_the_websocket_entry_point(db, no_auth_override):
    _seed_user(db, "socket-user")
    old_token = _redeem_token(_browser(), _mint_token_for(db, "socket-user"))
    _redeem_token(_browser(), _mint_token_for(db, "socket-user"))

    with pytest.raises(HTTPException) as excinfo:
        verify_token_service(old_token, db)

    assert excinfo.value.status_code == status.HTTP_401_UNAUTHORIZED
    assert excinfo.value.detail == SESSION_SUPERSEDED_DETAIL