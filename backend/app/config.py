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
    CLOUDINARY_ALLOWED_FORMATS: str | None = None

    # --- Organising the library ---
    #
    # Cloudinary offers two different things that both read like "put this in a named
    # collection", and only one of them can be driven from code:
    #
    #   * *Asset folders* are addressable. They have an Admin API (`folders`) and an upload
    #     parameter (`asset_folder`), and on a dynamic-folder-mode account they are purely a
    #     Media Library location: public_id and delivery URL are untouched.
    #   * *Collections* are a DAM concept: a flat conceptual grouping that an asset can belong
    #     to more than once, and that shows up under Assets rather than under Images. There is
    #     no REST endpoint and no SDK method for them anywhere in Cloudinary's APIs -- the Admin
    #     API reference has folders, resources, tags and metadata_fields, and no collections.
    #     They can only be assembled in the Media Library console.
    #
    # So the grouping is carried by tags, which the Upload API does accept. A *dynamic*
    # collection with a rule on these tags then fills itself with no further calls from us, and
    # an operator who would rather browse folders can point a folder at the same tag instead.
    # Neither setting can affect delivery: tags are metadata, and they are not part of a URL.
    CLOUDINARY_PROFILE_ASSET_TAG: str = "profile_picture"
    CLOUDINARY_LOVE_NOTE_ASSET_TAG: str = "love_note"

    # --- Private (signed) love note delivery ---
    #
    # Love note images are delivered straight from Cloudinary's CDN, never through this
    # application, so the thing that authorises a fetch is a Cloudinary *signed delivery URL*
    # minted here. The signature is the SDK's SHA-1 URL signature over the object's path, keyed
    # by `CLOUDINARY_API_SECRET`, which is the same credential the upload already uses. Nothing
    # account-side has to be configured for it to work.
    #
    # **The signature is not a token and does not expire.** Cloudinary's two private-delivery
    # mechanisms are genuinely different things, and conflating them is the mistake this file
    # used to make. An *authentication token* is `?__cld_token__=exp=...~hmac=...`, carries an
    # expiry, and is checked against a separate account-wide **Auth token key** configured under
    # Console -> Settings -> Delivery. A *signed URL* is an `s--<signature>--` path segment
    # derived from the API secret, needs no console configuration, and is valid forever once
    # issued. Only the token mechanism expires.
    #
    # That matters because a token ACL is not merely decorative: applying
    # `access_control=[{"access_type": "token"}]` to an upload makes Cloudinary reject even a
    # correctly signed URL and demand a real token instead. So an account with no Auth token key
    # will *accept* such an upload, report the ACL back, and then 401 every delivery -- signed or
    # signed-with-token. Auth token keys are not available on this plan, so the token ACL is not
    # used and delivery is signed-URL based.
    #
    # The cost is accepted knowingly and is not small: a signed love note URL does not expire,
    # cannot be revoked without destroying and re-uploading the object, and is a bearer
    # capability for as long as it exists. What still holds is that the URL cannot be forged
    # without the API secret, is never stored, is minted only after the application has
    # authorised the viewer, and is scoped to one immutable object's path. What is given up is
    # the short expiry window, so there is deliberately **no TTL setting here** -- leaving one
    # would let an operator believe love note URLs time out when they do not.
    #
    # See ``storage_service.love_note_delivery_url``.

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


def validate_cloudinary_settings() -> None:
    """Refuse to start if the image store cannot actually serve images.

    The three credentials are checked as a group because they are genuinely all-or-nothing:
    without the API secret there is no signature, and a signed delivery URL is the *only* way a
    love note image is ever served. That makes an incomplete configuration a broken deployment
    rather than a degraded one, and there is deliberately no fallback -- serving a love note
    from a weaker path because the strong one was unconfigured would be a privacy incident, not
    a graceful degradation.

    There used to be a second failure mode here, a missing ``CLOUDINARY_AUTH_TOKEN_KEY``, and it
    was the more important of the two: the API secret alone would have let profile pictures work
    while *every love note image* failed, and the failure would otherwise only have surfaced as a
    503 in the inbox of the person who was sent a note. That check is gone because the token
    mechanism is no longer used -- signed URLs are keyed off the API secret that is validated
    here, so there is no longer a second credential that can be missing on its own.
    """
    credentials = (settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET)
    if not all(credentials):
        raise RuntimeError(
            "Cloudinary is not configured: CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and "
            "CLOUDINARY_API_SECRET must all be set to store and deliver images. The API secret "
            "is what signs private love note delivery URLs, so it is required for love notes and "
            "not just for uploads."
        )

