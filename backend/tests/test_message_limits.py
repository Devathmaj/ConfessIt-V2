"""How much of a conversation two people may fill, and what happens when they try.

Two caps, and the interesting part is that they are not the same number: a conversation holds
``CONVERSATION_MESSAGE_MAX`` messages and one participant may send
``CONVERSATION_PARTICIPANT_MESSAGE_MAX`` of them. The first is a ceiling on a transcript; the
second is what keeps that transcript an exchange rather than a monologue. Twice the second is
the first, which is why the per-person cap is the one that binds in ordinary use and the
conversation cap is a backstop -- it exists for conversations whose rows were written without
the per-person rule in force, not for the path a client can walk.

These tests pin the parts that fail quietly. A cap enforced without a lock lets two sends
racing past the same count both insert, and that only shows up under concurrency, so there is
a test for it here rather than a comment about it. A cap that refunded a removed message would
make the ceiling depend on how many reports succeeded, and there is a test for that too. And
the wording of a refusal is part of the behaviour: a participant who has spent their own
allowance and a conversation that has run out of room are different situations, and telling
someone the wrong one implicates the other person's conduct.

The service is exercised directly rather than through the router. The cap is a statement about
rows in a table, so the tests that matter are about what is and is not stored; the router's
shape is covered once, at the end, where the response contract is asserted.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    UserRecord,
)
from app.limits import CONVERSATION_MESSAGE_MAX, CONVERSATION_PARTICIPANT_MESSAGE_MAX
from app.main import app
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services.auth_service import get_current_user
from app.services.message_service import (
    CONVERSATION_LIMIT_REACHED,
    MESSAGE_LIMIT_CONVERSATION_DETAIL,
    MESSAGE_LIMIT_PARTICIPANT_DETAIL,
    PARTICIPANT_LIMIT_REACHED,
    message_service,
)


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
    """A matched, accepted conversation inside its own four-hour window.

    Well inside the window so that a refusal can only be about the message allowance.
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
        return str(conversation.id)


def fill(db, conversation_id, regno, count, *, other="bob", start=0):
    """Write ``count`` messages straight into the table, bypassing the service.

    The only way to reach a conversation whose rows do not match what the per-person rule
    would have produced: an existing database, or a build that enforced only the conversation
    ceiling. Rows inserted this way are indistinguishable afterwards, which is the point.
    """
    with db.sessions.begin() as session:
        for index in range(start, start + count):
            session.add(
                MessageRecord(
                    conversation_id=conversation_id, sender_regno=regno,
                    receiver_regno=other, body=f"seeded {index}",
                    created_at=datetime.now(timezone.utc) + timedelta(seconds=index),
                    is_read=True,
                )
            )


def stored_message_count(db) -> int:
    with db.sessions() as session:
        return len(list(session.scalars(select(MessageRecord)).all()))


def send(db, conversation_id, regno, body="hello"):
    return message_service.send_message(db, conversation_id, as_user(db, regno), body)


def refusal_for(db, conversation_id, regno, body="one too many"):
    """The refusal a send earns, asserted to be a refusal rather than a success."""
    with pytest.raises(HTTPException) as excinfo:
        send(db, conversation_id, regno, body)
    return excinfo.value


# --------------------------------------------------------------------- one participant share


def test_a_participant_may_send_their_full_share(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")

    for index in range(CONVERSATION_PARTICIPANT_MESSAGE_MAX):
        send(db, conversation_id, "alice", f"message {index}")

    assert stored_message_count(db) == CONVERSATION_PARTICIPANT_MESSAGE_MAX


def test_the_last_message_of_a_share_still_goes_out(db):
    # The cap is inclusive of the hundredth. An off-by-one here would silently cost a
    # participant one turn of a conversation that is supposed to be an exchange.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX - 1)

    sent = send(db, conversation_id, "alice", "the hundredth")

    assert sent["body"] == "the hundredth"
    assert sent["quota"]["participant_count"] == CONVERSATION_PARTICIPANT_MESSAGE_MAX
    assert sent["quota"]["can_send"] is False
    assert sent["quota"]["limit_reached"] == PARTICIPANT_LIMIT_REACHED


def test_the_message_past_a_participants_share_is_refused_and_stores_nothing(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)

    refused = refusal_for(db, conversation_id, "alice")

    assert refused.status_code == 403
    assert refused.detail == MESSAGE_LIMIT_PARTICIPANT_DETAIL
    assert stored_message_count(db) == CONVERSATION_PARTICIPANT_MESSAGE_MAX


def test_a_partner_may_still_speak_after_the_other_has_run_out(db):
    # The whole reason for a per-person cap rather than only a per-conversation one: the
    # thread has room in it, and the person in it who has not spent their share gets to use
    # it. A conversation that filled up on one side's messages is not finished.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)

    sent = send(db, conversation_id, "bob", "my turn")

    assert sent["body"] == "my turn"
    assert sent["quota"]["participant_count"] == 1
    assert sent["quota"]["conversation_remaining"] == (
        CONVERSATION_MESSAGE_MAX - CONVERSATION_PARTICIPANT_MESSAGE_MAX - 1
    )


# ------------------------------------------------------------------- the conversation ceiling


def test_a_conversation_holding_its_ceiling_refuses_a_partner_with_room(db):
    # The backstop. A conversation can only reach the ceiling while both participants still
    # have room if its rows were written without the per-person rule in force, which is
    # exactly when the conversation cap is the only thing left saying no.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_MESSAGE_MAX)

    refused = refusal_for(db, conversation_id, "bob")

    assert refused.status_code == 403
    assert refused.detail == MESSAGE_LIMIT_CONVERSATION_DETAIL
    assert stored_message_count(db) == CONVERSATION_MESSAGE_MAX


def test_the_message_that_fills_a_conversation_still_goes_out(db):
    # The ceiling is inclusive of the two hundredth, for the same reason the hundredth is:
    # a conversation that stops one message early is a message two people could have had.
    #
    # With two participants the two caps are reached on the same send -- 100 each -- so the
    # conversation filling is reported as what it is here: the caller's own share running out
    # at the same moment, which is the wording a user is told, not a statement about the
    # partner's half.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    half = CONVERSATION_PARTICIPANT_MESSAGE_MAX - 1
    fill(db, conversation_id, "alice", half)
    fill(db, conversation_id, "bob", half, other="alice", start=half)
    fill(db, conversation_id, "alice", 1, start=half)

    sent = send(db, conversation_id, "bob", "the last one")

    assert sent["quota"]["conversation_count"] == CONVERSATION_MESSAGE_MAX
    assert sent["quota"]["conversation_remaining"] == 0
    assert sent["quota"]["participant_count"] == CONVERSATION_PARTICIPANT_MESSAGE_MAX
    assert sent["quota"]["limit_reached"] == PARTICIPANT_LIMIT_REACHED
    assert stored_message_count(db) == CONVERSATION_MESSAGE_MAX

    assert refusal_for(db, conversation_id, "alice").detail == MESSAGE_LIMIT_PARTICIPANT_DETAIL


def test_a_participants_own_share_is_the_one_reported_when_both_are_spent(db):
    # Both numbers reach their maximum together in normal use, so which limit is named is a
    # decision rather than an accident. The caller's own share wins: it is the one they
    # exhausted, and the conversation reaching 200 is a consequence of it rather than an
    # accusation about what the other person did.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)
    fill(
        db, conversation_id, "bob", CONVERSATION_PARTICIPANT_MESSAGE_MAX,
        other="alice", start=CONVERSATION_PARTICIPANT_MESSAGE_MAX,
    )

    refused = refusal_for(db, conversation_id, "alice")

    assert refused.detail == MESSAGE_LIMIT_PARTICIPANT_DETAIL
    assert message_service.conversation_quota(db, conversation_id, "alice")["limit_reached"] == (
        PARTICIPANT_LIMIT_REACHED
    )


# -------------------------------------------------------------------- what counts towards it


def test_a_removed_message_still_counts_towards_the_share(db):
    # A message a moderator took down is still a message that was written: it is still in the
    # archive, and the other party still sees the hole where it was. Refunding the allowance
    # would let the ceiling depend on how many reports succeeded -- a user who could get a
    # message removed would have bought back the right to write another.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)

    removed_id = None
    with db.sessions.begin() as session:
        row = session.scalar(
            select(MessageRecord).where(MessageRecord.conversation_id == conversation_id)
        )
        row.removed_at = datetime.now(timezone.utc)
        removed_id = str(row.id)

    # The transcript no longer shows it...
    assert all(
        m["id"] != removed_id for m in message_service.get_messages(
            db, conversation_id, as_user(db, "alice")
        )
    )
    # ...but the allowance it spent is not given back.
    assert message_service.conversation_quota(db, conversation_id, "alice")[
        "participant_count"
    ] == CONVERSATION_PARTICIPANT_MESSAGE_MAX
    assert refusal_for(db, conversation_id, "alice").detail == MESSAGE_LIMIT_PARTICIPANT_DETAIL


def test_the_share_is_counted_per_participant_and_not_per_conversation(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", 60)
    fill(db, conversation_id, "bob", 60, other="alice", start=60)

    alice = message_service.conversation_quota(db, conversation_id, "alice")
    bob = message_service.conversation_quota(db, conversation_id, "bob")

    assert (alice["conversation_count"], alice["participant_count"]) == (120, 60)
    assert (bob["conversation_count"], bob["participant_count"]) == (120, 60)
    assert alice["can_send"] and bob["can_send"]


# ------------------------------------------------------------------------------ the readback


def test_the_allowance_is_reported_against_the_caller(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", 12)
    fill(db, conversation_id, "bob", 3, other="alice", start=12)

    alice = message_service.conversation_quota(db, conversation_id, "alice")

    assert alice["conversation_limit"] == CONVERSATION_MESSAGE_MAX
    assert alice["participant_limit"] == CONVERSATION_PARTICIPANT_MESSAGE_MAX
    assert alice["conversation_count"] == 15
    assert alice["participant_count"] == 12
    assert alice["conversation_remaining"] == CONVERSATION_MESSAGE_MAX - 15
    assert alice["participant_remaining"] == CONVERSATION_PARTICIPANT_MESSAGE_MAX - 12
    assert alice["limit_reached"] is None


def test_the_allowance_is_reported_for_a_thread_that_can_no_longer_be_written(db):
    # The chat dialog opens on the read, including for a thread that is closed or past its
    # window, and it is the read that has to say why the composer is gone. A report that
    # refused to count anything for a terminated conversation would leave the dialog with
    # nothing to explain the refusal it already knows is coming.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)

    with db.sessions.begin() as session:
        session.get(ConversationRecord, conversation_id).status = "terminated"

    quota = message_service.conversation_quota(db, conversation_id, "alice")

    assert quota["can_send"] is False
    assert quota["limit_reached"] == PARTICIPANT_LIMIT_REACHED


def test_a_send_reports_the_allowance_it_left_behind(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")

    sent = send(db, conversation_id, "alice")

    assert sent["quota"] == {
        "conversation_limit": CONVERSATION_MESSAGE_MAX,
        "participant_limit": CONVERSATION_PARTICIPANT_MESSAGE_MAX,
        "conversation_count": 1,
        "participant_count": 1,
        "conversation_remaining": CONVERSATION_MESSAGE_MAX - 1,
        "participant_remaining": CONVERSATION_PARTICIPANT_MESSAGE_MAX - 1,
        "can_send": True,
        "limit_reached": None,
    }


# ------------------------------------------------------------------- the whole transcript fits


def test_every_message_of_a_full_conversation_is_returned_in_one_read(db):
    # The ceiling is the read window. A conversation at its maximum must return all of it in
    # one response, because a thread that hides its own newest messages from the two people in
    # it is broken in a way a longer transcript would have been broken in before the cap.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    half = CONVERSATION_MESSAGE_MAX // 2
    fill(db, conversation_id, "alice", half)
    fill(db, conversation_id, "bob", half, other="alice", start=half)

    assert len(message_service.get_messages(db, conversation_id, as_user(db, "alice"))) == (
        CONVERSATION_MESSAGE_MAX
    )


# --------------------------------------------------------------------------- the race itself


def test_two_sends_racing_past_the_same_count_cannot_both_pass(db):
    # The cap is enforced by counting rows, so a check that is not serialised with the insert
    # lets both racing sends read 99 and both write. This is what the conversation row lock
    # in ``send_message`` is for, and it is the one failure here that no sequential test can
    # see: the assertions are about the total, not about which message was refused.
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX - 1)

    alice = as_user(db, "alice")

    def attempt(body):
        try:
            message_service.send_message(db, conversation_id, alice, body)
            return "sent"
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["first", "second"]))

    assert sorted(outcomes, key=str) == [403, "sent"]
    assert stored_message_count(db) == CONVERSATION_PARTICIPANT_MESSAGE_MAX


# ------------------------------------------------------------------------- the router shape


@pytest.fixture
def alice_client(db):
    """A client signed in as alice, a real ``users`` row."""
    make_user(db, "alice", "female")

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: as_user(db, "alice")
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = previous


def test_reading_a_conversation_returns_its_allowance_alongside_the_transcript(
    db, alice_client
):
    # The dialog has to learn the allowance from the read it already makes; a client that had
    # to ask again would have a window in which it was showing a composer it was about to take
    # away.
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    send(db, conversation_id, "bob", "from bob")

    response = alice_client.get(f"/messages/{conversation_id}")

    assert response.status_code == 200
    body = response.json()
    assert [m["body"] for m in body["messages"]] == ["from bob"]
    assert body["count"] == 1
    assert body["quota"]["participant_count"] == 0
    assert body["quota"]["conversation_count"] == 1
    assert body["quota"]["participant_remaining"] == CONVERSATION_PARTICIPANT_MESSAGE_MAX
    assert body["quota"]["can_send"] is True


def test_sending_over_the_router_refuses_with_the_allowances_own_wording(db, alice_client):
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", CONVERSATION_PARTICIPANT_MESSAGE_MAX)

    response = alice_client.post(
        "/messages/send", json={"conversation_id": conversation_id, "body": "one too many"}
    )

    assert response.status_code == 403
    assert response.json()["detail"] == MESSAGE_LIMIT_PARTICIPANT_DETAIL


def test_a_read_cannot_ask_for_more_than_a_conversation_can_hold(db, alice_client):
    # The window is bounded by the ceiling, so a larger limit could only ever return the same
    # reply. Refusing it says the request was impossible; clamping it would leave the caller
    # believing it had asked for a window it cannot have.
    make_user(db, "bob", "male")
    conversation_id = make_accepted_conversation(db, "alice", "bob")

    assert alice_client.get(f"/messages/{conversation_id}?limit={CONVERSATION_MESSAGE_MAX}").status_code == 200
    assert alice_client.get(
        f"/messages/{conversation_id}?limit={CONVERSATION_MESSAGE_MAX + 1}"
    ).status_code == 422