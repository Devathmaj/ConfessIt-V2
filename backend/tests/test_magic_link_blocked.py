"""Tests for magic-link requests made by accounts an admin has blocked.

The block used to be enforced only when the link was redeemed, so a blocked account was
still handed a live, logged magic link on every request. These tests pin the refusal to
the point where the link would have been minted.
"""

import pytest
from fastapi import HTTPException

from app import create_token as create_token_module
from app.accounts import AccountType
from app.db_models import UserRecord
from app.services.auth_service import generate_magic_link_service


OWNER_ID = "11111111-1111-1111-1111-111111111111"


def make_user_row(regno: str, **overrides) -> UserRecord:
    values = dict(
        id=OWNER_ID, regno=regno, name=regno.title(), email=f"{regno}@example.com",
        emoji=None, bio=None, year=2, department="Test_Class", profile_picture_url=None,
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
async def test_blocked_account_is_refused_before_a_link_is_minted(minted_tokens):
    db = _FakeDatabase({"blocked": make_user_row("blocked", is_blocked=True)})

    with pytest.raises(HTTPException) as excinfo:
        await generate_magic_link_service(_FakeRequest("blocked"), db, AccountType.USERS.value)

    assert excinfo.value.status_code == 403
    assert "not available" in excinfo.value.detail
    assert minted_tokens == []
    assert db.added == []


@pytest.mark.asyncio
async def test_active_account_still_gets_a_link(minted_tokens):
    db = _FakeDatabase({"active": make_user_row("active")})

    result = await generate_magic_link_service(_FakeRequest("active"), db, AccountType.USERS.value)

    assert "message" in result
    assert minted_tokens == [(OWNER_ID, "users")]
