"""Paging a conversation thread from the newest window upwards.

``GET /messages/{conversation_id}`` used to answer the whole transcript in one
response, oldest first, capped only by the conversation ceiling. A thread approaching
that ceiling makes every open of the dialog load it all. These tests pin the
replacement contract:

* every read answers the newest window by default -- latest message last -- with the
  total beside it, and never silently more than asked for;
* ``before`` names the oldest message on screen and answers the next older page, so
  the dialog grows upwards without shifting under concurrent sends: an offset would
  move every row down when a new message lands between two reads, while a row id
  cannot move;
* pages never repeat or skip a row, however the ties fall;
* a missing anchor, or one from another conversation, is a 404 rather than a best
  guess;
* reading an older page marks read only what it returned -- the unread badge answers
  what is unread, and a page that cleared it for rows nobody opened would be lying
  about that.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import ConversationRecord, MatchRecord, MessageRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services import message_service
from app.services.auth_service import get_current_user

WINDOW = timedelta(hours=4)


def make_user(db, regno):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender="other",
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


def make_accepted_conversation(db, initiator, receiver):
    created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
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


def fill(db, conversation_id, sender, receiver, count, *, start=0):
    """Straight rows with staggered stamps, bypassing the send path's allowances."""
    base = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        for index in range(start, start + count):
            session.add(
                MessageRecord(
                    conversation_id=conversation_id, sender_regno=sender,
                    receiver_regno=receiver, body=f"seeded {index}",
                    created_at=base + timedelta(seconds=index),
                    is_read=True,
                )
            )


@pytest.fixture
def alice_client(db):
    """A client signed in as alice, a real ``users`` row."""
    make_user(db, "alice")

    previous = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = lambda: as_user(db, "alice")
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
        else:
            app.dependency_overrides.pop(get_current_user, None)


def bodies(page):
    return [message["body"] for message in page["messages"]]


def test_a_thread_opens_on_the_newest_window_with_the_total(db, alice_client):
    make_user(db, "bob")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", "bob", 5)

    page = alice_client.get(f"/messages/{conversation_id}", params={"limit": 3}).json()

    assert set(page) >= {"messages", "count", "total", "conversation_id", "quota"}
    assert page["total"] == 5
    assert page["count"] == 3
    # Latest last: the opening window is the newest end of the thread.
    assert bodies(page) == ["seeded 2", "seeded 3", "seeded 4"]


def test_before_walks_older_without_repeating_or_skipping(db, alice_client):
    make_user(db, "bob")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", "bob", 5)

    first = alice_client.get(f"/messages/{conversation_id}", params={"limit": 2}).json()
    assert bodies(first) == ["seeded 3", "seeded 4"]
    anchor = first["messages"][0]["id"]

    second = alice_client.get(
        f"/messages/{conversation_id}", params={"before": anchor, "limit": 2}).json()
    assert bodies(second) == ["seeded 1", "seeded 2"]
    assert second["total"] == 5

    third = alice_client.get(
        f"/messages/{conversation_id}", params={"before": second["messages"][0]["id"], "limit": 2}
    ).json()
    assert bodies(third) == ["seeded 0"]
    seen = [message["id"] for page in (first, second, third) for message in page["messages"]]
    assert len(set(seen)) == 5


def test_a_missing_or_foreign_anchor_is_refused(db, alice_client):
    make_user(db, "bob")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    fill(db, conversation_id, "alice", "bob", 1)
    other_conversation = make_accepted_conversation(db, "alice", "bob")
    fill(db, other_conversation, "alice", "bob", 1)
    foreign_id = alice_client.get(f"/messages/{other_conversation}").json()["messages"][0]["id"]

    assert alice_client.get(
        f"/messages/{conversation_id}",
        params={"before": "11111111-1111-1111-1111-111111111111"},
    ).status_code == 404
    assert alice_client.get(
        f"/messages/{conversation_id}", params={"before": foreign_id}).status_code == 404


def test_reading_an_older_page_marks_read_only_what_it_returned(db, alice_client):
    make_user(db, "bob")
    conversation_id = make_accepted_conversation(db, "alice", "bob")
    # Addressed to alice, unread: what the badge counts.
    with db.sessions.begin() as session:
        base = datetime.now(timezone.utc)
        for index in range(3):
            session.add(
                MessageRecord(
                    conversation_id=conversation_id, sender_regno="bob",
                    receiver_regno="alice", body=f"for alice {index}",
                    created_at=base + timedelta(seconds=index),
                    is_read=False,
                )
            )

    unread = lambda: alice_client.get(
        f"/messages/{conversation_id}", params={"limit": 1}).json()["messages"][0]
    assert unread()["is_read"] is False

    # Newest one marks exactly itself read; the two older rows stay unread behind it.
    newest = alice_client.get(f"/messages/{conversation_id}", params={"limit": 1}).json()
    assert bodies(newest) == ["for alice 2"]
    with db.sessions() as session:
        states = {
            row.body: row.is_read for row in session.scalars(
                select(MessageRecord).where(MessageRecord.conversation_id == conversation_id)
            ).all()
        }
    assert states == {"for alice 0": False, "for alice 1": False, "for alice 2": True}
