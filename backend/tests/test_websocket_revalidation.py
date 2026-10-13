"""Live matchmaking WebSockets stop when the session behind them stops.

F-02 regression coverage: the handshake is not a lifetime grant. Once the
session is revoked, superseded, expired, or the account is blocked, a published
message must not be delivered and the socket must close as auth-required on its
next frame -- including a non-ping frame.
"""

import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from app.accounts import AccountType
from app.db_models import (
    ConversationRecord,
    LoginTokenRecord,
    MatchRecord,
    UserRecord,
    find_account_in_table,
)
from app.main import app
from app.services.auth_service import (
    SESSION_STATUS_ACTIVE,
    access_token_session_id,
    account_from_row,
    create_websocket_ticket,
)
from ws_helpers import install_fake_pubsub

WINDOW = timedelta(hours=4)


def make_user(db, regno: str, name: str) -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name,
            email=f"{regno}@example.com",
            year=2,
            department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_accepted_conversation(db, initiator: str, receiver: str) -> str:
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with db.sessions.begin() as session:
        match_row = MatchRecord(
            user_1_regno=initiator,
            user_2_regno=receiver,
            created_at=created_at,
            expires_at=created_at + WINDOW,
        )
        session.add(match_row)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match_row.id),
            initiator_regno=initiator,
            receiver_regno=receiver,
            status="accepted",
            created_at=created_at,
            requested_at=created_at,
            accepted_at=created_at,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id)


def access_token_for(db, regno: str) -> str:
    """Log a real participant in through the real magic-link flow."""
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
    assert found is not None, f"no users account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"user-agent": "WebSocket revalidation test"}

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


def websocket_ticket(db, regno: str) -> str:
    """Mint the same short-lived ticket the browser exchanges for its socket."""
    access_token = access_token_for(db, regno)
    session_id = access_token_session_id(access_token)
    assert session_id is not None
    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
        assert found is not None
        account = account_from_row(found[0], found[1])
    return create_websocket_ticket(db, account, session_id)["ticket"]


def invalidate(db, regno: str, mode: str) -> None:
    """End the account's live session in the same way the real lifecycle does."""
    with db.sessions.begin() as session:
        user = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        assert user is not None
        if mode == "blocked":
            user.is_blocked = True
            return
        session_row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.account_id == str(user.id),
                LoginTokenRecord.session_status == SESSION_STATUS_ACTIVE,
            )
        )
        assert session_row is not None
        if mode == "superseded":
            session_row.session_status = "superseded"
        elif mode == "revoked":
            session_row.session_status = "revoked"
        elif mode == "expired":
            session_row.session_expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        else:  # pragma: no cover - guards the parametrization
            raise AssertionError(f"unknown invalidation mode {mode!r}")


def test_a_live_chat_socket_still_receives_published_messages(db, monkeypatch):
    """Positive control: the fake subscription really reaches a valid socket."""
    make_user(db, "ws_alice", "Alice")
    make_user(db, "ws_bob", "Bob")
    conversation_id = make_accepted_conversation(db, "ws_alice", "ws_bob")
    ticket = websocket_ticket(db, "ws_alice")
    pubsub = install_fake_pubsub(monkeypatch)

    http = TestClient(app, client=("127.0.0.1", 51234))
    try:
        with http.websocket_connect(
            f"/messages/ws/{conversation_id}?ticket={ticket}"
        ) as ws:
            assert ws.receive_json()["type"] == "connected"

            pubsub.publish({
                "id": "live-message",
                "conversation_id": conversation_id,
                "sender_regno": "ws_bob",
                "body": "this one is live",
            })
            event = ws.receive_json()
            assert event["type"] == "message"
            assert event["data"]["body"] == "this one is live"
    finally:
        http.close()


@pytest.mark.parametrize("mode", ["revoked", "superseded", "expired", "blocked"])
@pytest.mark.parametrize("frame", ["ping", "not-a-ping"])
def test_a_live_chat_socket_stops_when_its_session_ends(db, monkeypatch, mode, frame):
    make_user(db, "ws_alice", "Alice")
    make_user(db, "ws_bob", "Bob")
    conversation_id = make_accepted_conversation(db, "ws_alice", "ws_bob")
    ticket = websocket_ticket(db, "ws_alice")
    pubsub = install_fake_pubsub(monkeypatch)

    http = TestClient(app, client=("127.0.0.1", 51234))
    try:
        with http.websocket_connect(
            f"/messages/ws/{conversation_id}?ticket={ticket}"
        ) as ws:
            hello = ws.receive_json()
            assert hello["type"] == "connected"

            ws.send_text("ping")
            assert ws.receive_text() == "pong"

            invalidate(db, "ws_alice", mode)
            pubsub.publish({
                "id": "message-after-invalidation",
                "conversation_id": conversation_id,
                "sender_regno": "ws_bob",
                "body": "must not arrive",
            })
            # Give the listener time to reach its pre-forward revalidation.
            time.sleep(0.2)

            ws.send_text(frame)
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_text()
            assert exc.value.code == 4001
    finally:
        http.close()
