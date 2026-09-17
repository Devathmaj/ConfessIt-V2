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
    # Ceiling on an entire request body, applied before the body is read. Derived from
    # the largest legitimate body (a base64 love note image) when unset; see
    # app.main.request_body_limit.
    MAX_REQUEST_BODY_BYTES: int | None = None
    # Uvicorn's connection ceiling. Bounds how many request bodies can be in flight at
    # once, which is the second line of defence if the body cap is being probed.
    MAX_CONCURRENCY: int = 100
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

    # --- Hard ceilings on how many accounts of each kind may ever exist ---
    #
    # A cap on *total rows per account table*, enforced when an account is created. It is a
    # capacity limit rather than a rate limit: the number is about how many accounts the
    # platform is licensed/expected to hold, not about how fast somebody is creating them.
    #
    # **Blocked accounts count.** The ceiling is on rows in the table, not on accounts that are
    # currently allowed to sign in, so ``MAX_USER_COUNT`` counts every row in ``users``
    # including the blocked ones. The alternative -- excluding blocked rows -- would make the
    # ceiling a function of a mutable moderation flag: an admin could block somebody to free a
    # slot, and the cap would stop being a cap on how many accounts exist and become a cap on
    # how many are polite. Blocking is reversible and is not deletion, so it must not be a way
    # around a limit.
    #
    # Each is independent: one table reaching its ceiling says nothing about the others, which
    # is what lets a deployment raise MAX_USER_COUNT for a new cohort without also minting
    # another admin.
    #
    # Zero is a legitimate value and means "this kind of account cannot be created at all",
    # which is a real configuration for a deployment that provisions staff out of band. It is
    # deliberately not treated as "unset" -- there is no sentinel for that, because a missing
    # variable falls back to the default below rather than to zero.
    MAX_USER_COUNT: int = 500
    MAX_ADMIN_COUNT: int = 10
    MAX_MODERATOR_COUNT: int = 25

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()
