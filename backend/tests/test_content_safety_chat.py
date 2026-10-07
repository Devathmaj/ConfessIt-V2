"""Azure AI Content Safety for chat messages.

Chat is the third text item moderated by the central dispatcher, and it
follows the same principal as confessions and their comments: everything >1 is
flagged + hidden and reported. Because a chat message is delivered the moment
it is persisted, the moderation runs before persistence and a flagged message
is born hidden -- it is never published to the recipient. The Content Safety API
is mocked; persistence, visibility (removed_at), reports and restore run against
the real PostgreSQL database.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    UserRecord,
)
from app.main import app
from app.models import AccountType, UserDetails
from app.services import content_safety_service as css
from app.services.auth_service import get_current_user
from app.services.report_resolution_service import unhide_message
from app.services import report_resolution_service

WINDOW = timedelta(hours=4)
SENDER = "chat-sender"
RECEIVER = "chat-receiver"
CLEAN = {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}


def _mock_moderation(monkeypatch, scores: dict):
    def _fake_decision(content_type, text, *, config=None):
        return css._decision_for(content_type, dict(scores))

    monkeypatch.setattr(css, "moderate_content", _fake_decision)


def _mock_moderation_failure(monkeypatch, code: str = "unavailable"):
    def _raise_decision(content_type, text, *, config=None):
        raise css.ContentSafetyError("provider down", code=code)

    monkeypatch.setattr(css, "moderate_content", _raise_decision)


def make_user(db, regno):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        ))


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        year=row.year, department=row.department, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        is_blocked=row.is_blocked, account_type=AccountType.USERS.value,
    )


def make_accepted_conversation(db, sender, receiver) -> str:
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender, user_2_regno=receiver, created_at=now, expires_at=now + WINDOW,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno=sender, receiver_regno=receiver,
            status="accepted", created_at=now,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id)


def message_for(db, body) -> MessageRecord:
    with db.sessions() as session:
        return session.scalar(select(MessageRecord).where(MessageRecord.body == body))


def report_for(db, message_id):
    with db.sessions() as session:
        return session.scalar(
            select(MessageReportRecord).where(
                MessageReportRecord.message_id == str(message_id)
            )
        )


@pytest.fixture
def cast(db):
    make_user(db, SENDER)
    make_user(db, RECEIVER)
    return make_accepted_conversation(db, SENDER, RECEIVER)


@pytest.fixture
def login_as(db):
    @contextmanager
    def _login(regno: str):
        user = as_user(db, regno)
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


# ------------------------------------------------------------------ delivery


def test_clean_message_is_delivered_and_unreported(db, client, login_as, cast, monkeypatch):
    _mock_moderation(monkeypatch, CLEAN)
    with login_as(SENDER):
        response = client.post("/messages/send", json={"conversation_id": cast, "body": "hello"})
    assert response.status_code == 201, response.text

    row = message_for(db, "hello")
    assert row is not None and row.removed_at is None
    assert report_for(db, row.id) is None


@pytest.mark.parametrize(
    "scores",
    [
        {"Hate": 2, "SelfHarm": 0, "Sexual": 0, "Violence": 0},
        {"Hate": 0, "SelfHarm": 4, "Sexual": 0, "Violence": 0},
        {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6},
    ],
)
def test_flagged_message_is_refused_hidden_and_reported(db, client, login_as, cast, monkeypatch, scores):
    _mock_moderation(monkeypatch, scores)
    with login_as(SENDER):
        response = client.post("/messages/send", json={"conversation_id": cast, "body": "bad text"})
    assert response.status_code == 422
    assert "violated our content policies" in response.json()["detail"]

    row = message_for(db, "bad text")
    assert row is not None
    assert row.removed_at is not None
    assert row.removed_by_regno == css.AUTOMATED_REMOVED_BY

    report = report_for(db, row.id)
    assert report is not None
    assert report.source == css.AUTOMATED_SOURCE
    assert report.status == "removed"
    assert report.reporter_regno is None
    assert report.reported_user_regno == SENDER
    assert report.reviewed_by_regno == css.AUTOMATED_REMOVED_BY
    assert css.build_report_reason_for(css.CONTENT_CHAT_MESSAGE, scores, max(scores.values())) in report.reason


def test_flagged_message_is_not_in_the_transcript(db, client, login_as, cast, monkeypatch):
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    with login_as(SENDER):
        client.post("/messages/send", json={"conversation_id": cast, "body": "secret threat"})

    with login_as(RECEIVER):
        transcript = client.get(f"/messages/{cast}")
    bodies = [message["body"] for message in transcript.json()["messages"]]
    assert "secret threat" not in bodies


def test_moderation_failure_aborts_before_persistence(db, client, login_as, cast, monkeypatch):
    _mock_moderation_failure(monkeypatch, code="unavailable")
    with login_as(SENDER):
        response = client.post("/messages/send", json={"conversation_id": cast, "body": "never stored"})
    assert response.status_code == 503
    assert message_for(db, "never stored") is None


def test_hidden_message_still_counts_toward_the_allowance(client, login_as, cast, monkeypatch):
    _mock_moderation(monkeypatch, {"Hate": 2, "SelfHarm": 0, "Sexual": 0, "Violence": 0})
    with login_as(SENDER):
        client.post("/messages/send", json={"conversation_id": cast, "body": "flagged"})
        # A second clean send still succeeds, proving the first did not wedge the thread.
        _mock_moderation(monkeypatch, CLEAN)
        ok = client.post("/messages/send", json={"conversation_id": cast, "body": "clean"})
    assert ok.status_code == 201


def test_conversation_preview_does_not_leak_a_hidden_message(db, client, login_as, cast, monkeypatch):
    _mock_moderation(monkeypatch, CLEAN)
    with login_as(SENDER):
        client.post("/messages/send", json={"conversation_id": cast, "body": "visible one"})
        _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
        client.post("/messages/send", json={"conversation_id": cast, "body": "hidden two"})

    with db.sessions() as session:
        conversation = session.get(ConversationRecord, cast)
    assert conversation.last_message_preview == "visible one"


# ------------------------------------------------------------------ admin queue + restore


def _flag_one(client, login_as, cast, monkeypatch, body="bad text"):
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    with login_as(SENDER):
        client.post("/messages/send", json={"conversation_id": cast, "body": body})


def test_automated_report_reaches_the_admin_queue(admin_client, client, login_as, cast, monkeypatch):
    _flag_one(client, login_as, cast, monkeypatch)

    reports = admin_client.get("/admin/messages/reports").json()["reports"]
    assert len(reports) == 1
    report = reports[0]
    assert report["reporter"]["name"] == css.AUTOMATED_REPORTER_DISPLAY_NAME
    assert report["reporter"]["regno"] is None
    assert report["source"] == css.AUTOMATED_SOURCE
    assert report["status"] == "removed"
    assert report["content_removed"] is True
    assert report["removed_by_regno"] == css.AUTOMATED_REMOVED_BY


def test_admin_can_restore_an_azure_hidden_message(db, admin_client, client, login_as, cast, monkeypatch):
    _flag_one(client, login_as, cast, monkeypatch)
    row = message_for(db, "bad text")

    restored = admin_client.patch(f"/admin/messages/{row.id}/unhide")
    assert restored.status_code == 200, restored.text
    assert restored.json()["already_visible"] is False

    assert message_for(db, "bad text").removed_at is None
    with login_as(RECEIVER):
        transcript = client.get(f"/messages/{cast}")
    assert "bad text" in [message["body"] for message in transcript.json()["messages"]]


def test_restore_is_idempotent_for_a_visible_message(db, admin_client, client, login_as, cast, monkeypatch):
    _flag_one(client, login_as, cast, monkeypatch)
    row = message_for(db, "bad text")
    admin_client.patch(f"/admin/messages/{row.id}/unhide")
    again = admin_client.patch(f"/admin/messages/{row.id}/unhide")
    assert again.status_code == 200
    assert again.json()["already_visible"] is True


def test_restore_refuses_a_manually_hidden_message(db, admin_client, client, login_as, cast, monkeypatch):
    _mock_moderation(monkeypatch, CLEAN)
    with login_as(SENDER):
        sent = client.post("/messages/send", json={"conversation_id": cast, "body": "reported one"})
    message_id = sent.json()["id"]

    with login_as(RECEIVER):
        filed = client.post("/messages/report", json={"message_id": message_id, "reason": "abuse"})
    report_id = filed.json()["id"]
    removed = admin_client.patch(f"/admin/messages/reports/{report_id}?status=removed")
    assert removed.status_code == 200, removed.text

    refused = admin_client.patch(f"/admin/messages/{message_id}/unhide")
    assert refused.status_code == 409


def test_unhide_restores_azure_hidden_message_and_preview(db, client, login_as, cast, monkeypatch):
    """The service recomputes the conversation preview when the restored message is newest."""
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    with login_as(SENDER):
        client.post("/messages/send", json={"conversation_id": cast, "body": "restored newest"})
    row = message_for(db, "restored newest")

    unhide_message(db, str(row.id), "staff-regno")

    with db.sessions() as session:
        conversation = session.get(ConversationRecord, cast)
    assert conversation.last_message_preview == "restored newest"


def test_unhide_refuses_a_message_hidden_by_a_human(db):
    now = datetime.now(timezone.utc)
    make_user(db, SENDER)
    make_user(db, RECEIVER)
    conversation_id = make_accepted_conversation(db, SENDER, RECEIVER)
    with db.sessions.begin() as session:
        row = MessageRecord(
            conversation_id=conversation_id, sender_regno=SENDER,
            receiver_regno=RECEIVER, body="manual", created_at=now, is_read=False,
            removed_at=now, removed_by_regno="moderator-1",
        )
        session.add(row)
        session.flush()
        message_id = str(row.id)

    with pytest.raises(HTTPException) as exc:
        report_resolution_service.unhide_message(db, message_id, "staff-regno")
    assert exc.value.status_code == 409


# ------------------------------------------------------------------ central contract


def test_chat_path_uses_no_local_configuration():
    import inspect

    from app.services.message_service import MessageService

    src = inspect.getsource(MessageService.send_message)
    assert "moderate_content" in src
    assert "CONTENT_SAFETY_ENDPOINT" not in src
    assert "CONTENT_SAFETY_API_KEY" not in src
    assert "config_from_settings" not in src


def test_moderate_chat_message_text_wrapper_agrees(monkeypatch):
    _mock_moderation(monkeypatch, {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 6})
    decision = css.moderate_chat_message_text("wrapper check")
    assert decision.content_type == css.CONTENT_CHAT_MESSAGE
    assert decision.should_report is True
    assert decision.should_hide is True
    assert decision.should_block is False
