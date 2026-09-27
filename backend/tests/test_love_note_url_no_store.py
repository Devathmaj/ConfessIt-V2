"""Signed love note URLs do not expire, so the responses that carry them must not be cached.

This is the direct price of the delivery mechanism chosen for this plan. A signed Cloudinary URL
never expires and cannot be retracted, so a copy deposited in a browser or proxy cache is a
capability that outlives the session that legitimately earned it -- and there is nothing a
revalidation (``no-cache``) could fall back on, because no server here can revoke the URL. Only
``no-store`` actually keeps the URL out of the cache, which is why the header is asserted as
exactly that rather than as any kind of cache-control.

Every response that mints one of these URLs has to carry the header, or it becomes the hole in
the design. There are three such responses today:

* ``POST /love-notes/send`` -- hands the URL to the sender;
* ``GET /love-notes/inbox`` -- hands a whole list of them to the recipient;
* ``GET /admin/love-notes/reports`` -- hands the reported note's URL to a reviewer.

Each has a test below, and each test asserts the header on a response that genuinely carries a
URL, so a future change that stops minting the URL cannot make the assertion pass vacuously.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import select

from app.accounts import AccountType
from app.db_models import LoveNoteRecord, UserRecord
from app.main import app
from app.models import UserDetails
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


def make_note(db, sender, recipient, *, status="approved"):
    with db.sessions.begin() as session:
        row = LoveNoteRecord(
            id=str(uuid4()),
            sender_id=sender["id"],
            recipient_id=recipient["id"],
            image_public_id=None,
            image_format="png",
            body="a love note",
            anonymous=False,
            status=status,
            created_at=datetime.now(timezone.utc),
        )
        row.image_public_id = storage_service.love_note_public_id(str(row.id))
        session.add(row)
        session.flush()
        return str(row.id)


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


def test_the_send_response_is_not_cached(monkeypatch, client, login_as, db):
    """``POST /love-notes/send`` mints the sender a URL; the response must not be cached.

    The upload is stubbed so the test never talks to Cloudinary -- the header is a property of
    the HTTP response, not of the object store, and that is what is being pinned.
    """
    sender = make_user(db, "send_sender")
    recipient = make_user(db, "send_recipient")
    monkeypatch.setattr(
        "app.services.love_note_service.storage_service.upload_love_note_image",
        lambda *, image_base64, love_note_id: {
            "public_id": f"love_notes/{love_note_id}/image",
            "format": "png",
        },
        raising=True,
    )

    with login_as("send_sender"):
        response = client.post(
            "/love-notes/send",
            json={
                "recipient_id": recipient["id"],
                "image_base64": png_base64(),
                "body": "hello",
                "anonymous": False,
            },
        )

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    url = response.json()["image_url"]
    assert "/s--" in url, "the header under test must be on a response that carries a URL"


def test_the_inbox_response_is_not_cached(client, login_as, db):
    """``GET /love-notes/inbox`` is where URLs *cluster*; none of them may be stored."""
    sender = make_user(db, "inbox_sender")
    recipient = make_user(db, "inbox_recipient")
    make_note(db, sender, recipient, status="approved")

    with login_as("inbox_recipient"):
        response = client.get("/love-notes/inbox")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert len(response.json()["notes"]) == 1
    assert "/s--" in response.json()["notes"][0]["image_url"]


def test_the_report_queue_response_is_not_cached(client, admin_client, login_as, db):
    """``GET /admin/love-notes/reports`` hands a reviewer the reported note's URL."""
    sender = make_user(db, "queue_sender")
    recipient = make_user(db, "queue_recipient")
    note_id = make_note(db, sender, recipient, status="approved")

    with login_as("queue_recipient"):
        assert (
            client.post(f"/love-notes/inbox/{note_id}/report", json={"reason": "harassment"}).status_code
            == 201
        )

    response = admin_client.get("/admin/love-notes/reports")

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    reports = response.json()["reports"]
    assert len(reports) == 1
    assert "/s--" in reports[0]["image_url"]