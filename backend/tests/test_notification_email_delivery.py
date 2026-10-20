"""Email delivery gates for participant and staff notifications.

The in-app inbox is deliberately independent of these switches. These tests pin
the delivery rules instead:

* a participant is emailed only when their own ``notifications_enabled`` column
  is true;
* a staff account needs its master switch and the category preference for that
  notice;
* a moderator is never emailed a game notice, even if the stored preference row
  says ``games_enabled = true``;
* a deduplicated staff notice is emailed only once.

Every call to the Functions hand-off is recorded in memory. Nothing here reads a
secret or contacts the Functions host, ACS, or Storage Queue.
"""

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import (
    AdminRecord,
    ModeratorRecord,
    NotificationRecord,
    StaffNotificationPreferenceRecord,
    UserRecord,
)
from app.services import (
    notification_links,
    notification_service,
    staff_notification_service,
)
from app.services.notification_service import create_notification_service
from app.services.report_counters import CONFESSION, record_removal
from app.services.staff_notification_service import (
    create_staff_notification,
    notify_game_pending,
)

ADMIN_ID = "11111111-1111-1111-1111-111111111111"
MODERATOR_ID = "22222222-2222-2222-2222-222222222222"


def make_user(db, regno: str, *, enabled: bool = True) -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2,
            department="Test_Class",
            gender="other",
            notifications_enabled=enabled,
        )
        session.add(row)
        session.flush()
        return str(row.id)


def make_staff(db, model, account_id: str, regno: str, *, enabled: bool = True) -> None:
    with db.sessions.begin() as session:
        session.add(
            model(
                id=account_id,
                regno=regno,
                name=f"Staff {regno}",
                email=f"{regno}@test.com",
                notifications_enabled=enabled,
            )
        )


def set_preference(db, account_type: str, account_id: str, **values) -> None:
    with db.sessions.begin() as session:
        row = session.get(
            StaffNotificationPreferenceRecord, (account_type, account_id)
        )
        assert row is not None
        for key, value in values.items():
            setattr(row, key, value)


def set_staff_enabled(db, model, account_id: str, enabled: bool) -> None:
    with db.sessions.begin() as session:
        row = session.get(model, account_id)
        assert row is not None
        row.notifications_enabled = enabled


def emails(calls):
    return [
        {
            "email": call["email"],
            "title": call["title"],
            "body": call["body"],
            "link": call.get("link"),
        }
        for call in calls
    ]


def test_participant_notification_emails_when_enabled(db, monkeypatch):
    make_user(db, "alice", enabled=True)
    calls = []

    def record(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(
        notification_service, "enqueue_notification_email", record
    )

    notification_id = create_notification_service(
        "alice", "You were matched", "A new match is waiting.", db
    )

    assert notification_id
    assert calls == [
        {
            "title": "You were matched",
            "body": "A new match is waiting.",
            "email": "alice@test.com",
            "name": "User alice",
            "regno": "alice",
        }
    ]


def test_participant_notification_stays_in_app_but_is_not_emailed_when_disabled(
    db, monkeypatch
):
    make_user(db, "bob", enabled=False)
    calls = []
    monkeypatch.setattr(
        notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    notification_id = create_notification_service(
        "bob", "Report reviewed", "Your report was reviewed.", db
    )

    assert notification_id
    assert calls == []
    with db.sessions() as session:
        row = session.scalar(
            select(NotificationRecord).where(
                NotificationRecord.recipient_regno == "bob"
            )
        )
    assert row is not None
    assert row.heading == "Report reviewed"


def test_participant_email_failure_does_not_undo_the_stored_notice(db, monkeypatch):
    make_user(db, "carol", enabled=True)

    def fail(**kwargs):
        raise RuntimeError("Functions host unavailable")

    monkeypatch.setattr(notification_service, "enqueue_notification_email", fail)

    notification_id = create_notification_service(
        "carol", "Report reviewed", "Your report was reviewed.", db
    )

    assert notification_id
    with db.sessions() as session:
        row = session.get(NotificationRecord, notification_id)
    assert row is not None


def test_participant_target_notification_email_links_to_the_item(db, monkeypatch):
    make_user(db, "alice")
    calls = []
    monkeypatch.setattr(
        notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        notification_links,
        "public_frontend_origin",
        lambda: "https://app.example.com",
    )

    notification_id = create_notification_service(
        "alice",
        "Someone replied",
        "Open the conversation.",
        db,
        target_kind="conversation",
        target_id="match-123",
    )

    assert notification_id
    assert calls[0]["link"] == "https://app.example.com/inbox?match=match-123"


def test_staff_report_emails_both_enabled_roles(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    make_staff(db, ModeratorRecord, MODERATOR_ID, "staffmod")
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        notification_links,
        "public_frontend_origin",
        lambda: "https://app.example.com",
    )

    create_staff_notification(
        db,
        audience="all",
        category="report",
        heading="New confession report",
        body="A participant reported a confession.",
        dedup_key="report:all",
    )

    assert {item["email"] for item in emails(calls)} == {
        "staffadmin@test.com",
        "staffmod@test.com",
    }
    assert {item["title"] for item in emails(calls)} == {"New confession report"}
    assert {item["body"] for item in emails(calls)} == {
        "A participant reported a confession."
    }
    assert {item["link"] for item in emails(calls)} == {
        "https://app.example.com/admin/reports"
    }


def test_pending_game_email_links_to_its_manage_screen(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )
    monkeypatch.setattr(
        notification_links,
        "public_frontend_origin",
        lambda: "https://app.example.com",
    )

    notify_game_pending(
        db,
        game_id="wordle",
        session_id="session-123",
        created_at=datetime.now(timezone.utc),
    )

    assert calls[0]["link"] == "https://app.example.com/admin/games/manage/wordle"


def test_staff_master_switch_blocks_every_category(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    make_staff(db, ModeratorRecord, MODERATOR_ID, "staffmod")
    set_staff_enabled(db, ModeratorRecord, MODERATOR_ID, False)
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    create_staff_notification(
        db,
        audience="all",
        category="love_note",
        heading="Love note awaiting approval",
        body="A note is waiting.",
        dedup_key="love-note:master-off",
    )

    assert {item["email"] for item in emails(calls)} == {"staffadmin@test.com"}


def test_admin_master_switch_blocks_every_category(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    make_staff(db, ModeratorRecord, MODERATOR_ID, "staffmod")
    set_staff_enabled(db, AdminRecord, ADMIN_ID, False)
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    create_staff_notification(
        db,
        audience="all",
        category="report",
        heading="New report",
        body="A report was filed.",
        dedup_key="report:admin-master-off",
    )

    assert {item["email"] for item in emails(calls)} == {"staffmod@test.com"}


def test_staff_category_preference_filters_matching_category(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    make_staff(db, ModeratorRecord, MODERATOR_ID, "staffmod")
    set_preference(
        db,
        AccountType.MODERATOR.value,
        MODERATOR_ID,
        love_notes_enabled=False,
    )
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    create_staff_notification(
        db,
        audience="all",
        category="love_note",
        heading="Love note awaiting approval",
        body="A note is waiting.",
        dedup_key="love-note:preference-off",
    )

    assert {item["email"] for item in emails(calls)} == {"staffadmin@test.com"}


def test_moderator_is_never_emailed_a_game_even_with_preference_on(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    make_staff(db, ModeratorRecord, MODERATOR_ID, "staffmod")
    with db.sessions() as session:
        moderator_prefs = session.get(
            StaffNotificationPreferenceRecord,
            (AccountType.MODERATOR.value, MODERATOR_ID),
        )
    assert moderator_prefs.games_enabled is True

    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    # audience="all" is not how game notices are stored today, but the role gate
    # must still protect a hand-written or future audience value.
    create_staff_notification(
        db,
        audience="all",
        category="game",
        heading="Wordle needs a word",
        body="A session is pending.",
        dedup_key="game:moderator-guard",
    )

    assert {item["email"] for item in emails(calls)} == {"staffadmin@test.com"}
    assert all("staffmod" not in item["email"] for item in emails(calls))


def test_admin_game_preference_can_turn_game_email_off(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    set_preference(
        db,
        AccountType.ADMIN.value,
        ADMIN_ID,
        games_enabled=False,
    )
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    create_staff_notification(
        db,
        audience="admin",
        category="game",
        heading="No featured game set for today",
        body="Feature a game.",
        dedup_key="game:admin-off",
    )

    assert calls == []


def test_deduplicated_staff_notice_is_emailed_once(db, monkeypatch):
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    first = create_staff_notification(
        db,
        audience="admin",
        category="report",
        heading="New report",
        body="A report was filed.",
        dedup_key="report:once",
    )
    second = create_staff_notification(
        db,
        audience="admin",
        category="report",
        heading="New report",
        body="A report was filed.",
        dedup_key="report:once",
    )

    assert first == second
    assert len(calls) == 1


def test_auto_block_notice_emails_admins_after_commit(db, monkeypatch):
    person = make_user(db, "person")
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    with db.sessions.begin() as session:
        for _ in range(6):
            record_removal(session, kind=CONFESSION, author_user_id=person)

    assert len(calls) == 1
    assert calls[0]["email"] == "staffadmin@test.com"
    assert calls[0]["title"] == "Participant automatically blocked"
    assert "person" in calls[0]["body"]


def test_auto_block_notice_is_not_emailed_when_the_transaction_rolls_back(
    db, monkeypatch
):
    person = make_user(db, "person")
    make_staff(db, AdminRecord, ADMIN_ID, "staffadmin")
    calls = []
    monkeypatch.setattr(
        staff_notification_service,
        "enqueue_notification_email",
        lambda **kwargs: calls.append(kwargs),
    )

    with pytest.raises(RuntimeError):
        with db.sessions.begin() as session:
            for _ in range(6):
                record_removal(session, kind=CONFESSION, author_user_id=person)
            raise RuntimeError("roll back the block")

    assert calls == []
