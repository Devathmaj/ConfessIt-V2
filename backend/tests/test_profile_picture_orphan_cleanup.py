"""A picture that was uploaded but never claimed is a picture nobody owns.

The profile-picture upload stores the object in Cloudinary and *then* writes the row
that names it. If the write fails, the object exists and no row names it: the
participant keeps their old picture, the new one is unreachable, and nothing will ever
delete it. The cleanup that drops such an object ran only for an ``HTTPException`` --
a refusal -- and not for a database fault, which rolls the transaction back in exactly
the same way.

So every upload that failed on the database leaked a Cloudinary object, silently. The
storage grew by one object per failed upload, and nothing in the stack said so.

The cleanup now covers every way out of the transaction, and the original failure is
re-raised: a database fault is still a database fault, and answering it as something
else would hide it.
"""

import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.accounts import AccountType
from app.db_models import UserRecord
from app.models import UserDetails
from app.services.profile_service import upload_profile_picture_service
from app.services.storage_service import storage_service

# A real 1x1 PNG, so these tests run through the real validator rather than around it.
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


@pytest.fixture
def live_storage(monkeypatch):
    """Stand in for Cloudinary: record what was stored and what was destroyed."""
    uploads: list[str] = []
    destroyed: list[str] = []

    def _upload(*, content, owner_regno):
        public_id = (
            f"profile_pics/{owner_regno}/Stored{len(uploads) + 1:04d}IdUnique/profile"
        )
        uploads.append(public_id)
        return {"public_id": public_id, "format": "png"}

    def _delete(public_id, **kwargs):
        destroyed.append(public_id)
        return True

    monkeypatch.setattr(storage_service, "upload_profile_image", _upload, raising=True)
    monkeypatch.setattr(storage_service, "delete_profile_asset", _delete, raising=True)
    monkeypatch.setattr(
        storage_service, "profile_delivery_url", lambda *a, **k: "https://cdn.example/x.png",
        raising=True,
    )
    return SimpleNamespace(uploads=uploads, destroyed=destroyed)


def make_user(db, regno: str = "alice") -> str:
    with db.sessions.begin() as session:
        row = UserRecord(
            regno=regno, name=f"User {regno}", email=f"{regno.lower()}@test.com",
            year=2, department="Test_Class", gender="other",
        )
        session.add(row)
        session.flush()
        return str(row.id)


def as_user(regno: str = "alice") -> UserDetails:
    return UserDetails(
        regno=regno, name="Test User", email=f"{regno.lower()}@test.com",
        year=2, department="Test_Class", gender="other",
        account_type=AccountType.USERS.value,
    )


def upload_file(content: bytes = ONE_PIXEL_PNG, filename: str = "me.png") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(content))


async def change_picture(db, *, regno: str = "alice", content: bytes = ONE_PIXEL_PNG):
    return await upload_profile_picture_service(as_user(regno), db, upload_file(content))


def stored_picture(db, regno: str = "alice"):
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        return row.profile_picture_public_id


def database_fault(*args, **kwargs):
    """A query that fails the way a dead connection fails."""
    raise OperationalError("SELECT 1", {}, Exception("connection reset by peer"))


# ------------------------------------------------------------------ the database fault


@pytest.mark.asyncio
async def test_a_database_fault_after_the_upload_discards_the_object(db, live_storage, monkeypatch):
    """The core regression.

    The upload succeeded, so the object exists. The write that would have named it
    failed, so nothing names it. The object has to be dropped, or it is a leak that
    nothing will ever clean up.
    """
    make_user(db)
    monkeypatch.setattr(
        "app.services.profile_service._consume_picture_attempt", database_fault, raising=True
    )

    with pytest.raises(OperationalError):
        await change_picture(db)

    assert len(live_storage.uploads) == 1, live_storage.uploads
    assert live_storage.destroyed == live_storage.uploads, live_storage.destroyed


@pytest.mark.asyncio
async def test_the_fault_is_still_raised_as_a_fault(db, live_storage, monkeypatch):
    """A database fault is not turned into something else.

    The cleanup runs on the way out, and the original exception is re-raised: a
    database fault is still a database fault, and answering it as a refusal would
    hide it from the logs and from the client.
    """
    make_user(db)
    monkeypatch.setattr(
        "app.services.profile_service._consume_picture_attempt", database_fault, raising=True
    )

    with pytest.raises(OperationalError) as excinfo:
        await change_picture(db)

    assert "connection reset" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_row_still_names_the_previous_picture(db, live_storage, monkeypatch):
    """The failed change changed nothing.

    The transaction rolled back, so the row still names the picture the participant
    had before -- not the one that was uploaded and thrown away.
    """
    make_user(db)
    with db.sessions.begin() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == "alice"))
        row.profile_picture_public_id = "profile_pics/alice/PreviousIdUnique/profile"
        row.profile_picture_format = "png"

    monkeypatch.setattr(
        "app.services.profile_service._consume_picture_attempt", database_fault, raising=True
    )

    with pytest.raises(OperationalError):
        await change_picture(db)

    assert stored_picture(db) == "profile_pics/alice/PreviousIdUnique/profile"


@pytest.mark.asyncio
async def test_a_failed_cleanup_does_not_hide_the_fault(db, live_storage, monkeypatch):
    """A cleanup that itself fails is still only a cleanup problem.

    The object is left behind in that case, which is a storage leak rather than a
    user-visible fault -- so the database fault is the one that is reported, and the
    original exception is what comes out.
    """
    make_user(db)
    monkeypatch.setattr(
        "app.services.profile_service._consume_picture_attempt", database_fault, raising=True
    )

    def _failing_delete(public_id, **kwargs):
        raise RuntimeError("the object store is down too")

    monkeypatch.setattr(storage_service, "delete_profile_asset", _failing_delete, raising=True)

    with pytest.raises(OperationalError):
        await change_picture(db)


# ------------------------------------------------------------------ the refusal, unchanged


@pytest.mark.asyncio
async def test_a_refusal_still_discards_the_object(db, live_storage, monkeypatch):
    """The refusal path keeps working exactly as it did.

    The refusal (here: the account vanished between the pre-check and the claim) is
    an ``HTTPException``, and it was always cleaned up. This pins that the broader
    cleanup did not change the refusal's behaviour.
    """
    make_user(db)
    monkeypatch.setattr(
        "app.services.profile_service._consume_picture_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(HTTPException(status_code=403, detail="no")),
        raising=True,
    )

    with pytest.raises(HTTPException):
        await change_picture(db)

    assert len(live_storage.uploads) == 1
    assert live_storage.destroyed == live_storage.uploads


@pytest.mark.asyncio
async def test_a_successful_upload_names_the_object_and_discards_nothing(db, live_storage):
    """The ordinary path is untouched: one object stored, none destroyed."""
    make_user(db)

    await change_picture(db)

    assert len(live_storage.uploads) == 1
    assert live_storage.destroyed == []
    assert stored_picture(db) == live_storage.uploads[0]
