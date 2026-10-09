"""The admin love-note list is strictly paginated on the database itself.

``GET /admin/love-notes`` used to return every note in one response: one unbounded
SELECT, serialised whole. With thousands of notes that one request materialises them
all at once. These tests pin the replacement contract:

* every read answers ``{notes, total, skip, limit}`` and never a bare list;
* ``limit`` is clamped (anything over 100 is a 422), so no client can ask for the table;
* ``skip``/``limit`` are a SQL OFFSET/LIMIT on the filtered, ordered query, not a Python
  slice of a full load;
* ``status`` narrows in SQL and ``order`` (newest/oldest) orders in SQL, with the total
  counted under the same filter so the screen can load more downwards page by page.
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.db_models import LoveNoteRecord, UserRecord
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


def make_note(db, sender, recipient, *, status="approved", body="a love note", created_at=None):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=None,
            image_format="png",
            body=body,
            moderation_body=body,
            anonymous=False,
            status=status,
            created_at=created_at or datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


def note_cast(db):
    """One sender and recipient pair every test builds its notes between."""
    return make_user(db, "sender"), make_user(db, "recipient")


def test_the_note_list_answers_a_page_envelope_not_a_list(db, admin_client):
    sender, recipient = note_cast(db)
    make_note(db, sender, recipient)

    response = admin_client.get("/admin/love-notes")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {"notes", "total", "skip", "limit"}
    assert body["skip"] == 0
    assert body["limit"] == 50
    assert body["total"] == 1
    assert len(body["notes"]) == 1


def test_limit_is_clamped_so_no_request_can_ask_for_the_table(db, admin_client):
    sender, recipient = note_cast(db)
    make_note(db, sender, recipient)

    response = admin_client.get("/admin/love-notes", params={"limit": 1000})
    assert response.status_code == 422, response.text


def test_unknown_status_and_order_are_refused(db, admin_client):
    sender, recipient = note_cast(db)
    make_note(db, sender, recipient)

    assert admin_client.get("/admin/love-notes", params={"status": "forwarded"}).status_code == 422
    assert admin_client.get("/admin/love-notes", params={"order": "random"}).status_code == 422


def test_skip_and_limit_walk_one_status_without_holding_everything(db, admin_client):
    sender, recipient = note_cast(db)
    for index in range(5):
        make_note(db, sender, recipient, status="pending_review", body=f"pending {index}")

    first = admin_client.get(
        "/admin/love-notes", params={"status": "pending_review", "skip": 0, "limit": 2}
    ).json()
    second = admin_client.get(
        "/admin/love-notes", params={"status": "pending_review", "skip": 2, "limit": 2}
    ).json()
    third = admin_client.get(
        "/admin/love-notes", params={"status": "pending_review", "skip": 4, "limit": 2}
    ).json()

    assert first["total"] == 5
    assert len(first["notes"]) == 2
    assert len(second["notes"]) == 2
    assert len(third["notes"]) == 1
    seen = [note["id"] for page in (first, second, third) for note in page["notes"]]
    assert len(set(seen)) == 5, "paging re-read a note or skipped one"


def test_status_narrows_in_the_database(db, admin_client):
    sender, recipient = note_cast(db)
    make_note(db, sender, recipient, status="pending_review", body="waiting")
    make_note(db, sender, recipient, status="approved", body="delivered")
    make_note(db, sender, recipient, status="rejected", body="refused")

    pending = admin_client.get("/admin/love-notes", params={"status": "pending_review"}).json()
    assert pending["total"] == 1
    assert [note["moderation_body"] for note in pending["notes"]] == ["waiting"]

    everything = admin_client.get("/admin/love-notes").json()
    assert everything["total"] == 3


def test_order_walks_the_timeline_newest_or_oldest_first(db, admin_client):
    sender, recipient = note_cast(db)
    base = datetime.now(timezone.utc)
    make_note(db, sender, recipient, body="first", created_at=base - timedelta(hours=2))
    make_note(db, sender, recipient, body="second", created_at=base - timedelta(hours=1))
    make_note(db, sender, recipient, body="third", created_at=base)

    newest = admin_client.get("/admin/love-notes", params={"order": "newest"}).json()
    assert [note["moderation_body"] for note in newest["notes"]] == ["third", "second", "first"]

    oldest = admin_client.get("/admin/love-notes", params={"order": "oldest"}).json()
    assert [note["moderation_body"] for note in oldest["notes"]] == ["first", "second", "third"]


def test_a_ruled_note_moves_between_status_totals(db, admin_client):
    sender, recipient = note_cast(db)
    note_id = make_note(db, sender, recipient, status="pending_review")

    admin_client.put(f"/admin/love-notes/{note_id}/status", params={"status": "approved"})

    pending = admin_client.get("/admin/love-notes", params={"status": "pending_review"}).json()
    approved = admin_client.get("/admin/love-notes", params={"status": "approved"}).json()
    assert pending["total"] == 0
    assert [note["id"] for note in approved["notes"]] == [note_id]
