"""A long name must not stop a message request from being delivered.

A participant's name may be 160 characters -- the column allows it, the profile form
takes it -- while the notification heading column holds 120. A heading built as
``f"Message request from {name}"`` therefore overflowed its column, and PostgreSQL does
not truncate an over-long value: it raises ``string_data_right_truncation`` and refuses
the insert.

The refusal landed in the wrong place. The conversation status was committed by
``_save_conversation`` in its own transaction *before* the notice was written, so the
request had already gone through when the insert failed. The service turned that into a
500, the initiator believed the request had failed, and the retry was refused as
"already requested" -- a thread neither participant could move, and a receiver who was
never told anything had happened.

Two things are pinned here, because either one alone leaves the hole open:

* **the heading is built to fit** -- the name is shortened and the wording is kept,
  because the wording is what the client matches on to tell a conversation notice from
  a standalone one, and a heading clipped as a whole would lose it; and
* **the notice is best-effort** -- whatever else goes wrong writing it, the decision
  that was already applied stands and the caller is told so.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.accounts import AccountType
from app.db_models import (
    ConversationRecord,
    MatchRecord,
    NotificationRecord,
    UserRecord,
)
from app.main import app
from app.models import UserDetails
from app.services import conversation_service, matchmaking_service
from app.services import notification_service
from app.services.auth_service import get_current_user
from app.services.notification_service import (
    NOTIFICATION_BODY_MAX,
    NOTIFICATION_HEADING_MAX,
    build_notification_heading,
    create_notification_service,
)

# A name at the width the ``users.name`` column allows. It is a legal name, not an
# attack: the profile form accepts it and the database stores it.
LONG_NAME = "A" * 160

# The wording the inbox client matches on to recognise a conversation notice. It
# lives in ``frontend/src/pages/InboxPage.tsx`` as CONVERSATION_NOTIFICATION_HEADINGS,
# and a heading that lost it would be rendered twice: once as the derived
# conversation card and once as a standalone notice.
REQUESTED_WORDING = "Message request from"
ACCEPTED_WORDING = "accepted your message request"
REJECTED_WORDING = "rejected your message request"


def make_user(db, regno, name=None, gender="female") -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=name or f"User {regno}", email=f"{regno.lower()}@test.com",
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
        account_type=AccountType.USERS.value, last_matchmaking_at=row.last_matchmaking_at,
    )


def add_pending_conversation(db, initiator, receiver) -> str:
    """A live match with a pending conversation on it -- the state a request starts from.

    The match is inside its own four-hour window so that a refusal a test sees can
    only have come from the thing under test.
    """
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        match = MatchRecord(
            user_1_regno=initiator, user_2_regno=receiver,
            created_at=now, expires_at=now + timedelta(hours=4),
        )
        session.add(match)
        session.flush()
        match_id = str(match.id)
        session.add(ConversationRecord(
            match_id=match_id, initiator_regno=initiator, receiver_regno=receiver,
            status="pending", created_at=now,
        ))
    return match_id


def conversation_status(db, match_id) -> str:
    with db.sessions() as session:
        row = session.scalar(
            select(ConversationRecord).where(ConversationRecord.match_id == match_id)
        )
        return row.status


def notices_for(db, regno) -> list:
    with db.sessions() as session:
        return list(
            session.scalars(
                select(NotificationRecord).where(
                    NotificationRecord.recipient_regno == regno
                )
            ).all()
        )


def _explode(*args, **kwargs):
    """A notification write that fails, standing in for any database fault.

    The truncation error was only the cheapest way to reach this; a full pool or a
    deadlock reaches the same place, and the request has to survive all of them.
    """
    raise RuntimeError("the notice could not be written")


@pytest.fixture
def signed_in_as(db):
    """Sign the requests of one test in as a participant who exists."""

    def _sign_in(regno) -> UserDetails:
        user = as_user(db, regno)
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    return _sign_in


# ------------------------------------------------- a long name does not break the request


def test_a_long_name_does_not_stop_the_request_from_being_stored(db):
    """The bug itself: the notice used to overflow its column and take the request with it."""
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")

    response = conversation_service.request_conversation_service(
        as_user(db, "alice"), match_id, db
    )

    assert response["status"] == "success"
    assert conversation_status(db, match_id) == "requested"
    assert len(notices_for(db, "bob")) == 1


def test_the_stored_heading_fits_the_column(db):
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")

    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)

    heading = notices_for(db, "bob")[0].heading
    assert len(heading) <= NOTIFICATION_HEADING_MAX


def test_the_stored_heading_keeps_the_wording_the_client_matches_on(db):
    """The name is shortened, not the wording.

    The inbox client tells a conversation notice from a standalone one by matching the
    wording, and derives the conversation card from the conversation itself. A heading
    clipped as a whole would lose the wording and be rendered as both.
    """
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")

    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)

    heading = notices_for(db, "bob")[0].heading
    assert REQUESTED_WORDING in heading
    assert heading.startswith(REQUESTED_WORDING)


def test_the_request_route_answers_201_and_stores_the_notice(db, client: TestClient, signed_in_as):
    """End to end: the status code the client acts on, and the notice it waits for."""
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    signed_in_as("alice")

    response = client.post("/conversations/request", json={"match_id": match_id})

    assert response.status_code == 201, response.text
    assert conversation_status(db, match_id) == "requested"
    assert len(notices_for(db, "bob")) == 1


def test_the_acceptance_heading_keeps_its_wording_for_a_long_name(db):
    make_user(db, "alice")
    make_user(db, "bob", name=LONG_NAME)
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)

    conversation_service.accept_conversation_service(as_user(db, "bob"), match_id, db)

    heading = notices_for(db, "alice")[0].heading
    assert ACCEPTED_WORDING in heading
    assert len(heading) <= NOTIFICATION_HEADING_MAX


def test_the_rejection_heading_keeps_its_wording_for_a_long_name(db):
    make_user(db, "alice")
    make_user(db, "bob", name=LONG_NAME)
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)

    conversation_service.reject_conversation_service(as_user(db, "bob"), match_id, db)

    heading = notices_for(db, "alice")[0].heading
    assert REJECTED_WORDING in heading
    assert len(heading) <= NOTIFICATION_HEADING_MAX


# ------------------------------- a notice that cannot be written does not undo the decision


def test_a_failed_notice_leaves_the_request_applied(db, monkeypatch):
    """The conversation was committed before the notice was written, so it stays committed.

    Before this, the failure propagated and the caller was told the request had failed.
    The retry was then refused as "already requested", which left the initiator
    believing nothing had happened and the receiver never hearing about it at all.
    """
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    monkeypatch.setattr(
        notification_service, "create_notification_service", _explode
    )

    response = conversation_service.request_conversation_service(
        as_user(db, "alice"), match_id, db
    )

    assert response["status"] == "success"
    assert conversation_status(db, match_id) == "requested"


def test_a_failed_notice_does_not_turn_the_request_into_a_500(
    db, client: TestClient, signed_in_as, monkeypatch
):
    make_user(db, "alice", name=LONG_NAME)
    make_user(db, "bob")
    match_id = add_pending_conversation(db, "alice", "bob")
    signed_in_as("alice")
    monkeypatch.setattr(
        notification_service, "create_notification_service", _explode
    )

    response = client.post("/conversations/request", json={"match_id": match_id})

    assert response.status_code != 500, response.text
    assert response.status_code == 201, response.text
    assert conversation_status(db, match_id) == "requested"


def test_a_failed_notice_leaves_the_acceptance_applied(db, monkeypatch):
    make_user(db, "alice")
    make_user(db, "bob", name=LONG_NAME)
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    monkeypatch.setattr(
        notification_service, "create_notification_service", _explode
    )

    response = conversation_service.accept_conversation_service(
        as_user(db, "bob"), match_id, db
    )

    assert response["status"] == "success"
    assert conversation_status(db, match_id) == "accepted"


def test_a_failed_notice_leaves_the_rejection_applied(db, monkeypatch):
    make_user(db, "alice")
    make_user(db, "bob", name=LONG_NAME)
    match_id = add_pending_conversation(db, "alice", "bob")
    conversation_service.request_conversation_service(as_user(db, "alice"), match_id, db)
    monkeypatch.setattr(
        notification_service, "create_notification_service", _explode
    )

    response = conversation_service.reject_conversation_service(
        as_user(db, "bob"), match_id, db
    )

    assert response["status"] == "success"
    assert conversation_status(db, match_id) == "rejected"


def test_a_failed_notice_leaves_the_match_applied(db, monkeypatch):
    """The same shape, in matchmaking: the match is committed, the notice is not.

    A partner who is never told they were matched is a participant who waits for a
    conversation that the server already says is theirs.
    """
    make_user(db, "alice", gender="female")
    make_user(db, "bob", gender="male")
    monkeypatch.setattr(
        notification_service, "create_notification_service", _explode
    )

    result = matchmaking_service.find_match_service(as_user(db, "alice"), db)

    assert result["status"] == "matched"
    with db.sessions() as session:
        assert session.scalar(
            select(func.count()).select_from(MatchRecord)
        ) == 1


# --------------------------------------------- the guard is in one place, not in every caller


def test_the_notification_write_clips_a_heading_that_overflows_the_column(db):
    """The choke point, so a caller added later inherits the guard.

    A heading that overflows its column is not a short notice: PostgreSQL refuses the
    write outright, so the notice is lost and -- in a request that has already
    committed something else -- the whole request is lost with it.
    """
    make_user(db, "alice")

    create_notification_service("alice", "x" * 500, "b" * 900, db)

    row = notices_for(db, "alice")[0]
    assert len(row.heading) == NOTIFICATION_HEADING_MAX
    assert len(row.body) == NOTIFICATION_BODY_MAX


def test_the_clipping_widths_are_the_column_widths():
    """The guard is read off the schema, so it cannot drift from what it guards.

    A number written out beside the column is a second declaration of one limit, and
    two declarations of a limit are how a field ends up writable past the column that
    holds it.
    """
    assert NotificationRecord.__table__.c.heading.type.length == NOTIFICATION_HEADING_MAX
    assert NotificationRecord.__table__.c.body.type.length == NOTIFICATION_BODY_MAX


def test_a_short_name_is_left_whole():
    heading = build_notification_heading("Message request from {name}", "Sam")
    assert heading == "Message request from Sam"


def test_a_long_name_is_shortened_and_the_wording_survives():
    heading = build_notification_heading("Message request from {name}", LONG_NAME)

    assert len(heading) == NOTIFICATION_HEADING_MAX
    assert REQUESTED_WORDING in heading
    assert LONG_NAME not in heading
