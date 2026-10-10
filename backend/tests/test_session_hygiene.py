"""Session hygiene: replay visibility and block-time session retirement (SEC-008).

Two gaps, both enforcement-neutral on their own:

* A refresh replay (second presentation of a rotated-away token) was refused
  correctly but left no countable trace and never told the holder. The refusal
  is unchanged; the event now counts on the session row and surfaces one
  hourly notice to the holder.
* Blocking flipped ``is_blocked`` while the session table still read ``active``.
  Every auth path re-checked the block, so nothing was exploitable -- but no
  status-only reader could tell a blocked session from a live one. Blocking now
  retires live sessions and unredeemed links in the same transaction, and
  unblocking does not resurrect them.
* The automatic block at the removal threshold is the same block: the same flag,
  the same retirement, and the same admin unblock route. The last test drives the
  whole arc -- removal, refusal, unblock, sign-in -- so the two cannot drift.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoginTokenRecord, NotificationRecord, UserRecord, find_account_in_table
from app.main import app
from app.services.auth_service import (
    SESSION_STATUS_ACTIVE,
    SESSION_STATUS_REVOKED,
    TOKEN_HASH_DOMAIN_MAGIC,
    TOKEN_HASH_DOMAIN_REFRESH,
    get_current_user,
    hash_token,
)
from app.services.report_counters import (
    CONFESSION,
    LOVE_NOTE,
    MESSAGE,
    record_removal,
)

VERIFY_ENDPOINT = "/auth/login/magic/verify"
REFRESH_ENDPOINT = "/auth/refresh"


@pytest.fixture
def no_auth_override():
    """Run the real ``get_current_user`` instead of the conftest's fixed account."""
    saved = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield
    finally:
        if saved is not None:
            app.dependency_overrides[get_current_user] = saved


class _FromHost:
    """Presents every request as arriving from one fixed address."""

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
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


def _mint_token_for(db, regno):
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
    assert found is not None
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "Test Agent"}

    token, _ = create_and_store_magic_token(db, str(row.id), resolved_type, _Request())
    return token


def _redeem_token(browser, plaintext):
    response = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _refresh_cookie(browser):
    return browser.cookies.get(settings.REFRESH_COOKIE_NAME)


def _session_row(db, session_id):
    with db.sessions() as session:
        return session.get(LoginTokenRecord, session_id)


def _session_id_of(db, raw_refresh):
    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.refresh_token_hash == hash_token(raw_refresh, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert row is not None
    return row.id


def _holder_notices(db, regno):
    with db.sessions() as session:
        return session.scalars(
            select(NotificationRecord).where(
                NotificationRecord.recipient_regno == regno
            )
        ).all()


def test_a_proven_replay_is_counted_and_notifies_the_holder_once(
    db, no_auth_override, monkeypatch
):
    """A proven replay counts on the session row and tells the holder once/hour."""

    class _FakeRedis:
        def __init__(self):
            self.keys = {}

        def set(self, name, value, ex=None, nx=False):
            if nx and name in self.keys:
                return None
            self.keys[name] = value
            return True

    monkeypatch.setattr(
        "app.services.redis_service.redis_service",
        SimpleNamespace(client=_FakeRedis()),
    )

    _seed_user(db, "replay-notice-user")
    browser = _browser()
    _redeem_token(browser, _mint_token_for(db, "replay-notice-user"))
    assert browser.post(REFRESH_ENDPOINT).status_code == 200
    fresh_cookie = _refresh_cookie(browser)

    session_id = _session_id_of(db, fresh_cookie)
    before = _session_row(db, session_id).attempt_count

    # Drive the proven-replay branch directly: presenting the rotated-away value
    # after the rotation committed is exactly what the branch recognises.
    from app.services import auth_service as auth_module

    auth_module._record_refresh_replay(db, session_id, "replay-notice-user")
    assert _session_row(db, session_id).attempt_count == before + 1

    notices = _holder_notices(db, "replay-notice-user")
    assert len(notices) == 1
    assert notices[0].heading == "An old session credential was used"

    # A second recording counts again but does not nag again (hourly gate).
    auth_module._record_refresh_replay(db, session_id, "replay-notice-user")
    assert _session_row(db, session_id).attempt_count == before + 2
    assert len(_holder_notices(db, "replay-notice-user")) == 1

    # The session itself is untouched by all of this: the fresh value works.
    assert browser.post(REFRESH_ENDPOINT).status_code == 200


def test_recording_a_replay_never_breaks_the_refusal(db, no_auth_override, monkeypatch):
    """Bookkeeping failure (dead Redis, bad row) must not turn a 401 into a 500."""
    from app.services import auth_service as auth_module

    class _DeadRedis:
        @property
        def client(self):
            raise ConnectionError("redis is gone")

    monkeypatch.setattr(
        "app.services.redis_service.redis_service", _DeadRedis()
    )

    # Unknown session and no holder: nothing to count, nobody to tell, no raise.
    auth_module._record_refresh_replay(db, "11111111-1111-1111-1111-111111111111", None)


def test_blocking_an_account_retires_its_sessions_and_links(db, no_auth_override, admin_client):
    """Blocking retires live sessions and unredeemed links; unblock resurrects nothing."""
    from conftest import override_get_admin_user

    _seed_user(db, "doomed-user")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "doomed-user"))
    cookie = _refresh_cookie(browser)
    live_link = _mint_token_for(db, "doomed-user")

    with db.sessions() as session:
        user_id = str(session.scalar(
            select(UserRecord.id).where(UserRecord.regno == "doomed-user")
        ))
        live_id = session.scalar(
            select(LoginTokenRecord.id).where(
                LoginTokenRecord.refresh_token_hash
                == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert _session_row(db, live_id).session_status == SESSION_STATUS_ACTIVE

    response = admin_client.put(f"/admin/users/{user_id}/block?blocked=true")
    assert response.status_code == 200, response.text

    retired = _session_row(db, live_id)
    assert retired.session_status == SESSION_STATUS_REVOKED
    with db.sessions() as session:
        link = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.token_hash
                == hash_token(live_link, domain=TOKEN_HASH_DOMAIN_MAGIC)
            )
        )
    assert link is not None and link.used is False and link.revoked is True

    # Resolve authz back to the real dependency: the admin override would answer
    # /auth/me as staff rather than refusing the dead session.
    app.dependency_overrides.pop(get_current_user, None)
    try:
        refused = browser.post(REFRESH_ENDPOINT)
        assert refused.status_code == 401
        access = browser.get(
            "/auth/me", headers={"Authorization": f"Bearer {access_token}"}
        )
        assert access.status_code == 401
    finally:
        app.dependency_overrides[get_current_user] = override_get_admin_user

    unblocked = admin_client.put(f"/admin/users/{user_id}/block?blocked=false")
    assert unblocked.status_code == 200, unblocked.text
    assert _session_row(db, live_id).session_status == SESSION_STATUS_REVOKED


def test_the_automatic_block_retires_sessions_and_an_admin_unblocks_sign_in(
    db, admin_client
):
    """Six removals block the account exactly as a manual block would.

    Driven through ``record_removal``, the funnel every removal path -- rulings, direct hides
    and the automated takedowns -- passes through, so the threshold logic is exercised where
    all of them meet it. The live session and the unredeemed link are retired in the same
    transaction, sign-in is refused while the flag is set, and the admin unblock route is what
    lets the participant in again.
    """
    _seed_user(db, "repeat-offender")
    browser = _browser()
    access_token = _redeem_token(browser, _mint_token_for(db, "repeat-offender"))
    cookie = _refresh_cookie(browser)
    live_link = _mint_token_for(db, "repeat-offender")
    assert access_token

    with db.sessions() as session:
        user_id = str(session.scalar(
            select(UserRecord.id).where(UserRecord.regno == "repeat-offender")
        ))
        live_id = session.scalar(
            select(LoginTokenRecord.id).where(
                LoginTokenRecord.refresh_token_hash
                == hash_token(cookie, domain=TOKEN_HASH_DOMAIN_REFRESH)
            )
        )
    assert _session_row(db, live_id).session_status == SESSION_STATUS_ACTIVE

    with db.sessions.begin() as session:
        for kind in (CONFESSION, LOVE_NOTE, MESSAGE, CONFESSION, LOVE_NOTE, MESSAGE):
            assert record_removal(session, kind=kind, author_regno="repeat-offender") is True

    with db.sessions() as session:
        assert session.get(UserRecord, user_id).is_blocked is True

    assert _session_row(db, live_id).session_status == SESSION_STATUS_REVOKED
    with db.sessions() as session:
        link = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.token_hash
                == hash_token(live_link, domain=TOKEN_HASH_DOMAIN_MAGIC)
            )
        )
    assert link is not None and link.used is False and link.revoked is True

    # The blocked account cannot sign in: a fresh link is refused at redemption.
    refused = _browser().get(
        f"{VERIFY_ENDPOINT}?token={_mint_token_for(db, 'repeat-offender')}"
    )
    assert refused.status_code == 403

    unblocked = admin_client.put(f"/admin/users/{user_id}/block?blocked=false")
    assert unblocked.status_code == 200, unblocked.text

    # And once an administrator clears the flag, a fresh link works again.
    fresh_access = _redeem_token(_browser(), _mint_token_for(db, "repeat-offender"))
    assert fresh_access
