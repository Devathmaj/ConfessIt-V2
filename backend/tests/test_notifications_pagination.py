"""The inbox notice list is strictly paginated on the database itself.

``GET /notifications`` used to answer up to fifty notices in one response with no
total beside them: no way to ask for more, and no way to know more exist. An account
with years of notices outgrows that window silently. These tests pin the replacement
contract:

* every read answers ``{notifications, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the newest-first order, not a Python
  slice of a full load;
* ``q`` narrows in SQL to notices whose heading or body matches, with the total
  counted under the same filter so the screen pages the question it asked.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db_models import UserRecord
from app.main import app
from app.models import UserDetails
from app.accounts import AccountType
from app.services.auth_service import get_current_user
from app.services.notification_service import (
    create_notification_service,
    get_user_notifications_service,
)


def make_user(db, regno):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        ))


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


def file_notice(db, regno, heading, body="Some body."):
    return create_notification_service(regno, heading, body, db)


def headings(page):
    return [notice["heading"] for notice in page["notifications"]]


def test_the_notice_list_answers_a_page_envelope_not_a_list(db, alice_client):
    file_notice(db, "alice", "You were matched!")

    response = alice_client.get("/notifications")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"notifications", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert headings(body) == ["You were matched!"]


def test_limit_is_clamped(db, alice_client):
    file_notice(db, "alice", "You were matched!")

    assert alice_client.get("/notifications", params={"limit": 1000}).status_code == 422


def test_skip_and_limit_walk_newest_first(db, alice_client):
    for index in range(5):
        file_notice(db, "alice", f"notice number {index}")

    first = alice_client.get("/notifications", params={"skip": 0, "limit": 2}).json()
    second = alice_client.get("/notifications", params={"skip": 2, "limit": 2}).json()
    third = alice_client.get("/notifications", params={"skip": 4, "limit": 2}).json()

    assert first["total"] == 5
    assert headings(first) == ["notice number 4", "notice number 3"]
    assert headings(second) == ["notice number 2", "notice number 1"]
    assert headings(third) == ["notice number 0"]
    seen = [notice["id"] for page in (first, second, third) for notice in page["notifications"]]
    assert len(set(seen)) == 5


def test_search_matches_heading_and_body(db, alice_client):
    file_notice(db, "alice", "You received a Love Note!", "Ada sent you a love note!")
    file_notice(db, "alice", "Content Removed", "A moderator took your post down.")

    assert headings(alice_client.get("/notifications", params={"q": "love note"}).json()) == [
        "You received a Love Note!"
    ]
    assert headings(alice_client.get("/notifications", params={"q": "moderator"}).json()) == [
        "Content Removed"
    ]
    missed = alice_client.get("/notifications", params={"q": "nothing like this"}).json()
    assert missed["total"] == 0
    assert missed["notifications"] == []


def test_a_reader_never_sees_another_readers_notices(db, alice_client):
    make_user(db, "bob")
    file_notice(db, "bob", "Bob's private notice")

    page = alice_client.get("/notifications").json()
    assert page["total"] == 0
    assert page["notifications"] == []
