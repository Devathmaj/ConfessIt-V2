"""Tests for profile-asset ownership on the delete path.

The read path already refuses to serve an asset the requesting account does not own
(``asset_delivery_service``). These tests pin the matching rule for destruction: an
account must not be able to wipe an object belonging to somebody else, even when the
stored ``profile_picture_url`` names it.

The stored value is treated as untrusted throughout, because it is not only ever written
by the owner: an admin creating a user can set that column to any string
(``routers/admin.py``), and the Cloudinary URL parser accepts any host.
"""

import logging
import os
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, UploadFile

from app.services import profile_service
from app.services.storage_service import storage_service

OWNER = "22CS045"
OTHER = "22CS099"

OWNED_PATH = f"{OWNER}/tok3n/profile_20260101_120000_000000.png"
FOREIGN_PATH = f"{OTHER}/tok9z9/profile_20260101_130000_000000.png"


def cloudinary_url_for(object_path: str) -> str:
    return (
        f"https://res.cloudinary.com/{storage_service.cloud_name}"
        f"/image/upload/v1767268800/{storage_service.bucket_name}/{object_path}"
    )


@pytest.fixture
def enabled(monkeypatch):
    """Force the delete path to be live so ``enabled`` never short-circuits the test."""
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    return storage_service


@pytest.fixture
def destroyed(monkeypatch, enabled):
    """Record every Cloudinary destroy call instead of performing it."""
    calls = []

    def _destroy(public_id, resource_type="image", invalidate=False, **kwargs):
        calls.append(public_id)
        return {"result": "ok"}

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )
    return calls


def expected_public_id(object_path: str) -> str:
    """The public_id the destroy call should carry (extension is dropped by design)."""
    return storage_service.build_public_id(storage_service.bucket_name, object_path)


def test_own_asset_is_deleted(enabled, destroyed):
    assert storage_service.delete_profile_asset(cloudinary_url_for(OWNED_PATH), owner_regno=OWNER)
    assert destroyed == [expected_public_id(OWNED_PATH)]


def test_foreign_asset_is_never_destroyed(enabled, destroyed):
    """The core rule: knowing the URL is not permission to delete it."""
    assert storage_service.delete_profile_asset(
        cloudinary_url_for(FOREIGN_PATH), owner_regno=OWNER
    ) is False
    assert destroyed == []


def test_foreign_asset_supplied_as_a_bare_path_is_refused(enabled, destroyed):
    assert storage_service.delete_profile_asset(FOREIGN_PATH, owner_regno=OWNER) is False
    assert destroyed == []


def test_foreign_asset_supplied_through_a_foreign_host_is_refused(enabled, destroyed):
    """The URL parser ignores the host, so a lookalike domain must not help an attacker.

    The URL keeps the real segment layout on purpose: an unparseable URL would be
    rejected earlier by the parser, and the test would pass without ever reaching the
    ownership check it exists to exercise.
    """
    spoofed = (
        f"https://evil.example.invalid/{storage_service.cloud_name}"
        f"/image/upload/v1767268800/{storage_service.bucket_name}/{FOREIGN_PATH}"
    )
    # Confirm the spoofed URL really does parse to the foreign object, so the assertion
    # below is about ownership and not about the URL being rejected as malformed.
    assert storage_service.normalize_stored_path(spoofed, "profile") == FOREIGN_PATH

    assert storage_service.delete_profile_asset(spoofed, owner_regno=OWNER) is False
    assert destroyed == []


def test_missing_owner_cannot_delete_anything(enabled, destroyed):
    """Without a known owner there is nothing to check against, so refuse."""
    assert storage_service.delete_profile_asset(cloudinary_url_for(OWNED_PATH), owner_regno="") is False
    assert destroyed == []


def test_refusal_is_logged_for_audit(enabled, destroyed, caplog):
    # app.logger.get_logger() sets propagate=False so a module's records are not
    # re-delivered by the root logger, which also means caplog's root handler never
    # sees them. Attach the capture handler to the module logger itself so the audit
    # assertion still observes the real record.
    target = logging.getLogger("app.services.storage_service")
    target.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger="app.services.storage_service"):
            storage_service.delete_profile_asset(cloudinary_url_for(FOREIGN_PATH), owner_regno=OWNER)
    finally:
        target.removeHandler(caplog.handler)

    refusals = [r for r in caplog.records if "Refused profile asset delete" in r.getMessage()]
    assert len(refusals) == 1
    message = refusals[0].getMessage()
    # The audit line has to name both sides, or it is not actionable after the fact.
    assert OWNER in message
    assert OTHER in message


def test_owner_match_uses_a_whole_segment_not_a_prefix(enabled, destroyed):
    """``22CS0455`` must not be treated as owning ``22CS045``'s folder."""
    lookalike_owner = OWNER + "5"
    assert storage_service.delete_profile_asset(
        cloudinary_url_for(OWNED_PATH), owner_regno=lookalike_owner
    ) is False
    assert destroyed == []


# ---------------------------------------------------------------------------
# Ordering: the previous object must survive a failed replacement.
# ---------------------------------------------------------------------------


def _upload_stub(monkeypatch, *, fail: bool):
    def _upload(*, file, content, user_folder):
        if fail:
            raise HTTPException(status_code=500, detail="Failed to upload profile image: upstream down")
        path = f"{user_folder}/newtok/new_20260101_140000_000000.png"
        return {
            "object_path": path,
            "cloudinary_url": cloudinary_url_for(path),
            "secure_url": cloudinary_url_for(path),
        }

    monkeypatch.setattr(storage_service, "upload_profile_image", _upload, raising=True)
    monkeypatch.setattr(storage_service, "verify_uploaded_asset", lambda *a, **k: True, raising=True)


def _user_details(regno, picture_url):
    return Mock(
        regno=regno,
        profile_picture_url=picture_url,
    )


def _fake_db():
    db = Mock()
    session = Mock()
    session.scalar.return_value = Mock(profile_picture_url=None)
    ctx = Mock()
    ctx.__enter__ = Mock(return_value=session)
    ctx.__exit__ = Mock(return_value=False)
    db.sessions.begin.return_value = ctx
    return db


async def _run_upload(monkeypatch, *, fail: bool, current):
    _upload_stub(monkeypatch, fail=fail)
    monkeypatch.setattr(
        profile_service.asset_delivery_service,
        "build_delivery_url",
        lambda **kwargs: "http://proxy/delivery",
        raising=True,
    )
    upload = UploadFile(filename="me.png", file=__import__("io").BytesIO(b"not-a-real-png"))
    monkeypatch.setattr(
        profile_service.storage_service, "validate_image_file", lambda *a, **k: "png", raising=True
    )
    return await profile_service.upload_profile_picture_service(current, _fake_db(), upload)


@pytest.mark.asyncio
async def test_failed_upload_leaves_the_previous_picture_intact(monkeypatch, enabled, destroyed):
    """Regression: the old asset used to be deleted before the new one was confirmed.

    A failed upload therefore left the account pointing at an object that had already
    been destroyed, and no retry could recover it.
    """
    current = _user_details(OWNER, cloudinary_url_for(OWNED_PATH))

    with pytest.raises(HTTPException):
        await _run_upload(monkeypatch, fail=True, current=current)

    assert destroyed == [], "previous asset was destroyed even though the upload failed"


@pytest.mark.asyncio
async def test_successful_upload_deletes_the_previous_asset(monkeypatch, enabled, destroyed):
    current = _user_details(OWNER, cloudinary_url_for(OWNED_PATH))

    result = await _run_upload(monkeypatch, fail=False, current=current)

    assert result["message"] == "Profile picture uploaded successfully"
    assert destroyed == [expected_public_id(OWNED_PATH)]


@pytest.mark.asyncio
async def test_replaced_foreign_asset_is_left_alone(monkeypatch, enabled, destroyed):
    """Even the normal replacement path must not touch another account's object.

    This is the scenario where an admin has pointed this account's column at somebody
    else's picture: the victim uploading a new avatar must not delete it.
    """
    current = _user_details(OWNER, cloudinary_url_for(FOREIGN_PATH))

    await _run_upload(monkeypatch, fail=False, current=current)

    assert destroyed == []


@pytest.mark.asyncio
async def test_delete_failure_does_not_fail_the_upload(monkeypatch, enabled):
    """The new picture is already saved, so a cleanup error must not surface as a 500."""
    current = _user_details(OWNER, cloudinary_url_for(OWNED_PATH))

    def _boom(*args, **kwargs):
        raise HTTPException(status_code=500, detail="Failed to delete file")

    monkeypatch.setattr(profile_service.storage_service, "delete_profile_asset", _boom, raising=True)

    result = await _run_upload(monkeypatch, fail=False, current=current)
    assert result["message"] == "Profile picture uploaded successfully"


def test_error_detail_no_longer_leaks_the_public_id(enabled, monkeypatch):
    """The raw Cloudinary error embeds the bucket and public_id, so it must not be returned."""
    def _destroy(public_id, **kwargs):
        raise RuntimeError("cloudinary 404 not found for public_id prof-secret-token")

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _destroy, raising=True
    )

    with pytest.raises(HTTPException) as excinfo:
        storage_service.delete_file_from_bucket("prof-bucket", OWNED_PATH)

    assert "prof-secret-token" not in str(excinfo.value.detail)
    assert excinfo.value.detail == "Failed to delete file"


def test_owner_segment_helper():
    assert storage_service.asset_owner_segment(OWNED_PATH) == OWNER
    assert storage_service.asset_owner_segment("") is None
    assert storage_service.asset_owner_segment(None) is None
    # A bare filename has no owner segment, so it cannot be authorised.
    assert storage_service.asset_owner_segment("profile.png") == "profile.png"
