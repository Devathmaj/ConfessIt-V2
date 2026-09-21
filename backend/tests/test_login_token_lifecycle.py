"""The lifecycle of a passwordless login token: minted, consumed once, refused after that.

A login link is a bearer credential printed to the developer console, so the server keeps
only its SHA-256 hash and holds the plaintext to nothing but the link itself. What these
tests pin is the rest of the contract:

* a token is bound to one account and dies on a short, configured clock (10-15 minutes);
* redemption succeeds exactly once -- including when several requests race for the same
  token, which is decided by a conditional UPDATE rather than by a read-then-write;
* expired, unknown and already-used tokens are all refused with one indistinguishable
  answer to the caller while the log still says which one actually happened;
* the login response itself never varies with whether the registration number exists.

Time is faked rather than waited out: the rate limiter reads its clock through one
function, and the fixtures below replace it so a five-minute window costs no five
minutes. Token expiry is arranged by backdating the stored row, which is the same state
a real token reaches by waiting.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoginTokenRecord, UserRecord, find_account_in_table
from app.main import app
from app.services import auth_service as auth_service_module
from app.services import login_rate_limit as login_rate_limit_module
from app.services.auth_service import LOGIN_LINK_RESPONSE_MESSAGE, hash_token

LOGIN_ENDPOINT = f"/auth/login/{AccountType.USERS.value}/magic"
VERIFY_ENDPOINT = "/auth/login/magic/verify"


class _FromHost:
    """Presents every request as arriving from one fixed address.

    Two things downstream depend on it: ``login_tokens.request_ip`` is an ``inet``
    column, which the default ``testclient`` hostname is not storable as, and redemption
    pins a token to the address it was minted from. The host is a parameter rather than
    a constant so a test can speak for more than one address without a second wrapper.
    """

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser: a client with its own cookie jar, and therefore its own client id."""
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
    """Mint a token the way the endpoint does, but hand the plaintext to the test only.

    Bypassing the endpoint here keeps the token-lifecycle tests independent of the
    request limits: a test about redemption should not have to spend a request from the
    client budget to obtain the thing it is about to redeem.
    """
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


def _token_count(db):
    with db.sessions() as session:
        return len(session.scalars(select(LoginTokenRecord)).all())


def _all_tokens(db):
    with db.sessions() as session:
        return session.scalars(select(LoginTokenRecord)).all()


def _backdate_expiry(db, plaintext, *, minutes=1):
    with db.sessions.begin() as session:
        session.execute(
            update(LoginTokenRecord)
            .where(LoginTokenRecord.token_hash == hash_token(plaintext))
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))
        )


@pytest.fixture
def clock(monkeypatch):
    """A fake clock for the limiter. Nothing here waits out a real window."""

    class _Clock:
        current = datetime.now(timezone.utc)

        def advance(self, seconds):
            self.current += timedelta(seconds=seconds)

    clock = _Clock()
    monkeypatch.setattr(login_rate_limit_module, "_utcnow", lambda: clock.current)
    return clock


@pytest.fixture
def auth_log(monkeypatch):
    """Captures the structured application log lines, as strings.

    The loggers in ``auth_service`` do not propagate (the console-only one must never
    reach the collector), so ``caplog`` alone would see nothing. Patching the two
    methods captures exactly what an operator would be able to search for later -- and
    notably not the console-only magic-link logger, whose whole purpose is to keep the
    plaintext token out of any retained log.
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


# ----------------------------------------------------------------------
# Expiry
# ----------------------------------------------------------------------


def test_a_minted_token_lives_for_a_configured_ttl_between_ten_and_fifteen_minutes(db):
    _seed_user(db, "ttl-user")
    browser = _browser()

    response = browser.post(LOGIN_ENDPOINT, json={"regno": "ttl-user"})
    assert response.status_code == 200, response.text

    rows = _all_tokens(db)
    assert len(rows) == 1
    ttl = rows[0].expires_at - rows[0].issued_at
    assert ttl == timedelta(minutes=settings.LOGIN_TOKEN_EXPIRE_MINUTES)
    assert timedelta(minutes=10) <= ttl <= timedelta(minutes=15)


def test_an_expired_token_is_refused(db, auth_log):
    _seed_user(db, "slow-user")
    plaintext = _mint_token_for(db, "slow-user")
    _backdate_expiry(db, plaintext, minutes=1)

    response = _browser().get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired token"
    assert _token_row(db, plaintext).used is False
    assert any("Expired magic link token" in line for line in auth_log["warning"])


# ----------------------------------------------------------------------
# Consumption and reuse
# ----------------------------------------------------------------------


def test_redeem_consumes_the_token(db, auth_log):
    _seed_user(db, "fresh-user")
    plaintext = _mint_token_for(db, "fresh-user")

    response = _browser().get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    row = _token_row(db, plaintext)
    assert row.used is True
    assert row.consumed_at is not None
    assert any("verification success" in line for line in auth_log["info"])


def test_a_consumed_token_cannot_be_redeemed_twice(db, auth_log):
    _seed_user(db, "one-shot-user")
    plaintext = _mint_token_for(db, "one-shot-user")
    browser = _browser()

    first = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    second = browser.get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    assert first.status_code == 200, first.text
    assert second.status_code == 400
    assert second.json()["detail"] == "Invalid or expired token"
    assert _token_row(db, plaintext).used is True
    assert any("Already-used magic link token" in line for line in auth_log["warning"])


def test_concurrent_redeem_attempts_produce_exactly_one_session(db):
    _seed_user(db, "raced-user")
    plaintext = _mint_token_for(db, "raced-user")

    def redeem(_):
        return _browser().get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(redeem, range(4)))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1, statuses
    assert sorted(set(statuses)) == [200, 400], statuses
    winner = next(response for response in responses if response.status_code == 200)
    assert winner.json()["access_token"]
    assert _token_row(db, plaintext).used is True


def test_an_unrecognised_token_is_refused(db, auth_log):
    response = _browser().get(f"{VERIFY_ENDPOINT}?token=not-a-real-token")

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired token"
    assert _token_count(db) == 0
    assert any("Invalid magic link token" in line for line in auth_log["warning"])


# ----------------------------------------------------------------------
# The response never varies with whether the account exists
# ----------------------------------------------------------------------


def test_the_login_response_is_identical_for_a_real_and_an_invented_registration_number(
    db, clock
):
    _seed_user(db, "real-user")
    real_browser = _browser()
    invented_browser = _browser()

    real = real_browser.post(LOGIN_ENDPOINT, json={"regno": "real-user"})
    invented = invented_browser.post(LOGIN_ENDPOINT, json={"regno": "never-registered"})

    assert real.status_code == invented.status_code == 200
    assert real.json() == invented.json() == {"message": LOGIN_LINK_RESPONSE_MESSAGE}
    assert "real-user" not in real.text
    assert "real-user@example.com" not in real.text
    assert "Real User" not in real.text

    cooled_real = real_browser.post(LOGIN_ENDPOINT, json={"regno": "real-user"})
    cooled_invented = invented_browser.post(LOGIN_ENDPOINT, json={"regno": "never-registered"})

    assert cooled_real.status_code == cooled_invented.status_code == 429
    assert cooled_real.json() == cooled_invented.json()
    assert cooled_real.headers["Retry-After"] == cooled_invented.headers["Retry-After"]


def test_the_login_response_never_echoes_account_details(db):
    _seed_user(db, "shy-user", name="Very Private Name")

    response = _browser().post(LOGIN_ENDPOINT, json={"regno": "shy-user"})

    assert response.status_code == 200
    assert "shy-user@example.com" not in response.text
    assert "Very Private Name" not in response.text
    assert LOGIN_LINK_RESPONSE_MESSAGE in response.text


# ----------------------------------------------------------------------
# Observability
# ----------------------------------------------------------------------


def test_every_token_outcome_is_logged_by_name(db, auth_log):
    _seed_user(db, "observed-user")

    minted = _browser().post(LOGIN_ENDPOINT, json={"regno": "observed-user"})
    assert minted.status_code == 200

    plaintext = _mint_token_for(db, "observed-user")
    expired = _mint_token_for(db, "observed-user")
    _backdate_expiry(db, expired, minutes=1)
    _browser().get(f"{VERIFY_ENDPOINT}?token={expired}")

    consumed = _mint_token_for(db, "observed-user")
    assert _browser().get(f"{VERIFY_ENDPOINT}?token={consumed}").status_code == 200
    assert _browser().get(f"{VERIFY_ENDPOINT}?token={consumed}").status_code == 400

    assert _browser().get(f"{VERIFY_ENDPOINT}?token=unheard-of").status_code == 400

    joined_info = "\n".join(auth_log["info"])
    joined_warning = "\n".join(auth_log["warning"])
    assert "Login request accepted" in joined_info
    assert "verification success" in joined_info
    assert "Expired magic link token" in joined_warning
    assert "Already-used magic link token" in joined_warning
    assert "Invalid magic link token" in joined_warning


def test_logs_carry_no_token_material(db, auth_log):
    _seed_user(db, "quiet-user")
    plaintext = _mint_token_for(db, "quiet-user")
    digest = hash_token(plaintext)

    assert _browser().get(f"{VERIFY_ENDPOINT}?token={plaintext}").status_code == 200
    assert _browser().get(f"{VERIFY_ENDPOINT}?token={plaintext}").status_code == 400
    assert _browser().get(f"{VERIFY_ENDPOINT}?token=unheard-of").status_code == 400
    _browser().post(LOGIN_ENDPOINT, json={"regno": "quiet-user"})

    everything = "\n".join(auth_log["info"] + auth_log["warning"])
    assert plaintext not in everything
    assert digest not in everything
