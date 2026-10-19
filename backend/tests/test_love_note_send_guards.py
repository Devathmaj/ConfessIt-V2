"""A love note is one per season, and a recipient's opt-out is the server's rule.

Two things the send screen implies were not the server's rule at all:

* **One note per season.** The season was claimed by reading ``love_notes_send_enabled``
  off the request's user and setting it to true in a later transaction. That is a
  read-then-write across two transactions, so two sends in flight -- a double-click,
  two tabs, a client retry -- both read an unspent season, both believed they held it,
  and both inserted a note. Two notes, two images, and a sender who spent one season
  and got two notes.

* **The recipient's opt-out.** ``GET /love-notes/users`` filters on
  ``love_notes_receive_enabled``, so a sender is never *offered* somebody who opted
  out. But the send path only checked that the recipient row existed, so any client
  that knew a recipient id could post a note to somebody who had turned the feature
  off. A preference the screen honours and the API does not is not a preference.

The season is now claimed by a conditional update inside the note's own transaction,
and the opt-out is read where the note is written. Both rules live in the service
rather than in the router, because the router's version of the season check was a
read on the way in -- which is exactly the read that let two racers through.

The concurrency test is the reason any of this matters: a sequential second send was
already refused before, and the race was not.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.accounts import AccountType
from app.db_models import LoveNoteRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services import love_note_service
from app.services.auth_service import get_current_user


def make_user(
    db,
    regno,
    *,
    love_notes_receive_enabled: bool = True,
    is_blocked: bool = False,
):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
            love_notes_receive_enabled=love_notes_receive_enabled,
            is_blocked=is_blocked,
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


def note_for(recipient_id):
    return love_note_service.LoveNoteCreate(
        recipient_id=recipient_id, body="hello", image_base64="aGk=", anonymous=False
    )


def note_count(db) -> int:
    with db.sessions() as session:
        return session.scalar(
            select(func.count()).select_from(LoveNoteRecord)
        ) or 0


def season_spent(db, regno) -> bool:
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        return row.love_notes_send_enabled


@pytest.fixture
def object_store(monkeypatch):
    """Both ends of the object store, so a test never talks to Cloudinary.

    The delete matters as much as the upload: a refused note discards the image it
    uploaded, and that is a path several of these tests deliberately take. Recording
    both also lets a test assert that a refused note left nothing behind.
    """
    uploaded: list[str] = []
    deleted: list[str] = []

    def _upload(*, image_base64, love_note_id):
        public_id = f"love_notes/{love_note_id}/image"
        uploaded.append(public_id)
        return {"public_id": public_id, "format": "png"}

    def _delete(public_id):
        deleted.append(public_id)
        return True

    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.upload_love_note_image",
        _upload,
        raising=True,
    )
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.delete_love_note_asset",
        _delete,
        raising=True,
    )
    return {"uploaded": uploaded, "deleted": deleted}


@pytest.fixture
def signed_in_as(db):
    """Sign the requests of one test in as a participant who exists."""

    @contextmanager
    def _sign_in(regno):
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: as_user(db, regno)
        try:
            yield
        finally:
            if previous is not None:
                app.dependency_overrides[get_current_user] = previous
            else:
                app.dependency_overrides.pop(get_current_user, None)

    return _sign_in


# --------------------------------------------------------------- one note per season


def test_a_second_send_in_the_same_season_is_refused(db, object_store):
    """The ordinary case: a sender who has already sent gets nothing for a second try."""
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient")

    first = love_note_service.send_love_note_service(
        note_for(recipient_id), as_user(db, "sender"), db
    )
    assert first["note_id"]

    with pytest.raises(HTTPException) as excinfo:
        love_note_service.send_love_note_service(
            note_for(recipient_id), as_user(db, "sender"), db
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == love_note_service.SEASON_ALREADY_CLAIMED
    assert note_count(db) == 1


def test_two_concurrent_sends_from_one_sender_produce_exactly_one_note(db, object_store):
    """The core regression.

    Two sends in flight both read an unspent season and both believed they held it.
    The season is now claimed by a conditional update inside the note's own
    transaction: it matches the row only while the flag is still false, and
    PostgreSQL re-checks that after the row lock is taken, so of two racers exactly
    one sees the claim succeed and the other is refused.

    The barrier is what makes the racers overlap. It does not decide the outcome --
    the database does -- but without it the threads would arrive one after another
    and the test would pass for the wrong reason.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient")

    workers = 4
    barrier = threading.Barrier(workers)
    results: list = []
    lock = threading.Lock()

    def attempt():
        # Read the sender before the barrier, so every racer starts from the same
        # unspent season -- which is the state that used to let them all through.
        sender = as_user(db, "sender")
        barrier.wait(timeout=30)
        try:
            outcome = love_note_service.send_love_note_service(
                note_for(recipient_id), sender, db
            )
        except HTTPException as exc:
            outcome = ("refused", exc.status_code)
        except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
            outcome = ("error", exc)
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(attempt) for _ in range(workers)]:
            future.result(timeout=60)

    sent = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, tuple) and r[0] == "refused"]
    errors = [r for r in results if isinstance(r, tuple) and r[0] == "error"]

    assert not errors, errors
    assert len(sent) == 1, f"expected exactly one note, got {len(sent)}: {results}"
    assert len(sent) + len(refused) == workers, results
    assert all(status == 403 for _, status in refused), results
    assert note_count(db) == 1


def test_a_refused_send_stores_nothing(db, object_store):
    """A refusal is not a note.

    The second send of the season is turned away, and nothing about it reaches the
    database: the one note that exists is still the only one.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    love_note_service.send_love_note_service(
        note_for(recipient_id), as_user(db, "sender"), db
    )

    with pytest.raises(HTTPException) as excinfo:
        love_note_service.send_love_note_service(
            note_for(recipient_id), as_user(db, "sender"), db
        )

    assert excinfo.value.status_code == 403
    assert note_count(db) == 1


def test_a_refused_send_discards_the_image_it_uploaded(db, object_store):
    """The object store is not left holding an image nothing points at.

    The note does not exist, so the image would be unreachable through any URL this
    application will ever produce -- which makes deleting it housekeeping rather than
    a decision about the note, but it still has to happen.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient")
    love_note_service.send_love_note_service(
        note_for(recipient_id), as_user(db, "sender"), db
    )

    with pytest.raises(HTTPException):
        love_note_service.send_love_note_service(
            note_for(recipient_id), as_user(db, "sender"), db
        )

    assert len(object_store["uploaded"]) == 2
    assert len(object_store["deleted"]) == 1
    assert object_store["deleted"][0] == object_store["uploaded"][1]


# ------------------------------------------------------------- the recipient's opt-out


def test_sending_to_a_recipient_who_opted_out_is_refused(db, object_store):
    """The rule the picker implies and the API did not enforce."""
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient", love_notes_receive_enabled=False)

    with pytest.raises(HTTPException) as excinfo:
        love_note_service.send_love_note_service(
            note_for(recipient_id), as_user(db, "sender"), db
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == love_note_service.RECIPIENT_NOT_ACCEPTING
    assert note_count(db) == 0


def test_a_recipient_who_opts_out_mid_send_is_still_refused(db, monkeypatch, object_store):
    """The authoritative reading is the one taken where the note is written.

    The upload happens between the two checks, so flipping the recipient's switch
    inside it reproduces a recipient who opts out while the note is on its way. The
    note must be refused -- and the sender's season must not be spent by the
    refusal, which is what the rollback of the claim is for: the claim and the note
    are one transaction, so a refusal takes the claim with it.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient")

    def _upload(*, image_base64, love_note_id):
        with db.sessions.begin() as session:
            session.get(UserRecord, recipient_id).love_notes_receive_enabled = False
        return {"public_id": f"love_notes/{love_note_id}/image", "format": "png"}

    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.upload_love_note_image",
        _upload,
        raising=True,
    )

    with pytest.raises(HTTPException) as excinfo:
        love_note_service.send_love_note_service(
            note_for(recipient_id), as_user(db, "sender"), db
        )

    assert excinfo.value.status_code == 403
    assert excinfo.value.detail == love_note_service.RECIPIENT_NOT_ACCEPTING
    assert note_count(db) == 0
    assert season_spent(db, "sender") is False


def test_a_refused_opt_out_does_not_spend_the_senders_season(db, object_store):
    """Being refused is not the same as having sent.

    A sender turned away by a recipient's opt-out may still send their one note to
    somebody else -- otherwise a single refused attempt would cost the season.
    """
    make_user(db, "sender")
    opted_out = make_user(db, "optedout", love_notes_receive_enabled=False)
    willing = make_user(db, "willing")

    with pytest.raises(HTTPException):
        love_note_service.send_love_note_service(
            note_for(opted_out), as_user(db, "sender"), db
        )
    assert season_spent(db, "sender") is False

    sent = love_note_service.send_love_note_service(
        note_for(willing), as_user(db, "sender"), db
    )
    assert sent["note_id"]
    assert note_count(db) == 1


def test_the_api_refuses_a_recipient_who_opted_out(db, client, object_store, signed_in_as):
    """The refusal is the server's, not the picker's.

    The picker already hides an opted-out account, so the screen never offers one.
    This is the request that bypasses the screen: the recipient id is known, the
    payload is well formed, and the answer is still no.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "recipient", love_notes_receive_enabled=False)

    with signed_in_as("sender"):
        response = client.post(
            "/love-notes/send",
            json={
                "recipient_id": recipient_id,
                "image_base64": "aGk=",
                "body": "hello",
                "anonymous": False,
            },
        )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == love_note_service.RECIPIENT_NOT_ACCEPTING
    assert note_count(db) == 0


def test_a_blocked_recipient_can_still_be_sent_a_note(db, object_store):
    """Blocking is not opting out, and the two are not the same switch.

    A blocked account may still receive a love note: love notes are moderated
    independently of matchmaking, and the blocked flag is what makes the *client*
    ask the sender to confirm before the note goes out. Turning that into a
    server-side refusal would be a different product decision, taken by accident.
    """
    make_user(db, "sender")
    recipient_id = make_user(db, "blocked", is_blocked=True)

    sent = love_note_service.send_love_note_service(
        note_for(recipient_id), as_user(db, "sender"), db
    )

    assert sent["note_id"]
    assert note_count(db) == 1
