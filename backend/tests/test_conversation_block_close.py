"""Blocking a conversation is closing it.

The mechanism, in one sentence: **the participant who closes a chat is the one who may
reopen it, and until then the chat is shut for both of them.** The thread stays
readable, neither participant can send into it again, and there is one closer at a
time -- a chat is not something two participants each hold shut, so there is no
mutual-blocking state and no list of closers to keep.

The storage used to model it as a list anyway. ``blocked_by`` was appended to on every
block, the payload exposed only ``blocked_by[0]``, and an unblock wiped the whole list.
Four things followed from the list that the mechanism does not want:

* a second participant could close a chat that was already closed (through the API --
  the bundled UI hides the button on a closed chat), and the list then held two
  closers while only the first was ever exposed;
* the second closer was refused the reopen that is theirs ("You can only unblock if
  you were the one who blocked"), because who may reopen was decided by list order
  rather than by who closed the chat;
* the first closer's reopen cleared the whole list, so the chat came back up against
  the other participant's wishes;
* and the second participant could put a lock on the chat the first one closed, since
  the reopen right would then be theirs alone.

So the closer is now one participant. A second close is refused (409) rather than
accepted -- the chat is already shut, so the close has already happened -- and the
participant who closed it keeps the right to reopen it.

These tests pin the mechanism itself, not the shape of the storage: the chat is shut
for both, only the closer may reopen it, and a second close cannot take that right
away.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import ConversationRecord, MatchRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user

WINDOW = timedelta(hours=4)


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


@pytest.fixture
def login_as(db):
    """Run the calls inside the block as a named participant."""

    @contextmanager
    def _login(regno: str):
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: as_user(db, regno)
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


@pytest.fixture
def chat(db, client, login_as):
    """An accepted conversation between two participants, with a message in it."""
    make_user(db, "alice")
    make_user(db, "bob")
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno="alice", user_2_regno="bob",
            created_at=now, expires_at=now + WINDOW,
        )
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id), initiator_regno="alice", receiver_regno="bob",
            status="accepted", created_at=now, accepted_at=now,
        )
        session.add(conversation)
        session.flush()
        match_id = str(match.id)
        conversation_id = str(conversation.id)

    with login_as("alice"):
        sent = client.post(
            "/messages/send", json={"conversation_id": conversation_id, "body": "hello bob"}
        )
    assert sent.status_code == 201, sent.text

    return {"match_id": match_id, "conversation_id": conversation_id}


def closers(db, conversation_id):
    """The participants on the conversation as closers."""
    with db.sessions() as session:
        row = session.get(ConversationRecord, conversation_id)
        return list(row.blocked_by or [])


def is_closed(db, conversation_id) -> bool:
    with db.sessions() as session:
        row = session.get(ConversationRecord, conversation_id)
        return bool(row.blocked_by)


# ------------------------------------------------------------------ the close


def test_closing_a_chat_shuts_it_for_both_participants(db, client, login_as, chat):
    """The close is the point: neither of them can send into the chat again.

    Not a mute of one participant by the other -- the chat is shut, for the sender
    as much as for the receiver.
    """
    with login_as("alice"):
        closed = client.post(f"/conversations/{chat['match_id']}/block")

    assert closed.status_code == 200, closed.text
    assert is_closed(db, chat["conversation_id"]) is True

    for regno in ("alice", "bob"):
        with login_as(regno):
            refused = client.post(
                "/messages/send",
                json={"conversation_id": chat["conversation_id"], "body": "still there?"},
            )
        assert refused.status_code == 403, f"{regno} was allowed to send: {refused.text}"
        assert "blocked" in refused.json()["detail"].lower()


def test_a_closed_chat_stays_readable(db, client, login_as, chat):
    """The thread is still there to read.

    A close is not a deletion: what was said before the chat was shut is still
    what the two of them can read.
    """
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    for regno in ("alice", "bob"):
        with login_as(regno):
            read = client.get(f"/messages/{chat['conversation_id']}")
        assert read.status_code == 200, read.text
        assert "hello bob" in read.text


def test_the_closer_is_the_participant_who_closed_it(db, client, login_as, chat):
    """One closer, named on the conversation."""
    with login_as("alice"):
        closed = client.post(f"/conversations/{chat['match_id']}/block")

    assert closed.status_code == 200, closed.text
    assert closed.json()["blocked_by"] == "alice"
    assert closers(db, chat["conversation_id"]) == ["alice"]


# ------------------------------------------------------------------ the reopen


def test_only_the_closer_may_reopen(db, client, login_as, chat):
    """The other participant cannot reopen a chat they did not close."""
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    with login_as("bob"):
        refused = client.post(f"/conversations/{chat['match_id']}/unblock")

    assert refused.status_code == 403, refused.text
    assert is_closed(db, chat["conversation_id"]) is True

    with login_as("alice"):
        reopened = client.post(f"/conversations/{chat['match_id']}/unblock")

    assert reopened.status_code == 200, reopened.text
    assert is_closed(db, chat["conversation_id"]) is False
    assert closers(db, chat["conversation_id"]) == []


def test_a_reopened_chat_takes_messages_again(db, client, login_as, chat):
    """Reopening is not only a flag: the chat works again."""
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")
        client.post(f"/conversations/{chat['match_id']}/unblock")

    with login_as("bob"):
        sent = client.post(
            "/messages/send",
            json={"conversation_id": chat["conversation_id"], "body": "back again"},
        )

    assert sent.status_code == 201, sent.text


def test_the_participant_who_did_not_close_is_told_why_not(db, client, login_as, chat):
    """The refusal says who may reopen it, so the answer is not a puzzle."""
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    with login_as("bob"):
        refused = client.post(f"/conversations/{chat['match_id']}/unblock")

    assert refused.status_code == 403
    assert "unblock" in refused.json()["detail"].lower()


# ------------------------------------------------------------------ the second close


def test_a_second_close_is_refused(db, client, login_as, chat):
    """Closing a chat that is already closed changes nothing.

    The chat is shut, so the close the caller asked for has already happened. What
    a second one could add is a second closer -- and then who may reopen it stops
    being a fact about the chat.
    """
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    with login_as("bob"):
        second = client.post(f"/conversations/{chat['match_id']}/block")

    assert second.status_code == 409, second.text
    assert is_closed(db, chat["conversation_id"]) is True


def test_a_second_close_cannot_take_the_reopen_right_away(db, client, login_as, chat):
    """The participant who closed the chat keeps the right to reopen it.

    The reopen right belongs to whoever closed the chat, so a second close must not
    move it to the other participant -- a close that another participant could lock
    shut is not a close. (The list this replaced did not move it either: it left the
    second closer with no right at all, which is the other half of the defect.)
    """
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    with login_as("bob"):
        client.post(f"/conversations/{chat['match_id']}/block")

    # Alice closed it, so Alice may reopen it.
    with login_as("alice"):
        reopened = client.post(f"/conversations/{chat['match_id']}/unblock")

    assert reopened.status_code == 200, reopened.text
    assert is_closed(db, chat["conversation_id"]) is False


def test_the_second_closer_never_holds_the_reopen_right(db, client, login_as, chat):
    """A close that was refused leaves no trace to be reopened with."""
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")

    with login_as("bob"):
        client.post(f"/conversations/{chat['match_id']}/block")

    assert closers(db, chat["conversation_id"]) == ["alice"]

    with login_as("bob"):
        refused = client.post(f"/conversations/{chat['match_id']}/unblock")

    assert refused.status_code == 403, refused.text


def test_closing_twice_by_the_same_participant_is_refused(db, client, login_as, chat):
    """Even the closer's own second click is refused.

    The chat is already shut, and the answer says so rather than pretending a
    second close did something.
    """
    with login_as("alice"):
        first = client.post(f"/conversations/{chat['match_id']}/block")
        second = client.post(f"/conversations/{chat['match_id']}/block")

    assert first.status_code == 200, first.text
    assert second.status_code == 409, second.text
    assert closers(db, chat["conversation_id"]) == ["alice"]


def test_a_reopened_chat_can_be_closed_again(db, client, login_as, chat):
    """The closer can close it again afterwards.

    Reopening clears the closer with the chat, so a later close is a fresh one --
    and it is accepted, because the chat is open at that point.
    """
    with login_as("alice"):
        client.post(f"/conversations/{chat['match_id']}/block")
        client.post(f"/conversations/{chat['match_id']}/unblock")
        closed_again = client.post(f"/conversations/{chat['match_id']}/block")

    assert closed_again.status_code == 200, closed_again.text
    assert closers(db, chat["conversation_id"]) == ["alice"]
