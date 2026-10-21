# app/services/storage_service.py
"""Cloudinary-backed image storage.

Profile pictures are public ``upload`` objects; love note images are ``authenticated`` and served only via SHA-1 signed URLs. A signed URL cannot be revoked (no Auth-token key on this plan), so it is a bearer credential: minted per response, never stored or logged, served with ``Cache-Control: no-store``.
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
from ..logger import get_logger, log_identity_hash, redact_log_identifiers

logger = get_logger(__name__)


# Fixed object layout: changing the prefixes would orphan existing objects.
PROFILE_FOLDER = "profile_pics"
LOVE_NOTES_FOLDER = "love_notes"

# Formats checked against the decoded image header, never the client's content type or filename.
BASE_ALLOWED_FORMATS = frozenset({"jpg", "jpeg", "png", "gif", "webp"})

# Path-safe public_id alphabet: prevents traversal into a neighbouring object.
_PUBLIC_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Cloudinary's authenticated-delivery signature segment; the CDN refuses unsigned URLs.
# Does not match ``__cld_token__`` (a different, unused mechanism).
SIGNED_URL_PATTERN = re.compile(r"/image/authenticated/s--[A-Za-z0-9_-]+--/")

# Cap on bytes fetched back during rotation: anything larger is corruption or a changed object.
ROTATE_DOWNLOAD_MAX_BYTES = 16 * 1024 * 1024
_ROTATE_DOWNLOAD_CHUNK_BYTES = 64 * 1024
_ROTATE_DOWNLOAD_TIMEOUT_SECONDS = 30.0


def _fetch_url_bytes(url: str, cap_bytes: int) -> bytes:
    """Fetch one URL's body with a byte cap; factored out so tests can replace the network."""
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
    """Reduce ``value`` to a safe public_id segment; outside characters become dashes, e.g. for regnos like ``cs/2024``."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "")).strip("-")
    return (cleaned[:limit].strip("-")) or "account"


_PROFILE_ID_FOR_LOG_RE = re.compile(
    rf"({re.escape(PROFILE_FOLDER)}/)([^/]+)(/|$)"
)


def _safe_public_id_for_log(public_id: str) -> str:
    """Hash the owner segment of a profile public_id so logs never carry a raw regno; love-note ids are unchanged."""
    return _PROFILE_ID_FOR_LOG_RE.sub(
        lambda match: (
            f"{match.group(1)}regno_hash:{log_identity_hash(match.group(2))}"
            f"{match.group(3)}"
        ),
        str(public_id or ""),
    )


class CloudinaryStorageService:
    """Image storage backed by Cloudinary; the single process-wide instance configures the SDK's global config, as that SDK expects."""

    # Bounded upload read granularity: keeps the peak buffer close to the cap.
    UPLOAD_READ_CHUNK_BYTES = 64 * 1024

    # Byte caps do not bound decode cost; pixel ceilings come from the header before any decode.
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
        """Read image limits from settings; malformed values fall back to a default rather than disabling the limit."""

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

        # Base64 bound enforced before b64decode so an oversized body never allocates a decoded copy.
        self.max_base64_chars = (self.max_image_bytes // 3) * 4 + 4096

        self.allowed_extensions = set(BASE_ALLOWED_FORMATS)
        extra_formats = settings.CLOUDINARY_ALLOWED_FORMATS
        if extra_formats:
            for fmt in extra_formats.split(","):
                cleaned = fmt.strip().lower()
                # Operators may widen accepted formats, but never with path characters.
                if cleaned and re.fullmatch(r"[a-z0-9]{2,8}", cleaned):
                    self.allowed_extensions.add(cleaned)

    def _configure_cloudinary(self) -> None:
        """Push this deployment's Cloudinary credentials into the SDK's global config from ``settings``."""
        self.cloud_name = settings.CLOUDINARY_CLOUD_NAME
        self.api_key = settings.CLOUDINARY_API_KEY
        self.api_secret = settings.CLOUDINARY_API_SECRET

        self.enabled = bool(self.cloud_name and self.api_key and self.api_secret)
        if self.enabled:
            cloudinary.config(
                cloud_name=self.cloud_name,
                api_key=self.api_key,
                api_secret=self.api_secret,
                # HTTPS only: a deployment cannot accidentally emit a downgradeable HTTP URL.
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
        """Normalise grouping tags for one upload; a bad label is dropped or skipped, never fails the upload."""
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
        """Read an upload into memory, capped: declared-size reject plus a chunked read guard (chunked requests carry no declared length)."""
        limit = self.max_image_bytes if max_bytes is None else max_bytes

        # ``UploadFile.size`` only exists on newer Starlette; the chunked read still enforces the cap.
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
        """Validate an image's size, type and geometry; returns the header-derived format (refusals run cap, then geometry, then full decode)."""
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

    # Bound on animated re-encode: decoding every frame is unbounded work, so an animation
    # past this frame count is refused rather than rebuilt.
    MAX_ANIMATED_FRAMES = 64

    def normalize_image(self, content: bytes, image_format: str) -> bytes:
        """Re-encode a validated image (EXIF/ICC/XMP stripped, orientation baked in) so the stored object is its pixels alone; animations are rebuilt within the frame cap."""
        if image_format not in BASE_ALLOWED_FORMATS:
            return content

        try:
            with Image.open(io.BytesIO(content)) as image:
                n_frames = getattr(image, "n_frames", 1)
                if n_frames > 1:
                    return self._normalize_animated(image, image_format, n_frames)
                frame = ImageOps.exif_transpose(image)
                save_format = "JPEG" if image_format in {"jpg", "jpeg"} else image_format.upper()
                save_kwargs: Dict[str, Any] = {"optimize": True}
                if save_format == "JPEG":
                    # Orientation is already applied, and JPEG has no alpha to lose.
                    frame = frame.convert("RGB")
                    save_kwargs["quality"] = 85
                elif save_format == "WEBP":
                    save_kwargs.pop("optimize")
                    save_kwargs["quality"] = 85
                reencoded = io.BytesIO()
                frame.save(reencoded, format=save_format, **save_kwargs)
                return reencoded.getvalue()
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "Could not re-encode a validated %s image (%s); storing it as received",
                image_format,
                exc,
            )
            return content

    def _normalize_animated(self, image: Image.Image, image_format: str, n_frames: int) -> bytes:
        """Re-encode an animation with all metadata stripped, bounded by frame cap."""
        from fastapi import HTTPException as _HTTPException

        if n_frames > self.MAX_ANIMATED_FRAMES:
            raise _HTTPException(
                status_code=413,
                detail=(
                    f"Animated image has too many frames ({n_frames}; "
                    f"max {self.MAX_ANIMATED_FRAMES})"
                ),
            )
        save_format = "GIF" if image_format == "gif" else image_format.upper()
        frames: list[Image.Image] = []
        durations: list[int] = []
        try:
            for index in range(n_frames):
                image.seek(index)
                frame = ImageOps.exif_transpose(image.copy())
                # Drop per-frame EXIF/XMP/comments/ICC.
                try:
                    frame.info.clear()
                except Exception:
                    pass
                frames.append(frame)
                durations.append(int(image.info.get("duration", 100)))
        except EOFError:
            pass
        if not frames:
            return b""
        out = io.BytesIO()
        first, rest = frames[0], frames[1:]
        save_kwargs: Dict[str, Any] = {
            "format": save_format,
            "save_all": True,
            "append_images": rest,
            "loop": 0,
        }
        if durations:
            save_kwargs["duration"] = durations
        # No exif=, icc_profile=, xmp= or comment= kwargs: metadata stays dropped.
        first.save(out, **save_kwargs)
        return out.getvalue()

    def _check_image_dimensions(self, width: Optional[int], height: Optional[int]) -> None:
        """Reject images whose pixel grid is too large; read from the header before any decode."""
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
            # PIL refuses while opening, before dimensions can be read: a size refusal, not a type one.
            raise HTTPException(
                status_code=413, detail="Image too large. The image claims too many pixels to process."
            )
        except Exception:
            return None

    @staticmethod
    def _get_image_size(content: bytes) -> Tuple[Optional[int], Optional[int]]:
        """Read ``(width, height)`` from the header; ``Image.open`` is lazy, so bombs are rejected unmaterialised."""
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
        """Materialise every pixel so a lying header is caught; runs after the geometry check bounds decode cost, warnings as errors."""
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                with Image.open(io.BytesIO(content)) as img:
                    img.load()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid image data")

    def decode_base64_image(self, image_base64: str) -> Tuple[bytes, str, Tuple[int, int]]:
        """Decode and validate a base64 image, checks in cost order; returns (bytes, format, (width, height))."""
        if not isinstance(image_base64, str):
            raise HTTPException(status_code=400, detail="Image data is required")

        # Raw-field length first: O(1), and stripping a data URI prefix only makes it shorter.
        if len(image_base64) > self.max_base64_chars:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {self.max_image_size_mb}MB",
            )

        # Accept both a bare base64 string and a data URI ("data:image/png;base64,...").
        payload = image_base64.split(",", 1)[1] if "," in image_base64 else image_base64
        # Fold whitespace: legal in base64, but it would count against the encoded-length budget.
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
        """An unguessable, path-safe asset id (144 bits, restricted alphabet)."""
        return secrets.token_urlsafe(18).replace("-", "A").replace("_", "B")

    @classmethod
    def profile_public_id(cls, owner_regno: str, asset_id: str) -> str:
        """The public_id of a profile picture: ``profile_pics/{regno}/{opaque_id}/profile``; the opaque segment defeats guessing by regno."""
        return (
            f"{PROFILE_FOLDER}/{_slug_segment(owner_regno)}/"
            f"{_slug_segment(asset_id, limit=64)}/profile"
        )

    @staticmethod
    def love_note_public_id(love_note_id: str) -> str:
        """The public_id of a love note image: ``love_notes/{love_note_id}/image``, keyed on the row's own id."""
        note_segment = _slug_segment(love_note_id)
        if not re.fullmatch(r"[A-Za-z0-9-]{32,36}", note_segment):
            raise HTTPException(status_code=400, detail="Invalid love note id")
        return f"{LOVE_NOTES_FOLDER}/{note_segment}/image"

    @classmethod
    def profile_owner_segment(cls, public_id: Optional[str]) -> Optional[str]:
        """The regno segment of a profile public_id, or None; exact-shape match keeps deletes on the caller's own object."""
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
        """The note id segment of a love note public_id, or None; exact-shape match, as for profiles."""
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
        """The public HTTPS URL for a profile picture, no transformations; off-host URLs fail closed."""
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
        if not url or not url.startswith("https://res.cloudinary.com/"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Profile picture delivery is not configured",
            )
        return url

    def love_note_delivery_url(
        self, public_id: Optional[str], image_format: Optional[str]
    ) -> Optional[str]:
        """A signed HTTPS URL for one love note image; fails closed on an unsigned, off-host or out-of-layout URL.

        The caller must already have decided the viewer is entitled: a signature proves the CDN will serve, not that this viewer should see. The signature does not expire (no Auth-token key on this plan), so it is a durable bearer credential -- never stored or logged, served only with ``Cache-Control: no-store`` and ``Referrer-Policy: no-referrer``.
        """
        if not public_id:
            return None
        self._ensure_enabled()
        # Only mint for objects in this layout: a drifted or tampered id fails rather than signing something unintended.
        if self.love_note_id_segment(public_id) is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Love note image delivery is not configured",
            )

        url, _ = cloudinary_url(
            public_id,
            resource_type="image",
            type="authenticated",
            format=image_format or None,
            secure=True,
            # SDK SHA-1 URL signature keyed by the API secret (same credential as uploads). No
            # ``auth_token``: that needs an account Auth-token key this plan does not have.
            sign_url=True,
        )
        # Signature proves the CDN will serve; the host check must fail on a misconfigured cloud name.
        if (
            not url
            or not url.startswith("https://res.cloudinary.com/")
            or not SIGNED_URL_PATTERN.search(url)
        ):
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
        """Upload a profile picture as a public object, returning ``{"public_id", "format"}`` identity (never a URL)."""
        self._ensure_enabled()
        image_format = self.validate_image_file(
            content, max_size_mb=self.max_image_size_mb
        )
        content = self.normalize_image(content, image_format)
        public_id = self.profile_public_id(owner_regno, self.new_asset_id())

        try:
            result = cloudinary_upload(
                io.BytesIO(content),
                public_id=public_id,
                resource_type="image",
                type="upload",
                overwrite=False,
                # No ``access_mode``/``access_control``: omitting both keeps Cloudinary's
                # public default for an ``upload`` object; an explicit mode can 401 while the URL looks broken.
                filename=f"profile.{image_format}",
                tags=self._asset_tags(settings.CLOUDINARY_PROFILE_ASSET_TAG),
            )
        except Exception as exc:
            logger.error(
                "Cloudinary profile upload failed for regno_hash=%s: %s",
                log_identity_hash(owner_regno),
                redact_log_identifiers(exc, owner_regno, public_id),
            )
            raise HTTPException(
                status_code=500, detail="Failed to upload profile image"
            ) from exc

        stored_public_id = result.get("public_id") or public_id
        stored_format = (result.get("format") or image_format).lower()

        # Defence in depth against account-level non-public settings: destroy rather than keep an unservable object.
        access_mode = (result.get("access_mode") or "").lower()
        if result.get("access_control") or access_mode in {"authenticated", "private"}:
            logger.error(
                "Cloudinary returned a non-public profile upload for regno_hash=%s "
                "(access_mode=%s, access_control=%s); destroying it",
                log_identity_hash(owner_regno),
                access_mode,
                result.get("access_control"),
            )
            self._destroy(stored_public_id, delivery_type="authenticated")
            raise HTTPException(
                status_code=500, detail="Failed to upload profile image"
            )

        logger.info(
            "Stored profile image for regno_hash=%s as %s.%s",
            log_identity_hash(owner_regno),
            _safe_public_id_for_log(stored_public_id),
            stored_format,
        )
        return {"public_id": stored_public_id, "format": stored_format}

    def upload_love_note_image(
        self, *, image_base64: str, love_note_id: str
    ) -> Dict[str, str]:
        """Upload a love note image as a private signed-delivery object, returning identity (EXIF stripped too)."""
        self._ensure_enabled()
        image_bytes, image_format, _dimensions = self.decode_base64_image(image_base64)
        # Strip EXIF/XMP/comments before upload; animated frames are capped inside.
        image_bytes = self.normalize_image(image_bytes, image_format)
        public_id = self.love_note_public_id(love_note_id)

        try:
            result = cloudinary_upload(
                io.BytesIO(image_bytes),
                public_id=public_id,
                resource_type="image",
                # ``authenticated`` refuses delivery without a valid signature; ``upload`` would serve anyone with the URL.
                type="authenticated",
                # No ``access_control``: a token ACL is stored even without an Auth-token key
                # yet makes the signature URL we issue undeliverable -- a different requirement, not a stronger one.
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

        # Verify stored state: non-authenticated is public, token ACL is undeliverable; destroy either.
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
        """Destroy one object, raising on failure; ``invalidate=True`` drops the cached rendition too."""
        try:
            cloudinary_destroy(
                public_id, resource_type="image", type=delivery_type, invalidate=True
            )
        except Exception as exc:
            logger.error(
                "Cloudinary destroy failed for %s: %s",
                _safe_public_id_for_log(public_id),
                redact_log_identifiers(
                    exc, public_id, self.profile_owner_segment(public_id)
                ),
                exc_info=True,
            )
            raise

    def delete_profile_asset(self, public_id: Optional[str], *, owner_regno: str) -> bool:
        """Destroy a profile picture, refusing anything the requesting account does not own; a missing object still returns True."""
        self._ensure_enabled()
        if not public_id:
            return False

        owner_segment = self.profile_owner_segment(public_id)
        if not owner_regno or not str(owner_regno).strip():
            logger.warning(
                "Refused profile asset delete: no owning account supplied (public_id=%s)",
                _safe_public_id_for_log(public_id),
            )
            return False
        if owner_segment is None or owner_segment != _slug_segment(owner_regno):
            logger.warning(
                "Refused profile asset delete: %s is not owned by regno_hash=%s",
                _safe_public_id_for_log(public_id),
                log_identity_hash(owner_regno),
            )
            return False

        try:
            result = cloudinary_destroy(
                public_id, resource_type="image", type="upload", invalidate=True
            )
        except Exception as exc:
            logger.error(
                "Cloudinary profile delete failed for %s (regno_hash=%s): %s",
                _safe_public_id_for_log(public_id),
                log_identity_hash(owner_regno),
                redact_log_identifiers(exc, public_id, owner_regno),
                exc_info=True,
            )
            return False
        logger.info(
            "Deleted profile asset %s for regno_hash=%s (result=%s)",
            _safe_public_id_for_log(public_id),
            log_identity_hash(owner_regno),
            result.get("result"),
        )
        return True

    def delete_love_note_asset(self, public_id: Optional[str]) -> bool:
        """Destroy a love note image, layout-checked first; a missing object is success and Cloudinary errors become a fixed HTTPException (SDK text embeds the cloud name and public_id)."""
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

        A signed URL is valid as long as the version it covers exists; re-uploading mints a new version, so old URLs stop verifying while entitled viewers' next view mints a fresh one. Best-effort: returns True when replaced, False on any failure, never raises.
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
                # The CDN caches by URL; old signed URLs are distinct keys that must stop resolving.
                invalidate=True,
                filename=f"love-note.{(image_format or 'png').lower()}",
                tags=self._asset_tags(settings.CLOUDINARY_LOVE_NOTE_ASSET_TAG),
            )
        except Exception as exc:
            logger.error("Love note asset rotation upload failed for %s: %s", public_id, exc)
            return False

        stored_type = ((result or {}).get("type") or "").lower()
        if stored_type != "authenticated":
            # An object that came back unsigned is worse than the leak being closed: destroy and report failure.
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
