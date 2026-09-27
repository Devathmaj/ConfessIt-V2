"""A notification should be clickable exactly when it has something to click through to.

The reader of a notification is being told something happened, and in most cases there is a
page that shows it: the thread a request or an acceptance is about, the note that was just
approved. ``target_kind`` and ``target_id`` are how the server says which page, and the client
turns them into a route.

A large share of notices have no such page, and saying so is the interesting half of this. A
match was drawn, a report was reviewed, content was taken down -- in each case the item either
does not exist for this reader or was never theirs to open, so a target would send them to a
page that cannot answer the notification. Those rows carry a null pair and are rendered
non-clickable.

These tests pin both halves: that a real item is reachable from its notification, and that the
informational notices still write no target at all. The second is the one worth having, because
the failure is silent -- a target on the matchmaking notice would look entirely reasonable in
the diff and would leave the reader on a page about a match that no longer exists.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db_models import (
    ConversationRecord,
    LoveNoteRecord,
    MatchRecord,
    NotificationRecord,
    UserRecord,
)
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services import conversation_service, love_note_service
from app.services.notification_service import (
    NOTIFICATION_TARGETS,
    create_notification_service,
    get_user_notifications_service,
)


@pytest.fixture
def db():
    return get_postgres_client()


def make_user(db, regno, gender="female") -> str:
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
        notifications_enabled=row.notifications_enabled,
        account_type="users", last_matchmaking_at=row.last_matchmaking_at,
    )


def add_matched_pair(db) -> tuple:
    """Two users, a live match between them, and a pending conversation on that match.

    Returns the match id, because the target is the match id -- every conversation route in
    the API is keyed by match, so that is the value a client has to be handed to reach the
    thread.
    """
    make_user(db, "alice", gender="female")
    make_user(db, "bob", gender="male")

    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno="alice", user_2_regno="bob",
            created_at=_now(), expires_at=_now() + timedelta(hours=4),
        )
        session.add(match)
        session.flush()
        match_id = str(match.id)

        session.add(
            ConversationRecord(
                match_id=match_id, initiator_regno="alice", receiver_regno="bob",
                status="pending", created_at=_now(),
            )
        )

    return match_id, "alice", "bob"


def _now():
    return datetime.now(timezone.utc)


def only_notification(db, regno) -> dict:
    """The reader's view of their notifications, as the API returns them."""
    rows = get_user_notifications_service(regno, db)["notifications"]
    assert len(rows) == 1, f"expected exactly one notification, got {len(rows)}"
    return rows[0]


def row_target(db, notification_id):
    with db.sessions() as session:
        row = session.scalar(
            select(NotificationRecord).where(NotificationRecord.id == notification_id)
        )
        return (row.target_kind, row.target_id)


# --- the target pair itself -------------------------------------------------------


def test_a_target_is_stored_and_returned(db):
    """The whole feature in one test: written on create, handed back on read."""
    make_user(db, "alice")

    notification_id = create_notification_service(
        "alice", "Someone said something", "Open this.",
        db, target_kind="conversation", target_id="match-123",
    )

    assert row_target(db, notification_id) == ("conversation", "match-123")
    served = only_notification(db, "alice")
    assert served["target_kind"] == "conversation"
    assert served["target_id"] == "match-123"


def test_a_notification_with_no_target_reports_nulls_not_missing_keys(db):
    """A client has to be able to tell "nothing to open" from "an older server"."""
    make_user(db, "alice")

    create_notification_service("alice", "You appeared in matchmaking", "You were matched.", db)

    served = only_notification(db, "alice")
    assert "target_kind" in served and "target_id" in served
    assert served["target_kind"] is None
    assert served["target_id"] is None


def test_half_a_target_is_refused(db):
    """A kind with no id opens nothing; an id with no kind is unreadable."""
    make_user(db, "alice")

    with pytest.raises(ValueError):
        create_notification_service("alice", "h", "b", db, target_kind="conversation")

    with pytest.raises(ValueError):
        create_notification_service("alice", "h", "b", db, target_id="match-123")

    assert get_user_notifications_service("alice", db)["notifications"] == []


def test_an_unknown_kind_is_refused_rather_than_stored(db):
    """A target the client cannot resolve is worse than none: it looks clickable and is not."""
    make_user(db, "alice")

    with pytest.raises(ValueError):
        create_notification_service(
            "alice", "h", "b", db, target_kind="report", target_id="r1"
        )

    assert get_user_notifications_service("alice", db)["notifications"] == []


def test_the_two_known_kinds_are_the_two_the_client_can_open(db):
    """Pins the vocabulary so adding a kind is a deliberate act on both sides."""
    assert NOTIFICATION_TARGETS == {"conversation", "love_note"}


# --- what real features write ------------------------------------------------------


def test_the_approved_note_notice_points_at_the_note(db):
    """The one notification where the whole point is the item.

    This notice is written at the one moment the note becomes readable in the recipient's
    inbox, so a reader who has to go and find it is the failure being guarded against.
    """
    add_love_note(db, sender="bob", recipient="alice", status="pending_review")

    approve_note(db, "alice")

    recipient_notice = only_notification(db, "alice")
    assert recipient_notice["target_kind"] == "love_note"
    assert recipient_notice["target_id"] == note_id_for(db, "alice")


def test_the_sender_delivery_notice_points_at_nothing(db):
    """A sender has no list of notes they have sent, so there is nowhere to land."""
    add_love_note(db, sender="bob", recipient="alice", status="pending_review")

    approve_note(db, "alice")

    sender_notice = only_notification(db, "bob")
    assert sender_notice["target_kind"] is None
    assert sender_notice["target_id"] is None


def approve_note(db, moderator_regno):
    """Run the moderator approval path, which is what writes both notices."""
    note_id = note_id_for(db, "alice")
    asyncio.run(
        love_note_service.update_love_note_status_service(note_id, "approved", db)
    )


def test_the_matchmaking_notice_points_at_nothing(db):
    """The regression this guards: a match id is not something the reader can open.

    A match is a fact about two people, not a page. It means nothing until someone asks for the
    conversation it opened, and there is no list of "your matches" to put it in -- so a target
    here would look reasonable and deliver the reader nowhere.
    """
    from app.services import matchmaking_service

    make_user(db, "alice", gender="female")
    make_user(db, "bob", gender="male")

    matchmaking_service.find_match_service(as_user(db, "alice"), db)

    served = only_notification(db, "bob")
    assert served["heading"] == "You appeared in matchmaking"
    assert served["target_kind"] is None
    assert served["target_id"] is None


def test_the_request_notice_points_at_the_match(db):
    """The reader has to be able to get to the thread the request is about.

    The target is the *match* id rather than the conversation id, because there is at most one
    conversation per match and every route that reaches a conversation is keyed by match.
    Handing over the conversation id instead would produce a target that looks right and
    resolves to nothing.
    """
    match_id, alice, bob = add_matched_pair(db)

    conversation_service.request_conversation_service(as_user(db, alice), match_id, db)

    notice = only_notification(db, bob)
    assert notice["target_kind"] == "conversation"
    assert notice["target_id"] == match_id


def test_the_acceptance_notice_points_at_the_match(db):
    match_id, alice, bob = add_matched_pair(db)
    conversation_service.request_conversation_service(as_user(db, alice), match_id, db)

    conversation_service.accept_conversation_service(as_user(db, bob), match_id, db)

    # Bob has two notices now: the request he received, and the acceptance he gave. The
    # acceptance is the newer row, and both are checked because the point is that every
    # notice in this family is reachable, not just the first one written.
    rows = get_user_notifications_service(bob, db)["notifications"]
    assert all(r["target_kind"] == "conversation" for r in rows)
    assert all(r["target_id"] == match_id for r in rows)


def test_the_rejection_notice_points_at_the_match(db):
    """Rejected included.

    A rejected thread is still the conversation the reader asked about, and the Message Box
    shows it read-only -- so the notification can go there. The one place that would be wrong
    is an *unrelated* thread, which is what a wrong id would produce.
    """
    match_id, alice, bob = add_matched_pair(db)
    conversation_service.request_conversation_service(as_user(db, alice), match_id, db)

    conversation_service.reject_conversation_service(as_user(db, bob), match_id, db)

    rejection = next(
        r for r in get_user_notifications_service(alice, db)["notifications"] if "rejected" in r["heading"]
    )
    assert rejection["target_kind"] == "conversation"
    assert rejection["target_id"] == match_id


def test_a_removal_notice_points_at_nothing(db):
    """The content a removal speaks for has already stopped being visible to its author."""
    make_user(db, "alice")

    create_notification_service(
        "alice", "Content Removed", "Your content was removed.", db,
    )

    served = only_notification(db, "alice")
    assert served["target_kind"] is None


def add_love_note(db, *, sender, recipient, status) -> str:
    """Both participants are created here, since the note references them by user id."""
    sender_id = make_user(db, sender, gender="male")
    recipient_id = make_user(db, recipient)

    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            sender_id=sender_id, recipient_id=recipient_id, body="hello",
            image_public_id="love_notes/mock-note-id/image", image_format="png", anonymous=False,
            status=status, created_at=_now(),
        )
        session.add(row)
        session.flush()
        return str(row.id)


def note_id_for(db, recipient_regno) -> str:
    """The single note addressed to this recipient. Keeps the assertions short."""
    with db.sessions() as session:
        recipient = session.scalar(
            select(UserRecord).where(UserRecord.regno == recipient_regno)
        )
        row = session.scalar(
            select(LoveNoteRecord).where(LoveNoteRecord.recipient_id == str(recipient.id))
        )
        return str(row.id)