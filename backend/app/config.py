import os
import secrets
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+psycopg://confessit:confessit@postgres:5432/ConfessIt"
    TEST_DATABASE_URL: str = "postgresql+psycopg://confessit:confessit@postgres:5432/ConfessIt_Test"
    DATABASE_NAME: str = "ConfessIt"

    # There is exactly one engine per process (see app.postgres), so the worst-case
    # connection count against Postgres is (POOL_SIZE + MAX_OVERFLOW) * WEB_WORKERS plus
    # whatever non-pooled connections exist. Keep that total under the server's
    # max_connections; growing the pool means lowering it here, not adding engines.
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10
    # How long a request waits for a pooled connection before failing. Bounding this turns
    # a saturated pool into a fast, visible 5xx instead of an unbounded request backlog.
    DATABASE_POOL_TIMEOUT: int = 10
    # Recycle connections before a proxy, load balancer or server idle timeout can drop
    # them, so a stale socket is never handed to a request.
    DATABASE_POOL_RECYCLE: int = 300
    # Number of uvicorn workers the backend is expected to run. Only used to log a
    # warning when the resulting connection budget exceeds a sane total.
    WEB_WORKERS: int = 1

    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    CLOUDINARY_URL: str | None = None
    CLOUDINARY_PROFILE_FOLDER: str | None = None
    CLOUDINARY_EVENTS_FOLDER: str | None = None
    CLOUDINARY_GALLERY_FOLDER: str | None = None
    MAX_IMAGE_SIZE_MB: int | None = None
    # Geometry caps for uploaded images. A byte limit alone does not bound image
    # processing: a small compressed file can describe a bitmap that costs gigabytes
    # to decode. Read by storage_service, which falls back to its own defaults.
    MAX_IMAGE_DIMENSION: int | None = None
    MAX_IMAGE_PIXELS: int | None = None
    PUBLIC_BASE_URL: str | None = None
    CLOUDINARY_USE_ADVANCED_TRANSFORMS: str | None = None
    CLOUDINARY_DEFAULT_VIEW_TTL: int | None = None
    CLOUDINARY_ALLOWED_FORMATS: str | None = None

    # Lifetime of the short-lived tokens embedded in backend image delivery URLs.
    # Defaults to the access-token lifetime when unset.
    ASSET_TOKEN_TTL_SECONDS: int | None = None

    # Absolute base URL of the backend that serves the image proxy routes. Must point at
    # the API, not the frontend. Falls back to PUBLIC_BASE_URL when unset.
    ASSET_BASE_URL: str | None = None

    SECRET_KEY: str = secrets.token_hex(32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REDIS_URL: str = "redis://redis:6379"
    CLOUD: bool = False

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
