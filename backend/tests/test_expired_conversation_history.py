"""An expired conversation is read-only, not unreadable.

Expiry and blocking are both write-restrictions, and both used to be enforced inside
``validate_conversation_participant``, which ``get_messages`` shares. Blocking is
deliberately also refused on read (see ``test_blocked_matchmaking``), but expiry is not:
once the four-hour window closes the thread is frozen, and the people in it still need to
see what they said before it froze. Leaving the check in the shared validator made
``get_messages`` answer 403 for an expired conversation, so the chat dialog rendered an
empty history for a thread that plainly had messages in it.

These tests pin the split: sending is refused after expiry, reading is not.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db_models import ConversationRecord, MatchRecord, MessageRecord, UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services.message_service import message_service


WINDOW = timedelta(hours=4)


def make_user(db, regno, gender, *, role="user"):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            class_name="Test_Class", gender=gender, role=role,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        class_name=row.class_name, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled, is_blocked=row.is_blocked,
        role=row.role,
    )


def make_accepted_conversation(db, initiator, receiver, *, created_at=None):
    """An accepted conversation, optionally created long enough ago to be expired.

    ``created_at`` is what the send-side expiry check measures against, and
    ``expires_at`` is what the client reads as ``is_expired``. Both are backdated together
    so the server and the dialog agree about the thread being closed.
    """
    now = datetime.now(timezone.utc)
    created_at = created_at or now
    with db.sessions.begin() as session:
        match_row = MatchRecord(
            user_1_regno=initiator, user_2_regno=receiver,
            created_at=created_at, expires_at=created_at + WINDOW,
        )
        session.add(match_row)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match_row.id), initiator_regno=initiator,
            receiver_regno=receiver, status="accepted", created_at=created_at,
        )
        session.add(conversation)
        session.flush()
        return str(match_row.id), str(conversation.id)


def stored_message_count(db) -> int:
    with db.sessions() as session:
        return session.scalar(select(func.count()).select_from(MessageRecord))


@pytest.fixture
def db():
    return get_postgres_client()


# ------------------------------------------------------------------ reading survives


def test_the_history_of_an_expired_conversation_is_still_readable(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "still here?")

    # The window closes on the existing thread; nothing about it is rebuilt.
    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).created_at = (
            datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
        )

    messages = message_service.get_messages(db, conversation_id, as_user(db, "alice"))

    assert [m["body"] for m in messages] == ["still here?"]


def test_both_sides_keep_reading_an_expired_conversation(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "first")
    message_service.send_message(db, conversation_id, as_user(db, "bob"), "second")

    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).created_at = (
            datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
        )

    for regno in ("alice", "bob"):
        bodies = [m["body"] for m in message_service.get_messages(db, conversation_id, as_user(db, regno))]
        assert bodies == ["first", "second"]


def test_an_expired_thread_already_open_is_readable_without_a_reload(db):
    """The dialog loads history once, on open. An expiry that lands later must not
    invalidate what was already read, and a fresh read of the same row still works."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")

    first = message_service.get_messages(db, conversation_id, as_user(db, "alice"))

    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).created_at = (
            datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
        )

    second = message_service.get_messages(db, conversation_id, as_user(db, "alice"))

    assert [m["body"] for m in first] == ["hello"]
    assert [m["body"] for m in second] == ["hello"]


def test_an_admin_can_still_moderate_an_expired_conversation(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_user(db, "mod", "other", role="admin")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")

    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).created_at = (
            datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
        )

    messages = message_service.get_messages(db, conversation_id, as_user(db, "mod"))

    assert [m["body"] for m in messages] == ["hello"]


# ------------------------------------------------------------------ writing stops


def test_a_message_to_an_expired_conversation_is_refused_and_stores_nothing(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(
        db, "alice", "bob", created_at=datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
    )

    with pytest.raises(HTTPException) as excinfo:
        message_service.send_message(db, conversation_id, as_user(db, "alice"), "too late")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "Conversation has expired"
    assert stored_message_count(db) == 0


def test_a_message_just_inside_the_window_is_still_accepted(db):
    """The move must not shift the boundary, only stop it applying to reads."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(
        db, "alice", "bob", created_at=datetime.now(timezone.utc) - WINDOW + timedelta(minutes=5)
    )

    result = message_service.send_message(db, conversation_id, as_user(db, "alice"), "in time")

    assert result["receiver_regno"] == "bob"
    assert stored_message_count(db) == 1


# ------------------------------------------------------------------ blocking is unchanged


def test_a_blocked_partner_is_still_unreadable_in_an_expired_conversation(db):
    """Blocking outranks expiry: an expired thread with a blocked partner stays shut.

    Guards the fix above against quietly reopening the blocked case, since both checks
    were removed from the same validator.
    """
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")

    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).created_at = (
            datetime.now(timezone.utc) - WINDOW - timedelta(minutes=1)
        )
        session.scalar(select(UserRecord).where(UserRecord.regno == "bob")).is_blocked = True

    with pytest.raises(HTTPException) as excinfo:
        message_service.get_messages(db, conversation_id, as_user(db, "alice"))

    assert excinfo.value.status_code == 403
