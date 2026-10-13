"""AES-256-GCM chat encryption, retention and evidence end to end.

These tests pin the parts that are easy to weaken silently:

* the primitive never reuses a nonce, authenticates the record metadata, and
  refuses tampered ciphertext;
* a key rotation keeps old rows readable without ever writing plaintext;
* a stored message is ciphertext only, with a retention deadline derived from
  the chat's own end, and a read stops exposing it the moment that deadline
  passes -- before the daily physical purge;
* the purge is bounded, idempotent, and clears the conversation preview that
  copied the purged message;
* a reported message keeps its own encrypted evidence after the live row is
  purged, and every moderator read of that evidence is audited.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.db_models import (
    ChatEvidenceAccessRecord,
    ChatMessageEvidenceRecord,
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    UserRecord,
)
from app.models import AccountType, UserDetails
from app.services import content_safety_service as css
from app.services.chat_encryption import (
    CHAT_TYPE_MATCHMAKING,
    ChatDecryptionError,
    ChatEncryptionConfigError,
    configured_keys,
    decrypt_text,
    encrypt_text,
    validate_startup_configuration,
)
from app.services.chat_message_crypto import (
    decrypt_matchmaking_message,
    decrypt_matchmaking_preview,
    decrypt_report_evidence,
    encrypt_report_evidence,
)
from app.services.chat_retention_service import cleanup_expired_chat_messages
from app.services.conversation_service import _conversation_data
from app.services.message_service import message_service
from tests.chat_helpers import make_message_record
from test_case_closed import players  # noqa: F401  (fixture discovery)

CLEAN = {"Hate": 0, "SelfHarm": 0, "Sexual": 0, "Violence": 0}
SENDER = "enc-sender"
RECEIVER = "enc-receiver"


def _aad(**over):
    values = {
        "chat_type": CHAT_TYPE_MATCHMAKING,
        "record_id": "message-1",
        "chat_id": "conversation-1",
        "sender_id": "sender-1",
    }
    values.update(over)
    return values


def _clean_moderation(monkeypatch):
    def _fake(content_type, text, *, config=None):
        return css._decision_for(content_type, dict(CLEAN))

    monkeypatch.setattr(css, "moderate_content", _fake)


# --------------------------------------------------------------- primitive


def test_each_encryption_uses_a_fresh_nonce_and_round_trips():
    first = encrypt_text("the same plaintext", **_aad())
    second = encrypt_text("the same plaintext", **_aad())

    assert len(first.nonce) == 12
    assert len(second.nonce) == 12
    assert first.nonce != second.nonce
    assert first.ciphertext != second.ciphertext
    # AESGCM.encrypt returns ciphertext + 16-byte tag.
    assert len(first.ciphertext) == len("the same plaintext".encode("utf-8")) + 16
    assert b"the same plaintext" not in first.ciphertext
    assert decrypt_text(
        first.ciphertext, first.nonce, first.key_version, **_aad()
    ) == "the same plaintext"


def test_authenticated_metadata_binds_the_ciphertext_to_its_record():
    payload = encrypt_text("bound body", **_aad())

    for changed in (
        {"record_id": "other-message"},
        {"chat_id": "other-conversation"},
        {"sender_id": "other-sender"},
        {"chat_type": "case_closed"},
        {"context": "other-context"},
    ):
        with pytest.raises(ChatDecryptionError):
            decrypt_text(
                payload.ciphertext, payload.nonce, payload.key_version, **_aad(**changed)
            )


def test_tampered_ciphertext_nonce_or_key_version_is_refused():
    payload = encrypt_text("tamper me", **_aad())
    tampered = bytes([payload.ciphertext[0] ^ 0x01]) + payload.ciphertext[1:]

    with pytest.raises(ChatDecryptionError):
        decrypt_text(tampered, payload.nonce, payload.key_version, **_aad())
    with pytest.raises(ChatDecryptionError):
        decrypt_text(payload.ciphertext, b"\x00" * 12, payload.key_version, **_aad())
    with pytest.raises(ChatDecryptionError):
        decrypt_text(payload.ciphertext, payload.nonce, "v9", **_aad())


def test_report_evidence_binds_the_original_turn_and_the_report():
    payload = encrypt_report_evidence(
        evidence_id="evidence-1",
        report_id="report-1",
        report_kind="matchmaking",
        message_id="message-1",
        chat_id="conversation-1",
        sender_ref="sender-1",
        plaintext="the offending turn",
    )
    row = SimpleNamespace(
        id="evidence-1",
        report_id="report-1",
        report_kind="matchmaking",
        message_id="message-1",
        chat_id="conversation-1",
        sender_ref="sender-1",
        body_ciphertext=payload.ciphertext,
        body_nonce=payload.nonce,
        body_key_version=payload.key_version,
    )
    assert decrypt_report_evidence(row) == "the offending turn"

    for field, value in (
        ("message_id", "message-2"),
        ("chat_id", "conversation-2"),
        ("sender_ref", "sender-2"),
        ("report_id", "report-2"),
    ):
        changed = SimpleNamespace(**{**row.__dict__, field: value})
        with pytest.raises(ChatDecryptionError):
            decrypt_report_evidence(changed)


def test_key_rotation_keeps_old_rows_readable(monkeypatch):
    original = encrypt_text("written under v1", **_aad())
    assert original.key_version == "v1"

    monkeypatch.setattr(
        settings,
        "CHAT_ENCRYPTION_KEY_V2",
        base64.b64encode(bytes(range(32))).decode(),
    )
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION", "v2")

    rotated = encrypt_text("written under v2", **_aad())
    assert rotated.key_version == "v2"
    assert decrypt_text(rotated.ciphertext, rotated.nonce, "v2", **_aad()) == "written under v2"
    # The old row still selects v1 from its own column, not the active version.
    assert decrypt_text(
        original.ciphertext, original.nonce, original.key_version, **_aad()
    ) == "written under v1"


def test_missing_or_malformed_keys_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION", "v1")
    # conftest exports the fixed test key, which wins over the settings object
    # (see configured_keys' environment scan), so remove both sources.
    monkeypatch.delenv("CHAT_ENCRYPTION_KEY_V1", raising=False)
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_KEY_V1", None)
    with pytest.raises(ChatEncryptionConfigError):
        validate_startup_configuration()

    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_KEY_V1", "not base64!")
    with pytest.raises(ChatEncryptionConfigError):
        validate_startup_configuration()

    monkeypatch.setattr(
        settings, "CHAT_ENCRYPTION_KEY_V1", base64.b64encode(b"too-short").decode()
    )
    with pytest.raises(ChatEncryptionConfigError):
        validate_startup_configuration()


def test_a_malformed_historical_key_is_rejected_at_startup(monkeypatch):
    """A bad V2 must fail boot even while V1 is active: otherwise it surfaces later as an
    unreadable message, long after the operator could connect it to the environment."""
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION", "v1")
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_KEY_V2", "%%%%")
    with pytest.raises(ChatEncryptionConfigError):
        validate_startup_configuration()


def test_configured_keys_ignores_blank_optional_entries(monkeypatch):
    monkeypatch.setattr(settings, "CHAT_ENCRYPTION_KEY_V2", "")
    assert "v2" not in configured_keys()


# --------------------------------------------------------------- fixtures


def _make_user(db, regno: str) -> UserDetails:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2,
            department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return UserDetails(
            id=str(row.id),
            regno=row.regno,
            name=row.name,
            email=row.email,
            year=row.year,
            department=row.department,
            gender=row.gender,
            matchmaking_enabled=True,
            notifications_enabled=True,
            account_type=AccountType.USERS.value,
        )


def _make_conversation(db, sender: UserDetails, receiver: UserDetails) -> tuple[str, datetime]:
    now = datetime.now(timezone.utc)
    match_expires_at = now + timedelta(hours=4)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=sender.regno,
            user_2_regno=receiver.regno,
            created_at=now,
            expires_at=match_expires_at,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id),
            initiator_regno=sender.regno,
            receiver_regno=receiver.regno,
            status="accepted",
            created_at=now,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id), match_expires_at


@pytest.fixture
def chat(db):
    sender = _make_user(db, SENDER)
    receiver = _make_user(db, RECEIVER)
    conversation_id, match_expires_at = _make_conversation(db, sender, receiver)
    return {
        "sender": sender,
        "receiver": receiver,
        "conversation_id": conversation_id,
        "match_expires_at": match_expires_at,
    }


# --------------------------------------------------------------- send path


def test_sent_message_is_ciphertext_with_the_match_retention_deadline(
    db, chat, monkeypatch
):
    _clean_moderation(monkeypatch)

    sent = message_service.send_message(
        db, chat["conversation_id"], chat["sender"], "a private sentence"
    )

    with db.sessions() as session:
        row = session.get(MessageRecord, sent["id"])
        conversation = session.get(ConversationRecord, chat["conversation_id"])
        assert row is not None
        assert row.body_ciphertext
        assert b"a private sentence" not in row.body_ciphertext
        assert len(row.body_nonce) == 12
        assert row.body_key_version == "v1"
        assert row.delete_after == chat["match_expires_at"] + timedelta(
            hours=settings.MATCHMAKING_CHAT_RETENTION_HOURS
        )
        assert decrypt_matchmaking_message(row) == "a private sentence"

        assert conversation.last_message_preview_ciphertext
        assert b"a private sentence" not in conversation.last_message_preview_ciphertext
        assert conversation.last_message_preview_delete_after == row.delete_after
        assert decrypt_matchmaking_preview(conversation) == "a private sentence"


def test_expired_message_is_logically_unreadable_before_the_purge(db, chat, monkeypatch):
    _clean_moderation(monkeypatch)
    sent = message_service.send_message(
        db, chat["conversation_id"], chat["sender"], "gone soon"
    )
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db.sessions.begin() as session:
        session.get(MessageRecord, sent["id"]).delete_after = past
        session.get(ConversationRecord, chat["conversation_id"]).last_message_preview_delete_after = past

    history = message_service.get_messages(db, chat["conversation_id"], chat["receiver"])
    assert history["messages"] == []
    assert history["total"] == 0

    with db.sessions() as session:
        conversation = session.get(ConversationRecord, chat["conversation_id"])
        # The ciphertext may still be present until the next purge, but the
        # conversation list must stop exposing it at its own deadline.
        assert conversation.last_message_preview_ciphertext is not None
        assert _conversation_data(conversation)["last_message_preview"] is None

    with pytest.raises(HTTPException) as refused:
        message_service.report_message(
            db, sent["id"], chat["receiver"], "too late to report"
        )
    assert refused.value.status_code == 404


def test_cleanup_is_batched_idempotent_and_clears_the_preview(db, chat, monkeypatch):
    _clean_moderation(monkeypatch)
    ids = [
        message_service.send_message(
            db, chat["conversation_id"], chat["sender"], f"expired {index}"
        )["id"]
        for index in range(3)
    ]
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db.sessions.begin() as session:
        for message_id in ids:
            session.get(MessageRecord, message_id).delete_after = past
        conversation = session.get(ConversationRecord, chat["conversation_id"])
        conversation.last_message_preview_delete_after = past

    first = cleanup_expired_chat_messages(db, now=datetime.now(timezone.utc), batch_size=2)
    assert first["matchmaking_messages_deleted"] == 3
    assert first["batches"] == 2

    with db.sessions() as session:
        assert session.scalars(select(MessageRecord)).all() == []
        conversation = session.get(ConversationRecord, chat["conversation_id"])
        assert conversation.last_message_preview_ciphertext is None
        assert conversation.last_message_preview_message_id is None
        assert conversation.last_message_preview_delete_after is None

    again = cleanup_expired_chat_messages(db, now=datetime.now(timezone.utc), batch_size=2)
    assert again["matchmaking_messages_deleted"] == 0
    assert again["batches"] == 0


def test_unexpired_messages_survive_a_cleanup(db, chat, monkeypatch):
    _clean_moderation(monkeypatch)
    sent = message_service.send_message(
        db, chat["conversation_id"], chat["sender"], "still readable"
    )

    cleanup_expired_chat_messages(db, now=datetime.now(timezone.utc), batch_size=2)

    with db.sessions() as session:
        assert session.get(MessageRecord, sent["id"]) is not None


# --------------------------------------------------------------- evidence


def test_reported_evidence_outlives_the_message_and_moderator_access_is_audited(
    db, chat, monkeypatch, admin_client
):
    _clean_moderation(monkeypatch)
    sent = message_service.send_message(
        db, chat["conversation_id"], chat["sender"], "the reported sentence"
    )
    reported = message_service.report_message(
        db, sent["id"], chat["receiver"], "this is abusive"
    )

    with db.sessions() as session:
        evidence = session.scalar(
            select(ChatMessageEvidenceRecord).where(
                ChatMessageEvidenceRecord.report_id == reported["id"]
            )
        )
        assert evidence is not None
        assert b"the reported sentence" not in evidence.body_ciphertext
        assert evidence.expires_at > datetime.now(timezone.utc)

    # Purge the live row but leave the report and its evidence in place.
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    with db.sessions.begin() as session:
        session.get(MessageRecord, sent["id"]).delete_after = past
    cleanup = cleanup_expired_chat_messages(
        db, now=datetime.now(timezone.utc), batch_size=10
    )
    assert cleanup["matchmaking_messages_deleted"] == 1

    with db.sessions() as session:
        assert session.get(MessageRecord, sent["id"]) is None
        assert session.get(MessageReportRecord, reported["id"]) is not None
        assert session.get(ChatMessageEvidenceRecord, str(evidence.id)) is not None

    queue = admin_client.get(
        "/admin/messages/reports", params={"status": "pending"}
    )
    assert queue.status_code == 200, queue.text
    reports = queue.json()["reports"]
    assert len(reports) == 1
    assert reports[0]["body"] == "the reported sentence"
    assert reports[0]["message_id"] is None

    with db.sessions() as session:
        audit = session.scalar(
            select(ChatEvidenceAccessRecord).where(
                ChatEvidenceAccessRecord.report_id == reported["id"]
            )
        )
        assert audit is not None
        assert audit.accessed_by_regno == "adminuser"
        assert audit.evidence_id == str(evidence.id)

    # Evidence has its own retention window. It becomes unreadable at the
    # deadline, before the purge, and a failed read must not add an audit row
    # claiming access that never delivered content.
    with db.sessions.begin() as session:
        session.get(ChatMessageEvidenceRecord, str(evidence.id)).expires_at = past
    expired_queue = admin_client.get(
        "/admin/messages/reports", params={"status": "pending"}
    ).json()
    assert expired_queue["reports"][0]["body"] == "[Message not found]"
    with db.sessions() as session:
        assert len(session.scalars(select(ChatEvidenceAccessRecord)).all()) == 1

    cleanup = cleanup_expired_chat_messages(
        db, now=datetime.now(timezone.utc), batch_size=10
    )
    assert cleanup["reported_evidence_deleted"] == 1
    with db.sessions() as session:
        assert session.get(ChatMessageEvidenceRecord, str(evidence.id)) is None


def test_evidence_for_an_unreported_message_is_never_created(db, chat, monkeypatch):
    _clean_moderation(monkeypatch)
    message_service.send_message(db, chat["conversation_id"], chat["sender"], "private")
    with db.sessions() as session:
        assert session.scalars(select(ChatMessageEvidenceRecord)).all() == []


def test_case_closed_event_deadline_is_the_exclusive_utc_midnight():
    """The pinned event's ``period_end_date`` is exclusive: the last playable day is
    the day before, and the session/chat deadline is 00:00 UTC on that end date."""
    from datetime import date

    from app.services.case_closed_gameplay import _event_deadline

    game = SimpleNamespace(period_end_date=date(2026, 1, 3))
    assert _event_deadline(game) == datetime(2026, 1, 3, 0, 0, tzinfo=timezone.utc)
    assert _event_deadline(SimpleNamespace(period_end_date=None)) is None


# --------------------------------------------------------------- Case Closed cleanup


def test_case_closed_messages_are_purged_by_the_same_cleanup(db, players):
    from app.db_models import CaseClosedMessageRecord, CaseClosedSessionRecord
    from test_case_closed import seed_event
    from tests.chat_helpers import make_case_closed_message_record

    game_id, package_id = seed_event(db)
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        game_session = CaseClosedSessionRecord(
            case_closed_game_id=game_id,
            case_package_id=package_id,
            user_a_id=players["a"].id,
            user_b_id=players["b"].id,
            status="investigating",
            expires_at=now - timedelta(seconds=1),
        )
        session.add(game_session)
        session.flush()
        expired = make_case_closed_message_record(
            session_id=str(game_session.id),
            sender_user_id=players["a"].id,
            body="expired turn",
            created_at=now - timedelta(hours=1),
            delete_after=now - timedelta(seconds=1),
        )
        live = make_case_closed_message_record(
            session_id=str(game_session.id),
            sender_user_id=players["a"].id,
            body="live turn",
            created_at=now,
            delete_after=now + timedelta(days=1),
        )
        session.add_all([expired, live])
        session.flush()
        expired_id, live_id = str(expired.id), str(live.id)

    summary = cleanup_expired_chat_messages(db, now=now, batch_size=10)

    assert summary["case_closed_messages_deleted"] == 1
    with db.sessions() as session:
        assert session.get(CaseClosedMessageRecord, expired_id) is None
        assert session.get(CaseClosedMessageRecord, live_id) is not None


def test_a_chat_lock_does_not_change_the_session_retention_deadline(db, players):
    """A confirmed accusation closes writes but the transcript stays until session expiry."""
    from app.db_models import CaseClosedSessionRecord
    from app.services.case_closed_messaging import thread_state
    from test_case_closed import seed_event

    game_id, package_id = seed_event(db)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=2)
    with db.sessions.begin() as session:
        game_session = CaseClosedSessionRecord(
            case_closed_game_id=game_id,
            case_package_id=package_id,
            user_a_id=players["a"].id,
            user_b_id=players["b"].id,
            status="investigating",
            expires_at=expires_at,
            locked_culprit_id="s1",
            accusation_locked_at=now,
        )
        session.add(game_session)
        session.flush()
        state = thread_state(game_session, now=now)
        assert state["chat_open"] is False
        assert game_session.expires_at == expires_at
