"""Two clicks on "report" is one report, not a server fault.

The report path checked for a duplicate and then inserted, and the insert is protected by
a unique constraint on ``(message_id, reporter_regno)``. The check is a read, so two
clicks arriving together -- a double-click, two tabs, a client retry -- both read "no
report yet" and both inserted. The second tripped the constraint and the request
answered **500**: a server fault for a complaint that was already filed, with the
participant left believing their report had failed when it had not.

The unique constraint is the authority. It is the only thing that makes the rule true
when two requests are in flight at once, because a read cannot settle a question two
readers are asking simultaneously. So the fix is not to make the read better -- it is to
translate the constraint's answer into the same answer the read gives: 409, "you have
already reported this message".

The other integrity failures are left alone. A constraint that is not this one is a real
fault and stays a real fault.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.db_models import (
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    MessageReportRecord,
    UserRecord,
)
from app.accounts import AccountType
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user
from app.services.message_service import message_service

WINDOW = timedelta(hours=4)
REASON = "this is hateful"
RECIPIENT = "receiver"


def make_user(db, regno, gender="other"):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="Test_Class", gender=gender,
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


def make_accepted_conversation(db, sender, receiver):
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


def report_count(db) -> int:
    with db.sessions() as session:
        return session.scalar(
            select(func.count()).select_from(MessageReportRecord)
        ) or 0


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


@pytest.fixture
def sent_message(db, client, login_as):
    """One message in an accepted conversation, sent by ``sender`` to ``receiver``."""
    make_user(db, "sender")
    make_user(db, RECIPIENT)
    conversation_id = make_accepted_conversation(db, "sender", RECIPIENT)

    with login_as("sender"):
        sent = client.post(
            "/messages/send", json={"conversation_id": conversation_id, "body": "a message"}
        )
    assert sent.status_code == 201, sent.text
    return {"message_id": sent.json()["id"], "conversation_id": conversation_id}


# ------------------------------------------------------------------ the duplicate


def test_a_second_report_of_the_same_message_is_refused_with_409(db, client, login_as, sent_message):
    """The ordinary case: the second click is told the report is already filed."""
    with login_as(RECIPIENT):
        first = client.post(
            "/messages/report",
            json={"message_id": sent_message["message_id"], "reason": REASON},
        )
        second = client.post(
            "/messages/report",
            json={"message_id": sent_message["message_id"], "reason": REASON},
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 409, second.text
    assert report_count(db) == 1


def test_two_concurrent_reports_of_one_message_produce_one_report(db, client, login_as, sent_message):
    """The core regression.

    Both reporters read "no report yet" and both inserted. The unique constraint is
    what settles it, and the loser of that race has to be told the report is already
    filed -- the same answer the sequential path gives -- rather than answering 500.

    The barrier is what makes the racers overlap. It does not decide the outcome --
    the database does -- but without it the threads would arrive one after another and
    the test would pass for the wrong reason.
    """
    reporter = as_user(db, RECIPIENT)
    workers = 2
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=30)
        try:
            result = message_service.report_message(
                db, sent_message["message_id"], reporter, REASON
            )
            outcome = ("filed", result["id"])
        except HTTPException as exc:
            outcome = ("refused", exc.status_code, exc.detail)
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            outcome = ("error", repr(exc))
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt) for _ in range(workers)]:
            future.result(timeout=60)

    errors = [o for o in outcomes if o[0] == "error"]
    assert not errors, errors
    assert not any(o[0] == "error" for o in outcomes), outcomes

    filed = [o for o in outcomes if o[0] == "filed"]
    refused = [o for o in outcomes if o[0] == "refused"]
    assert len(filed) == 1, outcomes
    assert len(refused) == 1, outcomes
    # A refusal, not a fault: the constraint's answer is translated into the same
    # answer the read gives.
    assert refused[0][1] == 409, outcomes
    assert report_count(db) == 1


def test_a_duplicate_report_never_answers_500(db, client, login_as, sent_message):
    """Whatever the race, the answer for a report that is already filed is 409.

    The two failure modes of the old path were indistinguishable from the outside --
    both were "the report did not work" -- and one of them was a fault in this
    service rather than in the caller's request.
    """
    with login_as(RECIPIENT):
        for _ in range(4):
            response = client.post(
                "/messages/report",
                json={"message_id": sent_message["message_id"], "reason": REASON},
            )
            assert response.status_code in (201, 409), response.text
            assert response.status_code != 500, response.text

    assert report_count(db) == 1


def test_reporting_a_second_message_is_still_accepted(db, client, login_as, sent_message):
    """The refusal is about one message, not about the reporter.

    One participant may report as many messages as they like; only a second report of
    the *same* message is refused.
    """
    with login_as("sender"):
        second_message = client.post(
            "/messages/send",
            json={"conversation_id": sent_message["conversation_id"], "body": "another one"},
        ).json()["id"]

    with login_as(RECIPIENT):
        first = client.post(
            "/messages/report", json={"message_id": sent_message["message_id"], "reason": REASON}
        )
        second = client.post(
            "/messages/report", json={"message_id": second_message, "reason": REASON}
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert report_count(db) == 2


def test_the_message_is_still_reported_once(db, client, login_as, sent_message):
    """A duplicate report does not file a second row, and does not charge the sender twice."""
    with login_as(RECIPIENT):
        client.post(
            "/messages/report", json={"message_id": sent_message["message_id"], "reason": REASON}
        )
        client.post(
            "/messages/report", json={"message_id": sent_message["message_id"], "reason": REASON}
        )

    with db.sessions() as session:
        rows = session.scalars(select(MessageReportRecord)).all()
        assert len(rows) == 1
        assert rows[0].reporter_regno == RECIPIENT
        assert rows[0].reported_user_regno == "sender"

    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MessageRecord)) == 1
