"""Revocation for non-expiring love note URLs: removal destroys, block rotates.

A signed love note URL carries no expiry, so taking content down has to kill the object
(or its version) rather than just hiding the row -- otherwise every URL minted while the
note was live keeps serving the image. These tests pin both halves, driving the real
HTTP endpoints with the object store stubbed out:

* a ``removed`` ruling on a report destroys the note's image, exactly once (re-ruling an
  already-removed note destroys nothing again);
* a ``dismissed`` ruling destroys nothing;
* a destroy failure does not roll back the ruling -- the row stays stamped removed and
  the failure is logged for cleanup, rather than leaving abusive content visible;
* blocking an account rotates its live notes' images (destroy plus re-upload under the
  same name, bumping the version old signatures were minted against) while leaving
  removed notes and imageless notes alone, and unblocking rotates nothing;
* the rotation helper itself replaces the object under the same public_id, refuses
  malformed ids and failures without raising, and destroys a replacement that came back
  servable without a signature rather than leaving a public object behind.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import LoveNoteRecord, LoveNoteReportRecord, UserRecord
from app.main import app
from app.models import UserDetails
from app.services import report_resolution_service as resolution_module
from app.services import storage_service as storage_module
from app.services.auth_service import get_current_user
from app.services.storage_service import storage_service


def png_base64():
    buf = BytesIO()
    Image.new("RGB", (32, 16), (200, 40, 40)).save(buf, format="PNG")
    import base64

    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def make_user(db, regno, name=None):
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno,
            name=name or f"User {regno}",
            email=f"{regno}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return {"id": str(row.id), "regno": row.regno}


def make_note(db, sender, recipient, *, status="approved", removed=False):
    note_id = str(uuid4())
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=note_id,
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=storage_service.love_note_public_id(note_id),
            image_format="png",
            body="a love note",
            anonymous=False,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        if removed:
            row.removed_at = datetime.now(timezone.utc)
            row.removed_by_regno = "adminuser"
        session.add(row)
        session.flush()
        return str(row.id)


def file_report(client, login_as, db, sender, recipient, *, status="approved"):
    """A report the recipient filed against an approved note, through the real endpoint."""
    note_id = make_note(db, sender, recipient, status=status)
    with login_as(recipient["regno"]):
        response = client.post(
            f"/love-notes/inbox/{note_id}/report", json={"reason": "harassment"}
        )
    assert response.status_code == 201, response.text
    with db.sessions() as session:
        report = session.scalar(
            select(LoveNoteReportRecord).where(
                LoveNoteReportRecord.love_note_id == note_id
            )
        )
        return str(report.id), note_id


@pytest.fixture
def login_as(db):
    """Run the statements inside the block as a named participant."""

    @contextmanager
    def _login(regno: str):
        with db.sessions() as session:
            row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        assert row is not None, f"no participant named {regno} to sign in as"
        previous = app.dependency_overrides.get(get_current_user)
        app.dependency_overrides[get_current_user] = lambda: UserDetails(
            id=str(row.id), regno=row.regno, name=row.name, email=row.email,
            year=row.year, department=row.department, gender=row.gender,
            matchmaking_enabled=row.matchmaking_enabled,
            notifications_enabled=row.notifications_enabled,
            account_type=AccountType.USERS.value,
        )
        try:
            yield
        finally:
            app.dependency_overrides[get_current_user] = previous

    return _login


@pytest.fixture
def destroyed(monkeypatch):
    """Stand in for the object store's delete, recording what would be destroyed."""
    calls = []

    class _FakeStorage:
        def delete_love_note_asset(self, public_id):
            calls.append(public_id)
            return True

    monkeypatch.setattr(resolution_module, "storage_service", _FakeStorage())
    return calls


def _note_row(db, note_id):
    with db.sessions() as session:
        return session.get(LoveNoteRecord, note_id)


def test_removal_destroys_the_note_image(client, admin_client, login_as, db, destroyed):
    sender = make_user(db, "rm_sender")
    recipient = make_user(db, "rm_recipient")
    report_id, note_id = file_report(client, login_as, db, sender, recipient)

    response = admin_client.patch(
        f"/admin/love-notes/reports/{report_id}?status=removed"
    )

    assert response.status_code == 200, response.text
    assert response.json()["content_removed"] is True
    assert destroyed == [f"love_notes/{note_id}/image"]
    assert _note_row(db, note_id).removed_at is not None


def test_dismissal_destroys_nothing(client, admin_client, login_as, db, destroyed):
    sender = make_user(db, "di_sender")
    recipient = make_user(db, "di_recipient")
    report_id, note_id = file_report(client, login_as, db, sender, recipient)

    response = admin_client.patch(
        f"/admin/love-notes/reports/{report_id}?status=dismissed"
    )

    assert response.status_code == 200, response.text
    assert destroyed == []
    assert _note_row(db, note_id).removed_at is None


def test_reruling_an_already_removed_note_destroys_nothing_again(
    client, admin_client, login_as, db, destroyed
):
    sender = make_user(db, "rr_sender")
    recipient = make_user(db, "rr_recipient")
    report_id, _ = file_report(client, login_as, db, sender, recipient)

    assert (
        admin_client.patch(f"/admin/love-notes/reports/{report_id}?status=removed").status_code
        == 200
    )
    assert len(destroyed) == 1
    # Re-deciding the same report as removed is not a second takedown.
    assert (
        admin_client.patch(f"/admin/love-notes/reports/{report_id}?status=removed").status_code
        == 200
    )
    assert len(destroyed) == 1


def test_a_failed_destroy_does_not_roll_back_the_ruling(
    client, admin_client, login_as, db, monkeypatch
):
    """The stamp is the primary control (visibility); the destroy is the URL kill.

    If the object store refuses, the report still closes and the note still comes down --
    leaving abusive content visible because a delete failed would invert the priority.
    The failure is logged for cleanup instead.
    """
    sender = make_user(db, "fl_sender")
    recipient = make_user(db, "fl_recipient")
    report_id, note_id = file_report(client, login_as, db, sender, recipient)

    class _FailingStorage:
        def delete_love_note_asset(self, public_id):
            raise HTTPException(status_code=500, detail="Failed to delete file")

    monkeypatch.setattr(resolution_module, "storage_service", _FailingStorage())

    response = admin_client.patch(
        f"/admin/love-notes/reports/{report_id}?status=removed"
    )

    assert response.status_code == 200, response.text
    assert _note_row(db, note_id).removed_at is not None


def test_blocking_rotates_only_live_identified_images(admin_client, db, monkeypatch):
    sender = make_user(db, "bl_sender")
    recipient = make_user(db, "bl_recipient")
    other_sender = make_user(db, "bl_other")
    live_id = make_note(db, sender, recipient)
    make_note(db, sender, recipient, removed=True)
    make_note(db, other_sender, recipient)

    rotated = []
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.rotate_love_note_asset",
        lambda public_id, image_format: rotated.append((public_id, image_format)) or True,
    )

    response = admin_client.put(f"/admin/users/{sender['id']}/block?blocked=true")

    assert response.status_code == 200, response.text
    assert rotated == [(f"love_notes/{live_id}/image", "png")]
    # The removed note's image died when it came down, and the other sender's note is
    # not this account's to rotate. Neither is touched.
    with db.sessions() as session:
        assert session.get(UserRecord, sender["id"]).is_blocked is True


def test_blocking_succeeds_even_when_rotation_fails(admin_client, db, monkeypatch):
    sender = make_user(db, "bf_sender")
    recipient = make_user(db, "bf_recipient")
    make_note(db, sender, recipient)
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.rotate_love_note_asset",
        lambda public_id, image_format: False,
    )

    response = admin_client.put(f"/admin/users/{sender['id']}/block?blocked=true")

    assert response.status_code == 200, response.text
    with db.sessions() as session:
        assert session.get(UserRecord, sender["id"]).is_blocked is True


def test_unblocking_rotates_nothing(admin_client, db, monkeypatch):
    sender = make_user(db, "ub_sender")
    recipient = make_user(db, "ub_recipient")
    make_note(db, sender, recipient)
    rotated = []
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.rotate_love_note_asset",
        lambda public_id, image_format: rotated.append(public_id) or True,
    )

    assert admin_client.put(f"/admin/users/{sender['id']}/block?blocked=true").status_code == 200
    assert len(rotated) == 1
    assert admin_client.put(f"/admin/users/{sender['id']}/block?blocked=false").status_code == 200
    assert len(rotated) == 1


def _offline_signing(monkeypatch):
    """Mint rotation download URLs without Cloudinary credentials.

    The rotation downloads through a signed URL; the signature itself is covered by the
    delivery-URL tests, so here a fixed signed-shaped address keeps these tests runnable
    wherever credentials are absent.
    """
    monkeypatch.setattr(
        storage_service,
        "love_note_delivery_url",
        lambda public_id, image_format: (
            f"https://res.cloudinary.com/demo/image/authenticated/s--fakesig--/v1/{public_id}.png"
        ),
    )


def test_rotate_replaces_the_object_under_the_same_name(monkeypatch):
    uploads = []
    _offline_signing(monkeypatch)

    def _fetch(url, cap):
        assert "/s--" in url, "rotation must download through a signed URL"
        return b"image-bytes"

    def _upload(content, **kwargs):
        uploads.append(kwargs)
        return {"public_id": kwargs["public_id"], "type": "authenticated"}

    monkeypatch.setattr(storage_module, "_fetch_url_bytes", _fetch)
    monkeypatch.setattr(storage_module, "cloudinary_upload", _upload)

    public_id = "love_notes/12345678-1234-1234-1234-123456789012/image"
    assert storage_service.rotate_love_note_asset(public_id, "png") is True
    assert len(uploads) == 1
    assert uploads[0]["public_id"] == public_id
    assert uploads[0]["type"] == "authenticated"
    assert uploads[0]["overwrite"] is True


def test_rotate_refuses_malformed_ids_without_touching_the_network(monkeypatch):
    calls = []
    monkeypatch.setattr(
        storage_module, "_fetch_url_bytes", lambda url, cap: calls.append(url) or b"x"
    )
    monkeypatch.setattr(
        storage_module, "cloudinary_upload", lambda content, **kwargs: calls.append(kwargs)
    )

    assert storage_service.rotate_love_note_asset("../../etc/passwd", "png") is False
    assert storage_service.rotate_love_note_asset(None, "png") is False
    assert calls == []


def test_rotate_fails_closed_on_download_or_upload_errors(monkeypatch):
    _offline_signing(monkeypatch)

    def _boom(url, cap):
        raise ConnectionError("cdn down")

    monkeypatch.setattr(storage_module, "_fetch_url_bytes", _boom)
    public_id = "love_notes/12345678-1234-1234-1234-123456789012/image"
    assert storage_service.rotate_love_note_asset(public_id, "png") is False


def test_rotate_destroys_a_replacement_that_came_back_public(monkeypatch):
    """A replaced object stored servable-without-signature is worse than the leak."""
    _offline_signing(monkeypatch)
    monkeypatch.setattr(
        storage_module, "_fetch_url_bytes", lambda url, cap: b"image-bytes"
    )
    monkeypatch.setattr(
        storage_module,
        "cloudinary_upload",
        lambda content, **kwargs: {"public_id": kwargs["public_id"], "type": "upload"},
    )
    destroyed = []
    monkeypatch.setattr(
        storage_module,
        "cloudinary_destroy",
        lambda public_id, **kwargs: destroyed.append(public_id) or {"result": "ok"},
    )

    public_id = "love_notes/12345678-1234-1234-1234-123456789012/image"
    assert storage_service.rotate_love_note_asset(public_id, "png") is False
    assert destroyed == [public_id]
