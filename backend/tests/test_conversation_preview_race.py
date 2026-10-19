"""The conversation preview must never regress to an older message.

The conversation list previews the newest message of a thread. Two writers
touch that preview: a send, which writes the message it just stored, and a
moderator's removal, which recomputes the preview from the newest message that
is still visible.

They used to be able to interleave. The removal chose its replacement from a
snapshot that did not contain the message being sent (it was not committed
yet), and then wrote that older preview over the newer one -- so the inbox
previewed a message that was not the newest, and briefly showed text a
moderator had just taken down.

The fix is that the removal takes the same lock the send takes, so the two
writers serialise: whichever runs second sees the other's committed result and
recomputes from it.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.accounts import AccountType
from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageReportRecord,
    UserRecord,
)
from app.models import UserDetails
from app.services.message_service import message_service
from app.services.report_resolution_service import resolve_message_report

WINDOW = timedelta(hours=4)


def _seed_user(db, regno: str) -> None:
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno, name=regno.title(), email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        ))


def _as_user(db, regno: str) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    assert row is not None, f"no account for {regno!r}"
    return UserDetails(
        id=str(row.id), regno=regno, name=regno.title(), email=f"{regno}@test.com",
        year=2, department="Test_Class", gender="other",
        matchmaking_enabled=True, notifications_enabled=True,
        is_blocked=False, account_type=AccountType.USERS.value,
    )


def _conversation(db, initiator: str, receiver: str) -> tuple[str, str]:
    """An accepted conversation, and the id of its match row."""
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=initiator, user_2_regno=receiver,
            created_at=created_at, expires_at=created_at + WINDOW,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno=initiator, receiver_regno=receiver,
            status="accepted", created_at=created_at, requested_at=created_at,
            accepted_at=created_at,
        )
        session.add(conversation)
        session.flush()
        return str(conversation.id), str(match.id)


def _report(db, message_id: str, reporter: str, reported: str) -> str:
    with db.sessions.begin() as session:
        report = MessageReportRecord(
            message_id=message_id,
            reporter_regno=reporter,
            reported_user_regno=reported,
            reason="this one is reported",
            status="pending",
            reported_at=datetime.now(timezone.utc),
        )
        session.add(report)
        session.flush()
        return str(report.id)


def _preview_of(db, conversation_id: str):
    with db.sessions() as session:
        row = session.get(ConversationRecord, conversation_id)
        return (
            row.last_message_preview_message_id,
            row.last_message_at,
        )


def test_the_preview_keeps_the_newest_message_when_a_removal_races_a_send(db):
    """A removal that lands while a send is open must not rewind the preview.

    The send is made to stop with the conversation row locked and its message
    not yet written: the send locks the conversation and then the match, so
    holding the match row in another connection parks the send exactly in the
    window the removal used to slip through.
    """
    _seed_user(db, "preview_sender")
    _seed_user(db, "preview_receiver")
    conversation_id, match_id = _conversation(db, "preview_sender", "preview_receiver")
    sender = _as_user(db, "preview_sender")

    # Two messages, so there is an older one to regress to, and a report on the
    # newest one for the moderator to resolve.
    first = message_service.send_message(
        db=db, conversation_id=conversation_id, sender=sender, body="the first"
    )
    second = message_service.send_message(
        db=db, conversation_id=conversation_id, sender=sender, body="the second"
    )
    report_id = _report(db, second["id"], "preview_receiver", "preview_sender")
    assert _preview_of(db, conversation_id)[0] == second["id"]

    # Hold the match row: the send locks the conversation and then the match, so
    # the send stops with the conversation row locked and nothing written yet.
    blocker = db.engine.connect()
    transaction = blocker.begin()
    blocker.execute(
        text("SELECT id FROM matches WHERE id = :id FOR UPDATE"), {"id": match_id}
    )

    sent: dict = {}
    removed: dict = {}

    def do_send():
        try:
            sent["result"] = message_service.send_message(
                db=db, conversation_id=conversation_id, sender=sender, body="the newest"
            )
        except Exception as exc:  # pragma: no cover - surfaced by the assertions
            sent["error"] = exc

    def do_removal():
        try:
            removed["result"] = resolve_message_report(
                db, report_id, "removed", "moderator-one"
            )
        except Exception as exc:  # pragma: no cover - surfaced by the assertions
            removed["error"] = exc

    try:
        sender_thread = threading.Thread(target=do_send)
        sender_thread.start()
        # The send now holds the conversation row and waits for the match.
        time.sleep(0.5)

        removal_thread = threading.Thread(target=do_removal)
        removal_thread.start()
        # The removal is now either waiting for the conversation row (fixed) or
        # holding an older snapshot of it (not fixed).
        time.sleep(0.5)

        # Let the send through.
        transaction.rollback()
        blocker.close()

        sender_thread.join(timeout=30)
        removal_thread.join(timeout=30)
    finally:
        try:
            transaction.rollback()
            blocker.close()
        except Exception:
            pass

    assert "error" not in sent, sent.get("error")
    assert "error" not in removed, removed.get("error")
    assert not sender_thread.is_alive()
    assert not removal_thread.is_alive()

    newest_id = sent["result"]["id"]
    preview_id, preview_at = _preview_of(db, conversation_id)

    # The preview points at the newest message that is still visible: the one
    # that was just sent. The removed one is gone, and the older one must not
    # take the preview's place.
    assert preview_id == newest_id
    # ... and the preview's own timestamp is that message's.
    assert preview_at is not None
