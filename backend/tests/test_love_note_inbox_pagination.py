"""The love-note inbox is strictly paginated on the database itself.

``GET /love-notes/inbox`` used to answer every approved note for the reader in one
response, each carrying a freshly minted image URL. A much-loved reader collects an
unbounded inbox, and every open of it minted URLs for all of it. These tests pin the
replacement contract:

* every read answers ``{notes, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the inbox;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the newest-first order, not a Python
  slice of a full load;
* the total counts the same visible notes, so the screen pages downwards knowing how
  many remain -- while removed notes stay out of the page and out of the total, and a
  reader never sees another reader's notes.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import LoveNoteRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services.auth_service import get_current_user
from app.services.storage_service import storage_service


def make_user(db, regno):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class",
            gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno}


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


@pytest.fixture
def login_as(db):
    """Run the calls inside the block as a named participant."""
    from contextlib import contextmanager

    @contextmanager
    def _login(regno):
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: as_user(db, regno)
        try:
            yield
        finally:
            if previous is not None:
                app.dependency_overrides[get_current_user] = previous
            else:
                app.dependency_overrides.pop(get_current_user, None)

    return _login


def make_note(db, sender, recipient, *, body="a love note", status="approved",
                created_at=None):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=None,
            image_format="png",
            body=body,
            anonymous=False,
            status=status,
            created_at=created_at or datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


def test_the_inbox_answers_a_page_envelope_not_a_list(db, client, login_as):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    make_note(db, sender, recipient, body="thinking of you")

    with login_as("recipient"):
        response = client.get("/love-notes/inbox")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"notes", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert [note["body"] for note in body["notes"]] == ["thinking of you"]


def test_limit_is_clamped(db, client, login_as):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    make_note(db, sender, recipient)

    with login_as("recipient"):
        assert client.get("/love-notes/inbox", params={"limit": 1000}).status_code == 422


def test_skip_and_limit_walk_newest_first(db, client, login_as):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    base = datetime.now(timezone.utc)
    for index in range(5):
        # Staggered stamps rather than insertion order: the walk must follow the clock,
        # and a coarse clock could stamp rapid inserts identically.
        make_note(db, sender, recipient, body=f"verse {index}",
                  created_at=base - timedelta(minutes=5 - index))

    with login_as("recipient"):
        first = client.get("/love-notes/inbox", params={"skip": 0, "limit": 2}).json()
        second = client.get("/love-notes/inbox", params={"skip": 2, "limit": 2}).json()
        third = client.get("/love-notes/inbox", params={"skip": 4, "limit": 2}).json()

    assert first["total"] == 5
    assert len(first["notes"]) == 2
    assert len(second["notes"]) == 2
    assert len(third["notes"]) == 1
    seen = [note["id"] for page in (first, second, third) for note in page["notes"]]
    assert len(set(seen)) == 5
    # Newest first: the walk meets the latest verse on its first page.
    assert first["notes"][0]["body"] == "verse 4"


def test_removed_and_unapproved_notes_stay_out_of_page_and_total(db, client, login_as):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    make_note(db, sender, recipient, body="still here")
    make_note(db, sender, recipient, body="never approved", status="pending_review")

    with login_as("recipient"):
        page = client.get("/love-notes/inbox").json()

    assert page["total"] == 1
    assert [note["body"] for note in page["notes"]] == ["still here"]


def test_a_reader_never_sees_another_readers_notes(db, client, login_as):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    stranger = make_user(db, "stranger")
    make_note(db, sender, recipient, body="not for you")

    with login_as("stranger"):
        page = client.get("/love-notes/inbox").json()

    assert page["total"] == 0
    assert page["notes"] == []
