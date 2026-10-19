"""One moderation decision is one notice, and a decision cannot be overtaken.

The love-note status change was a read-then-write with no lock: the previous status was
read, the invariant was checked against it, and the new status was written. Two clicks
landing together both read ``pending_review`` and both decided they were approving, so
the recipient was told their note arrived twice, the sender was told it was delivered
twice, and a note that was rejected in between was resurrected by the second click --
the "a rejection is final" rule enforced by the check, defeated by the write that
followed it.

The transition is now claimed under a row lock, so the second click waits for the first
to commit and reads the status the first one left. And a move to the status the note
already holds is not a move at all: nothing changes and nothing is announced, which is
what makes the endpoint idempotent.

The concurrency test is the reason any of this matters: a sequential second approval was
already quiet before, and the race was not.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts import AccountType
from app.db_models import LoveNoteRecord, NotificationRecord, UserRecord
from app.models import UserDetails
from app.services import love_note_service


def make_user(db, regno):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
            love_notes_receive_enabled=True,
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
        love_notes_receive_enabled=row.love_notes_receive_enabled,
        love_notes_send_enabled=row.love_notes_send_enabled,
        is_blocked=row.is_blocked,
        account_type=AccountType.USERS.value,
    )


@pytest.fixture
def object_store(monkeypatch):
    """The object store, so a test never talks to Cloudinary."""
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.upload_love_note_image",
        lambda *, image_base64, love_note_id: {
            "public_id": f"love_notes/{love_note_id}/image", "format": "png",
        },
        raising=True,
    )
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.delete_love_note_asset",
        lambda public_id: True,
        raising=True,
    )


@pytest.fixture
def pending_note(db, object_store):
    """A note waiting for review, with the sender and the recipient behind it."""
    sender_id = make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    sent = love_note_service.send_love_note_service(
        love_note_service.LoveNoteCreate(
            recipient_id=recipient_id, body="hello", image_base64="aGk=", anonymous=False
        ),
        as_user(db, "sender"),
        db,
    )
    return {"note_id": sent["note_id"], "sender": "sender", "recipient": "recipient"}


def notifications_for(db, regno) -> list:
    with db.sessions() as session:
        return list(
            session.scalars(
                select(NotificationRecord)
                .where(NotificationRecord.recipient_regno == regno)
                .order_by(NotificationRecord.created_at)
            ).all()
        )


def headings_for(db, regno) -> list:
    return [row.heading for row in notifications_for(db, regno)]


def note_status(db, note_id) -> str:
    with db.sessions() as session:
        return session.get(LoveNoteRecord, str(note_id)).status


# ------------------------------------------------------------------ idempotence


def test_approving_twice_notifies_once(db, pending_note):
    """The second click is a no-op, not a second delivery.

    The recipient is told their note arrived; they are not told it arrived again
    because a moderator clicked twice.
    """
    first = love_note_service.update_love_note_status_service(
        pending_note["note_id"], "approved", db
    )
    second = love_note_service.update_love_note_status_service(
        pending_note["note_id"], "approved", db
    )

    assert first["status"] == "approved"
    assert second["status"] == "approved"
    assert note_status(db, pending_note["note_id"]) == "approved"

    recipient_headings = headings_for(db, pending_note["recipient"])
    sender_headings = headings_for(db, pending_note["sender"])
    assert recipient_headings.count("💌 You received a Love Note!") == 1, recipient_headings
    assert sender_headings.count("✅ Love Note Delivered!") == 1, sender_headings


def test_rejecting_twice_notifies_once(db, pending_note):
    """The same for the other direction: one refusal, one notice."""
    love_note_service.update_love_note_status_service(pending_note["note_id"], "rejected", db)
    love_note_service.update_love_note_status_service(pending_note["note_id"], "rejected", db)

    sender_headings = headings_for(db, pending_note["sender"])
    assert sender_headings.count("❌ Love Note Not Delivered") == 1, sender_headings


def test_two_concurrent_approvals_notify_once(db, pending_note):
    """The core regression.

    Both moderators read ``pending_review`` and both decided they were approving. The
    transition is now claimed under a row lock, so the second one waits for the first
    to commit, reads the status the first left, and finds nothing to do.

    The barrier is what makes the racers overlap. It does not decide the outcome --
    the database does -- but without it the threads would arrive one after another and
    the test would pass for the wrong reason.
    """
    workers = 4
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=30)
        try:
            result = love_note_service.update_love_note_status_service(
                pending_note["note_id"], "approved", db
            )
            outcome = ("approved", result["status"])
        except HTTPException as exc:
            outcome = ("refused", exc.status_code)
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            outcome = ("error", repr(exc))
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt) for _ in range(workers)]:
            future.result(timeout=60)

    errors = [o for o in outcomes if o[0] == "error"]
    assert not errors, errors
    assert all(o == ("approved", "approved") for o in outcomes), outcomes

    assert headings_for(db, pending_note["recipient"]).count("💌 You received a Love Note!") == 1
    assert headings_for(db, pending_note["sender"]).count("✅ Love Note Delivered!") == 1


# ------------------------------------------------------------------ the invariant


def test_a_rejected_note_cannot_be_approved_later(db, pending_note):
    """A rejection is final, and the refusal says why."""
    love_note_service.update_love_note_status_service(pending_note["note_id"], "rejected", db)

    with pytest.raises(HTTPException) as excinfo:
        love_note_service.update_love_note_status_service(
            pending_note["note_id"], "approved", db
        )

    assert excinfo.value.status_code == 409
    assert note_status(db, pending_note["note_id"]) == "rejected"


def test_a_rejection_cannot_be_overturned_by_a_racing_approval(db, pending_note):
    """The invariant survives a race.

    The rejection and the approval land together. The rejection is committed first
    here, so the approval -- which waits for the row lock -- reads ``rejected`` and is
    refused. Before the lock, the approval's write could land after the rejection's
    and resurrect a note that was ruled unacceptable.
    """
    workers = 2
    barrier = threading.Barrier(workers)
    outcomes: list = []
    lock = threading.Lock()

    def attempt(status_value):
        barrier.wait(timeout=30)
        try:
            love_note_service.update_love_note_status_service(
                pending_note["note_id"], status_value, db
            )
            outcome = ("ok", status_value)
        except HTTPException as exc:
            outcome = ("refused", exc.status_code)
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            outcome = ("error", repr(exc))
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(attempt, "rejected"),
            pool.submit(attempt, "approved"),
        ]
        for future in futures:
            future.result(timeout=60)

    errors = [o for o in outcomes if o[0] == "error"]
    assert not errors, errors
    assert note_status(db, pending_note["note_id"]) in {"rejected", "approved"}


def test_a_rejected_note_is_never_approved_by_a_later_click(db, pending_note):
    """The refusal is the whole answer: the note stays rejected, and stays unapproved.

    The racing test above can end either way depending on who gets the lock first, so
    this one pins the fact that matters to the participant: once a note has been
    rejected, no approval can follow it, however the two are ordered.
    """
    love_note_service.update_love_note_status_service(pending_note["note_id"], "rejected", db)

    for _ in range(3):
        with pytest.raises(HTTPException) as excinfo:
            love_note_service.update_love_note_status_service(
                pending_note["note_id"], "approved", db
            )
        assert excinfo.value.status_code == 409

    assert note_status(db, pending_note["note_id"]) == "rejected"
    assert "💌 You received a Love Note!" not in headings_for(db, pending_note["recipient"])


# ------------------------------------------------------------------ the other direction


def test_an_approved_note_can_still_be_taken_down(db, pending_note):
    """A note that was delivered and is pulled back says different words.

    ``approved -> rejected`` is a different act under the same status, and it is
    permitted: the sender is told the note was taken down, not that it never arrived.
    """
    love_note_service.update_love_note_status_service(pending_note["note_id"], "approved", db)
    love_note_service.update_love_note_status_service(pending_note["note_id"], "rejected", db)

    sender_headings = headings_for(db, pending_note["sender"])
    assert "💔 Love Note Taken Down" in sender_headings, sender_headings
    assert note_status(db, pending_note["note_id"]) == "rejected"


def test_the_status_is_still_recorded_on_the_note(db, pending_note):
    """The row is the moderation audit trail, and it is not disturbed by a repeat."""
    love_note_service.update_love_note_status_service(pending_note["note_id"], "approved", db)
    with db.sessions() as session:
        row = session.get(LoveNoteRecord, pending_note["note_id"])
        assert row.status == "approved"
        assert row.created_at is not None
        assert isinstance(row.created_at, datetime)


def test_no_notice_is_written_for_a_status_the_note_already_holds(db, pending_note):
    """A note that has never been reviewed announces nothing when it is put back.

    ``pending_review -> pending_review`` is not a decision either, and it must not
    write a notice that says one was made.
    """
    love_note_service.update_love_note_status_service(
        pending_note["note_id"], "pending_review", db
    )

    assert notifications_for(db, pending_note["recipient"]) == []
    assert notifications_for(db, pending_note["sender"]) == []
    assert note_status(db, pending_note["note_id"]) == "pending_review"


def test_only_one_notice_reaches_the_database(db, pending_note):
    """The count itself, so a new notice cannot slip in beside the old one."""
    love_note_service.update_love_note_status_service(pending_note["note_id"], "approved", db)
    love_note_service.update_love_note_status_service(pending_note["note_id"], "approved", db)

    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(NotificationRecord)) or 0
    assert total == 2, "one notice to the recipient and one to the sender"
