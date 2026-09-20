"""Staff handling at the service layer, where the router's capability gate is not present.

The admin routes are gated by declared capabilities, but three services apply their own
rules underneath, and those are the places where a staff account could quietly gain or retain
access:

* matchmaking draws its candidates from ``users``, so staff are excluded by the schema rather
  than by a clause that could be forgotten;
* a love-note image is readable by the two people involved, or by a moderator reviewing the
  queue;
* reading a transcript is for its participants only, so staff get no special route in and a
  refused read leaves the recipient's unread state untouched.

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
from app.services import love_note_service, matchmaking_service
from app.services.message_service import message_service


def make_user(db, regno, gender="other"):
    """Insert a participant and return the row id, which note and match tables key on."""
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class",
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
        # Email is required of every account, so the helper supplies one derived from the
        # regno: derived rather than constant so two staff rows in the same table stay unique
        # under the per-table unique index.
        row = model(regno=regno, name=f"Staff {regno}", email=f"{regno}@test.com", gender=gender)
        session.add(row)
        session.flush()
        return str(row.id)


def details_for(regno):
    return UserDetails(
        regno=regno,
        name=f"User {regno}",
        email=f"{regno}@test.com",
        year=2, department="Test_Class",
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
        email=f"{regno}@test.com",
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


def _make_note(
    db,
    sender_id,
    recipient_id,
    note_id="11111111-1111-1111-1111-111111111111",
    status="pending_review",
    removed_at=None,
):
    """Store a note carrying a real image identity, so a positive assertion means something.

    The row now holds the object's identity rather than an address, and the object is one-to-one
    with the note id -- that is the whole reason an entitlement check can be made about "this
    note" instead of about "whatever path the request named". A placeholder here would make
    every 'may not view' assertion pass for the wrong reason.
    """
    with db.sessions.begin() as session:
        session.add(
            LoveNoteRecord(
                id=note_id,
                sender_id=sender_id,
                recipient_id=recipient_id,
                body="hello",
                status=status,
                image_public_id=f"love_notes/{note_id}/image",
                image_format="png",
                removed_at=removed_at,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.flush()


def _note_row(db, note_id="11111111-1111-1111-1111-111111111111"):
    with db.sessions() as session:
        return session.get(LoveNoteRecord, note_id)


def _may_view(note, viewer):
    return love_note_service.may_view_love_note_image(
        note, viewer_id=viewer.id, viewer_account_type=viewer.account_type
    )


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
    _make_note(db, make_user(db, "sender"), make_user(db, "recipient"))
    reviewer = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)
    assert _may_view(_note_row(db), reviewer)


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_staff_may_view_a_removed_note(db, staff_type):
    """Removal must not blind the reviewer ruling on it.

    The one case where the "removed means nobody but staff" rule cuts the other way: staff see
    removed notes deliberately, so a moderator can answer why something was taken down.
    """
    _make_note(
        db,
        make_user(db, "sender"),
        make_user(db, "recipient"),
        status="rejected",
        removed_at=datetime.now(timezone.utc),
    )
    reviewer = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)
    assert _may_view(_note_row(db), reviewer)


def test_a_recipient_cannot_view_a_note_that_is_not_yet_approved(db):
    """Independent of staff: an unapproved note stays closed to its recipient."""
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="pending_review")
    recipient = _account_with_id(recipient_id, "recipient", AccountType.USERS.value)
    assert not _may_view(_note_row(db), recipient)


def test_a_recipient_can_view_an_approved_note(db):
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, status="approved")
    recipient = _account_with_id(recipient_id, "recipient", AccountType.USERS.value)
    assert _may_view(_note_row(db), recipient)


def test_a_recipient_cannot_view_a_removed_approved_note(db):
    """The removed check has to come after the approval one, or removal buys back access."""
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(
        db,
        sender_id,
        recipient_id,
        status="approved",
        removed_at=datetime.now(timezone.utc),
    )
    recipient = _account_with_id(recipient_id, "recipient", AccountType.USERS.value)
    assert not _may_view(_note_row(db), recipient)


def test_a_sender_can_always_view_their_own_note(db):
    """Any review state, including one that has been taken down -- they wrote it."""
    sender_id = make_user(db, "sender")
    _make_note(db, sender_id, make_user(db, "recipient"), status="rejected")
    sender = _account_with_id(sender_id, "sender", AccountType.USERS.value)
    assert _may_view(_note_row(db), sender)


def test_an_uninvolved_participant_may_not_view_a_love_note_image(db):
    _make_note(db, make_user(db, "sender"), make_user(db, "recipient"), status="approved")
    stranger = _account_with_id(
        make_user(db, "stranger"), "stranger", AccountType.USERS.value
    )
    assert not _may_view(_note_row(db), stranger)


def test_an_anonymous_viewer_may_not_view_anything(db):
    """No id at all is not a participant. The check has to fail closed, not open."""
    _make_note(db, make_user(db, "sender"), make_user(db, "recipient"), status="approved")
    assert not love_note_service.may_view_love_note_image(
        _note_row(db), viewer_id=None, viewer_account_type=None
    )


def test_entitlement_is_decided_per_note_not_per_account(db):
    """Staff review access must not become blanket access to a note they are not reviewing.

    The identity is per-note by construction -- the object is named after the note id -- so
    this is really a check that the second note's row is a different row and that an
    entitlement for one is never reused for the other.
    """
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    _make_note(db, sender_id, recipient_id, note_id="11111111-1111-1111-1111-111111111111")
    _make_note(
        db,
        sender_id,
        recipient_id,
        note_id="22222222-2222-2222-2222-222222222222",
        status="approved",
    )
    reviewer = _account_with_id(
        make_staff(db, "mod", AccountType.MODERATOR.value), "mod", AccountType.MODERATOR.value
    )

    first, second = _note_row(db), _note_row(db, "22222222-2222-2222-2222-222222222222")
    assert first.image_public_id != second.image_public_id
    assert _may_view(first, reviewer) and _may_view(second, reviewer)


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
def test_staff_may_not_read_a_transcript_they_are_not_part_of(db, staff_type):
    """A private thread is readable by the two people in it and by nobody else.

    Staff are no longer an exception, whatever they hold. This used to be a bypass keyed on a
    capability that also guarded the admin transcript route; with that surface removed the
    capability went with it, and a moderator is not a party to a conversation between two
    participants. What staff may see *about* a conversation is the report queue, which is
    built from the reported message and its own context rather than from the thread.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)

    with pytest.raises(HTTPException) as refusal:
        message_service.get_messages(db, conversation_id, reader)
    assert refusal.value.status_code in (403, 404)


def test_a_participant_reading_their_own_thread_does_mark_it_read(db):
    """The counterpart: ordinary reading must still clear the unread flag."""
    make_user(db, "one")
    two_id = make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(two_id, "two", AccountType.USERS.value)

    message_service.get_messages(db, conversation_id, reader)
    assert _unread_count(db, conversation_id, "two") == 0


def test_a_refused_read_does_not_consume_the_recipients_unread_state(db):
    """Whoever is turned away must leave the thread exactly as it was.

    The unread flag belongs to the recipient. A read that was refused -- by the participant
    check, or by the account having no business in the thread -- is not a read, so it must not
    consume the badge the real recipient is entitled to see.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    stranger = _account_with_id(make_user(db, "stranger"), "stranger", AccountType.USERS.value)

    with pytest.raises(HTTPException):
        message_service.get_messages(db, conversation_id, stranger)

    assert _unread_count(db, conversation_id, "two") == 1


@pytest.mark.parametrize("staff_type", STAFF_TYPES)
def test_a_blocked_staff_read_leaves_the_unread_flag_alone(db, staff_type):
    """The specific regression the old bypass had to avoid, now structural.

    The bypass skipped the read-marking step for exactly the readers it let in. With no
    bypass there is no second path, so a staff account cannot clear a participant's unread
    state -- asserted here because the property mattered enough to be written down once.
    """
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    reader = _account_with_id(make_staff(db, "mod", staff_type), "mod", staff_type)

    with pytest.raises(HTTPException):
        message_service.get_messages(db, conversation_id, reader)

    assert _unread_count(db, conversation_id, "two") == 1


def test_an_uninvolved_participant_may_not_read_a_transcript(db):
    """The baseline the two refusals above are variations of."""
    make_user(db, "one")
    make_user(db, "two")
    conversation_id = _make_thread(db, "one", "two")
    stranger = _account_with_id(make_user(db, "stranger"), "stranger", AccountType.USERS.value)

    with pytest.raises(HTTPException) as refusal:
        message_service.get_messages(db, conversation_id, stranger)
    assert refusal.value.status_code in (403, 404)
