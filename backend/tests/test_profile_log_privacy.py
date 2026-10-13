"""Profile-image failures must not put raw regnos in retained logs.

F-05 regression coverage. The profile-picture upload and cleanup paths emit
WARNING/ERROR records, and those records are exported to OTLP/Loki for the full
retention period. A regno -- or a Cloudinary public_id, which contains the regno
as its folder segment -- identifies the person whose picture failed. These tests
drive the failure paths and assert that only the shared peppered tags remain,
including when the original exception repeats the public_id.
"""

import io

import pytest
from fastapi import HTTPException, UploadFile

from app.logger import log_identity_hash
from app.services import account_picture_service, profile_service
from app.services import storage_service as storage_service_module
from app.services.storage_service import storage_service

OWNER = "22CS045"
ACTOR = "STAFF009"
PUBLIC_ID = f"profile_pics/{OWNER}/AbCdEfGhIjKlMnOpQrStUv/profile"
NEW_PUBLIC_ID = f"profile_pics/{OWNER}/ZyXwVuTsRqPoNmLkJiHgFe/profile"


@pytest.fixture
def retained_logs(monkeypatch):
    """Capture what the module loggers would send to the retained handler."""
    lines: list[str] = []

    def _capture(message, *args, **kwargs):
        rendered = str(message)
        if args:
            try:
                rendered = rendered % args
            except Exception:  # pragma: no cover - defensive test helper
                rendered = f"{rendered} {args!r}"
        lines.append(rendered)

    for module in (account_picture_service, profile_service, storage_service_module):
        monkeypatch.setattr(module.logger, "warning", _capture)
        monkeypatch.setattr(module.logger, "error", _capture)
    return lines


def _assert_private(
    lines: list[str], *identifiers: str, expect_actor: bool = True
) -> str:
    text = "\n".join(lines)
    for identifier in identifiers:
        assert identifier not in text, f"raw identifier {identifier!r} reached a log line"
    assert f"regno_hash={log_identity_hash(OWNER)}" in text
    if expect_actor:
        assert f"actor_hash={log_identity_hash(ACTOR)}" in text
    return text


def test_upload_failure_hashes_owner_and_actor(retained_logs, monkeypatch):
    """The exact path the audit named: ``store_account_picture``'s error line."""

    def _fail(**kwargs):
        raise HTTPException(status_code=500, detail="Cloudinary is unavailable")

    monkeypatch.setattr(
        account_picture_service.storage_service,
        "upload_profile_image",
        _fail,
        raising=True,
    )

    with pytest.raises(HTTPException):
        account_picture_service.store_account_picture(
            content=b"not-reached",
            owner_regno=OWNER,
            actor_regno=ACTOR,
        )

    _assert_private(retained_logs, OWNER, ACTOR)


@pytest.mark.asyncio
async def test_validation_failure_does_not_log_raw_regnos(retained_logs, monkeypatch):
    async def _read(_file):
        return b"prepared"

    def _invalid(*args, **kwargs):
        raise RuntimeError(f"could not decode {OWNER} upload by {ACTOR}")

    monkeypatch.setattr(storage_service, "read_upload_capped", _read, raising=True)
    monkeypatch.setattr(storage_service, "validate_image_file", _invalid, raising=True)

    file = UploadFile(filename="me.png", file=io.BytesIO(b"ignored"))
    with pytest.raises(HTTPException):
        await account_picture_service.read_and_prepare_account_picture(
            file=file,
            owner_regno=OWNER,
            actor_regno=ACTOR,
        )

    _assert_private(retained_logs, OWNER, ACTOR)


def test_storage_upload_failure_hashes_the_regno_and_redacts_the_public_id(
    retained_logs, monkeypatch
):
    """Cloudinary's error can repeat the public_id, which embeds the regno."""
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    monkeypatch.setattr(
        storage_service, "validate_image_file", lambda *a, **k: "png", raising=True
    )
    monkeypatch.setattr(
        storage_service, "normalize_image", lambda content, *a, **k: content, raising=True
    )

    def _fail(_file_obj, **kwargs):
        raise RuntimeError(
            f"cloudinary rejected {kwargs['public_id']} while uploading for {OWNER}"
        )

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_upload", _fail, raising=True
    )

    with pytest.raises(HTTPException):
        storage_service.upload_profile_image(content=b"prepared", owner_regno=OWNER)

    text = _assert_private(retained_logs, OWNER, expect_actor=False)
    assert f"profile_pics/{OWNER}" not in text
    assert "Cloudinary profile upload failed" in text


def test_delete_failure_hashes_both_sides_and_the_destroy_path(
    retained_logs, monkeypatch
):
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)

    def _fail(public_id, **kwargs):
        raise RuntimeError(f"cloudinary could not delete {public_id} for {OWNER}")

    monkeypatch.setattr(
        "app.services.storage_service.cloudinary_destroy", _fail, raising=True
    )

    assert storage_service.delete_profile_asset(PUBLIC_ID, owner_regno=OWNER) is False
    with pytest.raises(RuntimeError):
        storage_service._destroy(PUBLIC_ID, delivery_type="upload")

    text = _assert_private(retained_logs, OWNER, PUBLIC_ID, expect_actor=False)
    assert "Cloudinary profile delete failed" in text
    assert "Cloudinary destroy failed" in text
    assert "AbCdEfGhIjKlMnOpQrStUv" in text


def test_retirement_and_discard_warnings_keep_only_tags(retained_logs, monkeypatch):
    def _delete_fails(public_id, **kwargs):
        raise HTTPException(
            status_code=500,
            detail=f"could not delete {public_id} for {OWNER}",
        )

    monkeypatch.setattr(
        account_picture_service.storage_service,
        "delete_profile_asset",
        _delete_fails,
        raising=True,
    )
    account_picture_service.retire_previous_picture(
        previous_public_id=PUBLIC_ID,
        new_public_id=NEW_PUBLIC_ID,
        owner_regno=OWNER,
        actor_regno=ACTOR,
    )

    monkeypatch.setattr(
        profile_service.storage_service,
        "delete_profile_asset",
        lambda public_id, **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"discard failed for {public_id} owner {OWNER}")
        ),
        raising=True,
    )
    profile_service._discard_uploaded_picture(PUBLIC_ID, OWNER)

    text = _assert_private(retained_logs, OWNER, ACTOR, PUBLIC_ID)
    assert "Failed to delete previous profile asset" in text
    assert "Failed to discard refused profile upload" in text
