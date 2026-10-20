import base64
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
# Same treatment for the token-hash pepper: fixed, test-only, and shared by every
# worker-equivalent in the run, so stored hashes minted in one test verify in the next.
os.environ.setdefault(
    "TOKEN_HASH_PEPPER",
    "bb5e97d44532f0cf881d23496899e04175c30d8db62b501c5a22057ab1200e31",
)
# Chat encryption is required at startup and is never allowed to fall back to
# plaintext. This fixed, test-only 32-byte key is set before app.config imports
# so message tests can encrypt/decrypt against the real service. It is not a
# deployment secret and exists only for this process group.
os.environ.setdefault(
    "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION",
    "v1",
)
os.environ.setdefault(
    "CHAT_ENCRYPTION_KEY_V1",
    base64.b64encode(b"confessit-test-key-32-bytes-0000").decode(),
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
from app.services.auth_service import get_current_user, get_current_user_optional
from app.services.redis_service import redis_service

# The out-of-band refresh-token body flow is gated behind ENABLE_DEBUG_ENDPOINTS in
# production, and the rotation mechanics tests drive the cookie jar instead (they
# hold raw tokens the way a browser holds cookies, presenting them per request or
# from the jar). The suite therefore runs with the flag off -- the production
# posture -- so a test can only pass through the credential path a deployment
# serves. The gate itself is pinned by dedicated tests that switch the flag on
# explicitly (see test_auth_csrf).
settings.ENABLE_DEBUG_ENDPOINTS = False

# The AI generation worker is a background thread that starts with the app's
# lifespan. Tests drive ``ai_game_automation.process_due_jobs`` directly --
# deterministically, with mocked providers -- so the thread must never run
# against the test database behind a test's back. Tests that exercise the
# startup hook itself re-enable the flag locally.
settings.AI_GENERATION_WORKER_ENABLED = False

# Magic-link tests exercise the real mint path. The email hand-off is a separate
# integration and must never reach the local Functions host from the suite, so
# the queue URL is cleared by default; the email integration tests set it (and
# stub the HTTP client) explicitly.
settings.EMAIL_FUNCTION_URL = None

# The host allowlist is read per request: Starlette's TestClient answers as
# "testserver", which can never resolve publicly and is not a production name, so the
# suite admits it here rather than baking a test-only hostname into the default.
if "testserver" not in settings.ALLOWED_HOSTS:
    settings.ALLOWED_HOSTS += ",testserver"

# The global flood brake counts in Redis, which the suite cannot reach (and must not
# need): disabled here, pinned by dedicated tests with a stubbed store.
settings.GLOBAL_RATE_LIMIT_PER_MINUTE = 0

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
    # Optional-auth routes (the public feed and search) resolve through a different
    # dependency, so a client presented as a specific author has to be that author there
    # too -- otherwise ownership flags such as ``is_mine`` are computed for nobody.
    saved_optional = app.dependency_overrides.get(get_current_user_optional)
    app.dependency_overrides[get_current_user_optional] = lambda: author
    try:
        yield _attach_client_portal(TestClient(app, follow_redirects=False))
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user
        app.dependency_overrides.pop(get_current_user_optional, None)
        if saved_optional is not None:
            app.dependency_overrides[get_current_user_optional] = saved_optional


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
    # Admin list totals are cached briefly (30s) beside each page. Truncating
    # every table without clearing that cache leaves the next test reading the
    # previous test's total, so the cache is part of the state that must not
    # leak either.
    try:
        from app.services.admin_service import _count_cache as _admin_count_cache

        _admin_count_cache.clear()
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _mock_content_safety_safe(monkeypatch):
    """Safe Content Safety default so existing creation paths keep working.

    Batch 1 moderation runs before persistence and fails closed on missing
    config or provider errors. Without this, every pre-existing test that
    posts via ``ConfessionService.create_confession`` would try the real
    Azure endpoint. This provides dummy config + a safe (all-zero) analysis
    so those paths behave as low-severity by default without real credentials.

    Validation is preserved: the mock still refuses missing endpoint/key, so
    the missing-configuration operational error remains testable by clearing
    the settings. Tests needing other severities or failures override this
    mock explicitly with their own ``monkeypatch.setattr``.
    """
    monkeypatch.setattr(settings, "CONTENT_SAFETY_ENDPOINT", "https://test-content-safety.example.com")
    monkeypatch.setattr(settings, "CONTENT_SAFETY_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CONTENT_SAFETY_REGION", "test-region")
    monkeypatch.setattr(settings, "CONTENT_SAFETY_TIMEOUT_SECONDS", 5.0)
    from app.services import content_safety_service as _css

    def _safe(text, *, config=None):
        cfg = config if config is not None else _css.config_from_settings(settings)
        if not cfg.endpoint or not str(cfg.endpoint).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_ENDPOINT is missing.",
                code="missing_config",
            )
        if not cfg.api_key or not str(cfg.api_key).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_API_KEY is missing.",
                code="missing_config",
            )
        if len(text or "") > _css.TEXT_MAX_LENGTH:
            raise _css.ContentSafetyError(
                "Confession text exceeds the moderation limit.",
                code="invalid_input",
            )
        return {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}

    monkeypatch.setattr(_css, "analyze_confession_text", _safe)
    monkeypatch.setattr(_css, "analyze_text", _safe)

    def _safe_image(image, *, config=None):
        cfg = config if config is not None else _css.config_from_settings(settings)
        if not cfg.endpoint or not str(cfg.endpoint).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_ENDPOINT is missing.",
                code="missing_config",
            )
        if not cfg.api_key or not str(cfg.api_key).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_API_KEY is missing.",
                code="missing_config",
            )
        if not (image or b""):
            raise _css.ContentSafetyError(
                "An empty image cannot be moderated.",
                code="invalid_input",
            )
        return {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}

    monkeypatch.setattr(_css, "analyze_image", _safe_image)
    # Central dispatcher also mocked safe so features calling moderate_content
    # / moderate_confession_text (no local config) stay offline in tests.
    # Validation preserved: missing endpoint/key still raises missing_config.
    def _safe_decision(content_type, content, *, config=None):
        cfg = config if config is not None else _css.config_from_settings(settings)
        if not cfg.endpoint or not str(cfg.endpoint).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_ENDPOINT is missing.",
                code="missing_config",
            )
        if not cfg.api_key or not str(cfg.api_key).strip():
            raise _css.ContentSafetyError(
                "Content moderation is not configured: CONTENT_SAFETY_API_KEY is missing.",
                code="missing_config",
            )
        scores = (
            _safe_image(content, config=cfg)
            if content_type in _css._IMAGE_CONTENT_TYPES
            else _safe(content, config=cfg)
        )
        return _css._decision_for(content_type, scores)

    def _safe_confession(text, *, config=None):
        return _safe_decision(_css.CONTENT_CONFESSION, text, config=config)

    def _safe_profile_picture(image, *, config=None):
        return _safe_decision(_css.CONTENT_PROFILE_PICTURE, image, config=config)

    monkeypatch.setattr(_css, "moderate_content", _safe_decision)
    monkeypatch.setattr(_css, "moderate_confession_text", _safe_confession)
    monkeypatch.setattr(_css, "moderate_profile_picture", _safe_profile_picture)


@pytest.fixture(autouse=True)
def _mock_pii_redaction_safe(monkeypatch):
    """Offline PII-redaction default so existing creation paths keep working.

    Love-note sends now run Azure AI Language PII detection once, at creation,
    to store the redacted moderation preview. Without this, every pre-existing
    test that sends a love note would call the real endpoint. This provides
    dummy config and a pass-through redaction: sample test bodies contain no
    personal data, so the provider's honest answer for them is the text
    unchanged, and the admin payload (``moderation_body``) equals the body the
    test wrote.

    Validation is preserved exactly like the Content Safety fixture: missing
    endpoint/key still raises ``missing_config`` and empty/over-long text still
    raises ``invalid_input``, so the failure paths remain testable by clearing
    settings. Tests that need real redaction logic restore the real functions
    and fake the SDK client at the ``get_pii_client`` seam (see
    ``test_love_note_pii_redaction.py``).
    """
    monkeypatch.setattr(settings, "PII_DETECTION_ENDPOINT", "https://test-pii.example.com")
    monkeypatch.setattr(settings, "PII_DETECTION_API_KEY", "test-key")
    monkeypatch.setattr(settings, "PII_DETECTION_REGION", "test-region")
    from app.services import pii_detection_service as _pds

    def _safe_redact(body, *, config=None):
        cfg = config if config is not None else _pds.config_from_settings(settings)
        if not cfg.endpoint or not str(cfg.endpoint).strip():
            raise _pds.PiiDetectionError(
                "PII detection is not configured: PII_DETECTION_ENDPOINT is missing.",
                code="missing_config",
            )
        if not cfg.api_key or not str(cfg.api_key).strip():
            raise _pds.PiiDetectionError(
                "PII detection is not configured: PII_DETECTION_API_KEY is missing.",
                code="missing_config",
            )
        text = body or ""
        if not text.strip():
            raise _pds.PiiDetectionError(
                "Empty text cannot be redacted.",
                code="invalid_input",
            )
        if len(text) > _pds.TEXT_MAX_LENGTH:
            raise _pds.PiiDetectionError(
                "Submitted text exceeds the PII detection limit.",
                code="invalid_input",
            )
        return _pds.LoveNoteRedaction(
            moderation_body=text,
            languages=("en",),
            entity_count=0,
        )

    monkeypatch.setattr(_pds, "redact_love_note_body", _safe_redact)
    # ``love_note_service`` imported the function by name, so the module-local
    # reference has to be replaced too -- patching only the defining module
    # would leave the send path and the retry endpoint calling the real one.
    from app.services import love_note_service as _lns

    monkeypatch.setattr(_lns, "redact_love_note_body", _safe_redact)
