"""What a profile picture has to be before it is allowed to become one.

Three rules are pinned here, and they are the ones a request cannot be trusted to apply for
itself:

* the *bytes* decide what a file is. The filename and the ``Content-Type`` are chosen by
  whoever sent it, so neither takes part in the decision -- a body of arbitrary bytes claiming
  ``image/png`` is refused by exactly the code that accepts an honest one;
* a refusal stops before Cloudinary. Size, type, geometry and the cooldown all run first, so a
  rejected file costs nothing but the request;
* only the caller's own row is written. The service is handed an account, never a target.

Normalisation is pinned as well: what reaches storage is the picture and not the file's
metadata, with the EXIF orientation already baked into the pixels it can no longer carry.
"""

import io
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import select
from starlette.datastructures import Headers

from app.accounts import AccountType
from app.db_models import UserRecord
from app.models import UserDetails
from app.services.profile_service import upload_profile_picture_service
from app.services.storage_service import storage_service

# Large enough that truncating it still leaves a header PIL can read, which is the only way to
# reach the decode check rather than being turned away as "not an image" one step earlier.
NOISE_SIDE = 256


@pytest.fixture
def cloudinary(monkeypatch):
    """The real upload path, with only the network replaced.

    Validation, the object id, the re-encode and the delivery URL are all still production
    code, so what ``uploads`` records is what would have left the process. A rejection test
    that finds this list empty was refused before the SDK was called, not merely before the
    bytes were sent.
    """
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)

    uploads: list[SimpleNamespace] = []
    destroyed: list[str] = []

    def _upload(file_obj, **kwargs):
        uploads.append(
            SimpleNamespace(
                public_id=kwargs["public_id"],
                filename=kwargs.get("filename"),
                raw=file_obj.read(),
            )
        )
        return {
            "public_id": kwargs["public_id"],
            "format": (kwargs.get("filename") or "").rsplit(".", 1)[-1],
        }

    def _destroy(public_id, **kwargs):
        destroyed.append(public_id)
        return {"result": "ok"}

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_upload", _upload, raising=True
    )
    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    monkeypatch.setattr(
        storage_service, "profile_delivery_url", lambda *a, **k: "https://cdn.example/p.png",
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


def upload_file(content: bytes, *, filename: str = "me.png",
                content_type: str | None = None) -> UploadFile:
    headers = Headers({"content-type": content_type}) if content_type else None
    return UploadFile(filename=filename, file=io.BytesIO(content), headers=headers)


def image_bytes(fmt: str, width: int = 32, height: int = 32, *, noise: bool = False) -> bytes:
    if noise:
        mode = "L" if fmt == "gif" else "RGB"
        channels = 1 if mode == "L" else 3
        image = Image.frombytes(mode, (width, height), os.urandom(width * height * channels))
    else:
        image = Image.new("RGB", (width, height), "red")
        if fmt == "gif":
            image = image.convert("P")
    buf = io.BytesIO()
    image.save(buf, format="JPEG" if fmt == "jpeg" else fmt.upper())
    return buf.getvalue()


def oriented_jpeg() -> bytes:
    """A 20x8 JPEG whose EXIF says it is sideways, and names the camera that took it."""
    image = Image.new("RGB", (20, 8), "red")
    exif = image.getexif()
    exif[0x0112] = 6  # Orientation: the right side is the top
    exif[0x010F] = "TestCam"  # Make
    exif[0x0110] = "ModelX"  # Model
    buf = io.BytesIO()
    image.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


def picture_state(db, regno: str):
    """``(profile_picture_public_id, last_profile_picture_at)``; ``(None, None)`` if there
    is no row at all, which is a state of its own rather than an error."""
    with db.sessions() as session:
        row = session.scalar(select(UserRecord).where(UserRecord.regno == regno))
        if row is None:
            return None, None
        return row.profile_picture_public_id, row.last_profile_picture_at


# ---------------------------------------------------------------------------
# Accepted: one per format the rule allows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["png", "jpeg", "gif"])
async def test_a_real_image_of_each_allowed_format_is_stored(db, cloudinary, fmt):
    make_user(db)

    result = await upload_profile_picture_service(
        as_user(), db, upload_file(image_bytes(fmt), filename=f"photo.{fmt}",
                                   content_type=f"image/{fmt}")
    )

    assert result["message"] == "Profile picture uploaded successfully"
    assert len(cloudinary.uploads) == 1
    # The format of the stored object comes from the bytes, and the bytes were re-encoded.
    assert cloudinary.uploads[0].filename == f"profile.{'jpeg' if fmt == 'jpeg' else fmt}"
    stored = Image.open(io.BytesIO(cloudinary.uploads[0].raw))
    assert stored.format.lower() in {"png", "jpeg", "gif"}

    public_id, changed_at = picture_state(db, "alice")
    assert public_id.startswith("profile_pics/alice/")
    assert changed_at is not None


@pytest.mark.asyncio
async def test_the_filename_and_content_type_do_not_decide_the_format(db, cloudinary):
    """A PNG called ``photo.jpg`` is stored as a png, because only its bytes were asked."""
    make_user(db)

    await upload_profile_picture_service(
        as_user(),
        db,
        upload_file(image_bytes("png"), filename="photo.jpg", content_type="image/jpeg"),
    )

    assert cloudinary.uploads[0].filename == "profile.png"


@pytest.mark.asyncio
async def test_metadata_does_not_reach_the_stored_object(db, cloudinary):
    """EXIF is stripped, and the orientation it carried is already in the pixels.

    A phone photo arrives sideways with a tag saying so. Dropping the tag without rotating
    the picture would store it the wrong way up, so the two halves of normalisation are
    pinned together here.
    """
    make_user(db)

    await upload_profile_picture_service(
        as_user(),
        db,
        upload_file(oriented_jpeg(), filename="IMG_0001.jpg", content_type="image/jpeg"),
    )

    stored = Image.open(io.BytesIO(cloudinary.uploads[0].raw))
    assert stored.size == (8, 20), "the orientation should have been baked into the pixels"
    assert not bytes(stored.getexif()), "EXIF should not survive the re-encode"
    assert "exif" not in stored.info


# ---------------------------------------------------------------------------
# Rejected: every one of these must stop before Cloudinary
# ---------------------------------------------------------------------------


def _rejected_case(case: str):
    """``(content, filename, content_type, expected status, fragment of the message)``."""
    if case == "bytes claiming to be a png":
        return (b"not an image, whatever the header says" * 4, "photo.png", "image/png",
                400, "Invalid file type")
    if case == "bytes claiming to be a jpeg":
        return (b"not an image, whatever the header says" * 4, "photo.jpg", "image/jpeg",
                400, "Invalid file type")
    if case == "a truncated png":
        truncated = image_bytes("png", NOISE_SIDE, NOISE_SIDE, noise=True)
        return (truncated[:-50], "photo.png", "image/png", 400, "Invalid image data")
    if case == "an oversized upload":
        return (b"\0" * (storage_service.max_image_bytes + 1), "photo.png", "image/png",
                413, "File too large")
    if case == "an image past the width limit":
        return (image_bytes("png", storage_service.max_image_dimension + 1, 1),
                "photo.png", "image/png", 413, "dimensions")
    if case == "an image past the pixel limit":
        # Each side is within the per-side cap; only the total is over.
        return (image_bytes("png", 4000, 3500), "photo.png", "image/png", 413, "total size")
    raise AssertionError(f"unknown case {case}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "bytes claiming to be a png",
        "bytes claiming to be a jpeg",
        "a truncated png",
        "an oversized upload",
        "an image past the width limit",
        "an image past the pixel limit",
    ],
)
async def test_a_rejected_file_never_reaches_cloudinary(db, cloudinary, case):
    """Refused, and refused before anything was stored or any window was claimed."""
    make_user(db)
    content, filename, content_type, expected_status, fragment = _rejected_case(case)

    with pytest.raises(HTTPException) as excinfo:
        await upload_profile_picture_service(
            as_user(),
            db,
            upload_file(content, filename=filename, content_type=content_type),
        )

    assert excinfo.value.status_code == expected_status, excinfo.value.detail
    assert fragment in excinfo.value.detail

    assert cloudinary.uploads == [], "a rejected file was handed to the storage provider"
    assert cloudinary.destroyed == []

    # Neither half of the participant's one change was spent by the refusal.
    public_id, changed_at = picture_state(db, "alice")
    assert public_id is None
    assert changed_at is None


# ---------------------------------------------------------------------------
# Authorisation: the caller's own account, and nobody else's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_callers_own_profile_is_written(db, cloudinary):
    make_user(db, "alice")
    make_user(db, "bob")

    await upload_profile_picture_service(
        as_user("alice"), db, upload_file(image_bytes("png"))
    )

    assert all("/alice/" in upload.public_id for upload in cloudinary.uploads)
    assert picture_state(db, "bob") == (None, None)


@pytest.mark.asyncio
async def test_a_session_with_no_account_row_is_refused(db, cloudinary):
    """Authenticated, but there is no row for the account to be authorised against.

    The object had already been stored by the time the row was looked up, so it is
    discarded: nothing names it and no later request can find it.
    """
    with pytest.raises(HTTPException) as excinfo:
        await upload_profile_picture_service(
            as_user("ghost"), db, upload_file(image_bytes("png"))
        )

    assert excinfo.value.status_code == 403
    assert len(cloudinary.uploads) == 1
    assert cloudinary.destroyed == [cloudinary.uploads[0].public_id]
    assert picture_state(db, "ghost") == (None, None)


def test_an_admin_cannot_use_the_participant_upload_route(admin_client):
    response = admin_client.post(
        "/profile/upload-picture",
        files={"file": ("photo.png", image_bytes("png"), "image/png")},
    )

    assert response.status_code == 403


def test_an_unauthenticated_upload_is_refused(client):
    """No token, no upload -- the route never reaches the service.

    The autouse auth override is popped for the length of the request and put back by the
    fixture that installed it, so this is the real dependency rather than a stub of it.
    """
    from app.main import app
    from app.services.auth_service import get_current_user
    from conftest import override_get_current_user

    app.dependency_overrides.pop(get_current_user, None)
    try:
        response = client.post(
            "/profile/upload-picture",
            files={"file": ("photo.png", image_bytes("png"), "image/png")},
        )
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user

    assert response.status_code in {401, 403}
