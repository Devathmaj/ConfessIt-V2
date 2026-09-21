"""Tests for magic-link requests made by accounts an admin has blocked.

The block used to be enforced only when the link was redeemed, so a blocked account was
still handed a live, logged magic link on every request. These tests pin the refusal to
the point where the link would have been minted.

A blocked account now gets the same body as an unknown one rather than a 403 naming the
block: a distinct error for "exists but blocked" confirms the registration number is
real, which is the existence probe the sign-in response no longer answers. What these
tests hold onto is the part that matters for abuse -- no token is minted, and nothing
about the answer distinguishes the blocked account from one that was never registered.
"""

import pytest

from app import create_token as create_token_module
from app.accounts import AccountType
from app.db_models import UserRecord
from app.services import auth_service as auth_service_module
from app.services.auth_service import LOGIN_LINK_RESPONSE_MESSAGE, generate_magic_link_service
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
        return "a-token"

    monkeypatch.setattr(create_token_module, "create_and_store_magic_token", _record)
    return minted


@pytest.mark.asyncio
async def test_blocked_account_gets_no_link_and_no_block_specific_answer(minted_tokens):
    db = _FakeDatabase({"blocked": make_user_row("blocked", is_blocked=True)})

    result = await generate_magic_link_service(_FakeRequest("blocked"), db, AccountType.USERS.value)

    assert result == {"message": LOGIN_LINK_RESPONSE_MESSAGE}
    assert "not available" not in result["message"]
    assert minted_tokens == []
    assert db.added == []


@pytest.mark.asyncio
async def test_a_blocked_account_answers_identically_to_an_unknown_one(minted_tokens):
    """Same body, same status, no token: the block must not read as confirmation.

    The two requests differ only in whether a row exists behind them, which is the one
    fact a sign-in form must never reveal. If they diverged, an attacker who had already
    guessed a registration number could verify it by blocking... or simply by noticing
    that every other answer looks different.
    """
    db = _FakeDatabase({"blocked": make_user_row("blocked", is_blocked=True)})

    blocked = await generate_magic_link_service(_FakeRequest("blocked"), db, AccountType.USERS.value)
    unknown = await generate_magic_link_service(_FakeRequest("never-registered"), db, AccountType.USERS.value)

    assert blocked == unknown
    assert minted_tokens == []


@pytest.mark.asyncio
async def test_active_account_still_gets_a_link(minted_tokens):
    db = _FakeDatabase({"active": make_user_row("active")})

    result = await generate_magic_link_service(_FakeRequest("active"), db, AccountType.USERS.value)

    assert result == {"message": LOGIN_LINK_RESPONSE_MESSAGE}
    assert minted_tokens == [(OWNER_ID, "users")]
