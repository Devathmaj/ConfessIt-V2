"""Staff handling at the service layer, where the router's capability gate is not present.

The admin routes are gated by declared capabilities, but three services apply their own
staff rules underneath, and those are the places where a moderator could quietly gain or
retain access:

* matchmaking refuses staff outright, and staff are excluded from the candidate pool;
* a love-note transcript or image is readable by the two people involved, or by a
  moderator reviewing the queue;
* reading a transcript as staff does not mark the thread as read, because a moderator
  looking at a report is not a participant.

These are deliberately unit-level: the capability is the input, and the assertion is about
which query gets built, so the tests do not depend on the router that happens to call in.
"""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.db_models import (
    ConversationRecord,
    LoveNoteRecord,
    MatchRecord,
    MessageRecord,
    UserRecord,
)
from app.models import UserDetails
from app.roles import Role
from app.services import asset_delivery_service, love_note_service, matchmaking_service
from app.services.message_service import message_service


def make_user(db, regno, gender="other", *, role=Role.USER.value):
    """Insert a user and return the row id, which is what the note and match tables key on."""
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            class_name="Test_Class",
            gender=gender,
            role=role,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def details_for(regno, role):
    return UserDetails(
        regno=regno,
        name=f"User {regno}",
        email=f"{regno}@test.com",
        class_name="Test_Class",
        gender="other",
        matchmaking_enabled=True,
        notifications_enabled=True,
        role=role,
    )


# --- staff cannot participate in matchmaking --------------------------------------


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_are_refused_a_matchmaking_cooldown_check(db, role):
    """A moderator who is also a participant would be a contradiction, so the service
    refuses before it looks at any state."""
    with pytest.raises(HTTPException) as refusal:
        matchmaking_service.check_matchmaking_cooldown_service(details_for("mod", role), db)
    assert refusal.value.status_code == 403


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_are_refused_matching(db, role):
    with pytest.raises(HTTPException) as refusal:
        matchmaking_service.find_match_service(details_for("mod", role), db)
    assert refusal.value.status_code == 403


def test_a_user_is_not_refused_by_the_staff_check(db):
    """The guard must not be so wide that ordinary participants lose matchmaking."""
    make_user(db, "plain")
    # No exception is the assertion: a user with no prior match is told to wait or is
    # paired, but is never blocked with a 403 for their role.
    try:
        matchmaking_service.check_matchmaking_cooldown_service(details_for("plain", Role.USER.value), db)
    except HTTPException as rejection:
        assert rejection.status_code != 403, "a plain user was refused as staff"


@pytest.mark.parametrize("staff_role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_are_excluded_from_the_candidate_pool(db, monkeypatch, staff_role):
    """The quieter half: staff must not appear as someone to be matched with.

    Being refused matchmaking yourself is not enough. A staff account left in the
    candidate pool is what puts a moderator into a stranger's inbox, so the pool query
    has to exclude them on its own. Seeding staff as the *only* eligible candidate makes
    the assertion unambiguous: if the filter were missing, a match would be created
    instead of the 404.
    """
    # Pin the ordinary cross-gender branch so the 404 can only be explained by the role
    # filter, never by the rare same-gender chance looking for a different gender.
    monkeypatch.setattr(matchmaking_service.random, "random", lambda: 0.99)
    make_user(db, "seeker", gender="male", role=Role.USER.value)
    make_user(db, "staffmod", gender="female", role=staff_role)

    with pytest.raises(HTTPException) as exhausted:
        matchmaking_service.find_match_service(details_for("seeker", Role.USER.value), db)
    assert exhausted.value.status_code == 404, "a staff account was offered as a match"

    with db.sessions.begin() as session:
        assert session.execute(select(MatchRecord).where(MatchRecord.user_1_regno == "seeker")).scalars().all() == []


def test_a_non_staff_candidate_is_still_offered(db, monkeypatch):
    """The counterweight: exclusion must not have emptied the pool by accident."""
    # Force the ordinary cross-gender branch; the rare same-gender chance would otherwise
    # look for a male candidate and fail intermittently rather than by design.
    monkeypatch.setattr(matchmaking_service.random, "random", lambda: 0.99)
    make_user(db, "seeker", gender="male", role=Role.USER.value)
    make_user(db, "staffmod", gender="female", role=Role.MODERATOR.value)
    make_user(db, "rival", gender="female", role=Role.USER.value)

    result = matchmaking_service.find_match_service(details_for("seeker", Role.USER.value), db)
    assert result["matched_with"]["regno"] == "rival"


# --- staff cannot send or receive love notes --------------------------------------


async def _send(note_data, sender, db):
    return await love_note_service.send_love_note_service(note_data, sender, db)


def _note(recipient_id, image_b64="aGk="):
    return love_note_service.LoveNoteCreate(
        recipient_id=recipient_id, body="hello", image_base64=image_b64, anonymous=False
    )


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_cannot_receive_a_love_note(db, role):
    """A reviewer must not also be a candidate, or moderation reads become self-serving."""
    recipient_id = make_user(db, "staffmod", role=role)
    with pytest.raises(HTTPException) as refusal:
        asyncio.run(_send(_note(recipient_id), details_for("sender", Role.USER.value), db))
    assert refusal.value.status_code == 403


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_cannot_send_a_love_note(db, role):
    recipient_id = make_user(db, "target", role=Role.USER.value)
    with pytest.raises(HTTPException) as refusal:
        asyncio.run(_send(_note(recipient_id), details_for("staffmod", role), db))
    assert refusal.value.status_code == 403


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


def _user_with_id(user_id, regno, role):
    return details_for(regno, role).model_copy(update={"id": user_id})


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_may_view_a_love_note_image_they_are_not_part_of(db, role):
    """A pending note's image is exactly what the review screen needs to show.

    Without this the moderator can list the note in the queue but not see the image,
    which is the dead end the route-level capability was meant to prevent.
    """
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id)
    reviewer = _user_with_id(make_user(db, "mod", role=role), "mod", role)
    assert asset_delivery_service._may_view_love_note_asset(db, reviewer, "love-notes/a.png")


def test_a_recipient_cannot_view_a_note_that_is_not_yet_approved(db):
    """Independent of staff: an unapproved note stays closed to its recipient."""
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="pending_review")
    recipient = _user_with_id(recipient_id, "recipient", Role.USER.value)
    assert not asset_delivery_service._may_view_love_note_asset(db, recipient, "love-notes/a.png")


def test_a_recipient_can_view_an_approved_note(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="approved")
    recipient = _user_with_id(recipient_id, "recipient", Role.USER.value)
    assert asset_delivery_service._may_view_love_note_asset(db, recipient, "love-notes/a.png")


def test_a_sender_can_always_view_their_own_note(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="rejected")
    sender = _user_with_id(sender_id, "sender", Role.USER.value)
    assert asset_delivery_service._may_view_love_note_asset(db, sender, "love-notes/a.png")


def test_an_uninvolved_user_may_not_view_a_love_note_image(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    stranger_id = make_user(db, "stranger")
    _make_note(db, sender_id, recipient_id, status="approved")
    stranger = _user_with_id(stranger_id, "stranger", Role.USER.value)
    assert not asset_delivery_service._may_view_love_note_asset(db, stranger, "love-notes/a.png")


def test_staff_review_access_does_not_grant_a_different_notes_image(db):
    """The capability widens the query, not the match on the asset.

    A moderator reviewing one note must not thereby be entitled to every other image in
    the system; the returned path still has to equal the one requested.
    """
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, path="love-notes/a.png")
    _make_note(db, sender_id, recipient_id, path="love-notes/b.png", status="approved")
    reviewer = _user_with_id(make_user(db, "mod", role=Role.MODERATOR.value), "mod", Role.MODERATOR.value)
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


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_may_read_a_transcript_they_are_not_part_of(db, role):
    """The same capability that guards the admin transcript route, applied here.

    Keyed on the capability rather than on the admin role so that a moderator can
    actually see the conversation a report is about.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _user_with_id(make_user(db, "mod", role=role), "mod", role)

    messages = message_service.get_messages(db, conversation_id, reader)
    assert [message["body"] for message in messages] == ["hello"]


@pytest.mark.parametrize("role", [Role.ADMIN.value, Role.MODERATOR.value])
def test_staff_reading_a_transcript_does_not_clear_the_unread_flag(db, role):
    """A moderator looking at a report is not a participant in the conversation.

    If a moderation read marked the thread read, the real recipient would never see the
    unread badge for a message a moderator happened to open first.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _user_with_id(make_user(db, "mod", role=role), "mod", role)

    message_service.get_messages(db, conversation_id, reader)
    assert _unread_count(db, conversation_id, "two") == 1, "a moderation read consumed the recipient's unread state"


def test_a_participant_reading_their_own_thread_does_mark_it_read(db):
    """The counterpart: ordinary reading must still clear the unread flag."""
    make_user(db, "one")
    two_id = make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _user_with_id(two_id, "two", Role.USER.value)

    message_service.get_messages(db, conversation_id, reader)
    assert _unread_count(db, conversation_id, "two") == 0


def test_an_uninvolved_user_may_not_read_a_transcript(db):
    """A plain account is not staff, so the participant check still applies to them."""
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    stranger = _user_with_id(make_user(db, "stranger"), "stranger", Role.USER.value)

    with pytest.raises(HTTPException) as refusal:
        message_service.get_messages(db, conversation_id, stranger)
    assert refusal.value.status_code in (403, 404)
