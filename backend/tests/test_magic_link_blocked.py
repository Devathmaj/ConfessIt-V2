"""Tests for magic-link requests made by accounts an admin has blocked.

The block used to be enforced only when the link was redeemed, so a blocked account was
still handed a live, logged magic link on every request. The refusal lives at the point
where the link would have been minted: nothing is ever stored for a blocked account.

The sign-in form answers uniformly rather than naming the outcome: a blocked account, an
unregistered registration number and a real, unblocked account all get the same 200 and
the same body, so the endpoint cannot be swept to learn existence or blocked state. What
these tests hold onto is the part that matters for abuse -- no token is ever minted for
either ineligible outcome -- and that the three answers are indistinguishable.
"""

import pytest
from datetime import datetime, timedelta, timezone

from app import create_token as create_token_module
from app.accounts import AccountType
from app.db_models import AdminRecord, UserRecord
from app.services import auth_service as auth_service_module
from app.services.auth_service import (
    LOGIN_LINK_SENT_MESSAGE,
    generate_magic_link_service,
)
from app.services.login_rate_limit import RateLimitVerdict


OWNER_ID = "11111111-1111-1111-1111-111111111111"


def make_user_row(regno: str, **overrides) -> UserRecord:
    values = dict(
        id=OWNER_ID, regno=regno, name=regno.title(), email=f"{regno}@example.com",
        bio=None, year=2, department="Test_Class",
        profile_picture_public_id=None, profile_picture_format=None,
        gender="male", interests=[], matchmaking_enabled=True, notifications_enabled=True,
        love_notes_receive_enabled=True, love_notes_send_enabled=False, is_blocked=False,
        last_login_at=None, last_login_ip=None, last_login_user_agent=None,
        last_matchmaking_at=None,
    )
    values.update(overrides)
    return UserRecord(**values)


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, db):
        self._db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def scalar(self, query):
        return self._db.resolve(query)

    def scalars(self, query):
        return _FakeResult([])

    def add(self, row):
        self._db.added.append(row)

    def execute(self, statement):
        return _FakeResult([])


class _FakeDatabase:
    """Resolves the registration-number lookup, and records anything written."""

    def __init__(self, users):
        self.users = dict(users)
        self.added = []

    def sessions(self):
        return _FakeSession(self)

    def sessions_begin(self):
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


@pytest.fixture(autouse=True)
def login_rate_limits_disabled(monkeypatch):
    """The service resolves rate limits through a real database; this fake has none.

    Rate limiting is exercised against the real stack in ``test_login_rate_limits``.
    Here it would only get in the way of the question these tests ask, so the limiter
    is replaced by one that always allows rather than by a fake that pretends to count.
    """
    monkeypatch.setattr(
        auth_service_module,
        "evaluate_login_rate_limits",
        lambda db, **kwargs: RateLimitVerdict(allowed=True),
    )


@pytest.fixture
def minted_tokens(monkeypatch):
    """Fail loudly if the service gets as far as minting a token."""
    minted = []

    def _record(db, account_id, account_type, request):
        minted.append((account_id, account_type))
        # What the real mint function would have stored alongside the token it returns.
        return ("a-token", datetime.now(timezone.utc) + timedelta(minutes=15))

    monkeypatch.setattr(create_token_module, "create_and_store_magic_token", _record)
    return minted


@pytest.mark.asyncio
async def test_blocked_account_gets_no_link_but_the_uniform_answer(minted_tokens):
    db = _FakeDatabase({"blocked": make_user_row("blocked", is_blocked=True)})

    result = await generate_magic_link_service(_FakeRequest("blocked"), db, AccountType.USERS.value)

    assert result["message"] == LOGIN_LINK_SENT_MESSAGE
    assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
    assert minted_tokens == []
    assert db.added == []


@pytest.mark.asyncio
async def test_blocked_account_gets_a_blocked_notice_instead_of_a_login_mail(
    monkeypatch, minted_tokens
):
    blocked_calls = []
    login_calls = []

    async def record_blocked(**kwargs):
        blocked_calls.append(kwargs)
        return True

    async def record_login(**kwargs):
        login_calls.append(kwargs)
        return True

    monkeypatch.setattr(
        auth_service_module,
        "_random_admin_contact_email",
        lambda db: "admin@example.com",
    )
    monkeypatch.setattr(
        auth_service_module.email_delivery_service,
        "enqueue_account_blocked_email",
        record_blocked,
    )
    monkeypatch.setattr(
        auth_service_module.email_delivery_service,
        "enqueue_login_email",
        record_login,
    )
    db = _FakeDatabase(
        {
            "blocked": make_user_row(
                "blocked", is_blocked=True, notifications_enabled=False
            )
        }
    )

    result = await generate_magic_link_service(
        _FakeRequest("blocked"), db, AccountType.USERS.value
    )

    assert result["message"] == LOGIN_LINK_SENT_MESSAGE
    assert blocked_calls == [
        {
            "email": "blocked@example.com",
            "name": "Blocked",
            "regno": "blocked",
            "admin_email": "admin@example.com",
        }
    ]
    assert login_calls == []
    assert minted_tokens == []


def test_admin_contact_is_chosen_from_unblocked_administrator_addresses(
    db, monkeypatch
):
    with db.sessions.begin() as session:
        session.add(
            AdminRecord(
                id="33333333-3333-3333-3333-333333333333",
                regno="admin-one",
                name="Admin One",
                email="admin-one@example.com",
                notifications_enabled=True,
                is_blocked=False,
            )
        )
        session.add(
            AdminRecord(
                id="44444444-4444-4444-4444-444444444444",
                regno="admin-two",
                name="Admin Two",
                email="admin-two@example.com",
                notifications_enabled=True,
                is_blocked=False,
            )
        )

    candidates = []

    def choose(options):
        candidates.extend(options)
        return options[0]

    monkeypatch.setattr(auth_service_module.secrets, "choice", choose)

    chosen = auth_service_module._random_admin_contact_email(db)

    assert chosen in {"admin-one@example.com", "admin-two@example.com"}
    assert set(candidates) == {"admin-one@example.com", "admin-two@example.com"}


@pytest.mark.asyncio
async def test_an_unknown_registration_number_gets_no_link_but_the_uniform_answer(minted_tokens):
    """No row, nothing minted, and the same answer a real account gets."""
    db = _FakeDatabase({})

    result = await generate_magic_link_service(
        _FakeRequest("never-registered"), db, AccountType.USERS.value
    )

    assert result["message"] == LOGIN_LINK_SENT_MESSAGE
    assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
    assert minted_tokens == []
    assert db.added == []


@pytest.mark.asyncio
async def test_ineligible_answers_are_indistinguishable_from_a_mint(minted_tokens):
    """The oracle test: status-free at this layer, so the bodies must match exactly
    in shape -- same keys, same message, same deadline shape -- whether eligible or not."""
    unknown = await generate_magic_link_service(
        _FakeRequest("never-registered"), _FakeDatabase({}), AccountType.USERS.value
    )
    blocked = await generate_magic_link_service(
        _FakeRequest("blocked"),
        _FakeDatabase({"blocked": make_user_row("blocked", is_blocked=True)}),
        AccountType.USERS.value,
    )
    active = await generate_magic_link_service(
        _FakeRequest("active"),
        _FakeDatabase({"active": make_user_row("active")}),
        AccountType.USERS.value,
    )

    assert set(unknown) == set(blocked) == set(active) == {"message", "expires_at"}
    assert unknown["message"] == blocked["message"] == active["message"] == LOGIN_LINK_SENT_MESSAGE
    for body in (unknown, blocked, active):
        deadline = datetime.fromisoformat(body["expires_at"])
        assert deadline > datetime.now(timezone.utc)
    assert minted_tokens == [(OWNER_ID, "users")]


@pytest.mark.asyncio
async def test_active_account_still_gets_a_link(minted_tokens):
    db = _FakeDatabase({"active": make_user_row("active")})

    result = await generate_magic_link_service(_FakeRequest("active"), db, AccountType.USERS.value)

    assert result["message"] == LOGIN_LINK_SENT_MESSAGE
    # The sign-in page needs the token's exact deadline; it is carried in the response.
    assert datetime.fromisoformat(result["expires_at"]) > datetime.now(timezone.utc)
    assert minted_tokens == [(OWNER_ID, "users")]
