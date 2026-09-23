"""Layered request limits on the passwordless login endpoint.

Three keys count every login request independently -- the submitted registration number,
a server-generated client id in a cookie, and the source address -- and the tests below
exercise each one on the real endpoint against the real database:

* ``account``: one link per registration number per cooldown window, plus a wider
  hourly ceiling, so a single account cannot be spammed with links;
* ``client``: a small budget per browser, which is what stops an attacker walking
  through sequential registration numbers (user1001, user1002, ...) while every
  per-account cooldown is technically respected;
* ``ip``: a generous throttle on total volume, generous because many legitimate users
  share one address.

Windows are advanced with a fake clock rather than waited out, and the state itself is
asserted on: it lives in one table, expires with its window, and is swept on the next
request. Concurrency is covered too, because a check-then-write that is atomic in a
single test is not atomic under a race.
"""

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.config import settings
from app.db_models import LoginRateLimitRecord, LoginTokenRecord, UserRecord
from app.main import app
from app.services import login_rate_limit as login_rate_limit_module

LOGIN_ENDPOINT = f"/auth/login/{AccountType.USERS.value}/magic"


class _FromHost:
    """Presents every request as arriving from a fixed, storable address.

    ``login_tokens.request_ip`` is an ``inet`` column, which the default ``testclient``
    hostname is not, and every token is pinned to the address it was minted from.
    """

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1", base_url="http://testserver"):
    """A browser: one client, one cookie jar, one client id."""
    return _attach_portal(TestClient(_FromHost(host), base_url=base_url))


_browser_stack: contextlib.ExitStack | None = None
_browser_lock = threading.Lock()


@pytest.fixture(autouse=True)
def _reuse_browser_portal():
    """Serve every ``_browser()`` request from one portal per test.

    A bare ``TestClient`` starts a fresh BlockingPortal (an OS thread plus an
    event loop) for every request and tears it down afterwards. This file builds
    dozens of clients -- and the concurrency test builds them on four threads at
    once -- so that churn is what turned the suite from minutes into an hour.
    Attaching one portal per test keeps requests on a single warm loop; the stack
    stops it when the test ends. Lifespan is deliberately not run, as before.
    """
    global _browser_stack
    with contextlib.ExitStack() as stack:
        _browser_stack = stack
        try:
            yield
        finally:
            _browser_stack = None


def _attach_portal(client):
    """Attach the test-scoped portal to a fresh client, if a test is running."""
    stack = _browser_stack
    if stack is None:
        return client
    with _browser_lock:
        client.portal = stack.enter_context(start_blocking_portal())
        stack.callback(setattr, client, "portal", None)
        stack.callback(client.close)
    return client


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


def _token_count(db):
    with db.sessions() as session:
        return len(session.scalars(select(LoginTokenRecord)).all())


def _login(browser, regno):
    return browser.post(LOGIN_ENDPOINT, json={"regno": regno})


@pytest.fixture
def clock(monkeypatch):
    """The limiter's clock, under the test's control.

    Every window here is five minutes or an hour of wall time, and none of them is
    waited out: the limiter reads ``now`` through one function, this fixture replaces
    that function, and ``advance`` moves it. The application's other clocks (token
    expiry, row timestamps) deliberately keep real time -- nothing below depends on
    them shifting.
    """

    class _Clock:
        current = datetime.now(timezone.utc)

        def advance(self, seconds):
            self.current += timedelta(seconds=seconds)

    clock = _Clock()
    monkeypatch.setattr(login_rate_limit_module, "_utcnow", lambda: clock.current)
    return clock


# ----------------------------------------------------------------------
# Account cooldown
# ----------------------------------------------------------------------


def test_one_login_request_per_account_per_cooldown_window(db):
    _seed_user(db, "cooldown-user")
    _seed_user(db, "other-user")
    browser = _browser()

    first = _login(browser, "cooldown-user")
    second = _login(browser, "cooldown-user")

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert "registration number" in second.json()["detail"]
    assert int(second.headers["Retry-After"]) > 0
    assert _token_count(db) == 1, "the refused request must not mint a second link"

    other = _login(browser, "other-user")
    assert other.status_code == 200, other.text
    assert _token_count(db) == 2


def test_the_account_cooldown_lifts_when_its_window_elapses(db, clock):
    _seed_user(db, "patient-user")
    browser = _browser()

    assert _login(browser, "patient-user").status_code == 200

    clock.advance(settings.LOGIN_ACCOUNT_COOLDOWN_SECONDS - 1)
    assert _login(browser, "patient-user").status_code == 429

    clock.advance(2)
    assert _login(browser, "patient-user").status_code == 200
    assert _token_count(db) == 2


def test_the_long_account_window_bounds_sustained_requesting(db, clock):
    """The short window alone still allows one link every window, forever.

    The wider, separately configured window is what caps the total: five links an hour
    is a drip, not a stream, however patiently the short windows are spaced.
    """
    _seed_user(db, "steady-user")
    browser = _browser()

    assert _login(browser, "steady-user").status_code == 200
    for _ in range(settings.LOGIN_ACCOUNT_LONG_WINDOW_LIMIT - 1):
        clock.advance(settings.LOGIN_ACCOUNT_COOLDOWN_SECONDS + 1)
        assert _login(browser, "steady-user").status_code == 200

    clock.advance(settings.LOGIN_ACCOUNT_COOLDOWN_SECONDS + 1)
    throttled = _login(browser, "steady-user")

    assert throttled.status_code == 429
    assert "Too many login requests for this registration number" in throttled.json()["detail"]
    assert _token_count(db) == settings.LOGIN_ACCOUNT_LONG_WINDOW_LIMIT


def test_concurrent_requests_cannot_share_one_account_cooldown(db):
    """Four simultaneous requests for one account produce one link between them.

    The counter is a single upsert per key, so the four requests serialise on the row
    and see counts 1, 2, 3 and 4 -- exactly one of which is inside the budget. A
    read-then-write limiter would let several of them read "0" before any wrote, and
    hand out four links for the price of one.
    """
    _seed_user(db, "rush-user")

    def request(_):
        return _login(_browser(), "rush-user")

    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(request, range(4)))

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == 1, statuses
    assert set(statuses) <= {200, 429}, statuses
    assert _token_count(db) == 1


# ----------------------------------------------------------------------
# Client-level limiting: the username-cycling attack
# ----------------------------------------------------------------------


def test_sequential_usernames_from_one_browser_hit_the_client_limit(db):
    """user1001, user1002, ... -- six different accounts, one browser, still throttled.

    Every per-account cooldown is respected (each username is requested exactly once),
    so the account key can never be what refuses this. The browser's own budget is, and
    it does not move when the username underneath it does.
    """
    regnos = [f"user{n}" for n in range(1001, 1007)]
    for regno in regnos:
        _seed_user(db, regno)
    browser = _browser()

    responses = [_login(browser, regno) for regno in regnos]

    statuses = [response.status_code for response in responses]
    assert statuses[:5] == [200] * 5, statuses
    assert statuses[5] == 429, statuses
    assert "browser" in responses[5].json()["detail"]
    assert int(responses[5].headers["Retry-After"]) > 0
    assert _token_count(db) == 5, "the throttled request must not mint a link"


def test_the_client_limit_is_per_browser_not_per_ip(db):
    """A second browser on the same address starts with its own fresh budget.

    The client id is what separates them: it is generated by the server, carried in a
    cookie, and never derived from anything the caller sends -- least of all the
    user-agent, which would make the key as forgeable as the header it came from.
    """
    _seed_user(db, "colocated-user")
    throttled_browser = _browser()
    for regno in ("ghost-a", "ghost-b", "ghost-c", "ghost-d", "ghost-e"):
        assert _login(throttled_browser, regno).status_code == 404
    assert _login(throttled_browser, "ghost-f").status_code == 429

    fresh_browser = _browser()
    assert _login(fresh_browser, "colocated-user").status_code == 200
    assert _token_count(db) == 1, "only the real account mints a link; the ghosts never did"


# ----------------------------------------------------------------------
# IP-level limiting
# ----------------------------------------------------------------------


def test_the_ip_limit_throttles_many_distinct_clients_on_one_address(db):
    limit = settings.LOGIN_IP_LIMIT
    browser = _browser()
    responses = []
    for index in range(limit + 1):
        browser.cookies.clear()
        responses.append(_login(browser, f"ghost-{index}"))

    statuses = [response.status_code for response in responses]
    assert statuses[:limit] == [404] * limit, statuses
    assert statuses[limit] == 429, statuses
    assert "network" in responses[limit].json()["detail"]
    assert int(responses[limit].headers["Retry-After"]) > 0


def test_legitimate_users_sharing_one_ip_are_not_throttled(db):
    """Six people behind one NAT, one request each: nothing is refused.

    The IP budget exists as a brake on volume, not as a ban -- it is sized so that a
    plausible burst of real people on a shared address passes while a sweep of hundreds
    of registration numbers does not. The stricter limit is the client's, which is why
    this test can give every user a browser of their own and still sit well under both.
    """
    regnos = [f"flatmate{n}" for n in range(6)]
    for regno in regnos:
        _seed_user(db, regno)

    responses = [_login(_browser(), regno) for regno in regnos]

    assert [response.status_code for response in responses] == [200] * len(regnos)
    assert _token_count(db) == len(regnos)
    assert len(regnos) < settings.LOGIN_IP_LIMIT


# ----------------------------------------------------------------------
# The client id itself
# ----------------------------------------------------------------------


def test_the_client_id_is_issued_once_and_then_reused(db):
    cookie_name = settings.LOGIN_CLIENT_COOKIE_NAME
    first_browser = _browser()

    first = _login(first_browser, "ghost-a")
    set_cookie = first.headers.get("set-cookie", "")
    assert cookie_name in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    issued = first_browser.cookies.get(cookie_name)
    assert issued

    second = _login(first_browser, "ghost-b")
    assert cookie_name not in (second.headers.get("set-cookie") or "")
    assert first_browser.cookies.get(cookie_name) == issued

    with db.sessions() as session:
        row = session.get(LoginRateLimitRecord, f"login:client:{issued}")
    assert row is not None, "both requests must have counted against one key"
    assert row.hits == 2

    other_browser = _browser()
    assert _login(other_browser, "ghost-c").status_code == 404
    assert other_browser.cookies.get(cookie_name) != issued


def test_a_malformed_client_cookie_is_reissued_rather_than_honoured(db):
    cookie_name = settings.LOGIN_CLIENT_COOKIE_NAME
    browser = _browser()
    browser.cookies.set(cookie_name, "short", domain="testserver")

    response = _login(browser, "ghost-a")

    assert response.status_code == 404
    issued = [cookie.value for cookie in browser.cookies.jar
              if cookie.name == cookie_name and cookie.value != "short"]
    assert issued, "the malformed value must be replaced with a fresh id"
    with db.sessions() as session:
        assert session.get(LoginRateLimitRecord, "login:client:short") is None


def test_the_client_cookie_is_marked_secure_when_the_request_arrives_over_https(db):
    browser = _browser(base_url="https://testserver")

    response = _login(browser, "ghost-a")

    assert response.status_code == 404
    assert "secure" in (response.headers.get("set-cookie") or "").lower()


# ----------------------------------------------------------------------
# Rate-limit state
# ----------------------------------------------------------------------


def test_rate_limit_state_expires_and_is_swept(db, clock):
    """Rows leave with their window, so the table only holds limits still being hit."""
    browser = _browser()
    assert _login(browser, "ghost-expiry").status_code == 404

    stale_key = "login:ip:203.0.113.9"
    past = clock.current - timedelta(minutes=10)
    with db.sessions.begin() as session:
        session.add(LoginRateLimitRecord(
            key=stale_key,
            window_started_at=past,
            hits=9,
            expires_at=past + timedelta(seconds=settings.LOGIN_IP_WINDOW_SECONDS),
        ))

    clock.advance(settings.LOGIN_IP_WINDOW_SECONDS + 1)
    assert _login(_browser(), "ghost-after-sweep").status_code == 404

    with db.sessions() as session:
        assert session.get(LoginRateLimitRecord, stale_key) is None
        assert session.get(LoginRateLimitRecord, "login:account:users:ghost-expiry") is None
        surviving = session.scalars(select(LoginRateLimitRecord)).all()
        assert surviving, "the request that just ran should have created its own windows"
        assert all(row.expires_at > clock.current for row in surviving)
