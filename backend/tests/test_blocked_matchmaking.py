"""A blocked account must not surface anywhere in matchmaking or chat.

The candidate query behind ``find_match`` already filters ``is_blocked``, but a match
drawn before the block is still a live row: it keeps showing up as the current match, as
an inbox request, and as the conversation behind the chat dialog. These tests pin the
re-check at each of those surfaces.

Love notes are the deliberate exception. A blocked profile stays in the recipient list
and still accepts a note; the sender is warned first, so all that is asserted here is that
the list reports the flag the warning depends on.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts import AccountRef, AccountType
from app.db_models import (
    ACCOUNT_MODELS,
    ConversationRecord,
    MatchRecord,
    MessageRecord,
    UserRecord,
)
from app.models import AccountDetails, UserDetails
from app.postgres import get_postgres_client
from app.services import conversation_service, love_note_service, matchmaking_service
from app.services.message_service import message_service


def make_user(db, regno, gender, *, is_blocked=False, love_notes_receive_enabled=True):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender=gender,
            is_blocked=is_blocked,
            love_notes_receive_enabled=love_notes_receive_enabled,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_staff(db, regno, account_type):
    """Write a staff account into the table that grants ``account_type``.

    Staff have no ``users`` row, so tests that need a moderator or admin acting on a
    conversation insert here and resolve through ``as_staff``. No ``gender`` argument: the
    staff tables have no such column, and passing one would be a ``TypeError`` rather than
    a value nobody reads.
    """
    with db.sessions.begin() as session:
        row = ACCOUNT_MODELS[account_type](
            regno=regno, name=f"Staff {regno}", email=f"{regno}@test.com"
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
        account_type=AccountType.USERS.value,
    )


def as_staff(db, regno, account_type) -> AccountDetails:
    with db.sessions() as session:
        row = session.scalar(
            select(ACCOUNT_MODELS[account_type]).where(ACCOUNT_MODELS[account_type].regno == regno)
        )
    return AccountDetails(
        id=str(row.id), regno=row.regno, name=row.name, email=f"{row.regno}@test.com",
        notifications_enabled=row.notifications_enabled, is_blocked=row.is_blocked,
        account_type=account_type,
    )


def set_blocked(db, regno, blocked: bool) -> None:
    with db.sessions.begin() as session:
        session.scalar(select(UserRecord).where(UserRecord.regno == regno)).is_blocked = blocked


def make_match_and_conversation(db, initiator, receiver, *, status="pending"):
    """A live match with its conversation, as matchmaking would have written it."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match_row = MatchRecord(
            user_1_regno=initiator, user_2_regno=receiver,
            created_at=now, expires_at=now + timedelta(hours=4),
        )
        session.add(match_row)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match_row.id), initiator_regno=initiator,
            receiver_regno=receiver, status=status, created_at=now,
        )
        session.add(conversation)
        session.flush()
        return str(match_row.id), str(conversation.id)


def conversation_status(db, conversation_id) -> str:
    with db.sessions() as session:
        return session.get(ConversationRecord, conversation_id).status


@pytest.fixture
def db():
    return get_postgres_client()


@pytest.fixture(autouse=True)
def no_rare_same_gender_roll(monkeypatch):
    """The 0.1% same-gender roll would leave a test with no candidate half the time."""
    monkeypatch.setattr(matchmaking_service, "SAME_GENDER_MATCH_CHANCE", 0)


# ------------------------------------------------------------------ the draw


def test_a_blocked_account_is_never_drawn_as_a_match(db):
    make_user(db, "alice", "female")
    make_user(db, "blocked", "male", is_blocked=True)
    make_user(db, "available", "male")

    result = matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert result["matched_with"]["regno"] == "available"


def test_a_blocked_account_is_drawn_no_longer_even_once_it_was_matched(db):
    """The pool is re-read on every draw, so a block takes effect without a re-match."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert excinfo.value.status_code == 404


# ------------------------------------------------------------------ existing matches


def test_the_current_match_is_reported_as_absent_when_the_partner_is_blocked(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_match_and_conversation(db, "alice", "bob")
    set_blocked(db, "bob", True)

    result = conversation_service.get_current_conversation_service(as_user(db, "alice"), db)

    assert result["status"] == "no_active_match"
    assert "other_user" not in result


def test_the_current_match_is_still_reported_while_the_partner_is_available(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_match_and_conversation(db, "alice", "bob")

    result = conversation_service.get_current_conversation_service(as_user(db, "alice"), db)

    assert result["status"] == "success"
    assert result["other_user"]["regno"] == "bob"


def test_a_request_from_a_blocked_initiator_is_not_offered(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_match_and_conversation(db, "alice", "bob", status="requested")
    set_blocked(db, "alice", True)

    result = conversation_service.get_received_conversations_service(as_user(db, "bob"), db)

    assert result["conversations"] == []


def test_a_request_from_an_available_initiator_is_still_offered(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_match_and_conversation(db, "alice", "bob", status="requested")

    result = conversation_service.get_received_conversations_service(as_user(db, "bob"), db)

    assert [c["other_user"]["regno"] for c in result["conversations"]] == ["alice"]


# ------------------------------------------------------------------ the request lifecycle


def test_a_message_request_to_a_blocked_partner_is_refused_and_changes_nothing(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, conversation_id = make_match_and_conversation(db, "alice", "bob")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)

    assert excinfo.value.status_code == 403
    assert conversation_status(db, conversation_id) == "pending"


def test_a_blocked_initiators_request_cannot_be_accepted(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, conversation_id = make_match_and_conversation(db, "alice", "bob", status="requested")
    set_blocked(db, "alice", True)

    with pytest.raises(HTTPException) as excinfo:
        conversation_service.accept_conversation_service(as_user(db, "bob"), match_id, db)

    assert excinfo.value.status_code == 403
    assert conversation_status(db, conversation_id) == "requested"


def test_an_available_participant_can_still_send_and_accept(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, conversation_id = make_match_and_conversation(db, "alice", "bob")

    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    assert conversation_status(db, conversation_id) == "requested"

    conversation_service.accept_conversation_service(as_user(db, "bob"), match_id, db)
    assert conversation_status(db, conversation_id) == "accepted"


def test_the_conversation_lookup_refuses_a_blocked_partner(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    match_id, _ = make_match_and_conversation(db, "alice", "bob", status="requested")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        conversation_service.get_conversation_by_match_service(as_user(db, "alice"), match_id, db)

    assert excinfo.value.status_code == 403


# ------------------------------------------------------------------ chat delivery


def test_a_message_to_a_blocked_partner_is_refused(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_match_and_conversation(db, "alice", "bob", status="accepted")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")

    assert excinfo.value.status_code == 403
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MessageRecord)) == 0


def test_a_message_to_an_available_partner_is_stored(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_match_and_conversation(db, "alice", "bob", status="accepted")

    result = message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")

    assert result["receiver_regno"] == "bob"


def test_the_history_of_a_blocked_partner_is_not_readable(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    _, conversation_id = make_match_and_conversation(db, "alice", "bob", status="accepted")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        message_service.get_messages(db, conversation_id, as_user(db, "alice"))

    assert excinfo.value.status_code == 403


@pytest.mark.parametrize("staff_type", (AccountType.ADMIN.value, AccountType.MODERATOR.value))
def test_staff_cannot_read_a_blocked_conversation_either(db, staff_type):
    """A block is not something a staff account can read past.

    This used to be a deliberate exception -- "moderation is the one job that has to keep
    working" -- keyed on the capability that guarded the admin transcript route. With that
    route gone the capability is gone, and so is the exception. A blocked thread is reached
    through the report queue, on the strength of a report, rather than by anyone with a
    staff account deciding to open it.
    """
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")
    make_staff(db, "mod", staff_type)
    _, conversation_id = make_match_and_conversation(db, "alice", "bob", status="accepted")
    message_service.send_message(db, conversation_id, as_user(db, "alice"), "hello")
    set_blocked(db, "bob", True)

    with pytest.raises(HTTPException) as excinfo:
        message_service.get_messages(
            db, conversation_id, as_staff(db, "mod", staff_type)
        )

    assert excinfo.value.status_code == 403


# ------------------------------------------------------------------ love notes stay open


@pytest.mark.asyncio
async def test_a_blocked_profile_is_still_offered_as_a_love_note_recipient(db):
    """The one channel that stays open, and it says so: the client warns before sending."""
    sender_id = make_user(db, "sender", "female")
    make_user(db, "blocked", "male", is_blocked=True)

    recipients = await love_note_service.get_all_users_service(
        db, AccountRef(regno="sender", account_id=sender_id, account_type="users")
    )

    by_regno_name = {r["name"]: r for r in recipients}
    assert by_regno_name["User blocked"]["is_blocked"] is True


@pytest.mark.asyncio
async def test_a_blocked_profile_that_opted_out_is_still_not_offered(db):
    """is_blocked must not become a back door around the opt-out filter."""
    sender_id = make_user(db, "sender", "female")
    make_user(db, "optedout", "male", is_blocked=True, love_notes_receive_enabled=False)

    recipients = await love_note_service.get_all_users_service(
        db, AccountRef(regno="sender", account_id=sender_id, account_type="users")
    )

    assert [r["name"] for r in recipients] == []
