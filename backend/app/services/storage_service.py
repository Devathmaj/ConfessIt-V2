# app/services/storage_service.py
"""Cloudinary-backed image storage.

Two kinds of image live here and they are deliberately not treated alike:

* **Profile pictures are public.** They are uploaded as ``type="upload"`` with no
  ``access_mode`` and no access control list, so Cloudinary serves them straight from its CDN
  over ordinary HTTPS. Nothing about them needs to be authorised at fetch time: a profile
  picture is shown in matchmaking lists, chat headers and moderation screens to people who are
  not the owner, and gating it per-viewer would mean a request per avatar on every page. What
  the application still owns is *which object a row points at* -- upload, replace and delete
  all stay here and all require authorisation -- and the unguessable per-account folder, so
  the URL cannot be enumerated from somebody else's regno.

* **Love note images are private.** They are uploaded as ``type="authenticated"``, which is
  what makes Cloudinary refuse the object outright unless the request carries a valid signed URL.
  Each delivery URL therefore carries a SHA-1 signature over that one object's exact URL path,
  keyed by the API secret, so it authorises one image and nothing else. There is no fallback: if
  the signature cannot be produced the request fails loudly rather than quietly serving the
  image from somewhere weaker.

  Note *what a signed URL is not*: it does not expire, and it cannot be revoked short of
  destroying and re-uploading the object. Cloudinary's expiring alternative, an authentication
  token (``?__cld_token__=exp=...~hmac=...``), needs a separate account-wide Auth token key that
  is not available on this plan -- and applying a token ACL on top of ``type="authenticated"``
  would make Cloudinary reject the signature as well. So the ACL is deliberately absent, and
  :func:`love_note_delivery_url` signs instead. See ``config`` for the full trade-off.

**What is stored is identity, not a URL.** Both kinds return ``(public_id, format)`` and that
is what the database holds. A URL is not a durable fact about an object: for a love note it is
a credential, and storing one would mean a signed string sitting in a column and in every
response that ever read it. The delivery URL is derived at the moment it is handed out, after
the application has decided the viewer is entitled to it.
"""

from __future__ import annotations

import base64
import io
import re
import secrets
import warnings
from typing import Any, Dict, Optional, Tuple

import cloudinary
import httpx
from cloudinary.uploader import destroy as cloudinary_destroy, upload as cloudinary_upload
from cloudinary.utils import cloudinary_url
from fastapi import HTTPException, UploadFile, status
from PIL import Image, ImageOps

from ..config import settings
from ..logger import get_logger

logger = get_logger(__name__)


# The single fixed object layout. These are not configurable: a layout that can be changed by an
# environment variable is a layout that a deployment can change *after* rows were written, which
# would orphan every object already stored under the old prefix.
PROFILE_FOLDER = "profile_pics"
LOVE_NOTES_FOLDER = "love_notes"

# The formats an upload is accepted as. Checked against the decoded image header, not the
# client-supplied content type or filename, because both of those are attacker-controlled.
BASE_ALLOWED_FORMATS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})

# A public_id segment may only hold these. Cloudinary would accept far more, but the layout above
# is generated from a regno and a random token, and constraining the alphabet means a stored
# value can never contain a separator, a traversal sequence or anything that would silently
# re-point a delete at a neighbouring object.
_PUBLIC_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Cloudinary's signed-URL signature appears as an ``s--<sig>--`` path segment immediately after
# the delivery type. It is checked rather than assumed: an unsigned URL for an ``authenticated``
# object is one the CDN refuses on every single fetch, and the failure needs to surface here
# where it can be reported, not in a browser as a permanently broken image. Deliberately does
# not match ``__cld_token__``, which is the *other* mechanism and is not used here.
SIGNED_URL_PATTERN = re.compile(r"/image/authenticated/s--[A-Za-z0-9_-]+--/")

# Ceiling on the bytes fetched back from the CDN during a rotation. Rotated objects were
# within the upload limit when stored, so anything past this is corruption or a changed
# object, and the rotation is refused rather than buffered without bound.
ROTATE_DOWNLOAD_MAX_BYTES = 16 * 1024 * 1024
_ROTATE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_ROTATE_DOWNLOAD_TIMEOUT_SECONDS = 30.0


def _fetch_url_bytes(url: str, cap_bytes: int) -> bytes:
    """Fetch one URL's body, bounded, raising on anything unexpected.

    Factored out (rather than inlined in the rotation below) so tests can replace the
    network without touching the SDK calls the rotation also makes.
    """
    chunks: list[bytes] = []
    total = 0
    with httpx.stream("GET", url, timeout=_ROTATE_DOWNLOAD_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(_ROTATE_DOWNLOAD_CHUNK_BYTES):
            total += len(chunk)
            if total > cap_bytes:
                raise ValueError(f"download exceeded {cap_bytes} bytes")
            chunks.append(chunk)
    return b"".join(chunks)


def _slug_segment(value: str, *, limit: int = 64) -> str:
    """Reduce ``value`` to characters that are safe inside a Cloudinary public_id segment.

    A regno is server-owned data, but it is still a human-chosen string, and a public_id is a
    path. Anything outside ``[A-Za-z0-9._-]`` becomes a dash so that a regno such as
    ``cs/2024`` cannot introduce a path segment of its own. Truncation is bounded and a trailing
    dash is stripped so the result is still a single, stable segment.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return (cleaned[:limit].strip("-")) or "account"


class CloudinaryStorageService:
    """Image storage backed by Cloudinary.

    A single process-wide instance (:data:`storage_service`) configures the Cloudinary SDK's
    global config once, which is what that SDK expects.
    """

    # Granularity of the bounded upload read. Small enough that the peak buffer stays close to
    # the cap instead of overshooting it by a whole chunk.
    UPLOAD_READ_CHUNK_BYTES = 64 * 1024

    # Per-side and total-pixel ceilings. A byte cap alone does not bound image processing: a
    # few hundred KB of compressed data can describe a bitmap that costs gigabytes to decode, so
    # both the per-side dimensions and the total pixel count are capped and read from the image
    # header before any pixel data is touched.
    DEFAULT_MAX_IMAGE_DIMENSION = 6000
    DEFAULT_MAX_IMAGE_PIXELS = 12_000_000
    DEFAULT_MAX_IMAGE_SIZE_MB = 2

    def __init__(self) -> None:
        self._configure_cloudinary()
        self._apply_limits()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    def _apply_limits(self) -> None:
        """Read the image limits from settings.

        Read on every construction rather than captured at import so a test that sets a limit is
        obeyed immediately and a value changed in the environment does not need a module reload.
        A malformed value falls back to the built-in default rather than disabling the limit,
        because a limit that quietly stops existing is worse than one that is merely wrong.
        """

        def positive_int(value: Any, default: int) -> int:
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                return default
            return parsed if parsed > 0 else default

        self.max_image_size_mb = positive_int(
            settings.MAX_IMAGE_SIZE_MB, self.DEFAULT_MAX_IMAGE_SIZE_MB
        )
        self.max_image_bytes = self.max_image_size_mb * 1024 * 1024
        self.max_image_dimension = positive_int(
            settings.MAX_IMAGE_DIMENSION, self.DEFAULT_MAX_IMAGE_DIMENSION
        )
        self.max_image_pixels = positive_int(
            settings.MAX_IMAGE_PIXELS, self.DEFAULT_MAX_IMAGE_PIXELS
        )

        # Upper bound on a base64 image payload, used by the JSON-body love note upload where the
        # encoded string arrives as a request field rather than as a multipart part. base64 spends
        # 4 characters per 3 bytes, plus slack for a data URI prefix and the padding on the final
        # group. Enforced *before* b64decode, so an oversized body is rejected without the decode
        # ever allocating a second copy of it.
        self.max_base64_chars = (self.max_image_bytes // 3) * 4 + 4096

        self.allowed_extensions = set(BASE_ALLOWED_FORMATS)
        extra_formats = settings.CLOUDINARY_ALLOWED_FORMATS
        if extra_formats:
            for fmt in extra_formats.split(","):
                cleaned = fmt.strip().lower()
                # An operator can widen the accepted formats, never by adding something that is
                # not an image at all -- a value carrying a dot or a slash would end up in a
                # public_id and a URL.
                if cleaned and re.fullmatch(r"[a-z0-9]{2,8}", cleaned):
                    self.allowed_extensions.add(cleaned)

    def _configure_cloudinary(self) -> None:
        """Push this deployment's Cloudinary credentials into the SDK's global config.

        Read from ``settings`` rather than ``os.environ`` so there is exactly one place that
        decides what this process is configured with.
        """
        self.cloud_name = settings.CLOUDINARY_CLOUD_NAME
        self.api_key = settings.CLOUDINARY_API_KEY
        self.api_secret = settings.CLOUDINARY_API_SECRET

        self.enabled = bool(self.cloud_name and self.api_key and self.api_secret)
        if self.enabled:
            cloudinary.config(
                cloud_name=self.cloud_name,
                api_key=self.api_key,
                api_secret=self.api_secret,
                # HTTPS only. Emitting a plain-HTTP delivery URL would hand the browser a
                # downgradeable address, so a deployment cannot opt out of this by accident.
                secure=True,
            )
        else:
            logger.warning(
                "Cloudinary is not fully configured; image upload and delivery are unavailable."
            )

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Image storage is not configured",
            )

    @staticmethod
    def _asset_tags(*configured: Optional[str]) -> list[str]:
        """Normalise the grouping tags for one upload.

        A tag is organisational metadata and nothing else: it does not appear in a delivery URL
        and it grants nothing. Cloudinary rejects an upload outright if a tag contains anything
        outside ``[A-Za-z0-9_-]``, so an operator who types a space or a dot into the setting would
        otherwise turn a mis-spelled label into every upload failing with a Cloudinary error. The
        characters are dropped, the tag is skipped entirely if that leaves nothing, and the
        mistake is logged rather than raised -- losing a label is recoverable, losing the picture
        is not.
        """
        tags = []
        for value in configured:
            cleaned = re.sub(r"[^A-Za-z0-9_-]", "", value or "")
            if not cleaned:
                if value:
                    logger.warning(
                        "Ignoring unusable Cloudinary asset tag %r: a tag may only contain "
                        "letters, digits, underscores and hyphens",
                        value,
                    )
                continue
            if cleaned not in tags:
                tags.append(cleaned)
        return tags

    # ------------------------------------------------------------------
    # Upload reading and validation
    # ------------------------------------------------------------------
    async def read_upload_capped(
        self, file: UploadFile, max_bytes: Optional[int] = None
    ) -> bytes:
        """Read an upload into memory without ever buffering more than ``max_bytes``.

        ``await file.read()`` with no size argument pulls the entire body into the process
        before any validation has run, so a single request can pin an arbitrary amount of RAM
        for as long as the client keeps sending. Two guards, because either one alone is
        bypassable:

        1. the size the multipart parser already read off the part headers, which refuses an
           oversize upload without touching its body at all;
        2. a chunked read that stops as soon as the accumulated bytes pass the cap. This is the
           one that matters, because a chunked request carries no declared length for the first
           guard to check.

        Defaults to ``max_image_bytes`` so a profile picture and a love note image are held to
        the same limit.
        """
        limit = self.max_image_bytes if max_bytes is None else max_bytes

        # getattr because UploadFile.size only exists on newer Starlette releases; when it is
        # absent the size is simply unknown and the chunked read below covers it.
        declared = getattr(file, "size", None)
        if declared is not None and declared > limit:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {limit // (1024 * 1024)}MB",
            )

        buffered = bytearray()
        while True:
            chunk = await file.read(self.UPLOAD_READ_CHUNK_BYTES)
            if not chunk:
                break
            buffered.extend(chunk)
            if len(buffered) > limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum size is {limit // (1024 * 1024)}MB",
                )

        return bytes(buffered)

    def validate_image_file(self, content: bytes, max_size_mb: Optional[int] = None) -> str:
        """Validate an uploaded image's size, type and geometry. Returns its format.

        The bytes have to *be* an image, not say that they are one. The format comes from the
        file's own header; neither the filename nor the ``Content-Type`` is consulted anywhere,
        because the client chose both and a header that merely claims ``image/png`` over a body
        of arbitrary bytes is the ordinary way to smuggle something past a type check.

        The order is what makes each refusal safe: the byte cap before anything parses the
        file, the geometry read from the header before any pixel is materialised, and the full
        decode last, once the geometry has bounded what that decode can cost.
        """
        if max_size_mb is not None:
            max_bytes = int(max_size_mb) * 1024 * 1024
            if len(content) > max_bytes:
                raise HTTPException(
                    status_code=413, detail=f"File too large. Maximum size is {max_size_mb}MB"
                )

        kind = self._get_image_format(content)
        if kind not in BASE_ALLOWED_FORMATS:
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Only images (JPEG, PNG, GIF, WebP) are allowed",
            )

        self._check_image_dimensions(*self._get_image_size(content))
        self._decode_fully(content)

        return kind

    def normalize_image(self, content: bytes, image_format: str) -> bytes:
        """Re-encode a validated image, so the object stored is its pixels and nothing else.

        Everything a file can carry that is not picture data is dropped here: EXIF, which holds
        when and where a photo was taken and the device that took it, ICC profiles, XMP and
        comments. The EXIF *orientation* is baked into the pixels first, which is what keeps a
        phone photo the right way up after the tag that said which way up it was has gone.

        Animated images are returned as they arrived. Only their first frame is validated, and
        re-encoding them would mean decoding every frame -- the unbounded work the geometry
        check exists to avoid -- so the safer of the two options is to store bytes that have
        already been read, sniffed and decoded rather than bytes that have been partly rebuilt.

        Falls back to the input on any failure. Those bytes passed :meth:`validate_image_file`,
        so they are safe to store; refusing a good picture because an optimisation failed would
        be trading a working upload for tidier metadata.
        """
        if image_format not in BASE_ALLOWED_FORMATS:
            return content

        try:
            with Image.open(io.BytesIO(content)) as image:
                if getattr(image, "n_frames", 1) > 1:
                    return content
                frame = ImageOps.exif_transpose(image)
                save_format = "JPEG" if image_format in {"jpg", "jpeg"} else image_format.upper()
                save_kwargs: Dict[str, Any] = {"optimize": True}
                if save_format == "JPEG":
                    # Orientation is already applied above, and JPEG has no alpha to lose.
                    frame = frame.convert("RGB")
                    save_kwargs["quality"] = 85
                elif save_format == "WEBP":
                    save_kwargs.pop("optimize")
                    save_kwargs["quality"] = 85
                reencoded = io.BytesIO()
                frame.save(reencoded, format=save_format, **save_kwargs)
                return reencoded.getvalue()
        except Exception as exc:
            logger.warning(
                "Could not re-encode a validated %s image (%s); storing it as received",
                image_format,
                exc,
            )
            return content

    def _check_image_dimensions(self, width: Optional[int], height: Optional[int]) -> None:
        """Reject images whose decoded pixel grid is too large to process safely.

        Called with dimensions read from the image header, before any pixel data is decoded, so
        the cost of the rejection is independent of the claimed size.
        """
        if not width or not height or width < 1 or height < 1:
            raise HTTPException(status_code=400, detail="Invalid image data")

        if width > self.max_image_dimension or height > self.max_image_dimension:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Image too large. Maximum dimensions are "
                    f"{self.max_image_dimension}x{self.max_image_dimension} pixels."
                ),
            )

        if width * height > self.max_image_pixels:
            raise HTTPException(
                status_code=413,
                detail=f"Image too large. Maximum total size is {self.max_image_pixels} pixels.",
            )

    @staticmethod
    def _get_image_format(content: bytes) -> Optional[str]:
        """The image format read from the file's header, or None if it is not an image."""
        try:
            with Image.open(io.BytesIO(content)) as img:
                return (img.format or "").lower() or None
        except Image.DecompressionBombError:
            # The header describes a bitmap past the point where materialising it could be
            # attempted at all. It is a size refusal rather than a type one, and PIL raises it
            # while opening -- before anything here could read the dimensions to say how much
            # too large it is, so the answer is stated in terms of what is known.
            raise HTTPException(
                status_code=413, detail="Image too large. The image claims too many pixels to process."
            )
        except Exception:
            return None

    @staticmethod
    def _get_image_size(content: bytes) -> Tuple[Optional[int], Optional[int]]:
        """Read ``(width, height)`` from the header.

        ``Image.open`` is lazy, so this does not decompress the image: a decompression bomb is
        rejected here without ever being materialised.
        """
        try:
            with Image.open(io.BytesIO(content)) as img:
                return img.size
        except Image.DecompressionBombError:
            raise HTTPException(
                status_code=413, detail="Image too large. The image claims too many pixels to process."
            )
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image data")

    @staticmethod
    def _decode_fully(content: bytes) -> None:
        """Materialise every pixel, so a header that lies about its own body is caught.

        ``Image.open`` only reads the header: a file can declare itself a PNG and then stop, or
        carry damage PIL merely warns about. Decoding turns both into a refusal, and it runs
        only after the geometry check has bounded what the decode may cost. Warnings become
        errors for the same reason -- "the image may be truncated" is a verdict, not a note to
        log and continue from.
        """
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with Image.open(io.BytesIO(content)) as img:
                    img.load()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image data")

    def decode_base64_image(self, image_base64: str) -> Tuple[bytes, str, Tuple[int, int]]:
        """Decode and validate a base64-encoded image, bounding work at every stage.

        Used by the love note upload, where the encoded string is already fully resident in
        memory by the time this runs. The order of the checks is the point:

        1. the encoded length, because ``b64decode`` allocates a second buffer of the same order
           and an unbounded body is the cheapest way to exhaust memory;
        2. the decoded length, before the buffer is handed to an image parser;
        3. format and header dimensions, read from the image header only.

        The first check is O(1) and happens before the payload is even scanned, so the cost of
        refusing a large body does not grow with the size of that body.

        Returns ``(decoded bytes, lowercase format, (width, height))``.
        """
        if not isinstance(image_base64, str):
            raise HTTPException(status_code=400, detail="Image data is required")

        # Checked against the raw field, before anything scans it. Stripping the data URI prefix
        # and folding whitespace out can only make the string shorter, so this is exact and it
        # costs O(1) -- an oversized body is turned away without walking its tens of megabytes,
        # let alone decoding it.
        if len(image_base64) > self.max_base64_chars:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {self.max_image_size_mb}MB",
            )

        # Accept both a bare base64 string and a data URI ("data:image/png;base64,...").
        payload = image_base64.split(",", 1)[1] if "," in image_base64 else image_base64
        # Fold out any whitespace, which is legal in base64 but would otherwise count against the
        # encoded-length budget without contributing any decoded bytes.
        payload = "".join(payload.split())
        if not payload:
            raise HTTPException(status_code=400, detail="Image data is required")

        try:
            image_bytes = base64.b64decode(payload)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image data")

        if not image_bytes:
            raise HTTPException(status_code=400, detail="Invalid image data")

        if len(image_bytes) > self.max_image_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {self.max_image_size_mb}MB",
            )

        image_format = self._get_image_format(image_bytes)
        if not image_format:
            raise HTTPException(status_code=400, detail="Invalid image data")
        if image_format not in self.allowed_extensions:
            raise HTTPException(
                status_code=415,
                detail="Invalid file type. Only images (JPEG, PNG, GIF, WebP) are allowed",
            )

        width, height = self._get_image_size(image_bytes)
        self._check_image_dimensions(width, height)
        return image_bytes, image_format, (width, height)

    # ------------------------------------------------------------------
    # Public identifier layout
    # ------------------------------------------------------------------
    @staticmethod
    def new_asset_id() -> str:
        """An unguessable, path-safe identifier for one uploaded object.

        18 url-safe bytes is 144 bits of entropy, which is far more than is needed to make a
        single object's path unguessable and costs nothing to store. The alphabet is restricted to
        the one :data:`_PUBLIC_ID_SEGMENT_RE` allows, so the generated value cannot introduce a
        path separator or a traversal sequence into somebody else's folder.
        """
        return secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")

    @classmethod
    def profile_public_id(cls, owner_regno: str, asset_id: str) -> str:
        """The public_id of a profile picture: ``profile_pics/{regno}/{opaque_id}/profile``.

        Grouping by regno keeps an account's pictures together and makes the folder obvious in a
        Cloudinary console; the opaque middle segment is what stops a third party from guessing
        somebody else's picture URL from their regno alone.
        """
        return (
            f"{PROFILE_FOLDER}/{_slug_segment(owner_regno)}/"
            f"{_slug_segment(asset_id, limit=64)}/profile"
        )

    @staticmethod
    def love_note_public_id(love_note_id: str) -> str:
        """The public_id of a love note image: ``love_notes/{love_note_id}/image``.

        Keyed on the note's own id rather than on anything the sender supplied. That makes the
        object one-to-one with the row: it cannot be re-pointed at a different note, a note's
        image cannot be guessed from a display name, and a note's id -- which is already only
        visible to the people entitled to see it -- is enough to find the object for deletion.
        """
        note_segment = _slug_segment(love_note_id)
        if not re.fullmatch(r"[A-Za-z0-9-]{32,36}", note_segment):
            raise HTTPException(status_code=400, detail="Invalid love note id")
        return f"{LOVE_NOTES_FOLDER}/{note_segment}/image"

    @classmethod
    def profile_owner_segment(cls, public_id: Optional[str]) -> Optional[str]:
        """The regno segment of a profile public_id, or None if it is not one.

        Used by :meth:`delete_profile_asset` to refuse destroying an object that does not belong
        to the account asking. The shape is fixed by :meth:`profile_public_id`, so this reads the
        second segment and refuses anything that does not have exactly that shape.
        """
        if not public_id:
            return None
        segments = str(public_id).split("/")
        if len(segments) != 4:
            return None
        root, owner, asset_id, filename = segments
        if root != PROFILE_FOLDER or filename != "profile":
            return None
        if not all(_PUBLIC_ID_SEGMENT_RE.match(segment) for segment in (owner, asset_id)):
            return None
        return owner

    @staticmethod
    def love_note_id_segment(public_id: Optional[str]) -> Optional[str]:
        """The note id segment of a love note public_id, or None if it is not one.

        Symmetric with :meth:`profile_owner_segment`, and used for the same reason: a delete has
        to be provably about the object the caller named, not about something that merely shares
        a prefix.
        """
        if not public_id:
            return None
        segments = str(public_id).split("/")
        if len(segments) != 3 or segments[0] != LOVE_NOTES_FOLDER or segments[2] != "image":
            return None
        if not all(_PUBLIC_ID_SEGMENT_RE.match(segment) for segment in segments):
            return None
        return segments[1]

    # ------------------------------------------------------------------
    # Delivery URLs
    # ------------------------------------------------------------------
    def profile_delivery_url(
        self, public_id: Optional[str], image_format: Optional[str]
    ) -> Optional[str]:
        """The ordinary public HTTPS URL for a profile picture.

        No transformation is requested: a delivery URL that is only valid for the original upload
        cannot be used to smuggle a different rendition past the checks above, and the frontend
        already renders these through ``<img>`` with its own CSS sizing. What is left out is
        exactly what a URL could otherwise be used for -- re-encoding the stored object into
        whatever shape a caller liked.
        """
        if not public_id:
            return None
        self._ensure_enabled()
        url, _ = cloudinary_url(
            public_id,
            resource_type="image",
            type="upload",
            format=image_format or None,
            secure=True,
        )
        return url

    def love_note_delivery_url(
        self, public_id: Optional[str], image_format: Optional[str]
    ) -> Optional[str]:
        """A signed HTTPS URL for one love note image.

        Cloudinary refuses an ``authenticated`` object unless the request carries a valid
        signature over its URL path, and validates that signature itself against the API secret.
        The signature minted here covers this one object's path, so it authorises this URL and
        nothing else -- a signature taken from one note cannot be moved onto another, because
        the public ID is part of what gets signed.

        The caller is responsible for having decided the viewer is entitled: a signature proves
        *when* someone was allowed to see this image, not *that* they were, so minting one for
        an unentitled viewer would hand out the capability it is supposed to gate.

        The signature does not expire. Cloudinary's expiring mechanism is the authentication
        token, which needs an account-wide Auth token key that is not available on this plan,
        and which is a different thing entirely from a URL signature -- see ``config``. That
        makes an issued URL a durable bearer capability, so the entitlement check above is the
        only gate there ever is, and the URL is derived per-response rather than stored.

        Fails closed. A URL that came back unsigned is a URL Cloudinary will refuse on every
        fetch, so the failure is raised here where the message can say why, rather than in the
        browser as a broken image. There is no public or proxied fallback: serving a love note
        from a weaker path because the strong one was unconfigured would be a privacy incident,
        not a degradation.
        """
        if not public_id:
            return None
        self._ensure_enabled()

        url, _ = cloudinary_url(
            public_id,
            resource_type="image",
            type="authenticated",
            format=image_format or None,
            secure=True,
            # This is the SDK's SHA-1 URL signature, keyed by the API secret -- the same
            # credential the upload already used, so nothing account-side has to be configured.
            # No ``auth_token`` is passed: supplying one would make the SDK skip this signature
            # and emit an ``__cld_token__`` parameter instead, which needs the account's Auth
            # token key and is unavailable here.
            sign_url=True,
        )
        if not url or not SIGNED_URL_PATTERN.search(url):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Love note image delivery is not configured",
            )
        return url

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    def upload_profile_image(
        self, *, content: bytes, owner_regno: str
    ) -> Dict[str, str]:
        """Upload a profile picture as a public object and return its identity.

        Returns ``{"public_id": ..., "format": ...}``. There is deliberately no URL in the return
        value: the caller stores what identifies the object, and every consumer that needs an
        address derives one for itself.

        Takes bytes rather than the upload part, because the format the object is stored under
        comes from :meth:`validate_image_file` reading those bytes -- not from the part's name or
        content type, which the client chose and which therefore never reach this call.
        """
        self._ensure_enabled()
        image_format = self.validate_image_file(
            content, max_size_mb=self.max_image_size_mb
        )
        public_id = self.profile_public_id(owner_regno, self.new_asset_id())

        try:
            result = cloudinary_upload(
                io.BytesIO(content),
                public_id=public_id,
                resource_type="image",
                type="upload",
                overwrite=False,
                # Deliberately no ``access_mode`` and no ``access_control``. An explicit
                # ``access_mode`` is how a deployment ends up with a public CDN URL for an object
                # that is actually stored private (the URL still resolves, it just 401s, which
                # looks like a broken image rather than a misconfiguration); omitting both is what
                # leaves Cloudinary's own public default in place for an ordinary ``upload``.
                filename=f"profile.{image_format}",
                tags=self._asset_tags(settings.CLOUDINARY_PROFILE_ASSET_TAG),
            )
        except Exception as exc:
            logger.error("Cloudinary profile upload failed for %s: %s", owner_regno, exc)
            raise HTTPException(
                status_code=500, detail="Failed to upload profile image"
            ) from exc

        stored_public_id = result.get("public_id") or public_id
        stored_format = (result.get("format") or image_format).lower()

        # Defence in depth against an account-level setting that forces non-public delivery. The
        # upload itself is harmless either way -- the URL above is derived, not stored -- but
        # leaving an object behind that the CDN will not serve is an orphan nobody can find.
        access_mode = (result.get("access_mode") or "").lower()
        if result.get("access_control") or access_mode in {"authenticated", "private"}:
            logger.error(
                "Cloudinary returned a non-public profile upload for %s (access_mode=%s, "
                "access_control=%s); destroying it",
                owner_regno, access_mode, result.get("access_control"),
            )
            self._destroy(stored_public_id, delivery_type="authenticated")
            raise HTTPException(
                status_code=500, detail="Failed to upload profile image"
            )

        logger.info(
            "Stored profile image for %s as %s.%s", owner_regno, stored_public_id, stored_format
        )
        return {"public_id": stored_public_id, "format": stored_format}

    def upload_love_note_image(
        self, *, image_base64: str, love_note_id: str
    ) -> Dict[str, str]:
        """Upload a love note image as a private, signed-delivery object and return its identity.

        Returns ``{"public_id": ..., "format": ...}``, the same shape as
        :meth:`upload_profile_image` and for the same reason: identity is durable, a URL is not.
        """
        self._ensure_enabled()
        image_bytes, image_format, _dimensions = self.decode_base64_image(image_base64)
        public_id = self.love_note_public_id(love_note_id)

        try:
            result = cloudinary_upload(
                io.BytesIO(image_bytes),
                public_id=public_id,
                resource_type="image",
                # ``authenticated`` is what makes Cloudinary refuse delivery without a valid
                # signature; ``upload`` on a public account would be served to anybody who had the
                # URL.
                type="authenticated",
                # Deliberately no ``access_control``. This used to apply
                # ``[{"access_type": "token"}]``, and that was a trap: Cloudinary *accepted* and
                # stored the ACL on an account with no Auth token key, reported it back in the
                # upload response so the verification below "passed", and then rejected the
                # signature-based URL the app actually issues -- so every love note image would
                # have been undeliverable while the upload log claimed it was protected.
                #
                # The ACL is now actively harmful and is asserted absent. A token ACL is not a
                # stronger version of a signed URL, it is a *different* requirement: it makes
                # Cloudinary demand a real token and treat the signature as insufficient. See
                # ``config`` for why the expiring token mechanism is unavailable on this plan.
                overwrite=False,
                filename=f"love-note.{image_format}",
                tags=self._asset_tags(settings.CLOUDINARY_LOVE_NOTE_ASSET_TAG),
            )
        except Exception as exc:
            logger.error("Cloudinary love note upload failed for note %s: %s", love_note_id, exc)
            raise HTTPException(
                status_code=500, detail="Failed to upload love note image"
            ) from exc

        stored_public_id = result.get("public_id") or public_id
        stored_format = (result.get("format") or image_format).lower()
        stored_type = (result.get("type") or "").lower()
        stored_access_control = result.get("access_control") or []

        # Verify Cloudinary stored what was asked for, in the direction that actually matters.
        # Two failure modes, and the second is the one that bites:
        #
        # * Not stored as ``authenticated`` -- the object would be publicly fetchable, so it is
        #   destroyed rather than left behind.
        # * Stored with a token ACL -- the object is private but *undeliverable*, because nothing
        #   here can mint a valid token. Destroying it makes the deployment fail loudly at send
        #   time, which is the only honest outcome; keeping it would produce a note whose image
        #   silently 403s in the recipient's inbox forever.
        token_acl = any(
            isinstance(entry, dict) and entry.get("access_type") == "token"
            for entry in stored_access_control
        )
        if stored_type != "authenticated" or token_acl:
            logger.error(
                "Cloudinary stored love note %s in a state we cannot deliver "
                "(type=%s, access_control=%s); destroying it",
                love_note_id, stored_type, stored_access_control,
            )
            self._destroy(stored_public_id, delivery_type=stored_type or "authenticated")
            raise HTTPException(
                status_code=503,
                detail=(
                    "Love note image delivery is not available: the configured Cloudinary "
                    "account did not store the image as private signed-delivery content."
                ),
            )

        logger.info("Stored love note image for note %s as %s", love_note_id, stored_public_id)
        return {"public_id": stored_public_id, "format": stored_format}

    # ------------------------------------------------------------------
    # Deletion
    # ------------------------------------------------------------------
    def _destroy(self, public_id: str, *, delivery_type: str) -> None:
        """Destroy one object, raising on an unexpected Cloudinary failure.

        ``invalidate=True`` drops the cached rendition as well as the original. Without it a
        deleted object can still be served from Cloudinary's CDN cache for a while afterwards,
        which would make "the note is gone" untrue at the CDN for as long as the cache entry lived.
        """
        try:
            cloudinary_destroy(
                public_id, resource_type="image", type=delivery_type, invalidate=True
            )
        except Exception as exc:
            logger.error("Cloudinary destroy failed for %s: %s", public_id, exc, exc_info=True)
            raise

    def delete_profile_asset(self, public_id: Optional[str], *, owner_regno: str) -> bool:
        """Destroy a profile picture, refusing anything the requesting account does not own.

        ``public_id`` comes from a database column and ``owner_regno`` from the account asking, so
        neither is trusted on its own: the identity is checked to sit under that account's own
        folder before anything is destroyed. ``owner_regno`` is keyword-only and required so a new
        call site cannot forget it -- an unowned delete is exactly the bug this guards against.

        Returns True when the object is gone afterwards, whether or not it was there to begin
        with: "already deleted" is the outcome the caller wanted, and treating a missing object as
        a failure would make a retried deletion look like an error forever.
        """
        self._ensure_enabled()
        if not public_id:
            return False

        owner_segment = self.profile_owner_segment(public_id)
        if not owner_regno or not str(owner_regno).strip():
            logger.warning(
                "Refused profile asset delete: no owning account supplied (public_id=%s)",
                public_id,
            )
            return False
        if owner_segment is None or owner_segment != _slug_segment(owner_regno):
            logger.warning(
                "Refused profile asset delete: %s is not owned by %s",
                public_id, owner_regno,
            )
            return False

        try:
            result = cloudinary_destroy(
                public_id, resource_type="image", type="upload", invalidate=True
            )
        except Exception as exc:
            logger.error(
                "Cloudinary profile delete failed for %s (owner %s): %s",
                public_id, owner_regno, exc, exc_info=True,
            )
            return False
        logger.info(
            "Deleted profile asset %s for %s (result=%s)",
            public_id, owner_regno, result.get("result"),
        )
        return True

    def delete_love_note_asset(self, public_id: Optional[str]) -> bool:
        """Destroy a love note image.

        Takes the public_id read from the authoritative row, not a value derived from a request,
        and checks it has the love note layout before acting -- so a corrupt or hand-written value
        cannot make a note's deletion destroy an unrelated object.

        A missing object is success. The row is being deleted either way, and the alternative --
        treating "Cloudinary has never heard of it" as an error -- would leave a note that cannot
        be deleted at all because its image was removed by hand.

        An unexpected failure raises an :class:`HTTPException` carrying a fixed message rather than
        the SDK's exception. Cloudinary's error text embeds the cloud name and the public_id, so a
        re-raise would hand both to any caller that forwards ``exc.detail`` -- and "wrap it in a
        handler" is exactly the kind of rule a future caller forgets. The real cause is logged with
        a traceback first, so nothing is lost by not propagating it.
        """
        self._ensure_enabled()
        if not public_id:
            return False
        if self.love_note_id_segment(public_id) is None:
            logger.error("Refused love note asset delete: %s is not a love note public_id", public_id)
            return False

        try:
            result = cloudinary_destroy(
                public_id, resource_type="image", type="authenticated", invalidate=True
            )
        except Exception as exc:
            logger.error("Cloudinary love note delete failed for %s: %s", public_id, exc, exc_info=True)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete file",
            )

        outcome = (result or {}).get("result")
        logger.info("Deleted love note asset %s (result=%s)", public_id, outcome)
        return outcome in {"ok", "not found"}

    def rotate_love_note_asset(
        self, public_id: Optional[str], image_format: Optional[str]
    ) -> bool:
        """Destroy and re-upload one love note image, killing previously minted URLs.

        A signed URL cannot be revoked: its signature covers the object's path and
        version, and stays valid as long as that version exists. Re-uploading the same
        bytes under the same public_id mints a new version, so every URL signed for the
        old one stops verifying while entitled viewers keep working -- their next view
        mints a fresh URL for the new version. The object is fetched back with a
        server-minted signature (this side holds the API secret), so rotation needs no
        bytes the database does not already describe.

        Best-effort by contract: returns True when the object was replaced, False on any
        failure, and never raises. Callers rotate on events (like an account block) that
        must go through whether or not the object store cooperates; a False is logged
        with the public_id so the orphan can be found and cleaned up later.
        """
        self._ensure_enabled()
        if not public_id or self.love_note_id_segment(public_id) is None:
            logger.error(
                "Refused love note asset rotation: %s is not a love note public_id",
                public_id,
            )
            return False

        try:
            download_url = self.love_note_delivery_url(public_id, image_format)
            if not download_url:
                logger.error("Refused love note asset rotation: no delivery URL for %s", public_id)
                return False
            content = _fetch_url_bytes(download_url, ROTATE_DOWNLOAD_MAX_BYTES)
        except Exception as exc:
            logger.error("Love note asset rotation download failed for %s: %s", public_id, exc)
            return False

        try:
            result = cloudinary_upload(
                io.BytesIO(content),
                public_id=public_id,
                resource_type="image",
                type="authenticated",
                overwrite=True,
                # The CDN caches by URL, and the old signed URLs are distinct cache keys
                # that must stop resolving -- same reasoning as :meth:`_destroy`.
                invalidate=True,
                filename=f"love-note.{(image_format or 'png').lower()}",
                tags=self._asset_tags(settings.CLOUDINARY_LOVE_NOTE_ASSET_TAG),
            )
        except Exception as exc:
            logger.error("Love note asset rotation upload failed for %s: %s", public_id, exc)
            return False

        stored_type = ((result or {}).get("type") or "").lower()
        if stored_type != "authenticated":
            # A replaced object that came back servable without a signature is worse
            # than the leak rotation was meant to close: destroy it rather than leave a
            # public object behind, and report failure.
            logger.error(
                "Love note asset rotation stored %s as %s; destroying it",
                public_id, stored_type,
            )
            try:
                self._destroy(public_id, delivery_type=stored_type or "authenticated")
            except Exception as exc:
                logger.error(
                    "Love note asset rotation cleanup destroy failed for %s: %s",
                    public_id, exc, exc_info=True,
                )
            return False

        logger.info("Rotated love note asset %s", public_id)
        return True


# Create a global instance for use across the application
storage_service = CloudinaryStorageService()
