import os
import io
import time
import secrets
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from fastapi import HTTPException, UploadFile
from urllib.parse import quote, unquote, urlparse
from dotenv import load_dotenv
import cloudinary
from cloudinary.uploader import upload as cloudinary_upload, destroy as cloudinary_destroy
from cloudinary.utils import cloudinary_url, api_sign_request
from PIL import Image
import re
import cloudinary.api as cloudinary_api

# Load environment variables from a .env file
load_dotenv()

# Logger
logger = logging.getLogger(__name__)


class CloudinaryStorageService:
    """Storage service implementation backed by Cloudinary."""

    # TTL Constants
    DEFAULT_SIGNED_URL_TTL = 3600  # 1 hour for viewing
    UPLOAD_SIGNATURE_TTL = 900     # 15 minutes for uploads
    TEMP_URL_TTL = 300            # 5 minutes for temporary access

    def __init__(self):
        self.cloudinary_url = os.getenv("CLOUDINARY_URL", "")
        self.api_key = os.getenv("CLOUDINARY_API_KEY")
        self.api_secret = os.getenv("CLOUDINARY_API_SECRET")
        self.cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")

        # Bucket names are reused from existing env vars to avoid touching other parts of the app.
        self.bucket_name = os.getenv("CLOUDINARY_PROFILE_FOLDER")
        self.events_bucket = os.getenv("CLOUDINARY_EVENTS_FOLDER")
        self.gallery_bucket = os.getenv("CLOUDINARY_GALLERY_FOLDER")

        try:
            self.max_image_size_mb = max(1, int(os.getenv("MAX_IMAGE_SIZE_MB", "2")))
        except ValueError:
            self.max_image_size_mb = 2
        self.max_image_bytes = self.max_image_size_mb * 1024 * 1024
        self.allowed_extensions = {"jpg", "jpeg", "png", "gif", "webp"}

        # Optional public base URL to construct absolute URLs (e.g., http://localhost:8080)
        self.public_base = (os.getenv("PUBLIC_BASE_URL", "") or "").rstrip("/")

        self.enabled = bool(self.cloudinary_url)
        self.can_sign = False

        # Feature toggles / security knobs
        self.use_advanced_transforms = os.getenv("CLOUDINARY_USE_ADVANCED_TRANSFORMS", "true").lower() in {"1", "true", "yes"}
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
            # Use public (non-authenticated) delivery with HTTP for development
            cfg_kwargs = {"secure": False}
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

        return kind or "jpeg"

    def generate_object_path(self, username: str, filename: str, image_type: str) -> str:
        """Generate a unique path with a random string for security (prevents URL guessing).
        
        Format: {username}/{random_string}/{timestamp}_{filename}
        The random string makes URLs unpredictable while keeping them organized by user.
        """
        # Generate a secure random string (16 chars = 96 bits of entropy)
        random_string = secrets.token_urlsafe(16)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
        safe_filename = os.path.basename(filename or f"profile.{image_type}")
        if not safe_filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
            safe_filename = f"{safe_filename}.{image_type}"
        # Sanitize filename to avoid spaces/special chars
        safe_filename = re.sub(r"[^A-Za-z0-9_.-]+", "-", safe_filename)
        return f"{username}/{random_string}/{timestamp}_{safe_filename}"

    def generate_generic_object_path(self, filename: str, image_type: Optional[str] = None, prefix: Optional[str] = None) -> str:
        """Generate a unique path for non-user specific uploads with random string."""
        random_string = secrets.token_urlsafe(16)
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
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
        username: Optional[str] = None,
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
            if not username:
                raise HTTPException(status_code=400, detail="Username is required for profile uploads")
            object_path = self.generate_object_path(username, filename, extension)
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
            "timestamp": timestamp,
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
            "timestamp": timestamp,
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
        expose this method directly to untrusted callers.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage disabled")
        if not self.can_sign:
            raise HTTPException(status_code=503, detail="Cloudinary signing credentials are not configured")
        bucket_name = self._route_bucket_map.get(route_key)
        if not bucket_name:
            raise HTTPException(status_code=400, detail="Invalid route key")
        # Force a fixed 1 hour (3600s) expiration window regardless of caller input
        ttl_seconds = self.DEFAULT_SIGNED_URL_TTL
        return self.get_signed_url_for_bucket(bucket_name, object_path, expires_in_seconds=ttl_seconds)

    def build_dynamic_transform_url(self, route_key: str, object_path: str, *, width: Optional[int] = None,
                                    height: Optional[int] = None, quality: str = "auto", format_auto: bool = True,
                                    crop: Optional[str] = None, secure: bool = False) -> str:
        """Build (optionally transformed) public delivery URL for an image.

        Use for on-demand thumbnails instead of storing multiple variants.
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage disabled")
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

    def upload_profile_image(self, *, file: UploadFile, content: bytes, username: str) -> Dict[str, object]:
        """Upload a profile image to Cloudinary as a public asset with random URL."""
        self._ensure_enabled()
        if not self.bucket_name:
            raise HTTPException(status_code=500, detail="Profile storage bucket is not configured")

        resolved_username = (username or "user").strip() or "user"
        image_format = self.validate_image_file(file, content, max_size_mb=self.max_image_size_mb)
        object_path = self.generate_object_path(resolved_username, file.filename or "profile", image_format)
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
                filename=file.filename or f"{resolved_username}-profile.{image_format}",
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
            secure=False,  # Use HTTP for development
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

    def with_profile_signed_url(self, user_doc: Optional[Dict[str, Any]], *, field: str = "profile_picture_id") -> Optional[Dict[str, Any]]:
        """Return a shallow copy of ``user_doc`` with a signed profile picture URL."""
        if not user_doc:
            return user_doc
        doc_copy: Dict[str, Any] = dict(user_doc)
        doc_copy[field] = self.get_signed_profile_url(doc_copy.get(field))
        return doc_copy

    def _build_secure_url(self, bucket_name: str, object_path: str) -> str:
        """Construct a secure, direct URL to a Cloudinary asset."""
        _, ext = os.path.splitext(object_path)
        fmt = ext.lstrip(".") or None
        public_id = self._build_public_id(bucket_name, object_path)
        url, _ = cloudinary_url(
            public_id,
            format=fmt,
            secure=False,  # Use HTTP for development
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

    def get_signed_url_for_bucket(self, bucket_name: str, object_path: str, expires_in_seconds: Optional[int] = None) -> str:
        """Generate a public URL for an image in a specific bucket.
        
        Since we now use public URLs with random strings for security (preventing URL guessing),
        we don't need time-based signing. The random string in the path provides the security.
        
        Args:
            bucket_name: The name of the bucket containing the object
            object_path: The path to the object within the bucket
            expires_in_seconds: Ignored (kept for backward compatibility)
        """
        if not self.enabled:
            raise HTTPException(status_code=503, detail="Object storage is disabled")

        return self._build_public_url(bucket_name, object_path)

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
    # ------------------------------------------------------------------
    # Deletion operations
    # ------------------------------------------------------------------
    def delete_file(self, object_path: str) -> bool:
        """Used to delete a file from the default bucket."""
        return self.delete_file_from_bucket(self.bucket_name, object_path)

    def delete_file_from_bucket(self, bucket_name: str, object_path: Optional[str]) -> bool:
        """Used to delete a file from a specified bucket."""
        if not object_path:
            return False
        if not self.enabled:
            return False
        try:
            public_id = self._build_public_id(bucket_name, object_path)
            result = cloudinary_destroy(public_id, resource_type="image", invalidate=True)
            return result.get("result") in {"ok", "not found"}
        except Exception as exc:
            print(f"Error deleting from Cloudinary (bucket={bucket_name}, path={object_path}): {exc}")
            raise HTTPException(status_code=500, detail=f"Failed to delete file: {str(exc)}")

    def delete_profile_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete a user's profile asset."""
        object_path = self.normalize_stored_path(identifier, "profile")
        return self.delete_file_from_bucket(self.bucket_name, object_path)

    def delete_event_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete an asset associated with an event."""
        object_path = self.normalize_stored_path(identifier, "events")
        return self.delete_file_from_bucket(self.events_bucket, object_path)

    def delete_gallery_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete an asset from the gallery."""
        object_path = self.normalize_stored_path(identifier, "gallery")
        return self.delete_file_from_bucket(self.gallery_bucket, object_path)

    def delete_committee_asset(self, identifier: Optional[str]) -> bool:
        """Used to delete a committee-related asset."""
        object_path = self.normalize_stored_path(identifier, "committee")
        return self.delete_file_from_bucket(self.gallery_bucket, object_path)

    def upload_love_note_image(self, *, image_base64: str, sender_username: str) -> Dict[str, Any]:
        """Upload a love note image (base64) to Cloudinary with a secure random URL.
        
        This method:
        1. Decodes the base64 image data
        2. Validates it's a proper image
        3. Generates a secure, unpredictable path using random tokens
        4. Uploads to Cloudinary in a dedicated love_notes folder
        5. Returns the Cloudinary URL for storage in the database
        
        Args:
            image_base64: Base64 encoded image string (with or without data URI prefix)
            sender_username: Username of the sender (used for organizing files)
            
        Returns:
            Dictionary containing the Cloudinary URL and metadata
        """
        self._ensure_enabled()
        
        # Create a dedicated folder for love notes if not configured
        love_notes_bucket = os.getenv("CLOUDINARY_LOVE_NOTES_FOLDER", "love_notes")
        
        try:
            # Remove data URI prefix if present
            if "," in image_base64:
                image_base64 = image_base64.split(",", 1)[1]
            
            # Decode base64 to bytes
            import base64
            image_bytes = base64.b64decode(image_base64)
            
            # Validate it's actually an image
            try:
                img = Image.open(io.BytesIO(image_bytes))
                image_format = img.format.lower() if img.format else "png"
                img.close()
            except Exception:
                raise HTTPException(status_code=400, detail="Invalid image data")
            
            # Generate a secure random path for the love note
            random_string = secrets.token_urlsafe(16)
            timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            safe_username = re.sub(r"[^A-Za-z0-9_.-]+", "-", sender_username)
            filename = f"love_note_{timestamp}.{image_format}"
            object_path = f"{safe_username}/{random_string}/{filename}"
            
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
            }
            
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=500, 
                detail=f"Failed to upload love note image to Cloudinary: {str(exc)}"
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

    def list_user_files(self, username: str, limit: int = 10) -> list:
        """List files for a specific user. Currently a placeholder."""
        # Cloudinary listing not currently required; return empty list for compatibility.
        return []


# Create a global instance for use across the application
storage_service = CloudinaryStorageService()
