import os
import io
import time
import secrets
import logging
import base64
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple
from fastapi import HTTPException, UploadFile
from urllib.parse import quote, unquote, urlparse
from dotenv import load_dotenv
import cloudinary
from cloudinary.uploader import upload as cloudinary_upload, destroy as cloudinary_destroy
from cloudinary.utils import cloudinary_url, api_sign_request, private_download_url
from PIL import Image
import re
import cloudinary.api as cloudinary_api

# Load environment variables from a .env file
load_dotenv()

# Logger
logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer from the environment, falling back to ``default``.

    A malformed value must not silently disable a limit, so anything unparseable or
    below ``minimum`` falls back to the built-in default rather than being trusted.
    """
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class CloudinaryStorageService:
    """Storage service implementation backed by Cloudinary."""

    # TTL Constants
    DEFAULT_SIGNED_URL_TTL = 3600  # 1 hour for viewing
    UPLOAD_SIGNATURE_TTL = 900     # 15 minutes for uploads
    TEMP_URL_TTL = 300            # 5 minutes for temporary access

    # Object path validation limits
    OBJECT_PATH_MAX_LENGTH = 512
    OBJECT_PATH_MIN_SEGMENTS = 2   # {folder}/{token}/{timestamp}_{name}.{ext}
    _OBJECT_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")

    # Granularity of the bounded upload read. Small enough that the peak buffer stays
    # close to the cap instead of overshooting it by a whole chunk.
    UPLOAD_READ_CHUNK_BYTES = 64 * 1024

    def __init__(self):
        self.cloudinary_url = os.getenv("CLOUDINARY_URL", "")
        self.api_key = os.getenv("CLOUDINARY_API_KEY")
        self.api_secret = os.getenv("CLOUDINARY_API_SECRET")
        self.cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

        # Bucket names are reused from existing env vars to avoid touching other parts of the app.
        self.bucket_name = os.getenv("CLOUDINARY_PROFILE_FOLDER")
        self.events_bucket = os.getenv("CLOUDINARY_EVENTS_FOLDER")
        self.gallery_bucket = os.getenv("CLOUDINARY_GALLERY_FOLDER")
        self.love_notes_bucket = os.getenv("CLOUDINARY_LOVE_NOTES_FOLDER", "love_notes")

        try:
            self.max_image_size_mb = max(1, int(os.getenv("MAX_IMAGE_SIZE_MB", "2")))
        except ValueError:
            self.max_image_size_mb = 2
        self.max_image_bytes = self.max_image_size_mb * 1024 * 1024
        self.allowed_extensions = {"jpg", "jpeg", "png", "gif", "webp"}

        # Geometry limits. A byte cap alone does not bound image processing: a few
        # hundred KB of compressed data can describe a bitmap that costs gigabytes
        # to decode, so both the per-side dimensions and the total pixel count are
        # capped and checked from the header before any pixel data is touched.
        self.max_image_dimension = _env_int("MAX_IMAGE_DIMENSION", 6000)
        self.max_image_pixels = _env_int("MAX_IMAGE_PIXELS", 12_000_000)

        # Upper bound on a base64 image payload, used for JSON-body uploads (love
        # notes) where the encoded string arrives as a request field. base64 spends
        # 4 characters per 3 bytes, plus slack for a data URI prefix and the padding
        # on the final group. Enforced *before* b64decode so an oversized body is
        # rejected without the decode ever allocating a second copy of it.
        self.max_base64_chars = (self.max_image_bytes // 3) * 4 + 4096

        # Optional public base URL to construct absolute URLs (e.g., http://localhost:8080)
        self.public_base = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")

        self.enabled = bool(self.cloudinary_url)
        self.can_sign = False

        # Feature toggles / security knobs
        self.use_advanced_transforms = os.getenv("CLOUDINARY_USE_ADVANCED_TRANSFORMS", "true").lower() in {"1", "true", "yes"}
        # Delivery scheme for every generated asset URL. Defaults to HTTPS: emitting plain
        # HTTP would hand the browser a downgradeable URL, so a deployment has to opt out
        # deliberately rather than inherit it from a development default.
        self.secure_urls = os.getenv("CLOUDINARY_SECURE_URLS", "true").lower() in {"1", "true", "yes"}
        self.default_view_ttl = int(os.getenv("CLOUDINARY_DEFAULT_VIEW_TTL", str(self.DEFAULT_SIGNED_URL_TTL)))
        self.max_view_ttl = int(os.getenv("CLOUDINARY_MAX_VIEW_TTL", "7200"))  # 2 hours cap by default
        # Additional allowed formats override (comma separated) if provided
        extra_formats = os.getenv("CLOUDINARY_ALLOWED_FORMATS")
        if extra_formats:
            for fmt in extra_formats.split(","):
                fmt_clean = fmt.strip().lower()
                if fmt_clean:
                    self.allowed_extensions.add(fmt_clean)

        if not self.enabled:
            print("Cloudinary storage disabled: CLOUDINARY_URL not configured")
        else:
            # Public (non-authenticated) delivery; the scheme comes from CLOUDINARY_SECURE_URLS.
            cfg_kwargs = {"secure": self.secure_urls}
            self.use_authenticated = False  # Always use public URLs with random strings for security
            if self.cloud_name:
                cfg_kwargs["cloud_name"] = self.cloud_name
                if self.api_key:
                    cfg_kwargs["api_key"] = self.api_key
                if self.api_secret:
                    cfg_kwargs["api_secret"] = self.api_secret
            else:
                if self.cloudinary_url:
                    cfg_kwargs["cloudinary_url"] = self.cloudinary_url
                if self.api_key:
                    cfg_kwargs["api_key"] = self.api_key
                if self.api_secret:
                    cfg_kwargs["api_secret"] = self.api_secret

            cfg = cloudinary.config(**cfg_kwargs)
            self.can_sign = bool(getattr(cfg, "api_secret", self.api_secret))
            if not self.can_sign:
                print("Cloudinary signing disabled: api_secret not configured")
            print("Cloudinary storage service initialized successfully (using public URLs with random strings)")

        # Route to bucket map used for parsing stored values
        self._route_bucket_map = {
            "profile": self.bucket_name,
            "events": self.events_bucket,
            "gallery": self.gallery_bucket,
            "committee": self.gallery_bucket,
            "love-notes": self.love_notes_bucket,
        }

    def _ensure_enabled(self):
        """Check if Cloudinary storage is enabled and properly configured."""
        if not self.enabled:
            raise RuntimeError("Cloudinary storage is not enabled. Please configure CLOUDINARY_URL environment variable.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _publicize(self, path: str) -> str:
        """Used to prefix a path with the PUBLIC_BASE_URL if configured."""
        if not path:
            return path
        return f"{self.public_base}{path}" if self.public_base else path

    def get_public_url_for_bucket(self, bucket_name: str, object_path: str, route_key: Optional[str] = None) -> str:
        """Construct the public API route for a stored object."""
        if route_key == "committee" and bucket_name == self.gallery_bucket:
            base = "/api/committee/files/"
        elif bucket_name == self.events_bucket:
            base = "/api/events/files/"
        elif bucket_name == self.gallery_bucket:
            base = "/api/gallery/files/"
        elif bucket_name == self.bucket_name:
            base = "/api/profile/files/"
        else:
            base = f"/api/files/{bucket_name}/"
        return self._publicize(f"{base}{quote(object_path)}")

    async def read_upload_capped(
        self, file: UploadFile, max_bytes: Optional[int] = None
    ) -> bytes:
        """Read an upload into memory without ever buffering more than ``max_bytes``.

        ``await file.read()`` with no size argument pulls the entire body into the
        process before any validation has run, so a single request can pin an arbitrary
        amount of RAM for as long as the client keeps sending. Two guards, because
        either one alone is bypassable:

        1. the size the multipart parser already read off the part headers, which
           refuses an oversize upload without touching its body at all;
        2. a chunked read that stops as soon as the accumulated bytes pass the cap. This
           is the one that matters, because a chunked request carries no declared
           length for the first guard to check.

        Defaults to ``max_image_bytes`` so a profile picture and a love note image are
        held to the same limit.
        """
        limit = self.max_image_bytes if max_bytes is None else max_bytes

        # getattr because UploadFile.size only exists on newer Starlette releases; when
        # it is absent the size is simply unknown and the chunked read below covers it.
        declared = getattr(file, "size", None)
        if declared is not None and declared > limit:
            raise HTTPException(
                status_code=413, detail=f"File too large. Maximum size is {limit // (1024 * 1024)}MB"
            )

        buffered = bytearray()
        while True:
            chunk = await file.read(self.UPLOAD_READ_CHUNK_BYTES)
            if not chunk:
                break
            buffered.extend(chunk)
            if len(buffered) > limit:
                raise HTTPException(
                    status_code=413, detail=f"File too large. Maximum size is {limit // (1024 * 1024)}MB"
                )

        return bytes(buffered)

    def validate_image_file(self, file: UploadFile, content: bytes, max_size_mb: Optional[int] = None) -> str:
        """Validate image size and content type.

        If max_size_mb is provided, enforce that size limit.
        If max_size_mb is None, no size validation is performed.
        """
        # Enforce size limit only if specified
        if max_size_mb is not None:
            max_bytes = int(max_size_mb) * 1024 * 1024
            if len(content) > max_bytes:
                raise HTTPException(status_code=413, detail=f"File too large. Maximum size is {max_size_mb}MB")

        kind = self._get_image_format(content)
        allowed_types = {"jpeg", "png", "gif", "webp"}
        allowed_mimes = {"image/jpeg", "image/jpg", "image/png", "image/gif", "image/webp"}

        if (kind not in allowed_types) and (file.content_type not in allowed_mimes):
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Only images (JPEG, PNG, GIF, WebP) are allowed"
            )

        if kind:
            # A byte cap does not bound decoding: the same few KB can describe a bitmap
            # that costs gigabytes to materialise, so the header geometry is checked too.
            width, height = self._get_image_size(content)
            self._check_image_dimensions(width, height)

        return kind or "jpeg"

    def _check_image_dimensions(self, width: Optional[int], height: Optional[int]) -> None:
        """Reject images whose decoded pixel grid is too large to process safely.

        Called with dimensions read from the image header, before any pixel data is
        decoded, so the cost of the rejection is independent of the claimed size.
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

    def decode_base64_image(self, image_base64: str) -> Tuple[bytes, str, Tuple[int, int]]:
        """Decode and validate a base64-encoded image, bounding work at every stage.

        Used by JSON-body upload paths, where the encoded string is already fully
        resident in memory by the time this runs. The order of the checks is the point:

        1. the encoded length, because ``b64decode`` allocates a second buffer of the
           same order and an unbounded body is the cheapest way to exhaust memory;
        2. the decoded length, before the buffer is handed to an image parser;
        3. format and header dimensions, read from the image header only -- PIL's
           ``Image.open`` is lazy, so nothing is decompressed until ``.size`` is
           compared against the limits above.

        The first check is O(1) and happens before the payload is even scanned, so the
        cost of refusing a large body does not grow with the size of that body.

        Returns:
            Tuple of (decoded bytes, lowercase format, (width, height)).
        """
        if not isinstance(image_base64, str):
            raise HTTPException(status_code=400, detail="Image data is required")

        # Checked against the raw field, before anything scans it. Stripping the data
        # URI prefix and folding whitespace out can only make the string shorter, so
        # this is exact and it costs O(1) -- an oversized body is turned away without
        # walking its tens of megabytes, let alone decoding it.
        if len(image_base64) > self.max_base64_chars:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Maximum size is {self.max_image_size_mb}MB",
            )

        # Accept both a bare base64 string and a data URI ("data:image/png;base64,...").
        payload = image_base64.split(",", 1)[1] if "," in image_base64 else image_base64
        # Fold out any whitespace, which is legal in base64 but would otherwise count
        # against the encoded-length budget without contributing any decoded bytes.
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

        try:
            img = Image.open(io.BytesIO(image_bytes))
            width, height = img.size
            image_format = (img.format or "").lower() or None
            img.close()
        except HTTPException:
            raise
        except Exception:
            # Unreadable, truncated, or a decompression bomb PIL refused outright.
            raise HTTPException(status_code=400, detail="Invalid image data")

        if not image_format:
            raise HTTPException(status_code=400, detail="Invalid image data")
        if image_format not in self.allowed_extensions:
            raise HTTPException(
                status_code=415,
                detail="Invalid file type. Only images (JPEG, PNG, GIF, WebP) are allowed",
            )

        self._check_image_dimensions(width, height)
        return image_bytes, image_format, (width, height)

    def generate_object_path(self, user_folder: str, filename: str, image_type: str) -> str:
        """Generate a unique path with a random string for security (prevents URL guessing).
        
        Format: {user_folder}/{random_string}/{timestamp}_{filename}
        The random string makes URLs unpredictable while keeping them organized by user.
        """
        # Generate a secure random string (16 chars = 96 bits of entropy)
        random_string = secrets.token_urlsafe(16)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        safe_filename = os.path.basename(filename or f"profile.{image_type}")
        if not safe_filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            safe_filename = f"{safe_filename}.{image_type}"
        # Sanitize filename to avoid spaces/special chars
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", safe_filename)
        return f"{user_folder}/{random_string}/{timestamp}_{safe_filename}"

    def generate_generic_object_path(self, filename: str, image_type: Optional[str] = None, prefix: Optional[str] = None) -> str:
        """Generate a unique path for non-user specific uploads with random string."""
        random_string = secrets.token_urlsafe(16)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        base_name = filename or (f"image.{image_type}" if image_type else "file")
        safe_filename = os.path.basename(base_name)
        if image_type and not safe_filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            safe_filename = f"{safe_filename}.{image_type}"
        # Sanitize filename
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", safe_filename)
        if prefix:
            return f"{prefix}/{random_string}/{timestamp}_{safe_filename}"
        return f"{random_string}/{timestamp}_{safe_filename}"

    def _build_public_id(self, bucket_name: str, object_path: str) -> str:
        """Construct the Cloudinary public ID from a bucket and object path."""
        combined = f"{bucket_name}/{object_path.lstrip('/')}" if bucket_name else object_path.lstrip("/")
        base, _ = os.path.splitext(combined)
        return base.rstrip("-_")

    def build_public_id(self, bucket_name: str, object_path: str) -> str:
        """Public helper to derive the Cloudinary public ID for an object path."""
        return self._build_public_id(bucket_name, object_path)

    def validate_object_path(self, object_path: Any) -> str:
        """Validate an untrusted object path before it is turned into a public ID.

        Upload helpers always generate paths shaped like
        ``{folder}/{token}/{timestamp}_{name}.{ext}`` (see ``generate_object_path`` and
        ``generate_generic_object_path``). Anything that cannot be produced by those
        helpers is rejected here, so a caller-supplied path can never escape its
        configured Cloudinary folder.

        Returns the normalised path, or raises ``HTTPException`` with 400.
        """
        if not isinstance(object_path, str):
            raise HTTPException(status_code=400, detail="Invalid file path")

        candidate = object_path.strip()
        if not candidate or len(candidate) > self.OBJECT_PATH_MAX_LENGTH:
            raise HTTPException(status_code=400, detail="Invalid file path")

        # Absolute paths, Windows separators and traversal sequences are never generated,
        # and are the only ways a path could reach a neighbouring folder or bucket.
        if candidate.startswith("/") or "\\" in candidate or ".." in candidate:
            raise HTTPException(status_code=400, detail="Invalid file path")

        # Percent signs are excluded by the segment allow-list below; this explicit check
        # keeps double-encoded traversal attempts from being silently normalised later.
        if "%" in candidate:
            raise HTTPException(status_code=400, detail="Invalid file path")

        # NUL and other control characters can truncate paths in downstream consumers.
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in candidate):
            raise HTTPException(status_code=400, detail="Invalid file path")

        segments = candidate.split("/")
        if len(segments) < self.OBJECT_PATH_MIN_SEGMENTS:
            raise HTTPException(status_code=400, detail="Invalid file path")
        for segment in segments:
            # Rejects empty segments ("//") and dot-only segments (".", "...") alike.
            if not segment or set(segment) == {"."}:
                raise HTTPException(status_code=400, detail="Invalid file path")
            if not self._OBJECT_PATH_SEGMENT_RE.match(segment):
                raise HTTPException(status_code=400, detail="Invalid file path")

        _, ext = os.path.splitext(segments[-1])
        if ext.lstrip(".").lower() not in self.allowed_extensions:
            raise HTTPException(status_code=400, detail="Unsupported file type")

        return candidate

    def _validate_extension(self, filename: str) -> str:
        """Used to validate the file extension for allowed image types."""
        _, ext = os.path.splitext(filename)
        clean = ext.lstrip(".").lower()
        if not clean:
            raise HTTPException(status_code=400, detail="Filename must include an image extension")
        if clean not in self.allowed_extensions:
            raise HTTPException(status_code=400, detail="Unsupported image type. Allowed: JPG, JPEG, PNG, GIF, WEBP")
        return clean

    def prepare_signed_upload(
        self,
        *,
        route_key: str,
        filename: str,
        user_folder: Optional[str] = None,
        prefix: Optional[str] = None,
        resource_type: str = "image",
        ttl_seconds: int = 900,
    ) -> Dict[str, object]:
        """Generate a short-lived Cloudinary signature for direct client uploads."""
        self._ensure_enabled()

        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid upload target")

        cfg = cloudinary.config(secure=True)
        api_secret = getattr(cfg, "api_secret", None)
        api_key = getattr(cfg, "api_key", None)
        cloud_name = getattr(cfg, "cloud_name", None)

        if not api_key or not api_secret or not cloud_name:
            raise HTTPException(status_code=500, detail="Cloudinary credentials are not fully configured")

        extension = self._validate_extension(filename)

        if route_key == "profile":
            if not user_folder:
                raise HTTPException(status_code=400, detail="display name is required for profile uploads")
            object_path = self.generate_object_path(user_folder, filename, extension)
        else:
            resolved_prefix = prefix
            if route_key == "committee" and not resolved_prefix:
                resolved_prefix = "committee"
            object_path = self.generate_generic_object_path(filename, extension, prefix=resolved_prefix)

        public_id = self._build_public_id(bucket_name, object_path)
        timestamp = int(time.time())
        expires_at = timestamp + int(ttl_seconds)

        # Decide the suggested max bytes to return to the client BEFORE building the params
        # so that it can be included in the signature parameters.
        # Profile uploads: strict small size (2MB). Others: generous large cap (100MB).
        suggested_max_bytes = 2 * 1024 * 1024 if route_key == "profile" else 100 * 1024 * 1024

        # Enhanced security parameters for upload (must be assembled after suggested_max_bytes is defined)
        # IMPORTANT: Only include parameters here that the client will ALSO send to Cloudinary.
        # Excluding max_file_size from signing because the frontend currently omits it, causing signature mismatch.
        params_to_sign = {
            "public_id": public_id,
            "created_at": timestamp,
            "invalidate": "true",
            "overwrite": "false",
            "access_mode": "public",  # Use public access with random strings
            # Add file type restrictions
            "allowed_formats": ",".join(sorted(self.allowed_extensions)),
        }

        # Optionally include transformation hints (only if explicitly enabled)
        if self.use_advanced_transforms:
            params_to_sign.update({
                "eager": "q_auto,f_auto",
                "colors": "true",
                "faces": "true",
            })

        signature = api_sign_request(params_to_sign, api_secret)

        upload_url = f"https://api.cloudinary.com/v1_1/{cloud_name}/{resource_type}/upload"
        asset_url = self.get_public_url_for_bucket(bucket_name, object_path, route_key)
        secure_url = self.get_secure_url_for_bucket(bucket_name, object_path)

        params = {k: str(v) for k, v in params_to_sign.items()}
        # Provide advisory (not signed) limits separately so frontend can enforce client-side if desired
        params_advisory = {
            "max_file_size": str(suggested_max_bytes)
        }

        response: Dict[str, object] = {
            "upload_url": upload_url,
            "api_key": api_key,
            "cloud_name": cloud_name,
            "signature": signature,
            "created_at": timestamp,
            "public_id": public_id,
            "object_path": object_path,
            "asset_url": asset_url,
            "secure_url": secure_url,
            "expires_at": expires_at,
            "resource_type": resource_type,
            "params": params,
            "advisory": params_advisory,
            "allowed_extensions": tuple(sorted(self.allowed_extensions)),
            "max_file_bytes": suggested_max_bytes,
        }

        return response

    # ------------------------------------------------------------------
    # Signed Delivery URL helpers (view / download)
    # ------------------------------------------------------------------
    def generate_temporary_view_url(self, route_key: str, object_path: str, ttl_seconds: Optional[int] = None) -> str:
        """Generate a short-lived signed URL for viewing an existing stored asset.

        NOTE: This MUST only be called from an authenticated backend route; do not
        expose this method directly to untrusted callers. ``object_path`` is validated
        because routes forward request paths straight into it.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage disabled")
        if not self.can_sign:
            raise HTTPException(status_code=503, detail="Cloudinary signing credentials are not configured")
        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid route key")
        object_path = self.validate_object_path(object_path)
        # The caller's window is honoured but clamped by _resolve_view_ttl, which makes
        # CLOUDINARY_DEFAULT_VIEW_TTL the default and CLOUDINARY_MAX_VIEW_TTL the ceiling.
        # Overriding the caller's value here instead would make both settings inert.
        return self.get_signed_url_for_bucket(bucket_name, object_path, expires_in_seconds=ttl_seconds)

    def build_dynamic_transform_url(self, route_key: str, object_path: str, *, width: Optional[int] = None,
                                    height: Optional[int] = None, quality: str = "auto", format_auto: bool = True,
                                    crop: Optional[str] = None, secure: Optional[bool] = None) -> str:
        """Build (optionally transformed) public delivery URL for an image.

        Use for on-demand thumbnails instead of storing multiple variants. ``secure``
        defaults to the configured CLOUDINARY_SECURE_URLS rather than plain HTTP.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage disabled")
        if secure is None:
            secure = self.secure_urls
        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid route key")
        public_id = self._build_public_id(bucket_name, object_path)
        transformation = []
        if width or height:
            t = {k: v for k, v in {"width": width, "height": height}.items() if v}
            if crop:
                t["crop"] = crop
            transformation.append(t)
        fmt = None
        if format_auto:
            fmt = None  # let Cloudinary decide via fetch_format=auto
        url, _ = cloudinary_url(
            public_id,
            secure=secure,
            resource_type="image",
            quality=quality,
            fetch_format="auto" if format_auto else None,
            type="upload",  # Use standard upload type
            transformation=transformation if transformation else None,
        )
        return url

    # ------------------------------------------------------------------
    # Post-upload verification (defense-in-depth for advisory limits)
    # ------------------------------------------------------------------
    def verify_uploaded_asset(self, route_key: str, object_path: str) -> Dict[str, object]:
        """Fetch the asset metadata from Cloudinary and enforce server-side policies.

        Intended to be called AFTER the client reports a successful direct upload.
        You can store returned metadata in DB if needed.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage disabled")
        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid route key")
        public_id = self._build_public_id(bucket_name, object_path)
        try:
            info = cloudinary_api.resource(public_id, resource_type="image", type="upload")
        except Exception as exc:  # broad catch: Cloudinary raises different exceptions
            raise HTTPException(status_code=404, detail=f"Asset not found: {exc}")

        bytes_size = info.get("bytes")
        fmt = (info.get("format") or "").lower()
        if bytes_size and bytes_size > self.max_image_bytes:
            # Optional: schedule deletion for oversize asset
            try:
                cloudinary_destroy(public_id, resource_type="image", invalidate=True)
            except Exception:
                pass
            raise HTTPException(status_code=413, detail="Uploaded file exceeds allowed size")
        if fmt and fmt not in self.allowed_extensions:
            try:
                cloudinary_destroy(public_id, resource_type="image", invalidate=True)
            except Exception:
                pass
            raise HTTPException(status_code=415, detail="Unsupported media format")
        return {
            "public_id": public_id,
            "bytes": bytes_size,
            "format": fmt,
            "width": info.get("width"),
            "height": info.get("height"),
            "created_at": info.get("created_at"),
            "secure_url": info.get("secure_url"),
        }

    def upload_profile_image(self, *, file: UploadFile, content: bytes, user_folder: str) -> Dict[str, object]:
        """Upload a profile image to Cloudinary as a public asset with random URL."""
        self._ensure_enabled()
        if not self.bucket_name:
            raise HTTPException(status_code=500, detail="Profile storage bucket is not configured")

        resolved_name = (user_folder or "user").strip() or "user"
        image_format = self.validate_image_file(file, content, max_size_mb=self.max_image_size_mb)
        object_path = self.generate_object_path(resolved_name, file.filename or "profile", image_format)
        public_id = self.build_public_id(self.bucket_name, object_path)

        try:
            upload_result = cloudinary_upload(
                io.BytesIO(content),
                public_id=public_id,
                resource_type="image",
                overwrite=True,
                invalidate=True,
                access_mode="public",  # Use public access instead of authenticated
                type="upload",  # Use standard upload type
                filename=file.filename or f"{resolved_name}-profile.{image_format}",
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to upload profile image: {exc}")

        # Get the public URL from Cloudinary's response
        cloudinary_url_result = upload_result.get("secure_url") or upload_result.get("url")
        
        # Also generate our own clean URL for consistency
        _, ext = os.path.splitext(object_path)
        fmt = ext.lstrip(".") or None
        url, _ = cloudinary_url(
            public_id,
            format=fmt,
            secure=self.secure_urls,
            resource_type="image",
            type="upload",
        )

        return {
            "object_path": object_path,
            "public_id": upload_result.get("public_id", public_id),
            "cloudinary_url": cloudinary_url_result or url,  # Full Cloudinary URL
            "url": cloudinary_url_result or url,  # Alias for consistency
            "version": upload_result.get("version"),
        }

    def get_signed_profile_url(self, identifier: Optional[str]) -> Optional[str]:
        """Return the profile URL. Since we now store full Cloudinary URLs with random strings,
        we can return them directly without additional signing.
        
        If identifier is already a full URL, return it.
        If it's an object path, construct the public URL from it.
        """
        if not self.enabled:
            return None
        
        if not identifier:
            return None
            
        # If it's already a full URL (http/https), return it directly
        if identifier.startswith("http://") or identifier.startswith("https://"):
            return identifier
            
        # If it's a data URL, return it
        if identifier.startswith("data:"):
            return identifier
            
        # Otherwise, it's an object path - construct the public URL
        try:
            return self._build_public_url(self.bucket_name, identifier)
        except Exception:
            return None

    def with_profile_signed_url(self, user_doc: Optional[Dict[str, Any]], *, field: str = "profile_picture_url") -> Optional[Dict[str, Any]]:
        """Return a shallow copy of ``user_doc`` with a signed profile picture URL."""
        if not user_doc:
            return user_doc
        doc_copy: Dict[str, Any] = dict(user_doc)
        doc_copy[field] = self.get_signed_profile_url(doc_copy.get(field))
        return doc_copy

    def _build_secure_url(self, bucket_name: str, object_path: str) -> str:
        """Construct a direct URL to a Cloudinary asset."""
        _, ext = os.path.splitext(object_path)
        fmt = ext.lstrip(".") or None
        public_id = self._build_public_id(bucket_name, object_path)
        url, _ = cloudinary_url(
            public_id,
            format=fmt,
            secure=self.secure_urls,
            resource_type="image",
            type="upload",
        )
        return url

    def _build_public_url(self, bucket_name: str, object_path: str) -> str:
        """Construct a public URL to a Cloudinary asset (alias for _build_secure_url)."""
        return self._build_secure_url(bucket_name, object_path)

    def get_secure_url_for_bucket(self, bucket_name: str, object_path: str) -> str:
        """Public helper to get a public URL for an object in a specific bucket."""
        return self._build_secure_url(bucket_name, object_path)

    def get_origin_url(self, route_key: str, object_path: str, secure: bool = True) -> str:
        """Return the untransformed Cloudinary origin URL for an object.

        Used by the asset delivery proxy, which fetches bytes server-side. Unlike
        ``_build_secure_url`` this defaults to HTTPS so the backend never issues a
        plain-HTTP request to the object store.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage is disabled")
        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid route key")
        public_id = self._build_public_id(bucket_name, object_path)
        _, ext = os.path.splitext(object_path)
        url, _ = cloudinary_url(
            public_id,
            format=ext.lstrip(".") or None,
            secure=secure,
            resource_type="image",
            type="upload",
        )
        return url

    def _resolve_view_ttl(self, requested: Optional[int]) -> int:
        """Clamp a requested view lifetime into the configured window.

        CLOUDINARY_DEFAULT_VIEW_TTL supplies the window when a caller does not ask for one
        and CLOUDINARY_MAX_VIEW_TTL caps it, so a caller cannot mint a link that outlives
        the policy just by passing a large ttl_seconds.
        """
        ttl = self.default_view_ttl if requested is None else int(requested)
        return max(1, min(ttl, self.max_view_ttl))

    def get_signed_url_for_bucket(self, bucket_name: str, object_path: str, expires_in_seconds: Optional[int] = None) -> str:
        """Generate a signed, time-limited delivery URL for an image in a specific bucket.

        The expiry is enforced by Cloudinary against a signature over ``expires_at``, not
        by this service, so the link really does stop working once the window closes. That
        enforcement only holds for assets uploaded with ``access_mode="authenticated"``;
        the upload paths in this service still upload public assets, which stay reachable
        at their unsigned URL regardless of what this returns. Treat these links as
        short-lived convenience URLs, not as the access-control boundary for public assets.

        Args:
            bucket_name: The name of the bucket containing the object
            object_path: The path to the object within the bucket
            expires_in_seconds: Requested lifetime; clamped to the configured window
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage is disabled")
        if not self.can_sign:
            raise HTTPException(status_code=503, detail="Cloudinary signing credentials are not configured")

        public_id = self._build_public_id(bucket_name, object_path)
        _, ext = os.path.splitext(object_path)
        expires_at = int(time.time()) + self._resolve_view_ttl(expires_in_seconds)
        return private_download_url(
            public_id,
            ext.lstrip(".") or None,
            resource_type="image",
            type="authenticated",
            expires_at=expires_at,
        )

    def _extract_from_cloudinary_url(self, url: str, bucket_name: str) -> Optional[str]:
        """Used to parse an object path from a full Cloudinary URL."""
        try:
            parsed = urlparse(url)
        except Exception:
            return None

        path = parsed.path.lstrip("/")
        if not path:
            return None

        segments = path.split("/")
        if len(segments) < 3:
            return None

        # Skip cloud_name, resource_type, delivery_type
        rest = segments[3:]  # Skip /cloud_name/image/upload/

        # Find the version part (v followed by digits)
        version_index = None
        for i, part in enumerate(rest):
            if part.startswith("v") and part[1:].isdigit():
                version_index = i
                break

        if version_index is None:
            return None

        # Public ID is everything after the version
        public_id_parts = rest[version_index + 1:]
        if not public_id_parts:
            return None

        public_id = "/".join(public_id_parts)

        # Remove bucket prefix if present
        if bucket_name and public_id.startswith(f"{bucket_name}/"):
            public_id = public_id[len(bucket_name) + 1:]

        return public_id

    def _extract_object_path(self, identifier: Optional[str], route_key: str, bucket_name: Optional[str]) -> Optional[str]:
        """Used to extract a clean object path from various identifier formats."""
        if not identifier:
            return None

        value = str(identifier).strip()
        if not value:
            return None

        if value.startswith("/api/"):
            remainder = value[len("/api/"):]
            parts = remainder.split("/", 2)
            if len(parts) >= 3 and parts[1] == "files":
                route = parts[0]
                if route == route_key:
                    return unquote(parts[2])
            # Generic pattern: /api/files/<bucket>/<object>
            if remainder.startswith("files/"):
                generic = remainder[len("files/"):]
                if bucket_name and generic.startswith(f"{bucket_name}/"):
                    generic = generic[len(bucket_name) + 1:]
                return unquote(generic)

        if value.startswith("http://") or value.startswith("https://"):
            return self._extract_from_cloudinary_url(value, bucket_name or "")

        return unquote(value.lstrip("/"))

    def normalize_stored_path(self, identifier: Optional[str], route_key: str) -> Optional[str]:
        """Used to normalize a stored identifier into a consistent object path."""
        bucket_name = self._route_bucket_map.get(route_key)
        return self._extract_object_path(identifier, route_key, bucket_name)

    def _get_image_format(self, content: bytes) -> Optional[str]:
        """Determine the image format from its binary content."""
        try:
            img = Image.open(io.BytesIO(content))
            return img.format.lower() if img.format else None
        except Exception:
            return None

    @staticmethod
    def _get_image_size(content: bytes) -> Tuple[Optional[int], Optional[int]]:
        """Read (width, height) from the image header, without decoding any pixels."""
        try:
            img = Image.open(io.BytesIO(content))
            width, height = img.size
            img.close()
            return width, height
        except Exception:
            return None, None
    # ------------------------------------------------------------------
    # Deletion operations
    # ------------------------------------------------------------------
    def delete_file(self, object_path: str) -> bool:
        """Used to delete a file from the default bucket."""
        return self.delete_file_from_bucket(self.bucket_name, object_path)

    def delete_file_from_bucket(
        self,
        bucket_name: str,
        object_path: Optional[str],
        *,
        actor: Optional[str] = None,
        asset_kind: Optional[str] = None,
    ) -> bool:
        """Used to delete a file from a specified bucket.

        ``actor`` and ``asset_kind`` are recorded on both the success and failure paths.
        Destructive Cloudinary calls are otherwise invisible after the fact: a wrong
        public_id is destroyed immediately and there is no way to tell afterwards which
        account caused it.
        """
        if not object_path:
            return False
        if not self.enabled:
            return False
        public_id = self._build_public_id(bucket_name, object_path)
        try:
            result = cloudinary_destroy(public_id, resource_type="image", invalidate=True)
        except Exception as exc:
            # Log the full detail server-side; the client only ever sees a generic
            # message, because the raw Cloudinary error embeds the bucket and public_id.
            logger.error(
                "Cloudinary delete failed bucket=%s kind=%s actor=%s public_id=%s: %s",
                bucket_name, asset_kind, actor, public_id, exc, exc_info=True,
            )
            raise HTTPException(status_code=500, detail="Failed to delete file")
        deleted = result.get("result") in {"ok", "not found"}
        logger.info(
            "Cloudinary delete bucket=%s kind=%s actor=%s public_id=%s result=%s",
            bucket_name, asset_kind, actor, public_id, result.get("result"),
        )
        return deleted

    @staticmethod
    def asset_owner_segment(object_path: Optional[str]) -> Optional[str]:
        """Return the account folder that owns ``object_path``, or None if unknowable.

        Every generated path is ``{owner}/{token}/{filename}`` (see
        ``generate_object_path``), so the first segment is the owner. This is the same
        fact ``asset_delivery_service`` relies on to authorise reads; deletion has to
        agree with it, or an account could be unable to see an asset yet still destroy it.
        """
        if not object_path:
            return None
        head = str(object_path).split("/", 1)[0].strip()
        return head or None

    def delete_profile_asset(
        self, identifier: Optional[str], *, owner_regno: str
    ) -> bool:
        """Used to delete a user's profile asset, refusing anything they do not own.

        The identifier is a stored column, not a trusted input, and the URL parser
        accepts any host -- so a value that was copied, or written by an admin creating
        a user, can name a different account's object. Requiring the parsed path to sit
        under the caller's own folder makes that impossible regardless of how the column
        came to hold the value.

        ``owner_regno`` is keyword-only and required so that no new call site can forget
        it: an unowned delete is exactly the bug this guards against.
        """
        object_path = self.normalize_stored_path(identifier, "profile")
        if not object_path:
            return False

        if not owner_regno or not str(owner_regno).strip():
            # Without a known owner there is nothing to check against, so refuse rather
            # than risk destroying an asset that belongs to somebody else.
            logger.warning(
                "Refused profile asset delete: no owning account supplied (actor=%s, public_id=%s)",
                owner_regno, self._build_public_id(self.bucket_name, object_path),
            )
            return False

        owner_segment = self.asset_owner_segment(object_path)
        if owner_segment != str(owner_regno).strip():
            logger.warning(
                "Refused profile asset delete: path is not owned by the requesting "
                "account (actor=%s, path_owner=%s, public_id=%s)",
                owner_regno, owner_segment,
                self._build_public_id(self.bucket_name, object_path),
            )
            return False

        return self.delete_file_from_bucket(
            self.bucket_name, object_path, actor=owner_regno, asset_kind="profile"
        )

    def delete_event_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete an asset associated with an event."""
        object_path = self.normalize_stored_path(identifier, "events")
        return self.delete_file_from_bucket(
            self.events_bucket, object_path, asset_kind="events"
        )

    def delete_gallery_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete an asset from the gallery."""
        object_path = self.normalize_stored_path(identifier, "gallery")
        return self.delete_file_from_bucket(
            self.gallery_bucket, object_path, asset_kind="gallery"
        )

    def delete_committee_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete a committee-related asset."""
        object_path = self.normalize_stored_path(identifier, "committee")
        return self.delete_file_from_bucket(
            self.gallery_bucket, object_path, asset_kind="committee"
        )

    def upload_love_note_image(self, *, image_base64: str, sender_name: str) -> Dict[str, Any]:
        """Upload a love note image (base64) to Cloudinary with a secure random URL.
        
        This method:
        1. Decodes the base64 image data, refusing payloads that exceed the encoded,
           decoded, or per-side/total-pixel limits
        2. Validates it's a proper, supported image
        3. Generates a secure, unpredictable path using random tokens
        4. Uploads to Cloudinary in a dedicated love_notes folder
        5. Returns the Cloudinary URL for storage in the database
        
        Args:
            image_base64: Base64 encoded image string (with or without data URI prefix)
            sender_name: display name of the sender (used for organizing files)
            
        Returns:
            Dictionary containing the Cloudinary URL and metadata

        Raises:
            HTTPException: 400 for undecodable/invalid data, 413 when the payload
                exceeds a size or dimension limit, 415 for an unsupported format.
        """
        self._ensure_enabled()
        
        # Create a dedicated folder for love notes if not configured
        love_notes_bucket = os.getenv("CLOUDINARY_LOVE_NOTES_FOLDER", "love_notes")
        
        try:
            # Decoding and validation are bounded before the buffer is parsed; see
            # decode_base64_image for why the checks run in that order.
            image_bytes, image_format, (width, height) = self.decode_base64_image(image_base64)
            
            # Generate a secure random path for the love note
            random_string = secrets.token_urlsafe(16)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
            safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", sender_name)
            filename = f"love_note_{timestamp}.{image_format}"
            object_path = f"{safe_name}/{random_string}/{filename}"
            
            # Build the Cloudinary public_id
            public_id = self._build_public_id(love_notes_bucket, object_path)
            
            # Upload to Cloudinary
            upload_result = cloudinary_upload(
                io.BytesIO(image_bytes),
                public_id=public_id,
                resource_type="image",
                overwrite=False,  # Don't overwrite - each note should be unique
                invalidate=True,
                access_mode="public",
                type="upload",
                filename=filename,
                # Second line of defence, for anything that reached Cloudinary without
                # passing the local checks. "limit" scales to fit inside the box and
                # never upscales, so a note at or below the limits is stored untouched
                # while an oversized one cannot be stored or delivered unbounded.
                transformation={
                    "width": self.max_image_dimension,
                    "height": self.max_image_dimension,
                    "crop": "limit",
                },
            )
            
            # Get the secure URL from Cloudinary
            cloudinary_secure_url = upload_result.get("secure_url") or upload_result.get("url")
            
            return {
                "object_path": object_path,
                "public_id": upload_result.get("public_id", public_id),
                "cloudinary_url": cloudinary_secure_url,
                "url": cloudinary_secure_url,
                "version": upload_result.get("version"),
                "format": upload_result.get("format", image_format),
                "width": width,
                "height": height,
            }
            
        except HTTPException:
            raise
        except Exception:
            # Log the cause server-side; the raw Cloudinary error embeds the bucket and
            # public_id, so the client only ever sees a generic message.
            logger.error("Failed to upload love note image to Cloudinary", exc_info=True)
            raise HTTPException(
                status_code=500, 
                detail="Failed to upload love note image to Cloudinary"
            )

    def delete_love_note_image(self, cloudinary_url: str) -> bool:
        """Delete a love note image from Cloudinary using its URL.
        
        Args:
            cloudinary_url: The Cloudinary URL of the love note image
            
        Returns:
            True if deletion was successful, False otherwise
        """
        try:
            # Extract public_id from the Cloudinary URL
            # Cloudinary URLs typically look like: https://res.cloudinary.com/{cloud_name}/image/upload/v{version}/{public_id}.{format}
            love_notes_bucket = os.getenv("CLOUDINARY_LOVE_NOTES_FOLDER", "love_notes")
            
            # Try to extract the object path from the URL
            if love_notes_bucket in cloudinary_url:
                # Parse out the path after the bucket name
                parts = cloudinary_url.split(love_notes_bucket + "/")
                if len(parts) > 1:
                    # Get everything after the bucket name, remove extension
                    object_path = parts[1].split("?")[0]  # Remove query params
                    base, _ = os.path.splitext(object_path)
                    return self.delete_file_from_bucket(love_notes_bucket, base)
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to delete love note image: {str(e)}")
            return False

    def list_user_files(self, user_folder: str, limit: int = 10) -> list:
        """List files for a specific user. Currently a placeholder."""
        # Cloudinary listing not currently required; return empty list for compatibility.
        return []


# Create a global instance for use across the application
storage_service = CloudinaryStorageService()
