"""Role-based staff notifications for admins and moderators.

Participant notices are addressed to one regno; staff notices are addressed to
a *role*. These tests pin that contract:

* filing any report (confession, comment, chat message, love note) stores one
  ``report`` notice with ``audience='all'``, visible to both roles, exactly
  once per report (dedup);
* a love note awaiting approval stores one ``love_note`` notice for both roles;
* game alerts (no featured game today, a manual-mode session waiting for its
  word) are computed live for admins only -- they appear while the condition
  holds and vanish when fixed, with no stored row;
* moderators see ``report`` + ``love_note`` only: the ``game`` category is
  refused, and the ``games_enabled`` switch is unwritable for them;
* read state is per account, and the master on/off switch writes the
  account's own ``notifications_enabled``;
* participants are refused everywhere.

The auth override is app-global, so one test never holds two identities at
once: ``act_as`` names exactly who is calling for each block.
"""

import contextlib
from datetime import date, datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    AdminRecord,
    ConfessionRecord,
    FeaturedGameRecord,
    GameConfigRecord,
    LoveNoteRecord,
    ModeratorRecord,
    StaffNotificationPreferenceRecord,
    StaffNotificationRecord,
    UserRecord,
    WordleGameRecord,
)
from app.main import app
from app.models import AccountDetails, UserDetails
from app.services.auth_service import get_current_user
from app.services.staff_notification_service import (
    create_staff_notification,
    notify_love_note_pending,
    notify_report_filed,
)

ADMIN_ID = "11111111-1111-1111-1111-111111111111"
MODERATOR_ID = "22222222-2222-2222-2222-222222222222"
MODERATOR_TWO_ID = "33333333-3333-3333-3333-333333333333"

ADMIN = {"account_id": ADMIN_ID, "regno": "staffadmin", "account_type": AccountType.ADMIN.value}
MODERATOR = {"account_id": MODERATOR_ID, "regno": "staffmod",
             "account_type": AccountType.MODERATOR.value}
MODERATOR_TWO = {"account_id": MODERATOR_TWO_ID, "regno": "staffmod2",
                 "account_type": AccountType.MODERATOR.value}


def make_user(db, regno, name=None):
    name = name or f"User {regno}"
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=name, email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno, "name": name}


def make_staff(db, *, account_id, regno, model):
    with db.sessions.begin() as session:
        session.add(model(id=account_id, regno=regno, name=regno, email=f"{regno}@test.com"))


@contextlib.contextmanager
def act_as(user=None, **staff):
    """Resolve ``get_current_user`` to one account for the block."""
    previous = app.dependency_overrides.get(get_current_user)
    if user is not None:
        details = UserDetails(
            id=user["id"], regno=user["regno"], name=user["name"],
            email=f"{user['regno']}@test.com", year=2, department="Test_Class",
            gender="other", account_type=AccountType.USERS.value,
        )
    else:
        details = AccountDetails(
            id=staff["account_id"], regno=staff["regno"], name=staff["regno"],
            email=f"{staff['regno']}@test.com", account_type=staff["account_type"],
        )
    app.dependency_overrides[get_current_user] = lambda: details
    try:
        yield
    finally:
        if previous is not None:
            app.dependency_overrides[get_current_user] = previous
        else:
            app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def api(db):
    make_staff(db, account_id=ADMIN_ID, regno="staffadmin", model=AdminRecord)
    make_staff(db, account_id=MODERATOR_ID, regno="staffmod", model=ModeratorRecord)
    make_staff(db, account_id=MODERATOR_TWO_ID, regno="staffmod2", model=ModeratorRecord)
    yield TestClient(app, follow_redirects=False)


def report_page(api, **identity):
    with act_as(**identity):
        response = api.get("/staff/notifications", params={"category": "report"})
    assert response.status_code == 200, response.text
    return response.json()


def test_report_hook_notifies_both_roles_exactly_once(db, api):
    author = make_user(db, "author")
    reporter = make_user(db, "reporter")
    with db.sessions.begin() as session:
        session.add(ConfessionRecord(
            body="a reportable confession", anonymous=False, comments_enabled=False,
            created_at=datetime.now(timezone.utc), author_id=author["id"],
        ))
    with db.sessions() as session:
        confession_id = session.scalar(select(ConfessionRecord.id))

    with act_as(user=reporter):
        response = api.post(f"/confessions/{confession_id}/report", json={"reason": "spam"})
    assert response.status_code == 200, response.text

    for identity in (ADMIN, MODERATOR):
        page = report_page(api, **identity)
        assert page["total"] == 1, page
        notice = page["notifications"][0]
        assert notice["category"] == "report"
        assert notice["audience"] == "all"
        assert "report" in notice["heading"].lower()

    # The hook is idempotent on the report: a retried notify call for the same
    # report id stores nothing new.
    with db.sessions() as session:
        stored = session.scalars(select(StaffNotificationRecord)).all()
    assert len(stored) == 1
    assert notify_report_filed(db, kind="confession", report_id="retry-me", summary="x") is not None
    assert notify_report_filed(db, kind="confession", report_id="retry-me", summary="x") is not None
    with db.sessions() as session:
        dupes = session.scalars(
            select(StaffNotificationRecord).where(
                StaffNotificationRecord.dedup_key == "report:retry-me")
        ).all()
    assert len(dupes) == 1


def test_love_note_pending_notifies_both_roles(db, api):
    sender = make_user(db, "sender")
    recipient = make_user(db, "recipient")
    with db.sessions.begin() as session:
        session.add(LoveNoteRecord(
            sender_id=sender["id"], recipient_id=recipient["id"],
            image_public_id="love_notes/note/image", image_format="png",
            body="a note", anonymous=False, status="approved",
            created_at=datetime.now(timezone.utc),
        ))
    with db.sessions() as session:
        note_id = session.scalar(select(LoveNoteRecord.id))

    assert notify_love_note_pending(db, note_id=str(note_id)) is not None

    for identity in (ADMIN, MODERATOR):
        with act_as(**identity):
            page = api.get("/staff/notifications", params={"category": "love_note"}).json()
        assert page["total"] == 1, page
        assert page["notifications"][0]["category"] == "love_note"


def test_moderator_cannot_reach_game_category(db, api):
    with act_as(**MODERATOR):
        assert api.get("/staff/notifications", params={"category": "game"}).status_code == 403
        page = api.get("/staff/notifications").json()
    assert set(page["categories"]) == {"report", "love_note"}
    assert all(n["category"] != "game" for n in page["notifications"])


def test_featured_missing_alert_is_live_and_admin_only(db, api):
    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "game"}).json()
    assert page["total"] >= 1
    missing = [n for n in page["notifications"] if n["id"].startswith("game:featured-missing")]
    assert len(missing) == 1
    assert missing[0]["is_live"] is True
    assert missing[0]["is_read"] is False

    # Fixing it clears the alert with no stored row left behind.
    with db.sessions.begin() as session:
        session.add(FeaturedGameRecord(
            game_date=date.today(), game_id="wordle", created_by="staffadmin"))
    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "game"}).json()
    assert all(not n["id"].startswith("game:featured-missing") for n in page["notifications"])
    with db.sessions() as session:
        assert session.scalars(
            select(StaffNotificationRecord).where(StaffNotificationRecord.category == "game")
        ).all() == []


def test_manual_wordle_pending_alert(db, api):
    with db.sessions.begin() as session:
        session.add(GameConfigRecord(game_id="wordle", content_mode="manual"))
        session.add(WordleGameRecord(
            game_date=date.today(), mode="custom", status="pending"))
        # A featured pick for today keeps the game page focused on the pending word.
        session.add(FeaturedGameRecord(
            game_date=date.today(), game_id="wordle", created_by="staffadmin"))
    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "game"}).json()
    pending = [n for n in page["notifications"] if n["id"].startswith("game:wordle-pending")]
    assert len(pending) == 1
    assert "word" in pending[0]["heading"].lower()


def test_preferences_admin_three_moderator_two(db, api):
    with act_as(**ADMIN):
        body = api.put("/staff/notifications/preferences", json={
            "reports_enabled": False, "love_notes_enabled": False, "games_enabled": False,
        }).json()
    assert body["preferences"] == {
        "reports_enabled": False, "love_notes_enabled": False, "games_enabled": False,
    }
    # The switches record intent; the feed still shows everything.
    with act_as(**ADMIN):
        page = api.get("/staff/notifications").json()
    assert page["preferences"]["reports_enabled"] is False

    with act_as(**MODERATOR):
        assert api.put(
            "/staff/notifications/preferences", json={"games_enabled": False}).status_code == 422
        assert api.put(
            "/staff/notifications/preferences", json={"bogus": True}).status_code == 422
        body = api.put(
            "/staff/notifications/preferences", json={"reports_enabled": False}).json()
    assert body["preferences"]["reports_enabled"] is False
    assert body["preferences"]["games_enabled"] is True


def test_enabled_switch_writes_own_row(db, api):
    reporter = make_user(db, "reporter")
    with act_as(**ADMIN):
        assert api.put("/staff/notifications/enabled", json={"enabled": False}).json() == {
            "notifications_enabled": False}
    with db.sessions() as session:
        assert session.get(AdminRecord, ADMIN_ID).notifications_enabled is False
    with act_as(**ADMIN):
        assert api.get("/staff/notifications").json()["notifications_enabled"] is False

    with act_as(**MODERATOR):
        assert api.put("/staff/notifications/enabled", json={"enabled": False}).json() == {
            "notifications_enabled": False}
    with act_as(user=reporter):
        assert api.put("/staff/notifications/enabled", json={"enabled": False}).status_code == 403


def test_read_state_is_per_account(db, api):
    create_staff_notification(db, audience="all", category="report",
                              heading="H", body="B", dedup_key="report:per-account")
    with db.sessions() as session:
        stored_id = session.scalar(
            select(StaffNotificationRecord.id).where(
                StaffNotificationRecord.dedup_key == "report:per-account"))

    with act_as(**MODERATOR):
        assert api.put("/staff/notifications/no-such-id/read").status_code == 404
        assert api.put(f"/staff/notifications/{stored_id}/read").status_code == 200
        assert api.get("/staff/notifications", params={"unread_only": True}).json()["total"] == 0

    # A second moderator with a different account still sees it unread...
    with act_as(**MODERATOR_TWO):
        assert api.get("/staff/notifications", params={"unread_only": True}).json()["total"] == 1

    # ...and so does the admin (a different role, same audience) -- beside the
    # live game alerts, which are always unread and computed, never stored.
    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"unread_only": True}).json()
    stored = [n for n in page["notifications"] if not n["is_live"]]
    assert len(stored) == 1
    assert stored[0]["is_read"] is False
    assert any(n["is_live"] and n["category"] == "game" for n in page["notifications"])


def test_participant_is_refused_everywhere(db, api):
    reporter = make_user(db, "reporter")
    with act_as(user=reporter):
        assert api.get("/staff/notifications").status_code == 403
        assert api.get("/staff/notifications/preferences").status_code == 403
        assert api.put("/staff/notifications/preferences", json={}).status_code == 403
        assert api.get("/staff/notifications/unread-count").status_code == 403


def test_preferences_default_to_all_on(db, api):
    with db.sessions() as session:
        assert session.get(StaffNotificationPreferenceRecord, ("admin", ADMIN_ID)) is None
    with act_as(**ADMIN):
        page = api.get("/staff/notifications").json()
    assert page["preferences"] == {
        "reports_enabled": True, "love_notes_enabled": True, "games_enabled": True,
    }
    assert page["notifications_enabled"] is True
