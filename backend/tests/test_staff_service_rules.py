"""Staff handling at the service layer, where the router's capability gate is not present.

The admin routes are gated by declared capabilities, but three services apply their own
rules underneath, and those are the places where a staff account could quietly gain or retain
access:

* matchmaking draws its candidates from ``users``, so staff are excluded by the schema rather
  than by a clause that could be forgotten;
* a love-note image is readable by the two people involved, or by a moderator reviewing the
  queue;
* reading a transcript as staff does not mark the thread as read, because a moderator looking
  at a report is not a participant.

These are deliberately unit-level: the account type is the input, and the assertion is about
which row the service can see, so the tests do not depend on the router that happens to call
in.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    AdminRecord,
    ConversationRecord,
    LoveNoteRecord,
    MatchRecord,
    MessageRecord,
    ModeratorRecord,
    UserRecord,
)
from app.models import AccountDetails, UserDetails
from app.services import asset_delivery_service, love_note_service, matchmaking_service
from app.services.message_service import message_service


def make_user(db, regno, gender="other"):
    """Insert a participant and return the row id, which note and match tables key on."""
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            class_name="Test_Class",
            gender=gender,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_staff(db, regno, account_type, gender="other"):
    """Insert a staff account into the table that grants ``account_type``.

    Staff are not participants, so they are written to ``admin`` or ``moderator`` and never
    to ``users``. That absence is what the service rules below are really testing.
    """
    model = AdminRecord if account_type == AccountType.ADMIN.value else ModeratorRecord
    with db.sessions.begin() as session:
        row = model(regno=regno, name=f"Staff {regno}", gender=gender)
        session.add(row)
        session.flush()
        return str(row.id)


def details_for(regno):
    return UserDetails(
        regno=regno,
        name=f"User {regno}",
        email=f"{regno}@test.com",
        class_name="Test_Class",
        gender="other",
        matchmaking_enabled=True,
        notifications_enabled=True,
        account_type=AccountType.USERS.value,
    )


def staff_details_for(regno, account_type, *, account_id=None):
    return AccountDetails(
        id=account_id or regno,
        regno=regno,
        name=f"Staff {regno}",
        gender="other",
        notifications_enabled=True,
        account_type=account_type,
    )


STAFF_TYPES = [AccountType.ADMIN.value, AccountType.MODERATOR.value]


# --- staff are absent from the participant pool -----------------------------------


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_staff_are_excluded_from_the_candidate_pool(db, monkeypatch, staff_type):
    """The quieter half of the old role check: staff must not appear as someone to match with.

    The pool query reads ``users`` and staff have no row there, so the exclusion is a
    property of the schema rather than a clause. Seeding staff as the *only* eligible
    candidate makes the assertion unambiguous: if staff were ever written to ``users`` a
    match would be created instead of the 404.
    """
    # Pin the ordinary cross-gender branch so the 404 can only be explained by the pool,
    # never by the rare same-gender chance looking for a different gender.
    monkeypatch.setattr(matchmaking_service.random, "random", lambda: 0.99)
    make_user(db, "seeker", gender="male")
    make_staff(db, "staffmod", staff_type, gender="female")

    with pytest.raises(HTTPException) as exhausted:
        matchmaking_service.find_match_service(details_for("seeker"), db)
    assert exhausted.value.status_code == 404, "a staff account was offered as a match"

    with db.sessions.begin() as session:
        assert session.execute(select(MatchRecord).where(MatchRecord.user_1_regno == "seeker")).scalars().all() == []


def test_a_participant_candidate_is_still_offered(db, monkeypatch):
    """The counterweight: the pool must not have been emptied by accident."""
    # Force the ordinary cross-gender branch; the rare same-gender chance would otherwise
    # look for a male candidate and fail intermittently rather than by design.
    monkeypatch.setattr(matchmaking_service.random, "random", lambda: 0.99)
    make_user(db, "seeker", gender="male")
    make_staff(db, "staffmod", AccountType.MODERATOR.value, gender="female")
    make_user(db, "rival", gender="female")

    result = matchmaking_service.find_match_service(details_for("seeker"), db)
    assert result["matched_with"]["regno"] == "rival"


def test_a_participant_is_not_refused_a_cooldown_check(db):
    """Ordinary participants lose nothing to the staff split."""
    make_user(db, "plain")
    try:
        matchmaking_service.check_matchmaking_cooldown_service(details_for("plain"), db)
    except HTTPException as rejection:
        assert rejection.status_code != 403, "a plain user was refused as staff"


# --- love notes cannot reach staff ------------------------------------------------


async def _send(note_data, sender, db):
    return await love_note_service.send_love_note_service(note_data, sender, db)


def _note(recipient_id, image_b64="aGk="):
    return love_note_service.LoveNoteCreate(
        recipient_id=recipient_id, body="hello", image_base64=image_b64, anonymous=False
    )


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_a_staff_id_cannot_be_a_love_note_recipient(db, staff_type):
    """A staff account has no ``users`` row, so the recipient lookup finds nothing.

    This is the same outcome the old "staff cannot receive" check produced, arrived at
    because the id names a table the note cannot reference.
    """
    staff_id = make_staff(db, "staffmod", staff_type)
    with pytest.raises(HTTPException) as refusal:
        asyncio.run(_send(_note(staff_id), details_for("sender"), db))
    assert refusal.value.status_code == 404


# --- love-note images ------------------------------------------------------------


def _make_note(db, sender_id, recipient_id, path="love-notes/a.png", status="pending_review"):
    """Store a note whose image is genuinely servable, so a positive assertion means something.

    An unparseable stored value would make every 'may not view' test pass for the wrong
    reason, so the URL here is built to be recognised by the same normaliser production uses.
    """
    stored = f"https://res.cloudinary.com/demo/image/upload/v1/{path}"
    with db.sessions.begin() as session:
        session.add(
            LoveNoteRecord(
                sender_id=sender_id,
                recipient_id=recipient_id,
                body="hello",
                status=status,
                image_url=stored,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.flush()


def _account_with_id(account_id, regno, account_type):
    if account_type == AccountType.USERS.value:
        return details_for(regno).model_copy(update={"id": account_id})
    return staff_details_for(regno, account_type, account_id=account_id)


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_staff_may_view_a_love_note_image_they_are_not_part_of(db, staff_type):
    """A pending note's image is exactly what the review screen needs to show.

    Without this the moderator can list the note in the queue but not see the image, which is
    the dead end the route-level capability was meant to prevent.
    """
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id)
    reviewer = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)
    assert asset_delivery_service._may_view_love_note_asset(db, reviewer, "love-notes/a.png")


def test_a_recipient_cannot_view_a_note_that_is_not_yet_approved(db):
    """Independent of staff: an unapproved note stays closed to its recipient."""
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="pending_review")
    recipient = _account_with_id(recipient_id, "recipient", AccountType.USERS.value)
    assert not asset_delivery_service._may_view_love_note_asset(db, recipient, "love-notes/a.png")


def test_a_recipient_can_view_an_approved_note(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="approved")
    recipient = _account_with_id(recipient_id, "recipient", AccountType.USERS.value)
    assert asset_delivery_service._may_view_love_note_asset(db, recipient, "love-notes/a.png")


def test_a_sender_can_always_view_their_own_note(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="rejected")
    sender = _account_with_id(sender_id, "sender", AccountType.USERS.value)
    assert asset_delivery_service._may_view_love_note_asset(db, sender, "love-notes/a.png")


def test_an_uninvolved_participant_may_not_view_a_love_note_image(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    stranger_id = make_user(db, "stranger")
    _make_note(db, sender_id, recipient_id, status="approved")
    stranger = _account_with_id(stranger_id, "stranger", AccountType.USERS.value)
    assert not asset_delivery_service._may_view_love_note_asset(db, stranger, "love-notes/a.png")


def test_staff_review_access_does_not_grant_a_different_notes_image(db):
    """The capability widens the query, not the match on the asset.

    A moderator reviewing one note must not thereby be entitled to every other image in the
    system; the returned path still has to equal the one requested.
    """
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, path="love-notes/a.png")
    _make_note(db, sender_id, recipient_id, path="love-notes/b.png", status="approved")
    reviewer = _account_with_id(
        make_staff(db, "mod", AccountType.MODERATOR.value), "mod", AccountType.MODERATOR.value
    )
    assert not asset_delivery_service._may_view_love_note_asset(db, reviewer, "love-notes/other.png")


# --- conversation transcripts ----------------------------------------------------


def _make_thread(db, one, two, body="hello"):
    """An accepted conversation between two participants, carrying one unread message."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(user_1_regno=one, user_2_regno=two, created_at=now, expires_at=now)
        session.add(match)
        session.flush()
        conversation = ConversationRecord(
            match_id=str(match.id),
            initiator_regno=one,
            receiver_regno=two,
            status="accepted",
            created_at=now,
        )
        session.add(conversation)
        session.flush()
        conversation_id = str(conversation.id)
        session.add(
            MessageRecord(
                conversation_id=conversation_id,
                sender_regno=one,
                receiver_regno=two,
                body=body,
                created_at=now,
                is_read=False,
            )
        )
        session.flush()
    return conversation_id


def _unread_count(db, conversation_id, regno):
    with db.sessions.begin() as session:
        return len(
            session.scalars(
                select(MessageRecord).where(
                    MessageRecord.conversation_id == conversation_id,
                    MessageRecord.receiver_regno == regno,
                    MessageRecord.is_read.is_(False),
                )
            ).all()
        )


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_staff_may_read_a_transcript_they_are_not_part_of(db, staff_type):
    """The same capability that guards the admin transcript route, applied here.

    Keyed on the account table's capabilities rather than on the admin role so that a
    moderator can actually see the conversation a report is about.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)

    messages = message_service.get_messages(db, conversation_id, reader)
    assert [message["body"] for message in messages] == ["hello"]


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_staff_reading_a_transcript_does_not_clear_the_unread_flag(db, staff_type):
    """A moderator looking at a report is not a participant in the conversation.

    If a moderation read marked the thread read, the real recipient would never see the
    unread badge for a message a moderator happened to open first.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)

    message_service.get_messages(db, conversation_id, reader)
    assert _unread_count(db, conversation_id, "two") == 1, "a moderation read consumed the recipient's unread state"


def test_a_participant_reading_their_own_thread_does_mark_it_read(db):
    """The counterpart: ordinary reading must still clear the unread flag."""
    make_user(db, "one")
    two_id = make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(two_id, "two", AccountType.USERS.value)

    message_service.get_messages(db, conversation_id, reader)
    assert _unread_count(db, conversation_id, "two") == 0


def test_an_uninvolved_participant_may_not_read_a_transcript(db):
    """A plain account is not staff, so the participant check still applies to them."""
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    stranger = _account_with_id(make_user(db, "stranger"), "stranger", AccountType.USERS.value)

    with pytest.raises(HTTPException) as refusal:
        message_service.get_messages(db, conversation_id, stranger)
    assert refusal.value.status_code in (403, 404)
