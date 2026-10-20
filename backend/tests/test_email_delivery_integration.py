"""Backend integration with the Azure Functions enqueue endpoint.

These tests never read either protected environment file and never contact a
real Functions host, Storage account, or ACS resource. The HTTP client is a
controlled in-memory fake, and settings are constructed explicitly.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from app import create_token as create_token_module
from app.accounts import AccountType
from app.config import Settings, settings
from app.db_models import UserRecord
from app.services import auth_service as auth_service_module
from app.services import email_delivery_service
from app.services.auth_service import generate_magic_link_service
from app.services.login_rate_limit import RateLimitVerdict


_VALID_KEY = "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1"
_ENQUEUE_URL = "http://localhost:7071/api/email"


class _FakeResponse:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _FakeAsyncClient:
    """Minimal ``httpx.AsyncClient`` stand-in with a recorded POST."""

    instances: list["_FakeAsyncClient"] = []
    response_status = 202
    error: Exception | None = None

    def __init__(self, *, timeout=None, **kwargs):
        self.timeout = timeout
        self.posts: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, *, json):
        self.posts.append((url, json))
        if type(self).error is not None:
            raise type(self).error
        return _FakeResponse(type(self).response_status)


class _FakeSyncClient:
    """Minimal ``httpx.Client`` stand-in with a recorded POST."""

    instances: list["_FakeSyncClient"] = []
    response_status = 202
    error: Exception | None = None

    def __init__(self, *, timeout=None, **kwargs):
        self.timeout = timeout
        self.posts: list[tuple[str, dict]] = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def post(self, url, *, json):
        self.posts.append((url, json))
        if type(self).error is not None:
            raise type(self).error
        return _FakeResponse(type(self).response_status)


class _RecordingLogger:
    def __init__(self):
        self.lines: list[str] = []

    def info(self, message, *args, **kwargs):
        self.lines.append(message % args if args else message)

    def warning(self, message, *args, **kwargs):
        self.lines.append(message % args if args else message)

    def error(self, message, *args, **kwargs):
        self.lines.append(message % args if args else message)


@pytest.fixture(autouse=True)
def _fake_http_client(monkeypatch):
    _FakeAsyncClient.instances.clear()
    _FakeAsyncClient.response_status = 202
    _FakeAsyncClient.error = None
    monkeypatch.setattr(email_delivery_service.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


@pytest.fixture
def fake_sync_client(monkeypatch):
    _FakeSyncClient.instances.clear()
    _FakeSyncClient.response_status = 202
    _FakeSyncClient.error = None
    monkeypatch.setattr(email_delivery_service.httpx, "Client", _FakeSyncClient)
    return _FakeSyncClient


@pytest.fixture
def logged(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr(email_delivery_service, "logger", recorder)
    return recorder


def test_email_function_url_is_loaded_by_settings(monkeypatch):
    monkeypatch.setenv("EMAIL_FUNCTION_URL", _ENQUEUE_URL)
    configured = Settings(
        _env_file=None,
        SECRET_KEY=_VALID_KEY,
        TOKEN_HASH_PEPPER=_VALID_KEY,
        DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt",
        TEST_DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt_Test",
        REDIS_URL="redis://db:6379",
    )
    assert configured.EMAIL_FUNCTION_URL == _ENQUEUE_URL


@pytest.mark.parametrize("bad", ["localhost:7071/api/email", "ftp://host/api/email"])
def test_email_function_url_rejects_invalid_values(monkeypatch, bad):
    monkeypatch.delenv("EMAIL_FUNCTION_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            EMAIL_FUNCTION_URL=bad,
            SECRET_KEY=_VALID_KEY,
            TOKEN_HASH_PEPPER=_VALID_KEY,
            DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt",
            TEST_DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt_Test",
            REDIS_URL="redis://db:6379",
        )


@pytest.mark.asyncio
async def test_enqueue_posts_the_expected_login_payload(monkeypatch, logged):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)

    queued = await email_delivery_service.enqueue_login_email(
        link="https://app.example.com/verify-login?token=secret-token",
        email="person@example.com",
        name="Test Person",
        regno="22/CS0455",
    )

    assert queued is True
    client = _FakeAsyncClient.instances[-1]
    assert client.posts == [
        (
            _ENQUEUE_URL,
            {
                "link": "https://app.example.com/verify-login?token=secret-token",
                "email": "person@example.com",
                "name": "Test Person",
                "regno": "22/CS0455",
                "type": "login",
            },
        )
    ]
    assert client.timeout is not None
    assert "accepted for queueing" in logged.lines[-1]
    # The link, address, name and regno are not logged; only a keyed hash is.
    assert "secret-token" not in logged.lines[-1]
    assert "person@example.com" not in logged.lines[-1]
    assert "Test Person" not in logged.lines[-1]
    assert "22/CS0455" not in logged.lines[-1]


def test_enqueue_notification_posts_the_expected_payload(
    monkeypatch, logged, fake_sync_client
):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)

    queued = email_delivery_service.enqueue_notification_email(
        title="New confession report",
        body="A participant reported a confession.",
        email="staff@example.com",
        name="Staff Person",
        regno="22/CS0455",
    )

    assert queued is True
    client = fake_sync_client.instances[-1]
    assert client.posts == [
        (
            _ENQUEUE_URL,
            {
                "title": "New confession report",
                "body": "A participant reported a confession.",
                "email": "staff@example.com",
                "name": "Staff Person",
                "regno": "22/CS0455",
                "type": "notification",
            },
        )
    ]
    assert client.timeout is not None
    assert "accepted for queueing" in logged.lines[-1]
    assert "New confession report" not in logged.lines[-1]
    assert "A participant reported a confession." not in logged.lines[-1]
    assert "staff@example.com" not in logged.lines[-1]
    assert "Staff Person" not in logged.lines[-1]
    assert "22/CS0455" not in logged.lines[-1]


def test_enqueue_notification_posts_the_optional_target_link(
    monkeypatch, logged, fake_sync_client
):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)

    queued = email_delivery_service.enqueue_notification_email(
        title="New confession report",
        body="A participant reported a confession.",
        email="staff@example.com",
        name="Staff Person",
        regno="22/CS0455",
        link="https://app.example.com/admin/reports",
    )

    assert queued is True
    client = fake_sync_client.instances[-1]
    assert client.posts == [
        (
            _ENQUEUE_URL,
            {
                "title": "New confession report",
                "body": "A participant reported a confession.",
                "email": "staff@example.com",
                "name": "Staff Person",
                "regno": "22/CS0455",
                "link": "https://app.example.com/admin/reports",
                "type": "notification",
            },
        )
    ]
    assert "https://app.example.com/admin/reports" not in logged.lines[-1]


def test_enqueue_notification_reports_non_2xx_without_exposing_content(
    monkeypatch, logged, fake_sync_client
):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)
    fake_sync_client.response_status = 503

    queued = email_delivery_service.enqueue_notification_email(
        title="New confession report",
        body="A participant reported a confession.",
        email="staff@example.com",
        name="Staff Person",
        regno="22/CS0455",
    )

    assert queued is False
    assert "status_code=503" in logged.lines[-1]
    assert "New confession report" not in logged.lines[-1]


@pytest.mark.asyncio
async def test_enqueue_reports_timeout_without_claiming_success(monkeypatch, logged):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)
    _FakeAsyncClient.error = httpx.TimeoutException("slow")

    queued = await email_delivery_service.enqueue_login_email(
        link="https://app.example.com/verify-login?token=secret-token",
        email="person@example.com",
        name="Test Person",
        regno="22/CS0455",
    )

    assert queued is False
    assert "timed out" in logged.lines[-1]
    assert "secret-token" not in logged.lines[-1]


@pytest.mark.asyncio
async def test_enqueue_reports_connection_failure(monkeypatch, logged):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)
    _FakeAsyncClient.error = httpx.ConnectError("refused")

    queued = await email_delivery_service.enqueue_login_email(
        link="https://app.example.com/verify-login?token=secret-token",
        email="person@example.com",
        name="Test Person",
        regno="22/CS0455",
    )

    assert queued is False
    assert "request failed" in logged.lines[-1]
    assert "secret-token" not in logged.lines[-1]


@pytest.mark.asyncio
async def test_enqueue_rejects_non_2xx_response(monkeypatch, logged):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", _ENQUEUE_URL)
    _FakeAsyncClient.response_status = 503

    queued = await email_delivery_service.enqueue_login_email(
        link="https://app.example.com/verify-login?token=secret-token",
        email="person@example.com",
        name="Test Person",
        regno="22/CS0455",
    )

    assert queued is False
    assert "status_code=503" in logged.lines[-1]


@pytest.mark.asyncio
async def test_enqueue_skips_http_when_url_is_unconfigured(monkeypatch, logged):
    monkeypatch.setattr(settings, "EMAIL_FUNCTION_URL", None)

    queued = await email_delivery_service.enqueue_login_email(
        link="https://app.example.com/verify-login?token=secret-token",
        email="person@example.com",
        name="Test Person",
        regno="22/CS0455",
    )

    assert queued is False
    assert _FakeAsyncClient.instances == []
    assert "EMAIL_FUNCTION_URL is not configured" in logged.lines[-1]


# --- Authentication flow integration -------------------------------------------


def _make_user_row(regno: str) -> UserRecord:
    return UserRecord(
        id="11111111-1111-1111-1111-111111111111",
        regno=regno,
        name="Active User",
        email="active@example.com",
        bio=None,
        year=2,
        department="Test_Class",
        profile_picture_public_id=None,
        profile_picture_format=None,
        gender="male",
        interests=[],
        matchmaking_enabled=True,
        notifications_enabled=True,
        love_notes_receive_enabled=True,
        love_notes_send_enabled=False,
        is_blocked=False,
        last_login_at=None,
        last_login_ip=None,
        last_login_user_agent=None,
        last_matchmaking_at=None,
    )


class _FakeSession:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def scalar(self, query):
        return self._db.resolve(query)


class _FakeDatabase:
    def __init__(self, users):
        self.users = dict(users)

    def sessions(self):
        return _FakeSession(self)

    def resolve(self, query):
        for value in query.compile().params.values():
            if isinstance(value, str):
                return self.users.get(value)
        return None


class _FakeRequest:
    def __init__(self, regno):
        self._payload = {"regno": regno}
        self.client = type("Client", (), {"host": "127.0.0.1"})()
        self.headers = {"user-agent": "Test Agent"}

    async def json(self):
        return self._payload


class _LinkCatcher:
    def __init__(self):
        self.messages = []

    def info(self, message, *args, **kwargs):
        self.messages.append(message % args if args else message)


@pytest.mark.asyncio
async def test_login_flow_preserves_console_link_and_queues_the_same_link(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://app.example.com")
    monkeypatch.setattr(
        auth_service_module,
        "evaluate_login_rate_limits",
        lambda db, **kwargs: RateLimitVerdict(allowed=True),
    )
    links = _LinkCatcher()
    monkeypatch.setattr(auth_service_module, "magic_link_logger", links)
    monkeypatch.setattr(
        create_token_module,
        "create_and_store_magic_token",
        lambda db, account_id, account_type, request: (
            "plaintext-token",
            datetime.now(timezone.utc) + timedelta(minutes=15),
        ),
    )

    queued_calls = []

    async def _record(**kwargs):
        queued_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        email_delivery_service, "enqueue_login_email", _record
    )

    result = await generate_magic_link_service(
        _FakeRequest("active"),
        _FakeDatabase({"active": _make_user_row("active")}),
        AccountType.USERS.value,
    )

    expected_link = "https://app.example.com/verify-login?token=plaintext-token"
    # The development console line remains exactly the same shape as before.
    assert links.messages == [f"Magic Link for Active User: {expected_link}"]
    assert queued_calls == [
        {
            "link": expected_link,
            "email": "active@example.com",
            "name": "Active User",
            "regno": "active",
        }
    ]
    assert result["message"]
