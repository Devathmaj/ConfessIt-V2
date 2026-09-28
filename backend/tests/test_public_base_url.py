"""Magic links point at the configured public frontend, never a hardcoded localhost.

The link used to be an f-string with ``http://localhost:5173`` baked in, so every
production login printed a link to nowhere (and trained users to copy tokens onto the
wrong host). The origin now comes from ``PUBLIC_BASE_URL`` with a localhost default for
local runs, and a cloud deployment without a real origin refuses to boot. These tests pin
all three: the default, the override (including a trailing slash), the minted link's
host, and the boot guard.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import create_token as create_token_module
from app.accounts import AccountType
from app.config import public_frontend_origin, settings, validate_public_base_url
from app.db_models import UserRecord
from app.services import auth_service as auth_service_module
from app.services.auth_service import generate_magic_link_service
from app.services.login_rate_limit import RateLimitVerdict


def make_user_row(regno: str, **overrides) -> UserRecord:
    values = dict(
        id="11111111-1111-1111-1111-111111111111", regno=regno, name=regno.title(),
        email=f"{regno}@example.com", bio=None, year=2, department="Test_Class",
        profile_picture_public_id=None, profile_picture_format=None,
        gender="male", interests=[], matchmaking_enabled=True, notifications_enabled=True,
        love_notes_receive_enabled=True, love_notes_send_enabled=False, is_blocked=False,
        last_login_at=None, last_login_ip=None, last_login_user_agent=None,
        last_matchmaking_at=None,
    )
    values.update(overrides)
    return UserRecord(**values)


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


@pytest.fixture(autouse=True)
def login_rate_limits_disabled(monkeypatch):
    monkeypatch.setattr(
        auth_service_module,
        "evaluate_login_rate_limits",
        lambda db, **kwargs: RateLimitVerdict(allowed=True),
    )


@pytest.fixture
def minted_and_logged(monkeypatch):
    """A mint that records instead of storing, and a log catcher for the link."""
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
    return links


def test_origin_defaults_to_local_dev(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    assert public_frontend_origin() == "http://localhost:5173"


def test_origin_honours_configuration_and_strips_a_trailing_slash(monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://app.example.com/")
    assert public_frontend_origin() == "https://app.example.com"


def test_boot_guard_ignores_local_runs(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD", False)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    validate_public_base_url()  # must not raise


def test_boot_guard_refuses_a_cloud_without_an_origin(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    with pytest.raises(RuntimeError):
        validate_public_base_url()


@pytest.mark.parametrize(
    "bad",
    [
        "http://localhost:5173",
        "http://127.0.0.1:8000/",
        "http://[::1]:5173",
        "https://app.localhost/verify",
    ],
)
def test_boot_guard_refuses_loopback_origins_in_the_cloud(monkeypatch, bad):
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", bad)
    with pytest.raises(RuntimeError):
        validate_public_base_url()


def test_boot_guard_allows_lookalike_domains(monkeypatch):
    # Exact host matching: a public name that merely contains the substring must pass.
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://mylocalhost.example.com")
    validate_public_base_url()  # must not raise


def test_boot_guard_accepts_a_real_origin_in_the_cloud(monkeypatch):
    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://app.example.com")
    validate_public_base_url()  # must not raise


@pytest.mark.asyncio
async def test_minted_link_uses_the_configured_origin(monkeypatch, minted_and_logged):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://app.example.com/")
    db = _FakeDatabase({"active": make_user_row("active")})

    await generate_magic_link_service(
        _FakeRequest("active"), db, AccountType.USERS.value
    )

    assert len(minted_and_logged.messages) == 1
    assert (
        "https://app.example.com/verify-login?token=plaintext-token"
        in minted_and_logged.messages[0]
    )
    assert "localhost" not in minted_and_logged.messages[0]


@pytest.mark.asyncio
async def test_minted_link_falls_back_to_local_dev(monkeypatch, minted_and_logged):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    db = _FakeDatabase({"active": make_user_row("active")})

    await generate_magic_link_service(
        _FakeRequest("active"), db, AccountType.USERS.value
    )

    assert "http://localhost:5173/verify-login?token=" in minted_and_logged.messages[0]
