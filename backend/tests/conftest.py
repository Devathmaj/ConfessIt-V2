import contextlib
import os
import threading

# The signing key is required and validated (see app.config), so the suite pins a
# fixed test-only key before anything imports the settings object below. Tests mint
# and verify real JWTs against it in-process; no test depends on its value, only on
# it being stable within the run. `setdefault` keeps an explicitly exported key
# (e.g. CI) authoritative.
os.environ.setdefault(
    "SECRET_KEY",
    "e359d5a93f086c7c3a90b56c390fd03a1e700708cc21c5a5ebaf7ab01436c491",
)

import pytest
from anyio.from_thread import start_blocking_portal
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import settings
from app.db_models import Base
from app.postgres import get_postgres_client

# Point the app at the test database before anything builds the shared engine, so tests
# exercise the real dependency wiring (including the confession router's provider) against
# an isolated database. clear_db truncates, so refuse to run if that is not a separate
# database from the one the app is configured to use.
_configured_database_url = settings.DATABASE_URL
if not settings.TEST_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must be set to run the test suite")
if settings.TEST_DATABASE_URL == _configured_database_url:
    raise RuntimeError(
        "TEST_DATABASE_URL is identical to DATABASE_URL; refusing to run a suite that "
        "truncates the application's own database"
    )
settings.DATABASE_URL = settings.TEST_DATABASE_URL

# Keep the suite on a bounded pool, with headroom for the concurrency tests: the
# login files race up to four workers, each holding one connection for the whole
# rate-limit transaction while they serialise on the same account row. A pool max
# of exactly four turned that serialisation into pool exhaustion, so the pool
# covers the workers plus overflow instead of matching them exactly.
settings.DATABASE_POOL_SIZE = 4
settings.DATABASE_MAX_OVERFLOW = 4
# Never recycle a connection mid-suite: recycling closes and re-opens the socket on
# the next checkout, which would reintroduce the simultaneous-connect burst below.
settings.DATABASE_POOL_RECYCLE = 3600

# ``app.main`` refuses to start unless the Cloudinary cloud name, API key and API secret are
# all present, because those three are what sign the private love note delivery URLs and there
# is no second code path to fall back to. That is the behaviour we want in every environment,
# so the suite does not weaken it and does not inject credentials of its own -- the API secret is
# a deployment secret, and a test that needed a specific one would be testing the secret rather
# than the behaviour. The suite only ever needs to *shape* URLs, which the real configured
# credentials do as well as any fake, and no test here talks to Cloudinary.
from app.main import app
from app.accounts import AccountType
from app.models import AccountDetails, UserDetails
from app.services.auth_service import get_current_user
from app.services.redis_service import redis_service

test_db = get_postgres_client()
test_db.initialize()

# Open the persistent pool connections up front, sequentially. With a `localhost`
# database URL, simultaneous *new* connections stall for ~130 s before succeeding
# (`localhost` resolves to ::1 first while the forwarded port listens on IPv4 only,
# and sequential connects are instant), and the concurrency tests check out up to
# four connections at once -- without this, every concurrent test pays that stall
# on its first burst of checkouts. Prefer 127.0.0.1 loopback URLs for host-run
# suites regardless; the warm pool removes the burst either way.
_warmed_connections = [test_db.engine.connect() for _ in range(settings.DATABASE_POOL_SIZE)]
for _warmed_connection in _warmed_connections:
    _warmed_connection.close()
del _warmed_connections, _warmed_connection


_client_portal_stack: contextlib.ExitStack | None = None
_client_portal_lock = threading.Lock()


@pytest.fixture(autouse=True)
def _reuse_client_portals():
    """Serve every bare fixture client from one portal per test.

    A bare ``TestClient`` starts a fresh BlockingPortal (an OS thread plus an
    event loop) for every request. The fixtures below deliberately skip lifespan,
    so attaching a test-scoped portal keeps their requests on one warm loop
    without paying a lifespan startup per client. The stack stops the portal and
    closes the clients when the test ends.
    """
    global _client_portal_stack
    with contextlib.ExitStack() as stack:
        _client_portal_stack = stack
        try:
            yield
        finally:
            _client_portal_stack = None


def _attach_client_portal(client):
    """Attach the test-scoped portal to a fixture client, if a test is running."""
    stack = _client_portal_stack
    if stack is None:
        return client
    with _client_portal_lock:
        client.portal = stack.enter_context(start_blocking_portal())
        stack.callback(setattr, client, "portal", None)
        stack.callback(client.close)
    return client


def override_get_current_user():
    """A real UserDetails for an account in the ``users`` table.

    A real model, not a dict. Endpoints are annotated with UserDetails and reach for
    attributes such as .regno and .id. A dict stub is only ever truthy enough for the
    dependency to resolve, so it lets a handler fail on an AttributeError instead of
    exercising the code it is supposed to.

    ``account_type`` defaults to ``users``, but the participant gate reads it, so it is
    named here rather than left implicit: this fixture has to keep resolving to a
    participant no matter what default the model picks up later.
    """
    return UserDetails(
        regno="testuser",
        name="Test User",
        email="test@user.com",
        year=2,
        department="Test Class",
        gender="other",
        matchmaking_enabled=True,
        notifications_enabled=True,
        account_type=AccountType.USERS.value,
    )


def override_get_admin_user():
    """A real AccountDetails for an account in the ``admin`` table.

    Staff are the shared model, not the participant one: an admin has no participant
    fields, and handing the routes a UserDetails carrying an ``account_type`` of ``admin``
    would describe a row that cannot exist in the schema.

    ``email`` is set because it is required of every account now. It is not incidental to
    these fixtures: the model validates it on construction, so an override without one raised
    at import and took the whole suite down rather than failing one test.
    """
    return AccountDetails(
        regno="adminuser",
        name="Admin User",
        email="admin@user.com",
        notifications_enabled=True,
        account_type=AccountType.ADMIN.value,
    )


def override_get_moderator_user():
    """A real AccountDetails for an account in the ``moderator`` table.

    Built from ``AccountType.MODERATOR`` rather than a literal, so the fixture cannot drift
    from the vocabulary if the table names ever change. ``email`` is required on every
    account, for the same reason the admin fixture carries one.
    """
    return AccountDetails(
        regno="moduser",
        name="Moderator User",
        email="moderator@user.com",
        notifications_enabled=True,
        account_type=AccountType.MODERATOR.value,
    )

app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(autouse=True)
def authenticated_user():
    """Re-install the auth override before every test.

    A test that needs to check the unauthenticated path pops this override itself. That
    used to leak: once popped it stayed popped, so every test module collected afterwards
    ran with no auth at all and every authenticated endpoint answered 401. Re-applying it
    per test makes the authenticated default independent of collection order while still
    letting an individual test opt out.
    """
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield
    app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(autouse=True)
def _stub_redis_publish(monkeypatch):
    """Skip Redis pub/sub delivery in tests.

    No test subscribes, and ``publish_message`` already swallows failures, so
    skipping changes nothing observable. Without this, every ``send_message``
    pays seconds for a best-effort notification nobody reads: on a host-run
    suite the compose ``redis`` hostname costs ~4 s per DNS miss (retried), and
    even a refused loopback connect costs ~2 s on some Windows hosts.
    """
    monkeypatch.setattr(redis_service, "publish_message", lambda *args, **kwargs: False)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def _confession_author_client(db):
    """A client whose participant resolves to a row that actually exists.

    The shared ``client`` fixture resolves ``get_current_user`` to a fresh ``UserDetails``
    on every call, and ``AccountDetails.id`` defaults to a new uuid, so the author id it
    hands the handler is not in ``users``. That is fine for the 422 paths -- they are
    refused before the insert -- but a test that needs a row to land in the database has
    to name the id up front and put that row there first.
    """
    author_id = "33333333-3333-3333-3333-333333333333"
    with test_db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, 'authoruser', 'Author', 'author@user.com', 2, 'Class', 'other') "
                "ON CONFLICT (regno) DO NOTHING"
            ),
            {"id": author_id},
        )
    author = UserDetails(
        id=author_id,
        regno="authoruser",
        name="Author",
        email="author@user.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )
    app.dependency_overrides[get_current_user] = lambda: author
    try:
        yield _attach_client_portal(TestClient(app, follow_redirects=False))
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture
def admin_client():
    """A client whose ``get_current_user`` resolves to an admin.

    ``admin_required`` depends on ``get_current_user``, so overriding that single
    dependency is enough to reach the admin routes.
    """
    app.dependency_overrides[get_current_user] = override_get_admin_user
    try:
        yield _attach_client_portal(TestClient(app, follow_redirects=False))
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture
def moderator_client():
    """A client whose ``get_current_user`` resolves to a moderator.

    Capability dependencies also hang off ``get_current_user``, so overriding that one
    dependency is enough to reach the moderation routes. This is what makes the
    admin-only / moderator-only split testable without minting a real admin token.
    """
    app.dependency_overrides[get_current_user] = override_get_moderator_user
    try:
        yield _attach_client_portal(TestClient(app, follow_redirects=False))
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(scope="module")
def db():
    return test_db


@pytest.fixture(autouse=True)
def clear_db():
    """Empty every application table so tests cannot leak state into each other."""
    with test_db.engine.begin() as connection:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    yield
