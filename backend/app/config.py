import os
from pydantic import Field, field_validator
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

    # --- Signing key for JWTs and game HMACs ---
    #
    # REQUIRED, with no default, and identical in every process serving this
    # deployment. There used to be a `secrets.token_hex(32)` default here, evaluated
    # once per import: every worker, restart and container minted its own key, so a
    # restart invalidated all outstanding JWTs and with WEB_WORKERS > 1 (or replicas)
    # each worker 401'd the tokens the others had minted. A random-per-process key is
    # an availability bug dressed as a secure default.
    #
    # Set it once per deployment, from the environment (docker-compose injects it from
    # the gitignored root .env; local runs read backend/.env), a vault, or any other
    # channel that is the same for every process and never committed to git. It must
    # be at least 32 characters and must not be a placeholder; the validator below
    # refuses to start on anything weaker, loudly, rather than running half-signed.
    #
    # Generate with:  python -c "import secrets; print(secrets.token_hex(32))"
    #
    # Rotation is a deliberate, coordinated act: every outstanding access token and
    # every game HMAC stops verifying the moment the key changes, so all processes
    # switch together or sessions flap.
    SECRET_KEY: str = Field(..., min_length=32)

    @field_validator("SECRET_KEY")
    @classmethod
    def _validate_secret_key(cls, value: str) -> str:
        """Refuse to start on a missing, short or placeholder signing key.

        Length alone is not strength: 32 repeats of one character, or the word
        "test" eight times, both pass a length check and both sign with ~nothing.
        The checks below are deliberately fail-closed -- a rejected start is
        always safer than a fleet signing with a guessable key -- and the error
        tells the operator exactly how to mint a real one.
        """
        key = value.strip()
        hint = (
            "Set SECRET_KEY to a random value with at least 32 characters, identical "
            "in every worker/replica, e.g. python -c "
            '"import secrets; print(secrets.token_hex(32))"'
        )
        if len(key) < 32:
            raise ValueError(f"SECRET_KEY must be at least 32 characters. {hint}")
        lowered = key.lower()
        # Substrings no hex key can contain, but every placeholder does. A urlsafe/base64
        # key could match one by astronomical coincidence; if it does, mint another -- a
        # rejected start is always safer than running with a guessable key.
        if any(marker in lowered for marker in ("changeme", "change-me", "change_me", "test", "secret", "password")):
            raise ValueError(f"SECRET_KEY must not be a placeholder or dictionary word. {hint}")
        # A key with almost no distinct characters (aaaa..., ababab...) has almost no
        # entropy no matter how long it is. A random 32-character hex key carries ~14
        # distinct characters, so this threshold cannot trip on a real key.
        if len(set(key)) < 8:
            raise ValueError(f"SECRET_KEY must not be a repeated pattern. {hint}")
        return key
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REDIS_URL: str = "redis://redis:6379"
    CLOUD: bool = False

    # --- Passwordless login: magic-link token lifetime ---
    #
    # One login link is good for one redemption inside this window and then it is gone
    # for good. The bounds are enforced rather than documented: a link that outlives a
    # few minutes is a bearer credential sitting in a browser history entry, and a link
    # that expires too fast to be clicked is a support ticket. Ten to fifteen minutes is
    # the range where both failure modes stay rare.
    LOGIN_TOKEN_EXPIRE_MINUTES: int = Field(default=15, ge=10, le=15)

    # --- Passwordless login: layered request limits ---
    #
    # Three independent keys, deliberately in increasing order of breadth, because they
    # defend against three different attacks:
    #
    #   account  one link per registration number per window, so one account cannot be
    #            spammed. Keyed by the *submitted* registration number rather than by a
    #            resolved account id, so the cooldown behaves identically for a regno
    #            that exists and one that does not -- otherwise the cooldown itself
    #            would be a username-enumeration oracle.
    #   client   one server-generated id in a secure cookie, so cycling through
    #            sequential regnos (user1001, user1002, ...) hits a limit that does not
    #            move when the username does. Strictest of the three.
    #   ip       a coarse throttle on total request volume. Deliberately generous and
    #            never permanent: many legitimate users share one address (campus NAT,
    #            mobile carrier), so this is a brake, not a ban.
    #
    # The long account window is a second, wider catch on the same key: the short window
    # alone allows one request every window forever, which is still a steady drip of
    # link-minting for one account over an hour.
    LOGIN_ACCOUNT_COOLDOWN_SECONDS: int = 300
    LOGIN_ACCOUNT_LONG_WINDOW_SECONDS: int = 3600
    LOGIN_ACCOUNT_LONG_WINDOW_LIMIT: int = 5
    LOGIN_CLIENT_WINDOW_SECONDS: int = 300
    LOGIN_CLIENT_LIMIT: int = 5
    LOGIN_IP_WINDOW_SECONDS: int = 300
    LOGIN_IP_LIMIT: int = 15

    # The client id is opaque rate-limit state, not a credential: it grants nothing on
    # its own and is only meaningful as a key. It is still a *server-generated* secret
    # in the sense that no part of it is derived from anything the caller controls
    # (user-agent, headers), so it cannot be forged into somebody else's bucket by
    # rewriting a header.
    LOGIN_CLIENT_COOKIE_NAME: str = "confessit_client_id"
    LOGIN_CLIENT_COOKIE_MAX_AGE: int = 60 * 60 * 24 * 30

    # --- Sessions: the hard lifetime of a login ---
    #
    # The access token expires on its own (30 minutes) and is renewed by refresh tokens
    # for as long as the *session* is alive, but nothing can push a session past this many
    # hours from the moment its magic link was redeemed. After it the account must sign in
    # again with a fresh magic link; a refresh that finds the deadline passed answers 401
    # like any other expired credential, and an access token cannot outlive it either
    # because the deadline is re-read from the session row on every request.
    LOGIN_SESSION_EXPIRE_HOURS: int = 24

    # The refresh token travels in an HttpOnly cookie scoped to /auth, so page scripts
    # cannot read it and the browser only attaches it to authentication endpoints. The
    # stored copy is a SHA-256 hash, never the raw value. SameSite and Secure follow the
    # client-cookie pattern in login_rate_limit (Lax, and secure when the request arrived
    # over HTTPS) rather than being configurable here.
    REFRESH_COOKIE_NAME: str = "confessit_refresh"
    REFRESH_COOKIE_PATH: str = "/auth"

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

    # --- Observability / Telemetry ---
    OTEL_EXPORTER_OTLP_INSECURE: bool = True
    OTEL_EXPORTER_OTLP_CERTIFICATE: str | None = None
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_SERVICE_NAME: str = "confessit-backend"

    # --- Feature flags ---
    ENABLE_DEBUG_ENDPOINTS: bool = False

    # --- CORS / server ---
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080"
    HOST: str = "0.0.0.0"
    PORT: int = 8001
    RELOAD: bool = True

    class Config:
        env_file = ".env"
        extra = "allow"


settings = Settings()


def public_frontend_origin() -> str:
    """The public origin magic links point at, without a trailing slash.

    ``PUBLIC_BASE_URL`` when set (any deployment users reach by name), plain local
    HTTP otherwise. Centralised so the login link, and anything else naming the
    frontend, cannot drift back to a hardcoded localhost one call site at a time.
    """
    return (
        (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
        or "http://localhost:5173"
    )


def validate_public_base_url() -> None:
    """Refuse to boot a cloud deployment whose links would point at localhost.

    A magic link is a bearer credential printed for a human to click: if
    ``PUBLIC_BASE_URL`` is unset or names localhost while ``CLOUD`` is on, every prod
    login mints a link to nowhere (a login DoS) and teaches users to copy tokens onto
    the wrong host (a phishing template). Local runs keep the localhost default and are
    unaffected.
    """
    if not settings.CLOUD:
        return
    base = (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be set when CLOUD is true: magic links are minted "
            "from it, and the localhost default would break every production login."
        )
    lowered = base.lower()
    # Exact host matching, not substring: "mylocalhost.example.com" is a real (if odd)
    # domain, while "localhost", "*.localhost", "127.*/[::1]" never leave the machine.
    host = lowered.split("://", 1)[-1].split("/")[0]
    if host.startswith("["):
        is_loopback = host.split("]")[0] + "]" == "[::1]"
    else:
        bare = host.split(":")[0]
        is_loopback = (
            bare == "localhost"
            or bare.endswith(".localhost")
            or bare.startswith("127.")
            or bare == "::1"
        )
    if is_loopback:
        raise RuntimeError(
            f"PUBLIC_BASE_URL must not point at loopback when CLOUD is true (got {base!r}): "
            "magic links minted for it would be unusable in production."
        )


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

