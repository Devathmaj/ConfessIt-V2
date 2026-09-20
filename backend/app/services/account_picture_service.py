# app/services/account_picture_service.py
"""Profile-picture upload: the rules, in one place, with the database write left to the caller.

The hard part of a profile-picture upload is not the Cloudinary call, it is the ordering and the
ownership. Cap the read before buffering it, validate that it really is an image, store the
object under the owner's own folder, and retire the previous object *only once the replacement
is saved*. Get the last one wrong and a failed upload leaves the account pointing at bytes that
have already been destroyed -- a broken avatar that no retry can recover, because the image is
gone.

Those rules do not fit inside one service function, because the rule that matters spans the
database write: :func:`upload_account_picture` does everything up to and including the upload and
returns the new identity, the caller persists it, and only then calls
:func:`retire_previous_picture` for the old one. Writing that sequence down twice -- once in a
comment claiming the write already happened, once in whatever order the code actually runs -- is
how the bug above got shipped the first time. So both halves live here and the caller only has to
know the order.

It is a separate module from ``profile_service`` because the upload is not participant-specific:
nothing in the rules mentions which table the row lives in, only which account it belongs to. That
is deliberate, because ``actor_regno`` is separate from ``owner_regno`` below -- a staff member
setting somebody else's picture is the same code path with two different regnos in it, and the
separation is what makes "who changed this person's picture" answerable from the log afterwards.

Profile pictures are delivered as ordinary public Cloudinary URLs. That is a deliberate choice
rather than a gap: they appear in matchmaking lists, chat headers and moderation screens to
people who are not the owner, so per-viewer authorisation would mean a round trip per avatar on
every page. Authorisation is still required to *change* a picture, and the object lives under a
per-account folder with an unguessable id, so a URL cannot be walked from a list of regnos.
"""

from fastapi import HTTPException, UploadFile, status

from ..logger import get_logger
from ..services.storage_service import storage_service

logger = get_logger(__name__)


async def upload_account_picture(
    *,
    file: UploadFile,
    owner_regno: str,
    actor_regno: str,
) -> dict:
    """Upload ``file`` as ``owner_regno``'s picture and return the new identity.

    ``actor_regno`` is who is performing the upload and is only used for logging. On the
    participant route the actor is the account owner; on the admin route it is the admin, and
    keeping the two apart in the log is what makes "who changed this person's picture, and when"
    answerable afterwards from the log alone.

    The return value carries the object's identity -- ``public_id`` and ``format`` -- because
    that is what the caller persists. The delivery URL in ``response`` is *derived*, for the
    browser: persisting a URL instead would put an address in the database that has to be parsed
    back into an identity before it can be deleted or re-signed, and the value would stop
    matching what ``delete_profile_asset`` expects to find.

    This function does not retire any previous object, and is not told what one is. The caller
    owns the row, so the caller is the only thing that can read the value being replaced, and it
    must read it in the same transaction that writes the new one -- see
    :func:`retire_previous_picture`. An earlier version deleted the old object here, which meant
    a failed database write left the row pointing at an object that had already been destroyed:
    the account showed a broken image and no retry could recover it, because the bytes were gone.
    """
    # Read through the bounded reader rather than ``await file.read()``, which would buffer the
    # whole body before anything had looked at it -- a 2 GB upload would sit in the process's
    # memory before the size check that exists to prevent exactly that ever ran.
    contents = await storage_service.read_upload_capped(file)

    try:
        storage_service.validate_image_file(
            file, contents, max_size_mb=storage_service.max_image_size_mb
        )
    except HTTPException:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Unexpected error validating profile image for %s (actor %s): %s",
            owner_regno, actor_regno, exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid image file"
        )

    try:
        stored = storage_service.upload_profile_image(
            file=file,
            content=contents,
            owner_regno=owner_regno,
        )
    except HTTPException:
        logger.error("Cloudinary upload failed for %s (actor %s)", owner_regno, actor_regno)
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Unexpected error uploading profile image for %s (actor %s): %s",
            owner_regno, actor_regno, exc,
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


def retire_previous_picture(
    *,
    previous_public_id: str | None,
    new_public_id: str | None,
    owner_regno: str,
    actor_regno: str,
) -> None:
    """Delete the object ``previous_public_id`` named, now that the new one is committed.

    Called by each route *after* its transaction has committed, which is the only point at which
    the ordering is actually true. Calling it earlier bought nothing and cost the one property
    that matters: a row that still points at something which exists.

    Two guards, in order:

    * ``delete_profile_asset`` re-checks that the object sits under this account's own folder, so
      a stored value naming somebody else's picture is refused rather than acted on.
    * The previous identity is compared against the new one. Re-uploading the same object is a
      no-op, not a deletion of the file the account still points at.
    * Failures are logged and never raised. The new picture is saved and the row already names
      it, so an orphaned object is a cleanup problem; raising would answer the operator's
      successful upload with a 500.
    """
    if not previous_public_id:
        return
    if new_public_id and previous_public_id == new_public_id:
        return

    try:
        storage_service.delete_profile_asset(previous_public_id, owner_regno=owner_regno)
    except HTTPException as exc:
        logger.warning(
            "Failed to delete previous profile asset for %s (actor %s): %s",
            owner_regno, actor_regno, exc.detail,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Unexpected error deleting previous profile asset for %s (actor %s): %s",
            owner_regno, actor_regno, exc,
        )
