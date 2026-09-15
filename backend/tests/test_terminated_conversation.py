"""A moderator-closed conversation is finished, not erased.

Termination used to stamp the match's ``expires_at`` with the current time. That made one
event look like two very different ones: to the matchmaking screen the match had simply run
out, so it announced that a new match could be drawn -- while ``last_matchmaking_at`` was
untouched and the server was still refusing the draw inside the four-hour cooldown. To the
conversation lists, which select on ``expires_at``, the receiver's thread vanished from their
message box and both parties lost the notification explaining what had happened.

These tests pin the properties that fixes: the cooldown is untouched and still reported as
running, the terminated thread stays listed and readable to both participants, and both are
told it happened. The last group covers conversations terminated by the previous build, whose
``expires_at`` was already rewritten, so the fix reaches rows that are already in the
database rather than only preventing new ones.
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
    NotificationRecord,
    UserRecord,
)
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services.conversation_service import (
    get_current_conversation_service,
    get_received_conversations_service,
)
from app.services.matchmaking_service import (
    check_matchmaking_cooldown_service,
    find_match_service,
)
from app.services.message_service import message_service


WINDOW = timedelta(hours=4)
TERMINATION_HEADING = "Conversation terminated by a moderator"


@pytest.fixture
def db():
    return get_postgres_client()


def make_user(db, regno, gender):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            class_name="Test_Class", gender=gender,
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
        last_matchmaking_at=row.last_matchmaking_at,
        account_type=AccountType.USERS.value,
    )


def make_accepted_conversation(db, initiator, receiver, *, minutes_ago=10):
    """A matched, accepted conversation whose initiator has spent their attempt.

    ``last_matchmaking_at`` is what the cooldown counts down from and the match expiry is
    what the conversation lists count down from, so both are written from one instant here --
    otherwise a test could pass for the wrong reason.
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
        match_id, conversation_id = str(match_row.id), str(conversation.id)

        session.scalar(select(UserRecord).where(UserRecord.regno == initiator)).last_matchmaking_at = (
            created_at
        )

    return match_id, conversation_id


def terminate(admin_client, conversation_id):
    response = admin_client.post(
        f"/admin/conversations/{conversation_id}/terminate", json={"reason": "abuse"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def match_expiry(db, match_id):
    with db.sessions() as session:
        return session.get(MatchRecord, match_id).expires_at


def notifications_for(db, regno):
    with db.sessions() as session:
        return list(
            session.scalars(
                select(NotificationRecord).where(NotificationRecord.recipient_regno == regno)
            ).all()
        )


def stored_message_count(db) -> int:
    with db.sessions() as session:
        return len(list(session.scalars(select(MessageRecord)).all()))


# ----------------------------------------------------- the cooldown is not what ends here


def test_terminating_leaves_the_match_expiry_alone(db, admin_client):
    # The regression: the endpoint rewrote expires_at to "now", which is what made every
    # client read a moderator decision as the four-hour window running out.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, conversation_id = make_accepted_conversation(db, "alice", "bob")
    before = match_expiry(db, match_id)

    terminate(admin_client, conversation_id)

    assert match_expiry(db, match_id) == before


def test_terminating_does_not_lift_the_cooldown(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob", minutes_ago=10)

    before = check_matchmaking_cooldown_service(as_user(db, "alice"), db)
    terminate(admin_client, conversation_id)
    after = check_matchmaking_cooldown_service(as_user(db, "alice"), db)

    assert before["status"] == "cooldown"
    assert after["status"] == "cooldown"


def test_the_cooldown_wait_is_still_the_real_one_after_a_termination(db, admin_client):
    # Not merely "still on cooldown": the remaining time has to be the one the server would
    # quote for a real attempt, because that is the number the user is shown. Asserted as a
    # window rather than to the minute, since the wait is quoted as of now and a moment has
    # already passed since the attempt was written.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob", minutes_ago=30)

    terminate(admin_client, conversation_id)
    answer = check_matchmaking_cooldown_service(as_user(db, "alice"), db)

    remaining = answer["remaining_hours"] * 3600 + answer["remaining_minutes"] * 60
    assert 3 * 3600 + 29 * 60 <= remaining <= 3 * 3600 + 30 * 60


def test_a_draw_is_still_refused_after_a_termination(db, admin_client):
    # The whole point: the thread is closed but the attempt is spent, so the draw the client
    # used to be invited to make is still the one the server refuses.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    with pytest.raises(HTTPException) as excinfo:
        find_match_service(as_user(db, "alice"), db)

    assert excinfo.value.status_code == 429


def test_a_partner_who_drew_nothing_is_untouched(db, admin_client):
    # The receiver spent no attempt of their own, so termination must not appear in their
    # eligibility either.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    assert check_matchmaking_cooldown_service(as_user(db, "bob"), db)["status"] == "eligible"


def test_a_terminated_match_is_still_reported_as_live_by_the_conversation_list(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    with db.sessions() as session:
        conversation = session.get(ConversationRecord, conversation_id)
        assert conversation.status == "terminated"
        assert conversation.terminated_at is not None
        # "Is this conversation still open" is read off the expiry, so a moderator-closed
        # thread has to keep saying yes.
        assert session.get(MatchRecord, match_id).expires_at > datetime.now(timezone.utc)


# ------------------------------------------------- both participants keep the conversation


def test_the_initiator_still_sees_the_conversation(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)
    response = get_current_conversation_service(as_user(db, "alice"), db)

    assert response["status"] == "success"
    assert response["conversation"]["id"] == conversation_id
    assert response["conversation"]["status"] == "terminated"


def test_the_receiver_still_sees_the_conversation(db, admin_client):
    # The reported bug: this list selects on expires_at, so rewriting it dropped the thread
    # out of the receiver's message box.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)
    response = get_received_conversations_service(as_user(db, "bob"), db)

    assert response["status"] == "success"
    assert [c["conversation"]["id"] for c in response["conversations"]] == [conversation_id]
    assert response["conversations"][0]["conversation"]["status"] == "terminated"


def test_both_participants_are_told_the_conversation_was_terminated(db, admin_client):
    # The second half of the reported bug: nothing was written to the notifications table, so
    # both sides simply stopped hearing about the conversation.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    for regno in ("alice", "bob"):
        assert TERMINATION_HEADING in [row.heading for row in notifications_for(db, regno)]


def test_a_termination_notification_carries_no_wall_clock_time(db, admin_client):
    # The row's own created_at renders in the reader's local time; a formatted clock string in
    # the body reads wrong for everyone outside the server's zone.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    row = next(r for r in notifications_for(db, "alice") if r.heading == TERMINATION_HEADING)
    assert not any(character.isdigit() for character in row.body)


# ------------------------------------------------------------------ finished, not unreadable


def test_the_transcript_of_a_terminated_conversation_is_still_readable(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "before it was shut")

    terminate(admin_client, conversation_id)

    messages = message_service.get_messages(db, conversation_id, as_user(db, "alice"))
    assert [m["body"] for m in messages] == ["before it was shut"]


def test_both_sides_keep_reading_a_terminated_conversation(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "one")
    message_service.send_message(db, conversation_id, as_user(db, "bob"), "two")

    terminate(admin_client, conversation_id)

    for regno in ("alice", "bob"):
        bodies = [
            m["body"]
            for m in message_service.get_messages(db, conversation_id, as_user(db, regno))
        ]
        assert bodies == ["one", "two"]


def test_a_message_to_a_terminated_conversation_is_refused_and_stores_nothing(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    terminate(admin_client, conversation_id)

    with pytest.raises(HTTPException) as excinfo:
        message_service.send_message(db, conversation_id, as_user(db, "alice"), "still here?")

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == "This conversation has been terminated"
    assert stored_message_count(db) == 0


def test_a_participant_can_still_report_a_message_from_a_terminated_conversation(db, admin_client):
    # The report menu is offered on whatever the reader can open, so the server has to agree
    # for the messages of a closed thread.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")
    sent = message_service.send_message(db, conversation_id, as_user(db, "alice"), "rude")

    terminate(admin_client, conversation_id)

    assert message_service.report_message(db, sent["id"], as_user(db, "bob"), "abuse")["id"]


# ---------------------------------------------- conversations terminated by the previous build


def make_expired_thread(db, status, *, hours_ago=5, terminated_hours_ago=None):
    """A thread whose match has already lapsed, in one of the two states that can coexist.

    ``terminated_hours_ago`` set is how the previous build wrote a termination: the match was
    stamped as expiring at the instant of the moderator's decision.
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


def test_a_thread_terminated_by_the_old_build_is_still_listed(db):
    # Those rows carry expires_at == terminated_at, so every clock-based query reads them as
    # lapsed. They are already in the database, so the fix has to reach them rather than only
    # preventing new ones.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_expired_thread(db, "terminated", hours_ago=1, terminated_hours_ago=1)

    response = get_received_conversations_service(as_user(db, "bob"), db)

    assert [c["conversation"]["status"] for c in response["conversations"]] == ["terminated"]


def test_the_initiator_sees_an_old_terminated_thread_too(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_expired_thread(db, "terminated", hours_ago=1, terminated_hours_ago=1)

    response = get_current_conversation_service(as_user(db, "alice"), db)

    assert response["status"] == "success"
    assert response["conversation"]["status"] == "terminated"


def test_a_lapsed_thread_is_not_listed_merely_because_it_is_old(db):
    # The counterpart: the same query must not turn every expired conversation back into an
    # open one, or the message box would fill with dead threads.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_expired_thread(db, "accepted", hours_ago=6)

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


def test_the_terminal_state_is_recorded_with_the_time_it_happened(db, admin_client):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_accepted_conversation(db, "alice", "bob")

    result = terminate(admin_client, conversation_id)

    with db.sessions() as session:
        terminated_at = session.get(ConversationRecord, conversation_id).terminated_at
    assert result["status"] == "terminated"
    assert terminated_at is not None
