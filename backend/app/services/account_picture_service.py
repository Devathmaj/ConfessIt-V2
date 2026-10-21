# app/services/account_picture_service.py
"""Account picture uploads with validation, storage and retirement in one service.

Read and validate before the cooldown check; store only normalized image bytes.
The caller commits the new identity before retiring the old object, preventing a
failed database write from leaving the account with a broken picture. Logs use
peppered actor/owner tags; delivery URLs are public and derived from stored identity.
"""

from fastapi import HTTPException, UploadFile, status

from ..logger import get_logger, log_identity_hash, redact_log_identifiers
from ..services.storage_service import storage_service

logger = get_logger(__name__)


async def read_and_prepare_account_picture(
    *,
    file: UploadFile,
    owner_regno: str,
    actor_regno: str,
) -> bytes:
    """Bound, validate and normalize the upload before any write or cooldown check.

    Validation uses decoded bytes, not filename or Content-Type; identities are logged
    only as stable peppered tags.
    """
    contents = await storage_service.read_upload_capped(file)

    try:
        image_format = storage_service.validate_image_file(
            contents, max_size_mb=storage_service.max_image_size_mb
        )
        contents = storage_service.normalize_image(contents, image_format)
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Unexpected error validating profile image for regno_hash=%s "
            "(actor_hash=%s): %s",
            log_identity_hash(owner_regno),
            log_identity_hash(actor_regno),
            redact_log_identifiers(exc, owner_regno, actor_regno),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image file"
        )

    # Re-encoding can only grow the file in unusual cases (a noisy photograph
    # re-saved at higher quality than it arrived), and the cap is a rule about
    # what may be stored, not merely about what may be read -- so it is
    # re-checked here, before the cooldown is even consulted.
    if len(contents) > storage_service.max_image_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {storage_service.max_image_size_mb}MB",
        )

    return contents


def store_account_picture(
    *,
    content: bytes,
    owner_regno: str,
    actor_regno: str,
) -> dict:
    """Store validated bytes and return the identity the caller persists.

    The delivery URL is derived; the caller retires the previous object only after
    its database transaction commits.
    """
    try:
        stored = storage_service.upload_profile_image(
            content=content,
            owner_regno=owner_regno,
        )
    except HTTPException:
        logger.error(
            "Cloudinary upload failed for regno_hash=%s (actor_hash=%s)",
            log_identity_hash(owner_regno),
            log_identity_hash(actor_regno),
        )
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Unexpected error uploading profile image for regno_hash=%s "
            "(actor_hash=%s): %s",
            log_identity_hash(owner_regno),
            log_identity_hash(actor_regno),
            redact_log_identifiers(exc, owner_regno, actor_regno),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to upload profile picture",
        )

    delivery_url = storage_service.profile_delivery_url(stored["public_id"], stored["format"])

    return {
        "public_id": stored["public_id"],
        "format": stored["format"],
        "response": {
            "profile_picture_url": delivery_url,
            "message": "Profile picture uploaded successfully",
        },
    }


async def upload_account_picture(
    *,
    file: UploadFile,
    owner_regno: str,
    actor_regno: str,
) -> dict:
    """Prepare and store a picture for ``owner_regno``; ``actor_regno`` is logging-only."""
    contents = await read_and_prepare_account_picture(
        file=file,
        owner_regno=owner_regno,
        actor_regno=actor_regno,
    )
    return store_account_picture(
        content=contents,
        owner_regno=owner_regno,
        actor_regno=actor_regno,
    )


def retire_previous_picture(
    *,
    previous_public_id: str | None,
    new_public_id: str | None,
    owner_regno: str,
    actor_regno: str,
) -> None:
    """Retire the old picture after commit, verifying ownership and skipping the new identity.

    Cleanup failures are logged; the committed row already names the replacement.
    """
    if not previous_public_id:
        return
    if new_public_id and previous_public_id == new_public_id:
        return

    try:
        storage_service.delete_profile_asset(previous_public_id, owner_regno=owner_regno)
    except HTTPException as exc:
        logger.warning(
            "Failed to delete previous profile asset for regno_hash=%s "
            "(actor_hash=%s): %s",
            log_identity_hash(owner_regno),
            log_identity_hash(actor_regno),
            redact_log_identifiers(
                exc.detail, owner_regno, actor_regno, previous_public_id
            ),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Unexpected error deleting previous profile asset for regno_hash=%s "
            "(actor_hash=%s): %s",
            log_identity_hash(owner_regno),
            log_identity_hash(actor_regno),
            redact_log_identifiers(
                exc, owner_regno, actor_regno, previous_public_id
            ),
        )
