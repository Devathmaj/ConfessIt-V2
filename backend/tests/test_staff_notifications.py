"""Role-based staff notifications for admins and moderators.

Participant notices are addressed to one regno; staff notices are addressed to
a *role*. These tests pin that contract:

* filing any report (confession, comment, chat message, love note) stores one
  ``report`` notice with ``audience='all'``, visible to both roles, exactly
  once per report (dedup);
* a love note awaiting approval stores one ``love_note`` notice for both roles;
* game notices (no featured game today, a manual-mode session waiting for its
  word, an empty Trivia Drop pipeline) are stored rows for admins only, written
  by the daily scheduler pass or at application start-up with the pass time as
  ``created_at``, and deduplicated by session id or UTC date;
* moderators see ``report`` + ``love_note`` only: the ``game`` category is
  refused, and the ``games_enabled`` switch is unwritable for them;
* creating an admin or a moderator writes an all-on preferences row for it,
  and the boot backfill repairs staff rows that predate that hook;
* the delivery gate refuses a moderator ``game`` by role even when the stored
  row says ``games_enabled = true``;
* read state is per account, and the master on/off switch writes the
  account's own ``notifications_enabled``;
* participants are refused everywhere.

The auth override is app-global, so one test never holds two identities at
once: ``act_as`` names exactly who is calling for each block.
"""

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

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
    TriviaDropQuestionRecord,
    UserRecord,
)
from app.main import app
from app.models import AccountDetails, UserDetails
from app.services.auth_service import get_current_user
from app.services.game_scheduler import run_reset_cycle
from app.services.report_counters import CONFESSION, record_removal
from app.services.staff_notification_service import (
    create_staff_notification,
    notify_love_note_pending,
    notify_report_filed,
    notify_trivia_drop_empty,
    should_deliver_staff_notification,
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


def test_auto_block_notice_reaches_admins_only(db, api):
    """The automatic block at the removal threshold stores an admin-only notice.

    Driven through ``record_removal`` itself -- the one funnel every removal path passes
    through -- so the test pins where the notice is staged rather than re-implementing the
    threshold. The audience is the point: an admin can clear the block, a moderator cannot,
    so only the admin's page carries it.
    """
    person = make_user(db, "person")
    with db.sessions.begin() as session:
        for _ in range(6):
            record_removal(session, kind=CONFESSION, author_user_id=person["id"])

    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "report"}).json()
    assert page["total"] == 1, page
    notice = page["notifications"][0]
    assert notice["audience"] == "admin"
    assert notice["category"] == "report"
    assert person["regno"] in notice["body"]

    with act_as(**MODERATOR):
        page = api.get("/staff/notifications", params={"category": "report"}).json()
    assert page["total"] == 0, page


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


def test_scheduler_records_featured_missing_notice(db, api):
    moment = datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)
    dedup_key = f"game:featured-missing:{moment.date().isoformat()}"

    run_reset_cycle(db, moment)

    with db.sessions() as session:
        notice = session.scalar(
            select(StaffNotificationRecord).where(
                StaffNotificationRecord.dedup_key == dedup_key
            )
        )
    assert notice is not None
    assert notice.audience == "admin"
    assert notice.category == "game"
    assert notice.created_at == moment

    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "game"}).json()
    serialised = next(n for n in page["notifications"] if n["id"] == str(notice.id))
    assert serialised["is_live"] is False
    assert serialised["is_read"] is False

    # Stored notices now carry read state like every other category.
    with act_as(**ADMIN):
        assert api.put(f"/staff/notifications/{notice.id}/read").status_code == 200
        unread = api.get(
            "/staff/notifications", params={"category": "game", "unread_only": True}
        ).json()
    assert str(notice.id) not in {n["id"] for n in unread["notifications"]}

    # A catch-up pass on the same UTC date writes nothing new.
    run_reset_cycle(db, moment + timedelta(hours=1))
    with db.sessions() as session:
        assert len(
            session.scalars(
                select(StaffNotificationRecord).where(
                    StaffNotificationRecord.dedup_key == dedup_key
                )
            ).all()
        ) == 1


def test_scheduler_skips_featured_notice_when_a_pick_exists(db, api):
    moment = datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)
    dedup_key = f"game:featured-missing:{moment.date().isoformat()}"
    with db.sessions.begin() as session:
        session.add(FeaturedGameRecord(
            game_date=moment.date(), game_id="wordle", created_by="staffadmin"))

    run_reset_cycle(db, moment)

    with db.sessions() as session:
        assert session.scalar(
            select(StaffNotificationRecord.id).where(
                StaffNotificationRecord.dedup_key == dedup_key
            )
        ) is None


def test_scheduler_ignores_a_featured_pick_for_another_date(db, api):
    """Scheduling tomorrow's pick does not silence today's missing-featured notice."""
    moment = datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)
    dedup_key = f"game:featured-missing:{moment.date().isoformat()}"
    with db.sessions.begin() as session:
        session.add(FeaturedGameRecord(
            game_date=moment.date() + timedelta(days=1),
            game_id="wordle", created_by="staffadmin"))

    run_reset_cycle(db, moment)

    with db.sessions() as session:
        assert session.scalar(
            select(StaffNotificationRecord.id).where(
                StaffNotificationRecord.dedup_key == dedup_key
            )
        ) is not None


def test_scheduler_records_pending_game_notices(db, api):
    moment = datetime(2026, 10, 8, 0, 1, tzinfo=timezone.utc)
    with db.sessions.begin() as session:
        session.add(GameConfigRecord(game_id="wordle", content_mode="manual"))
        session.add(GameConfigRecord(game_id="anagram", content_mode="manual"))
        # Connections is manual-only; the fresh config row is already pending.
        # Keep the game page focused on the pending content, not the featured card.
        session.add(FeaturedGameRecord(
            game_date=moment.date(), game_id="wordle", created_by="staffadmin"))

    summary = run_reset_cycle(db, moment)
    expected = {
        "wordle": "Wordle",
        "anagram": "Anagram",
        "connections": "Connections",
    }
    dedup_keys = {}
    for game_id, label in expected.items():
        session_id = summary["games"][game_id]["session_id"]
        assert summary["games"][game_id]["status"] == "pending"
        dedup_keys[game_id] = f"game:pending:{game_id}:{session_id}"

    with db.sessions() as session:
        notices = {
            game_id: session.scalar(
                select(StaffNotificationRecord).where(
                    StaffNotificationRecord.dedup_key == dedup_key
                )
            )
            for game_id, dedup_key in dedup_keys.items()
        }
    for game_id, label in expected.items():
        notice = notices[game_id]
        assert notice is not None
        assert notice.audience == "admin"
        assert notice.category == "game"
        assert notice.created_at == moment
        assert label in notice.heading
    assert "puzzle" in notices["connections"].heading.lower()

    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"category": "game"}).json()
    serialised = {
        n["id"]: n for n in page["notifications"] if n["id"] in {
            str(notice.id) for notice in notices.values()
        }
    }
    assert len(serialised) == 3
    assert all(n["is_live"] is False for n in serialised.values())

    # The next scheduler pass sees the same pending sessions and writes nothing.
    run_reset_cycle(db, moment + timedelta(hours=1))
    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(StaffNotificationRecord)
                .where(
                    StaffNotificationRecord.dedup_key.in_(list(dedup_keys.values()))
                )
            )
            == 3
        )


def test_startup_trivia_notice_is_stored_and_deduped(db, api):
    moment = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
    dedup_key = f"game:trivia-empty:{moment.date().isoformat()}"

    first = notify_trivia_drop_empty(db, now=moment)
    second = notify_trivia_drop_empty(db, now=moment + timedelta(hours=3))
    assert first is not None
    assert second == first

    with db.sessions() as session:
        notices = session.scalars(
            select(StaffNotificationRecord).where(
                StaffNotificationRecord.dedup_key == dedup_key
            )
        ).all()
    assert len(notices) == 1
    assert notices[0].audience == "admin"
    assert notices[0].category == "game"
    assert notices[0].created_at == moment
    assert "Trivia Drop" in notices[0].heading

    # Once a question is live or scheduled there is nothing to report.
    with db.sessions.begin() as session:
        session.add(TriviaDropQuestionRecord(
            question_text="Which one is live?", status="scheduled",
            created_by="staffadmin",
        ))
    assert notify_trivia_drop_empty(db, now=moment + timedelta(days=1)) is None


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
    game_id = create_staff_notification(
        db, audience="admin", category="game",
        heading="G", body="B", dedup_key="game:per-account",
    )
    assert game_id is not None
    with db.sessions() as session:
        stored_id = str(session.scalar(
            select(StaffNotificationRecord.id).where(
                StaffNotificationRecord.dedup_key == "report:per-account")))

    with act_as(**MODERATOR):
        assert api.put("/staff/notifications/no-such-id/read").status_code == 404
        assert api.put(f"/staff/notifications/{stored_id}/read").status_code == 200
        assert api.get("/staff/notifications", params={"unread_only": True}).json()["total"] == 0

    # A second moderator with a different account still sees the report unread.
    with act_as(**MODERATOR_TWO):
        assert api.get("/staff/notifications", params={"unread_only": True}).json()["total"] == 1

    # The admin has the report plus its own stored game notice; marking the
    # game notice read affects only the admin, and only that row.
    with act_as(**ADMIN):
        page = api.get("/staff/notifications", params={"unread_only": True}).json()
    assert page["total"] == 2
    assert all(n["is_live"] is False for n in page["notifications"])
    assert next(n for n in page["notifications"] if n["id"] == game_id)["is_read"] is False

    with act_as(**ADMIN):
        assert api.put(f"/staff/notifications/{game_id}/read").status_code == 200
        page = api.get("/staff/notifications", params={"unread_only": True}).json()
    assert {n["id"] for n in page["notifications"]} == {stored_id}


def test_participant_is_refused_everywhere(db, api):
    reporter = make_user(db, "reporter")
    with act_as(user=reporter):
        assert api.get("/staff/notifications").status_code == 403
        assert api.get("/staff/notifications/preferences").status_code == 403
        assert api.put("/staff/notifications/preferences", json={}).status_code == 403
        assert api.get("/staff/notifications/unread-count").status_code == 403


def test_staff_creation_writes_all_on_preferences(db, api):
    with db.sessions() as session:
        rows = {
            (row.account_type, row.account_id): row
            for row in session.scalars(select(StaffNotificationPreferenceRecord)).all()
        }
    assert set(rows) == {
        ("admin", ADMIN_ID),
        ("moderator", MODERATOR_ID),
        ("moderator", MODERATOR_TWO_ID),
    }
    for row in rows.values():
        assert (
            row.reports_enabled,
            row.love_notes_enabled,
            row.games_enabled,
        ) == (True, True, True)

    with act_as(**ADMIN):
        page = api.get("/staff/notifications").json()
    assert page["preferences"] == {
        "reports_enabled": True, "love_notes_enabled": True, "games_enabled": True,
    }
    assert page["notifications_enabled"] is True


def test_new_staff_account_gets_all_on_preferences(db, api):
    """The API creation path writes the row in the same transaction as the account."""
    for account_type, regno in (
        ("admin", "created-admin"),
        ("moderator", "created-moderator"),
    ):
        with act_as(**ADMIN):
            response = api.post(
                "/admin/users",
                json={
                    "account_type": account_type,
                    "regno": regno,
                    "name": regno,
                    "email": f"{regno}@test.com",
                },
            )
        assert response.status_code == 201, response.text

        with db.sessions() as session:
            row = session.get(
                StaffNotificationPreferenceRecord,
                (account_type, response.json()["id"]),
            )
        assert row is not None
        assert (
            row.reports_enabled,
            row.love_notes_enabled,
            row.games_enabled,
        ) == (True, True, True)


def test_boot_backfill_repairs_missing_staff_preferences(db, api):
    with db.sessions.begin() as session:
        session.execute(delete(StaffNotificationPreferenceRecord))

    db.backfill_staff_notification_preferences()

    with db.sessions() as session:
        rows = session.scalars(select(StaffNotificationPreferenceRecord)).all()
    assert {(row.account_type, row.account_id) for row in rows} == {
        ("admin", ADMIN_ID),
        ("moderator", MODERATOR_ID),
        ("moderator", MODERATOR_TWO_ID),
    }
    assert all(
        row.reports_enabled and row.love_notes_enabled and row.games_enabled
        for row in rows
    )

    # Safe to run on every boot: a second pass inserts nothing.
    db.backfill_staff_notification_preferences()
    with db.sessions() as session:
        assert len(session.scalars(select(StaffNotificationPreferenceRecord)).all()) == 3


def test_delivery_gate_refuses_game_to_moderator_by_role(db, api):
    """A stored ``games_enabled = true`` is not permission to send a moderator games."""
    with db.sessions() as session:
        moderator_prefs = session.get(
            StaffNotificationPreferenceRecord, ("moderator", MODERATOR_ID)
        )
        assert moderator_prefs.games_enabled is True

    assert should_deliver_staff_notification(
        db, account_type="moderator", account_id=MODERATOR_ID, category="game"
    ) is False
    assert should_deliver_staff_notification(
        db, account_type="moderator", account_id=MODERATOR_ID, category="report"
    ) is True
    assert should_deliver_staff_notification(
        db, account_type="admin", account_id=ADMIN_ID, category="game"
    ) is True

    # The switch still means something for the role that may receive the
    # category: an admin who turns games off is not sent them.
    with db.sessions.begin() as session:
        admin_prefs = session.get(
            StaffNotificationPreferenceRecord, ("admin", ADMIN_ID)
        )
        admin_prefs.games_enabled = False
    assert should_deliver_staff_notification(
        db, account_type="admin", account_id=ADMIN_ID, category="game"
    ) is False
    assert should_deliver_staff_notification(
        db, account_type="admin", account_id=ADMIN_ID, category="report"
    ) is True


def test_delivery_gate_honours_master_and_category_switches(db, api):
    with db.sessions.begin() as session:
        session.get(ModeratorRecord, MODERATOR_ID).notifications_enabled = False
    assert should_deliver_staff_notification(
        db, account_type="moderator", account_id=MODERATOR_ID, category="report"
    ) is False

    with db.sessions.begin() as session:
        session.get(ModeratorRecord, MODERATOR_ID).notifications_enabled = True
        session.get(
            StaffNotificationPreferenceRecord, ("moderator", MODERATOR_ID)
        ).reports_enabled = False
    assert should_deliver_staff_notification(
        db, account_type="moderator", account_id=MODERATOR_ID, category="report"
    ) is False
    assert should_deliver_staff_notification(
        db, account_type="moderator", account_id=MODERATOR_ID, category="love_note"
    ) is True
