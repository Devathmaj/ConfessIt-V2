# app/services/account_picture_service.py
"""Profile-picture upload for *any* account, whatever table it lives in.

This exists because the upload has two callers with two different needs and one shared set of
hard rules. A participant uploads their own picture from ``ProfilePage``; an admin uploads
somebody else's from Profile Review, and that upload works on ``admin`` and ``moderator`` rows
too, not only ``users``. The rules -- cap the read before buffering, validate that it is
actually an image, upload under the owner's own folder, verify what landed, store the URL, and
retire the previous object *only once the new one is saved* -- are identical in both cases, and
they are the rules that matter: they are what stops one account's upload writing into another
account's folder, and what stops a failed upload from leaving an account pointing at something
that has already been deleted.

The save-before-delete rule spans the database write, so it cannot live entirely inside this
module. :func:`upload_account_picture` does everything up to and including the upload; the caller
persists the new URL, and only then calls :func:`retire_previous_picture` for the old one. Both
halves live here so the rule is written down once.

The alternative was a second copy in the admin router with the participant's row lookup swapped
for a generic one. That version is a bug generator with a long fuse: the deletion-ordering rule
above is easy to get subtly wrong in the copy, and the copy is the one that never gets read
again after it works. So the shared logic lives here once, and both routes call it.
"""

from fastapi import HTTPException, UploadFile, status

from ..accounts import AccountRef
from ..logger import get_logger
from ..services import asset_delivery_service
from ..services.storage_service import storage_service

logger = get_logger(__name__)


async def upload_account_picture(
    *,
    file: UploadFile,
    owner_regno: str,
    account: AccountRef,
    previous_picture_url: str | None = None,
    actor_regno: str,
) -> dict:
    """Upload ``file`` as ``owner_regno``'s picture and return the delivery URL.

    ``account`` is the reference used to build a proxy delivery URL, which is what callers are
    handed: the browser is given the proxy's address rather than the raw Cloudinary one, so a
    client cannot lift a stored URL and replay it outside the proxy's authorisation check.

    ``previous_picture_url`` is the value *before* this upload. It is passed in rather than
    read from the row because the caller has already loaded the account and re-reading it here
    would be a second query for a value it is holding; passing it also keeps this function free
    of any table-specific lookup, which is what makes it usable for the staff tables.

    ``actor_regno`` is who is performing the upload and is only used for logging. On the
    participant route the actor is the account owner; on the admin route it is the admin, and
    keeping the two apart in the log is what makes "who changed this person's picture, and
    when" answerable afterwards from the log alone.

    The return value carries two URLs because they are deliberately not the same string. The
    caller persists ``stored_identifier`` -- the raw Cloudinary URL -- because that is what
    ``asset_delivery_service`` needs to resolve the object later, and it hands ``response`` back
    to the browser. Persisting the proxy URL instead would mean every later resolution had to
    unwrap it again first, and the value in the database would stop matching what
    ``delete_profile_asset`` and the proxy's own lookups expect to find.

    This function deliberately does **not** retire ``previous_picture_url``. It cannot: the
    replacement is not stored until the caller writes it, and the caller writes it after this
    returns. An earlier version deleted the old object here while its comment claimed the
    opposite, which meant a failed database write left the row pointing at an object that had
    already been destroyed -- the account showed a broken image and no retry could recover it,
    because the bytes were gone.
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

    upload_target_name = owner_regno or "user"
    try:
        upload_result = storage_service.upload_profile_image(
            file=file,
            content=contents,
            user_folder=upload_target_name,
        )
        storage_service.verify_uploaded_asset("profile", upload_result["object_path"])
    except HTTPException:
        logger.error(
            "Cloudinary upload failed for %s (actor %s)", owner_regno, actor_regno
        )
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

    # Store the full Cloudinary URL in the database (not just the object path).
    cloudinary_url = upload_result["cloudinary_url"]
    delivery_url = asset_delivery_service.build_delivery_url(
        kind="profile", stored_identifier=cloudinary_url, account=account
    )

    # Only now that the replacement is stored -- by the caller, which has the row open -- do we
    # retire the old object. Deleting it before the upload was attempted meant a failed upload
    # left the account pointing at an asset that had already been destroyed, which no retry
    # could recover from. ``delete_profile_asset`` re-checks that the path is under this
    # account's folder, so a stored value naming somebody else's object is refused rather than
    # acted on.
    return {
        "stored_identifier": cloudinary_url,
        "response": {
            "profile_picture_url": delivery_url or cloudinary_url,
            "url": delivery_url or cloudinary_url,
            "message": "Profile picture uploaded successfully",
        },
    }


def retire_previous_picture(
    *,
    previous_picture_url: str | None,
    owner_regno: str,
    new_stored_identifier: str,
    actor_regno: str,
) -> None:
    """Delete the object ``previous_picture_url`` named, now that the new one is committed.

    Called by each route *after* its transaction has committed, which is the only point at which
    the ordering is actually true. Calling it earlier bought nothing and cost the one property
    that matters: a row that still points at something which exists.

    Three guards, in order:

    * ``normalize_stored_path`` refuses a stored value that does not resolve to an object under
      this account's own folder, so a URL naming somebody else's picture is not acted on.
    * The previous path is compared against the new one. Re-uploading the same object is a
      no-op, not a deletion of the file the account still points at.
    * Failures are logged and never raised. The new picture is saved and the row already names
      it, so an orphaned object is a cleanup problem; raising would answer the operator's
      successful upload with a 500.
    """
    if not previous_picture_url:
        return

    previous_path = storage_service.normalize_stored_path(previous_picture_url, "profile")
    new_path = storage_service.normalize_stored_path(new_stored_identifier, "profile")
    if not previous_path or previous_path == new_path:
        return

    try:
        storage_service.delete_profile_asset(previous_picture_url, owner_regno=owner_regno)
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