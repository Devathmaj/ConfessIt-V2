"""A conversation closed by a moderator is finished, not erased.

There is no longer a route that writes this state -- the moderation surface that produced it
was removed, and with it the ability for anyone to close a thread. The state itself is not
gone, though: ``conversations.status = 'terminated'`` and ``terminated_at`` remain columns,
every participant-facing query still branches on them, and rows written by the build that
predates the removal are sitting in databases right now with no migration to take them out.
These tests cover how the participants' own surfaces treat such a thread, because that
behaviour is still live code and still reachable.

The distinction that matters, and that the old tests existed to protect: a closed thread is
**finished, not unreadable**. It stays in both parties' lists, its transcript still opens,
and a message of its own can still be reported -- what stops is sending. Expiry is a
different axis and these tests hold the thread inside its match window so that a refusal can
only have come from the termination.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    UserRecord,
)
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services.conversation_service import (
    get_current_conversation_service,
    get_received_conversations_service,
)
from app.services.message_service import message_service


WINDOW = timedelta(hours=4)


@pytest.fixture
def db():
    return get_postgres_client()


def make_user(db, regno, gender):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender=gender,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=row.email,
        year=row.year, department=row.department, gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled, is_blocked=row.is_blocked,
        last_matchmaking_at=row.last_matchmaking_at,
        account_type=AccountType.USERS.value,
    )


def make_accepted_conversation(db, initiator, receiver, *, minutes_ago=10):
    """A matched, accepted conversation inside its own four-hour match window.

    Kept well inside the window so that any refusal a test sees is attributable to the
    termination rather than to the match having run out.
    """
    created_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    with db.sessions.begin() as session:
        match_row = MatchRecord(
            user_1_regno=initiator, user_2_regno=receiver,
            created_at=created_at, expires_at=created_at + WINDOW,
        )
        session.add(match_row)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match_row.id), initiator_regno=initiator,
            receiver_regno=receiver, status="accepted",
            created_at=created_at, requested_at=created_at, accepted_at=created_at,
        )
        session.add(conversation)
        session.flush()
        return str(match_row.id), str(conversation.id)


def mark_terminated(db, conversation_id, *, minutes_ago=5):
    """Write the terminal state the way the removed route did.

    Status and ``terminated_at`` only. The match's ``expires_at`` is deliberately left
    alone: that was the whole point of the fix this file originally guarded, and a moderator
    closing a thread is not the same event as the window running out.
    """
    with db.sessions.begin() as session:
        conversation = session.get(ConversationRecord, conversation_id)
        conversation.status = "terminated"
        conversation.terminated_at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


def make_lapsed_thread(db, status, *, hours_ago=5, terminated_hours_ago=None):
    """A thread whose match has already run out, in one of the two states that can coexist.

    ``terminated_hours_ago`` set is how an older build recorded a termination: the match was
    stamped as expiring at the instant of the decision, so the row reads as lapsed by every
    clock-based query. Both shapes are in existing databases.
    """
    expires_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    created_at = expires_at - WINDOW
    with db.sessions.begin() as session:
        match_row = MatchRecord(
            user_1_regno="alice", user_2_regno="bob", created_at=created_at, expires_at=expires_at,
        )
        session.add(match_row)
        session.flush()
        session.add(ConversationRecord(
            match_id=str(match_row.id), initiator_regno="alice", receiver_regno="bob",
            status=status, created_at=created_at,
            terminated_at=(
                datetime.now(timezone.utc) - timedelta(hours=terminated_hours_ago)
                if terminated_hours_ago is not None
                else None
            ),
        ))


def stored_message_count(db) -> int:
    with db.sessions() as session:
        return len(list(session.scalars(select(MessageRecord)).all()))


# ------------------------------------------------- both participants keep the conversation


def test_a_closed_thread_is_still_in_the_initiators_list(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    mark_terminated(db, conversation_id)
    response = get_current_conversation_service(as_user(db, "alice"), db)

    assert response["status"] == "success"
    assert response["conversation"]["id"] == conversation_id
    assert response["conversation"]["status"] == "terminated"


def test_a_closed_thread_is_still_in_the_receivers_list(db):
    # This list selects on the match expiry, so anything that rewrote ``expires_at`` dropped
    # the thread out of the receiver's message box. A closed thread has to keep saying live.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    mark_terminated(db, conversation_id)
    response = get_received_conversations_service(as_user(db, "bob"), db)

    assert response["status"] == "success"
    assert [c["conversation"]["id"] for c in response["conversations"]] == [conversation_id]
    assert response["conversations"][0]["conversation"]["status"] == "terminated"


# ------------------------------------------------------------------ finished, not unreadable


def test_the_transcript_of_a_closed_thread_is_still_readable(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "before it was shut")

    mark_terminated(db, conversation_id)

    messages = message_service.get_messages(db, conversation_id, as_user(db, "alice"))["messages"]
    assert [m["body"] for m in messages] == ["before it was shut"]


def test_both_sides_keep_reading_a_closed_thread(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "one")
    message_service.send_message(db, conversation_id, as_user(db, "bob"), "two")

    mark_terminated(db, conversation_id)

    for regno in ("alice", "bob"):
        bodies = [
            m["body"]
            for m in message_service.get_messages(
                db, conversation_id, as_user(db, regno)
            )["messages"]
        ]
        assert bodies == ["one", "two"]


def test_a_message_to_a_closed_thread_is_refused_and_stores_nothing(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    mark_terminated(db, conversation_id)

    with pytest.raises(HTTPException) as excinfo:
        message_service.send_message(db, conversation_id, as_user(db, "alice"), "still here?")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "This conversation has been terminated"
    assert stored_message_count(db) == 0


def test_a_participant_can_still_report_a_message_from_a_closed_thread(db):
    # The report menu is offered on whatever the reader can open, so the server has to agree
    # for the messages of a closed thread. This is the one route out of a conversation that
    # does not depend on it still being open, and a thread nobody can report is exactly the
    # case where a report matters most.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    sent = message_service.send_message(db, conversation_id, as_user(db, "alice"), "rude")

    mark_terminated(db, conversation_id)

    assert message_service.report_message(db, sent["id"], as_user(db, "bob"), "abuse")["id"]


# ------------------------------------------- threads closed by a build that could close them


def test_a_thread_closed_by_the_old_build_is_still_listed(db):
    # Those rows carry expires_at == terminated_at, so every clock-based query reads them as
    # lapsed. They are already in the database, so the participants' lists have to cope.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_lapsed_thread(db, "terminated", hours_ago=1, terminated_hours_ago=1)

    response = get_received_conversations_service(as_user(db, "bob"), db)

    assert [c["conversation"]["status"] for c in response["conversations"]] == ["terminated"]


def test_the_initiator_sees_a_thread_closed_by_the_old_build_too(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_lapsed_thread(db, "terminated", hours_ago=1, terminated_hours_ago=1)

    response = get_current_conversation_service(as_user(db, "alice"), db)

    assert response["status"] == "success"
    assert response["conversation"]["status"] == "terminated"


def test_a_lapsed_thread_is_not_listed_merely_because_it_is_old(db):
    # The counterpart: the same query must not turn every expired conversation back into an
    # open one, or the message box would fill with dead threads. Being closed is not what
    # keeps a thread listed here -- the match window is -- so an old accepted thread goes.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_lapsed_thread(db, "accepted", hours_ago=6)

    assert get_received_conversations_service(as_user(db, "bob"), db) == {
        "status": "no_received_conversations",
        "conversations": [],
    }


# --------------------------------------------------------------------- other states intact


def test_a_live_conversation_is_still_listed_for_the_receiver(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    response = get_received_conversations_service(as_user(db, "bob"), db)

    assert [c["conversation"]["id"] for c in response["conversations"]] == [conversation_id]
    assert response["conversations"][0]["conversation"]["status"] == "accepted"
