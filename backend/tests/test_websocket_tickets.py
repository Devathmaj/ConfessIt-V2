"""WebSocket tickets: authenticated exchange, single use, short life, session-bound.

F-03 regression coverage. The socket URL must not accept an access token, and a
ticket copied out of a handshake log must stop being useful after one handshake,
60 seconds, or the end of the session it was minted from.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.websockets import WebSocketDisconnect

from app.accounts import AccountType
from app.db_models import (
    LoginTokenRecord,
    UserRecord,
    WebSocketTicketRecord,
    find_account_in_table,
)
from app.main import app
from app.services.auth_service import (
    SESSION_STATUS_ACTIVE,
    TOKEN_HASH_DOMAIN_WEBSOCKET,
    WEBSOCKET_TICKET_EXPIRE_SECONDS,
    access_token_session_id,
    account_from_row,
    consume_websocket_ticket,
    create_websocket_ticket,
    get_current_user,
    hash_token,
)


def make_user(db, regno: str) -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=regno.title(),
            email=f"{regno}@example.com",
            year=2,
            department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def access_token_for(db, regno: str) -> str:
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
    assert found is not None, f"no users account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "WebSocket ticket test"}

    plaintext, _ = create_and_store_magic_token(
        db, str(row.id), resolved_type, _Request()
    )
    browser = TestClient(app, client=("127.0.0.1", 51234))
    try:
        response = browser.get(f"/auth/login/magic/verify?token={plaintext}")
        assert response.status_code == 200, response.text
        return response.json()["access_token"]
    finally:
        browser.close()


def account_for(db, regno: str):
    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
        assert found is not None
        return account_from_row(found[0], found[1])


def session_id_for(db, token: str) -> str:
    session_id = access_token_session_id(token)
    assert session_id is not None
    return session_id


def mint_ticket(db, regno: str):
    token = access_token_for(db, regno)
    account = account_for(db, regno)
    session_id = session_id_for(db, token)
    return token, account, create_websocket_ticket(db, account, session_id)["ticket"]


@pytest.fixture
def as_account():
    """Install a participant override and put the previous dependency back."""
    previous = app.dependency_overrides.get(get_current_user)

    def install(account):
        app.dependency_overrides[get_current_user] = lambda: account

    try:
        yield install
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_current_user, None)
        else:
            app.dependency_overrides[get_current_user] = previous


def test_a_participant_exchanges_an_access_token_for_a_single_use_ticket(db, as_account):
    make_user(db, "ticket_user")
    token = access_token_for(db, "ticket_user")
    account = account_for(db, "ticket_user")
    as_account(account)

    client = TestClient(app, client=("127.0.0.1", 51234))
    try:
        response = client.post(
            "/auth/ws-ticket",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        client.close()

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ticket"]
    assert body["ticket"] != token
    assert body["expires_in"] == WEBSOCKET_TICKET_EXPIRE_SECONDS
    assert body["expires_at"]

    consumed = consume_websocket_ticket(db, body["ticket"])
    assert consumed is not None
    assert str(consumed[0].id) == str(account.id)
    assert consumed[1] == session_id_for(db, token)
    assert consume_websocket_ticket(db, body["ticket"]) is None


def test_the_websocket_does_not_accept_an_access_token_in_the_query(db):
    make_user(db, "legacy_ws_user")
    token = access_token_for(db, "legacy_ws_user")
    client = TestClient(app, client=("127.0.0.1", 51234))
    try:
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(
                f"/messages/ws/not-a-conversation?token={token}"
            ) as ws:
                ws.receive_text()
        assert exc.value.code == 4001
    finally:
        client.close()


def test_an_expired_ticket_cannot_be_redeemed(db):
    make_user(db, "expired_ticket_user")
    _, _, ticket = mint_ticket(db, "expired_ticket_user")
    with db.sessions.begin() as session:
        session.execute(
            update(WebSocketTicketRecord)
            .where(
                WebSocketTicketRecord.ticket_hash
                == hash_token(ticket, domain=TOKEN_HASH_DOMAIN_WEBSOCKET)
            )
            .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    assert consume_websocket_ticket(db, ticket) is None


@pytest.mark.parametrize("mode", ["revoked", "superseded", "expired", "blocked"])
def test_a_ticket_stops_working_when_its_session_ends(db, mode):
    make_user(db, "ending_ticket_user")
    _, account, ticket = mint_ticket(db, "ending_ticket_user")
    with db.sessions.begin() as session:
        user = session.scalar(
            select(UserRecord).where(UserRecord.regno == "ending_ticket_user")
        )
        assert user is not None
        if mode == "blocked":
            user.is_blocked = True
        else:
            session_row = session.scalar(
                select(LoginTokenRecord).where(
                    LoginTokenRecord.account_id == str(account.id),
                    LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE,
                )
            )
            assert session_row is not None
            if mode == "superseded":
                session_row.session_status = "superseded"
            elif mode == "revoked":
                session_row.session_status = "revoked"
            else:  # expired
                session_row.session_expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )

    assert consume_websocket_ticket(db, ticket) is None
