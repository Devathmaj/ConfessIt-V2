"""A notification must not carry a wall-clock time the server made up.

``create_notification_service`` persists ``body`` as text, and the matchmaking notice used to
interpolate ``now.strftime(...)`` into it. ``now`` is UTC and the server does not know where
the recipient is, so the string was frozen at the server's zone: a reader at UTC+5:30 saw
"matched at 12:00 PM" for a match made at 17:31, and no later fix could reach it because the
text was already in the database.

The row's own ``created_at`` is timezone-aware and is rendered by the client in the reader's
own zone, which is the only place the reader's zone is knowable. These tests pin that the
matchmaking body stays free of absolute times, so the next person to reach for ``strftime``
here meets a failing test.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db_models import NotificationRecord, UserRecord
from app.models import UserDetails
from app.postgres import get_postgres_client
from app.services import matchmaking_service


# "12:00 PM", "05:31 pm", "17:31". Deliberately loose -- the point is that no clock reading
# of any shape is baked into the text.
CLOCK_READING = re.compile(r"\b\d{1,2}:\d{2}\s*(?:[ap]\.?m\.?)?\b", re.IGNORECASE)


def make_user(db, regno, gender, *, matchmaking_enabled=True, last_matchmaking_at=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender=gender,
            matchmaking_enabled=matchmaking_enabled,
            last_matchmaking_at=last_matchmaking_at,
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


def notifications_for(db, regno) -> list:
    with db.sessions() as session:
        return session.scalars(
            select(NotificationRecord)
            .where(NotificationRecord.recipient_regno == regno)
            .order_by(NotificationRecord.created_at.desc())
        ).all()


@pytest.fixture
def db():
    return get_postgres_client()


@pytest.fixture(autouse=True)
def no_rare_same_gender_roll(monkeypatch):
    """The 0.1% same-gender roll would leave a test with no candidate half the time."""
    monkeypatch.setattr(matchmaking_service, "SAME_GENDER_MATCH_CHANCE", 0)


def test_the_match_notice_names_the_partner(db):
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    matchmaking_service.find_match_service(as_user(db, "alice"), db)

    notices = notifications_for(db, "bob")
    assert [n.heading for n in notices] == ["You appeared in matchmaking"]
    assert notices[0].body == "You were matched with User alice"


def test_the_match_notice_embeds_no_clock_reading(db):
    """The regression: the body used to end in 'at September 28, 2026 at 12:00 PM'."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    matchmaking_service.find_match_service(as_user(db, "alice"), db)

    body = notifications_for(db, "bob")[0].body
    assert not CLOCK_READING.search(body), f"notification body embeds a time: {body!r}"


def test_the_match_notice_embeds_no_date_either(db):
    """A bare date is just as wrong: it is equally tied to the server's zone."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    matchmaking_service.find_match_service(as_user(db, "alice"), db)

    body = notifications_for(db, "bob")[0].body
    month = datetime.now(timezone.utc).strftime("%B")
    assert month not in body, f"notification body embeds a month name: {body!r}"


def test_the_notice_carries_a_timezone_aware_timestamp_instead(db):
    """What the client renders locally is the row's created_at, not the body text."""
    make_user(db, "alice", "female")
    make_user(db, "bob", "male")

    matchmaking_service.find_match_service(as_user(db, "alice"), db)

    created_at = notifications_for(db, "bob")[0].created_at
    assert created_at.tzinfo is not None
    # Roughly now; the point is it is a real instant, not a preformatted string.
    assert abs(datetime.now(timezone.utc) - created_at) < timedelta(minutes=5)
