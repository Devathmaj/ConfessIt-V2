"""Two decisions on one conversation must not both be taken.

Accept and reject read the conversation's status, check it, and then write. Two requests
that read before either wrote both pass the check and both write: the second silently
overwrites the first, and both send a notice -- so the initiator can be told the request
was accepted *and* rejected. A double-click, two tabs, or a client retrying a request that
timed out produce exactly that; the same read-then-write is in the request path, so two
clicks there create two requests and two notices.

The transition is now conditional -- ``UPDATE ... WHERE id = :id AND status = :from``.
PostgreSQL re-checks the predicate after it takes the row lock, so of two racers exactly
one moves the row and only that one sends its notice; the other answers 409.

The races below are arranged rather than hoped for: the test holds the conversation's row
lock, starts the calls, and waits until both are parked inside their ``UPDATE``. Both have
therefore already read the row and passed the status check, which is the state this finding
describes. Releasing the lock then lets them through together. Against the unfixed code
both write and both answer success.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text

from app.accounts import AccountType
from app.db_models import ConversationRecord, MatchRecord, NotificationRecord, UserRecord
from app.models import UserDetails
from app.services import conversation_service

ACCEPTED_WORDING = "accepted your message request"
REJECTED_WORDING = "rejected your message request"
REQUESTED_WORDING = "Message request from"


# --- fixtures ---------------------------------------------------------------------


def make_user(db, regno, name=None) -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name or f"User {regno}",
            email=f"{regno.lower()}@test.com",
            year=2,
            department="Test_Class",
            gender="female",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(db, regno) -> UserDetails:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
    return UserDetails(
        id=str(row.id),
        regno=row.regno,
        name=row.name,
        email=row.email,
        year=row.year,
        department=row.department,
        gender=row.gender,
        matchmaking_enabled=row.matchmaking_enabled,
        notifications_enabled=row.notifications_enabled,
        account_type=AccountType.USERS.value,
        last_matchmaking_at=row.last_matchmaking_at,
    )


def add_pending_conversation(db, initiator, receiver) -> str:
    """A live match with a pending conversation on it -- the state a request starts from."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=initiator,
            user_2_regno=receiver,
            created_at=now,
            expires_at=now + timedelta(hours=4),
        )
        session.add(match)
        session.flush()
        match_id = str(match.id)
        session.add(
            ConversationRecord(
                match_id=match_id,
                initiator_regno=initiator,
                receiver_regno=receiver,
                status="pending",
                created_at=now,
            )
        )
    return match_id


def conversation_id_for(db, match_id) -> str:
    with db.sessions() as session:
        return str(
            session.scalar(
                select(ConversationRecord.id).where(
                    ConversationRecord.match_id == match_id
                )
            )
        )


def conversation_status(db, match_id) -> str:
    with db.sessions() as session:
        return session.scalar(
            select(ConversationRecord.status).where(
                ConversationRecord.match_id == match_id
            )
        )


def notices_for(db, regno) -> list:
    with db.sessions() as session:
        return list(
            session.scalars(
                select(NotificationRecord).where(
                    NotificationRecord.recipient_regno == regno
                )
            ).all()
        )


# --- arranging the race -----------------------------------------------------------


def hold_conversation_row(db, conversation_id):
    """Take the row lock the requests will queue behind, on a connection of our own."""
    connection = db.engine.connect()
    transaction = connection.begin()
    connection.execute(
        text("SELECT id FROM conversations WHERE id = :id FOR UPDATE"),
        {"id": conversation_id},
    )
    return connection, transaction


def wait_for_blocked_updates(db, expected=2, timeout=20.0) -> int:
    """Wait until ``expected`` requests are parked inside their ``UPDATE``.

    ``pg_stat_activity`` reports a backend that is waiting for a lock and the statement it
    is waiting in. Both conditions matter: the statement names the step that comes *after*
    the status check, and the wait proves it has not completed. Seeing two of them is the
    barrier -- both requests have read the row, both passed, neither has written.
    """
    deadline = time.monotonic() + timeout
    waiting = 0
    while time.monotonic() < deadline:
        with db.engine.connect() as connection:
            waiting = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND wait_event_type = 'Lock' "
                    "AND query ILIKE '%UPDATE conversations%'"
                )
            )
        if waiting >= expected:
            return int(waiting)
        time.sleep(0.02)
    raise AssertionError(
        f"only {waiting} of {expected} requests reached the row lock within {timeout}s"
    )


def call_in_thread(fn, results, name):
    def run():
        try:
            results.append(("ok", fn()))
        except HTTPException as exc:
            results.append(("refused", exc.status_code))
        except Exception as exc:  # a crash is a failed test, reported not swallowed
            results.append(("error", exc))

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


def run_past_the_row_lock(db, conversation_id, calls):
    """Start ``calls`` together, let them all reach their UPDATE, then release them."""
    connection, transaction = hold_conversation_row(db, conversation_id)
    try:
        results = []
        threads = [
            call_in_thread(call, results, name=f"call-{index}")
            for index, call in enumerate(calls)
        ]
        wait_for_blocked_updates(db, expected=len(calls))
    finally:
        transaction.rollback()
        connection.close()

    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive(), f"{thread.name} never finished"

    assert len(results) == len(calls), results
    return results


def outcomes(results):
    return sorted(status for kind, status in results if kind == "refused")


# --- the races --------------------------------------------------------------------


def test_two_accepts_at_once_produce_one_decision_and_one_notice(db):
    """The finding: a double-clicked accept overwrote itself and notified twice."""
    make_user(db, "alice")
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    bob = as_user(db, "bob")

    results = run_past_the_row_lock(
        db,
        conversation_id_for(db, match_id),
        [
            lambda: conversation_service.accept_conversation_service(bob, match_id, db)
            for _ in range(2)
        ],
    )

    assert [kind for kind, _ in results].count("ok") == 1, results
    assert outcomes(results) == [409], results
    assert conversation_status(db, match_id) == "accepted"
    assert len(notices_for(db, "alice")) == 1


def test_an_accept_and_a_reject_at_once_cannot_both_be_told(db):
    """The contradiction the finding names: accepted *and* rejected in the same inbox."""
    make_user(db, "alice")
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    bob = as_user(db, "bob")

    results = run_past_the_row_lock(
        db,
        conversation_id_for(db, match_id),
        [
            lambda: conversation_service.accept_conversation_service(bob, match_id, db),
            lambda: conversation_service.reject_conversation_service(bob, match_id, db),
        ],
    )

    assert [kind for kind, _ in results].count("ok") == 1, results
    assert outcomes(results) == [409], results

    status = conversation_status(db, match_id)
    notices = notices_for(db, "alice")
    assert len(notices) == 1, [notice.heading for notice in notices]
    assert status in {"accepted", "rejected"}
    if status == "accepted":
        assert ACCEPTED_WORDING in notices[0].heading
    else:
        assert REJECTED_WORDING in notices[0].heading


def test_two_requests_at_once_send_one_request(db):
    """The same read-then-write sits in the request path, one step earlier."""
    make_user(db, "alice")
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    alice = as_user(db, "alice")

    results = run_past_the_row_lock(
        db,
        conversation_id_for(db, match_id),
        [
            lambda: conversation_service.request_conversation_service(
                alice, match_id, db
            )
            for _ in range(2)
        ],
    )

    assert [kind for kind, _ in results].count("ok") == 1, results
    assert outcomes(results) == [409], results
    assert conversation_status(db, match_id) == "requested"

    notices = notices_for(db, "bob")
    assert len(notices) == 1, [notice.heading for notice in notices]
    assert REQUESTED_WORDING in notices[0].heading


# --- the ordinary retry, without a race -------------------------------------------


def test_a_second_accept_after_the_first_finished_is_refused_without_a_notice(db):
    """A retry that arrives after the winner committed takes the 409 pre-check, not a write."""
    make_user(db, "alice")
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    bob = as_user(db, "bob")

    conversation_service.accept_conversation_service(bob, match_id, db)
    with pytest.raises(HTTPException) as refusal:
        conversation_service.accept_conversation_service(bob, match_id, db)

    assert refusal.value.status_code == 409
    assert conversation_status(db, match_id) == "accepted"
    assert len(notices_for(db, "alice")) == 1
