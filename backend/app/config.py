import os
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.engine import URL

from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

def _reject_weak_secret(value: str, *, field: str) -> str:
    """Refuse a missing, short, placeholder or low-entropy deployment secret; shared by
    SECRET_KEY and TOKEN_HASH_PEPPER so the two cannot drift apart. Length alone is not
    strength, and the checks fail closed."""
    key = value.strip()
    hint = (
        f"Set {field} to a random value with at least 32 characters, identical "
        'in every worker/replica, e.g. python -c "import secrets; '
        'print(secrets.token_hex(32))"'
    )
    if len(key) < 32:
        raise ValueError(f"{field} must be at least 32 characters. {hint}")
    lowered = key.lower()
    # Substrings no hex key contains but every placeholder does.
    if any(marker in lowered for marker in ("changeme", "change-me", "change_me", "test", "secret", "password")):
        raise ValueError(f"{field} must not be a placeholder or dictionary word. {hint}")
    # Repeated patterns (aaaa..., ababab...) have almost no entropy. A random 32-char
    # hex key carries ~14 distinct characters, so 8 cannot trip a real key.
    if len(set(key)) < 8:
        raise ValueError(f"{field} must not be a repeated pattern. {hint}")
    return key


def _require_url_scheme(value: str, *, field: str, schemes: tuple[str, ...]) -> str:
    """Refuse a blank connection string or one for the wrong driver: a scheme typo
    otherwise surfaces as a confusing driver error deep in engine creation -- or
    connects somewhere unintended."""
    text = (value or "").strip()
    if not text:
        raise ValueError(f"{field} must not be blank.")
    lowered = text.lower()
    if not any(lowered.startswith(f"{scheme}://") for scheme in schemes):
        raise ValueError(
            f"{field} must start with one of "
            f"{', '.join(f'{scheme}://' for scheme in schemes)} (got {text[:24]!r}...)."
        )
    return value


def _require_positive_int_or_none(value: int | None, *, field: str) -> int | None:
    """Refuse a zero or negative ceiling (0 refuses everything or breaks uploads);
    unset stays unset so services use their own defaults."""
    if value is not None and value <= 0:
        raise ValueError(f"{field} must be a positive integer when set (got {value}).")
    return value


class Settings(BaseSettings):
    # Keep connection details as separate environment variables so credentials with
    # URL-reserved characters are escaped safely when SQLAlchemy builds the DSN.
    PGHOST: str = Field(...)
    PGUSER: str = Field(...)
    PGPORT: int = 5432
    PGDATABASE: str = Field(...)
    PGPASSWORD: str = Field(...)
    PGSSLMODE: str = "require"

    @property
    def DATABASE_URL(self) -> str:
        """SQLAlchemy URL assembled from the standard PG* settings."""
        return URL.create(
            "postgresql+psycopg",
            username=self.PGUSER,
            password=self.PGPASSWORD,
            host=self.PGHOST,
            port=self.PGPORT,
            database=self.PGDATABASE,
            query={"sslmode": self.PGSSLMODE},
        ).render_as_string(hide_password=False)

    @property
    def TEST_DATABASE_URL(self) -> str:
        """Conventional isolated test database on the same PostgreSQL server."""
        return URL.create(
            "postgresql+psycopg",
            username=self.PGUSER,
            password=self.PGPASSWORD,
            host=self.PGHOST,
            port=self.PGPORT,
            database=f"{self.PGDATABASE}_Test",
            query={"sslmode": self.PGSSLMODE},
        ).render_as_string(hide_password=False)

    # One engine per process (see app.postgres), so the worst-case Postgres connection
    # count is (POOL_SIZE + MAX_OVERFLOW) * WEB_WORKERS; keep that under max_connections.
    DATABASE_POOL_SIZE: int = 5
    DATABASE_MAX_OVERFLOW: int = 10
    # Bounds how long a request waits for a pooled connection: a saturated pool becomes a
    # fast, visible 5xx instead of an unbounded backlog.
    DATABASE_POOL_TIMEOUT: int = 10
    # Recycle before a proxy, load balancer or server idle timeout drops connections, so a
    # stale socket is never handed to a request.
    DATABASE_POOL_RECYCLE: int = 300
    # Expected uvicorn worker count; only used to warn when the connection budget is high.
    WEB_WORKERS: int = 1

    CLOUDINARY_CLOUD_NAME: str | None = None
    CLOUDINARY_API_KEY: str | None = None
    CLOUDINARY_API_SECRET: str | None = None
    CLOUDINARY_URL: str | None = None
    MAX_IMAGE_SIZE_MB: int | None = None
    # A byte limit does not bound decode cost: a small compressed file can describe a
    # bitmap that costs gigabytes. Read by storage_service, which has its own defaults.
    MAX_IMAGE_DIMENSION: int | None = None
    MAX_IMAGE_PIXELS: int | None = None
    # Applied before the body is read; derived from the largest legitimate body (a base64
    # love note image) when unset. See app.main.request_body_limit.
    MAX_REQUEST_BODY_BYTES: int | None = None
    # Uvicorn's connection ceiling: the second line of defence if the body cap is probed.
    MAX_CONCURRENCY: int = Field(default=100, ge=1)
    PUBLIC_BASE_URL: str | None = None
    CLOUDINARY_ALLOWED_FORMATS: str | None = None

    # --- Magic-link email queue ---
    # Login posts links to the Azure Functions enqueue endpoint (Azure Storage Queue -> ACS
    # Email); optional so local runs/tests work without the host (console link fallback).
    EMAIL_FUNCTION_URL: str | None = None

    @field_validator("EMAIL_FUNCTION_URL")
    @classmethod
    def _validate_email_function_url(cls, value: str | None) -> str | None:
        """Accept only a non-blank http(s) URL for the Functions enqueue endpoint."""
        if value is None:
            return None
        text_value = value.strip()
        if not text_value:
            return None
        parsed = urlsplit(text_value)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "EMAIL_FUNCTION_URL must be an http(s) URL "
                f"(got {text_value[:24]!r}...)."
            )
        return text_value

    @field_validator(
        "MAX_IMAGE_SIZE_MB",
        "MAX_IMAGE_DIMENSION",
        "MAX_IMAGE_PIXELS",
        "MAX_REQUEST_BODY_BYTES",
    )
    @classmethod
    def _validate_positive_ceiling(cls, value: int | None, info: ValidationInfo) -> int | None:
        """A zero or negative ceiling refuses everything; unset stays unset."""
        return _require_positive_int_or_none(value, field=info.field_name)

    # --- Organising the library ---
    # Cloudinary collections exist only in the Media Library console (no REST/SDK method), so
    # grouping is carried by Upload-API tags that a dynamic collection's rule fills; folders
    # can point at the same tag. Tags never affect delivery URLs.
    CLOUDINARY_PROFILE_ASSET_TAG: str = "profile_picture"
    CLOUDINARY_LOVE_NOTE_ASSET_TAG: str = "love_note"

    # --- Private (signed) love note delivery ---
    # Served from Cloudinary's CDN behind a *signed delivery URL* (SHA-1, keyed by
    # CLOUDINARY_API_SECRET), not an auth token (unavailable on this plan; its ACL even rejects
    # correct signed URLs). It never expires -- minted only after the app authorises the viewer.

    # --- Signing key for JWTs and game HMACs ---
    # REQUIRED and identical in every process: a random-per-process default invalidated
    # outstanding JWTs on restart and made workers 401 each other's tokens. Rotation is
    # coordinated -- every access token and game HMAC stops verifying at once.
    SECRET_KEY: str = Field(..., min_length=32)

    @field_validator("SECRET_KEY")
    @classmethod
    def _validate_secret_key(cls, value: str) -> str:
        """Refuse to start on a missing, short or placeholder signing key."""
        return _reject_weak_secret(value, field="SECRET_KEY")

    # --- Pepper for stored token hashes ---
    # REQUIRED and validated like SECRET_KEY, identical in every process: without it a database
    # dump turns stored magic-link/refresh hashes into an offline search problem. Rotation
    # invalidates every outstanding session and unredeemed magic link at once.
    TOKEN_HASH_PEPPER: str = Field(..., min_length=32)

    @field_validator("TOKEN_HASH_PEPPER")
    @classmethod
    def _validate_token_hash_pepper(cls, value: str) -> str:
        """Refuse to start on a missing, short or placeholder pepper."""
        return _reject_weak_secret(value, field="TOKEN_HASH_PEPPER")

    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    # Required; the backend/.env template resolves the password from the container
    # environment. ``rediss://`` opts into TLS (redis_service requires a valid certificate).
    REDIS_URL: str = Field(...)

    # --- Redis pools ---
    # Two pools: a subscriber holds its connection for the socket's life while commands are short-
    # lived; sharing one let a busy chat drain it, failing publishes (and the login limiter) while
    # REST still answered 201. The subscriber ceiling doubles as concurrent chats per worker.
    REDIS_COMMAND_POOL_MAX_CONNECTIONS: int = Field(default=50, ge=1)
    REDIS_PUBSUB_POOL_MAX_CONNECTIONS: int = Field(default=100, ge=1)

    @field_validator("REDIS_URL")
    @classmethod
    def _validate_redis_url(cls, value: str) -> str:
        """Only redis/rediss schemes: a typo otherwise fails long after boot as an opaque connection error."""
        return _require_url_scheme(value, field="REDIS_URL", schemes=("redis", "rediss"))

    # --- Reverse proxy and cookie transport ---
    # Comma-separated IPs/CIDRs allowed to set X-Forwarded-*; empty trusts no forwarded header
    # (otherwise a direct client could declare its own scheme as https and upgrade its cookies).
    # When set, ProxyHeadersMiddleware runs outermost so every reader sees the client-facing scheme.
    TRUSTED_PROXY_HOSTS: str = ""
    # None (default) follows the proxy-corrected request scheme; True/False force Secure.
    # False is never correct for a network-reachable deployment.
    COOKIE_SECURE: bool | None = None

    # --- Passwordless login: magic-link token lifetime ---
    # One redemption, then gone. A link outliving a few minutes is a bearer credential in browser
    # history; one too short to click is a support ticket.
    LOGIN_TOKEN_EXPIRE_MINUTES: int = Field(default=15, ge=10, le=15)

    # --- Passwordless login: layered request limits ---
    # Three keys of increasing breadth: account keys the *submitted* regno (no enumeration
    # oracle), client is a cookie id defeating regno cycling, ip a coarse never-permanent brake
    # (campus NAT/carriers). The long account window catches the steady drip the short window allows.
    LOGIN_ACCOUNT_COOLDOWN_SECONDS: int = 300
    LOGIN_ACCOUNT_LONG_WINDOW_SECONDS: int = 3600
    LOGIN_ACCOUNT_LONG_WINDOW_LIMIT: int = 5
    LOGIN_CLIENT_WINDOW_SECONDS: int = 300
    LOGIN_CLIENT_LIMIT: int = 5
    LOGIN_IP_WINDOW_SECONDS: int = 300
    LOGIN_IP_LIMIT: int = 15

    # Opaque rate-limit state, not a credential: server-generated, so rewriting a header
    # cannot move a caller into somebody else's bucket.
    LOGIN_CLIENT_COOKIE_NAME: str = "confessit_client_id"
    LOGIN_CLIENT_COOKIE_MAX_AGE: int = 60 * 60 * 24 * 30

    # --- Sessions: the hard lifetime of a login ---
    # Nothing can push a session past this many hours from magic-link redemption; a refresh
    # past it answers 401, and the deadline is re-read from the session row on every request.
    LOGIN_SESSION_EXPIRE_HOURS: int = 24

    # Refresh token: HttpOnly cookie scoped to /auth, stored only as a SHA-256 hash.
    # SameSite/Secure follow login_rate_limit's client-cookie pattern (Lax; secure when the
    # request arrived over HTTPS) rather than being configurable here.
    REFRESH_COOKIE_NAME: str = "confessit_refresh"
    REFRESH_COOKIE_PATH: str = "/auth"

    # --- Hard ceilings on account rows per table ---
    # Enforced at creation -- a capacity limit, not a rate limit; even blocked accounts count and
    # each table is independent. Zero means "this kind cannot be created"; a missing variable
    # falls back to the default below, never to zero.
    MAX_USER_COUNT: int = Field(default=500, ge=0)
    MAX_ADMIN_COUNT: int = Field(default=10, ge=0)
    MAX_MODERATOR_COUNT: int = Field(default=25, ge=0)

    # --- Observability / Telemetry ---
    # Secure by default: spans/logs/metrics carry regnos, IPs and session identifiers. Local
    # Compose sets OTEL_EXPORTER_OTLP_INSECURE=true explicitly; a remote collector must present TLS.
    OTEL_EXPORTER_OTLP_INSECURE: bool = False
    OTEL_EXPORTER_OTLP_CERTIFICATE: str | None = None
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_SERVICE_NAME: str = "confessit-backend"

    # --- Chat message encryption at rest ---
    # AES-256-GCM before PostgreSQL. The active version encrypts new writes; old rows keep their
    # version, so rotating never makes them unreadable. V2..V8 make a future rotation
    # configuration-only; keys are validated at startup when set.
    CHAT_ENCRYPTION_ACTIVE_KEY_VERSION: str = "v1"
    CHAT_ENCRYPTION_KEY_V1: str | None = None
    CHAT_ENCRYPTION_KEY_V2: str | None = None
    CHAT_ENCRYPTION_KEY_V3: str | None = None
    CHAT_ENCRYPTION_KEY_V4: str | None = None
    CHAT_ENCRYPTION_KEY_V5: str | None = None
    CHAT_ENCRYPTION_KEY_V6: str | None = None
    CHAT_ENCRYPTION_KEY_V7: str | None = None
    CHAT_ENCRYPTION_KEY_V8: str | None = None

    @field_validator("CHAT_ENCRYPTION_ACTIVE_KEY_VERSION")
    @classmethod
    def _validate_chat_key_version(cls, value: str) -> str:
        """Normalise the version label; key material is validated by the service."""
        version = (value or "").strip().lower()
        if not version:
            raise ValueError("CHAT_ENCRYPTION_ACTIVE_KEY_VERSION must not be blank.")
        if not version.startswith("v") or not version[1:].isdigit():
            raise ValueError(
                "CHAT_ENCRYPTION_ACTIVE_KEY_VERSION must look like v1, v2, ... "
                f"(got {value!r})."
            )
        return version

    # Chats keep their four-hour sending window; messages stay readable this many hours
    # after the match's own ``expires_at``, then the daily scheduler purges them.
    MATCHMAKING_CHAT_RETENTION_HOURS: int = Field(default=24, ge=1)
    # Rows one cleanup statement batch handles, so a backlog does not hold one long transaction.
    CHAT_CLEANUP_BATCH_SIZE: int = Field(default=500, ge=1)
    # Reported/flagged evidence is stored separately from chat history and encrypted again,
    # with its own retention window.
    REPORTED_MESSAGE_EVIDENCE_RETENTION_DAYS: int = Field(default=30, ge=1)

    # --- Feature flags ---
    ENABLE_DEBUG_ENDPOINTS: bool = False

    # --- LLM ---
    # Story generation only (gameplay never calls the provider). ``groq`` uses Groq's SDK;
    # ``foundry`` uses the OpenAI SDK against Microsoft Foundry; ``openai``/``auto`` are legacy
    # spellings for old deployments. Switching providers is configuration, never a code change.
    LLM_PROVIDER: str = "groq"
    LLM_API_KEY: str | None = None
    LLM_BASE_URL: str | None = None
    LLM_MODEL: str | None = None

    @field_validator("LLM_PROVIDER", mode="before")
    @classmethod
    def _normalize_llm_provider(cls, value: Any) -> str:
        """Trim, lowercase and admit only known provider spellings; unset means Groq and an
        unrecognised value fails at boot rather than mid-generation."""
        text = str(value or "").strip().lower() or "groq"
        if text not in ("groq", "foundry", "openai", "auto"):
            raise ValueError(
                f"LLM_PROVIDER must be 'groq' or 'foundry' "
                f"(got {value!r}); 'openai' and 'auto' remain accepted as "
                "legacy spellings."
            )
        return text

    # --- Automated AI session generation (Plot Twist / Case Closed) ---
    # Configures the in-process worker draining the queue the 00:01 UTC scheduler enqueues into.
    # Safe on every process: jobs are claimed with SKIP LOCKED and a shared advisory lock
    # serialises generation, so an extra uvicorn worker costs one polling query, never a second run.
    AI_GENERATION_WORKER_ENABLED: bool = True
    # How often an idle worker checks the queue (seconds).
    AI_GENERATION_POLL_SECONDS: int = Field(default=30, ge=5, le=3600)
    # How long a worker waits for the shared generation lock before yielding; the job stays
    # queued and the next pass retries.
    AI_GENERATION_LOCK_WAIT_SECONDS: int = Field(default=120, ge=1, le=1800)

    # --- Semantic search embeddings (Azure) ---
    # One Azure-hosted model with fixed invariants rather than knobs: EMBEDDING_MODEL must be
    # "text-embedding-3-small", EMBEDDING_DIMENSIONS 1536 (PostgreSQL vector(1536)), shared by
    # query and confession embeddings. Credentials stay in the runtime environment, never committed.
    EMBEDDING_ENDPOINT: str = "https://confessit-model.services.ai.azure.com"
    EMBEDDING_API_KEY: str | None = None
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    EMBEDDING_DIMENSIONS: int = 1536
    EMBEDDING_SEARCH_ENABLED: bool = True
    # Kept for backward compatibility; Foundry v1 embeddings takes no api-version param.
    EMBEDDING_API_VERSION: str = "2024-02-01"
    # Optional deployment name when it differs from the model id; unset means use the model
    # name. Explicit configuration, never invented silently.
    EMBEDDING_DEPLOYMENT: str | None = None
    # Bounded HTTP behavior for the provider client (seconds / attempts).
    EMBEDDING_TIMEOUT_SECONDS: float = 10.0
    EMBEDDING_MAX_RETRIES: int = 2

    # --- Azure AI Content Safety (confession moderation) ---
    # New confessions are analyzed with the Analyze Text API (2024-09-01) before embedding;
    # comments are out of scope. Credentials stay in the runtime environment, never committed.
    CONTENT_SAFETY_ENDPOINT: str | None = None
    CONTENT_SAFETY_API_KEY: str | None = None
    CONTENT_SAFETY_REGION: str | None = None
    # Bounded HTTP behavior for the Content Safety client (seconds).
    CONTENT_SAFETY_TIMEOUT_SECONDS: float = 10.0

    @field_validator(
        "CONTENT_SAFETY_ENDPOINT",
        "CONTENT_SAFETY_API_KEY",
        "CONTENT_SAFETY_REGION",
        mode="before",
    )
    @classmethod
    def _coerce_empty_content_safety_optional(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("CONTENT_SAFETY_ENDPOINT")
    @classmethod
    def _validate_content_safety_endpoint(cls, value: str | None) -> str | None:
        """Non-blank https endpoint when set; None means unconfigured."""
        if value is None:
            return None
        text_value = value.strip().rstrip("/")
        if not text_value:
            return None
        lowered = text_value.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            raise ValueError(
                f"CONTENT_SAFETY_ENDPOINT must be an http(s) URL (got {value!r})."
            )
        return text_value

    # --- Azure AI Language: PII detection ---
    # Confession content is scanned for PII before acceptance. Credentials stay in the
    # runtime environment and are never committed or logged.
    PII_DETECTION_ENDPOINT: str | None = None
    PII_DETECTION_API_KEY: str | None = None
    PII_DETECTION_REGION: str | None = None

    @field_validator(
        "PII_DETECTION_ENDPOINT",
        "PII_DETECTION_API_KEY",
        "PII_DETECTION_REGION",
        mode="before",
    )
    @classmethod
    def _coerce_empty_pii_detection_optional(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("PII_DETECTION_ENDPOINT")
    @classmethod
    def _validate_pii_detection_endpoint(cls, value: str | None) -> str | None:
        """Non-blank http(s) endpoint when set; None means unconfigured."""
        if value is None:
            return None
        text_value = value.strip().rstrip("/")
        if not text_value:
            return None
        lowered = text_value.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            raise ValueError(
                f"PII_DETECTION_ENDPOINT must be an http(s) URL (got {value!r})."
            )
        return text_value

    @field_validator("EMBEDDING_ENDPOINT", mode="before")
    @classmethod
    def _coerce_empty_embedding_endpoint(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "https://confessit-model.services.ai.azure.com"
        return value

    @field_validator("EMBEDDING_MODEL", mode="before")
    @classmethod
    def _coerce_empty_embedding_model(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return "text-embedding-3-small"
        return value

    @field_validator("EMBEDDING_DIMENSIONS", mode="before")
    @classmethod
    def _coerce_empty_embedding_dims(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 1536
        return value

    @field_validator("EMBEDDING_API_KEY", "EMBEDDING_DEPLOYMENT", mode="before")
    @classmethod
    def _coerce_empty_embedding_optional(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("EMBEDDING_MODEL")
    @classmethod
    def _validate_embedding_model(cls, value: str | None) -> str:
        """Only text-embedding-3-small: query and index vectors must match."""
        text_value = (value or "").strip() or "text-embedding-3-small"
        if text_value != "text-embedding-3-small":
            raise ValueError(
                "EMBEDDING_MODEL must be 'text-embedding-3-small' "
                f"(got {value!r}): query and confession embeddings must use "
                "the same compatible model."
            )
        return text_value

    @field_validator("EMBEDDING_DIMENSIONS")
    @classmethod
    def _validate_embedding_dimensions(cls, value: int | None) -> int:
        """Only 1536: the database column is vector(1536)."""
        resolved = 1536 if value is None else value
        if resolved != 1536:
            raise ValueError(
                f"EMBEDDING_DIMENSIONS must be 1536 (got {value}): "
                "the PostgreSQL column is vector(1536) and vectors must "
                "never be truncated, padded, or reshaped."
            )
        return resolved

    @field_validator("EMBEDDING_ENDPOINT")
    @classmethod
    def _validate_embedding_endpoint(cls, value: str | None) -> str:
        """Non-blank https endpoint; the request path is built from it."""
        text_value = (value or "").strip().rstrip("/") or "https://confessit-model.services.ai.azure.com"
        if not text_value:
            raise ValueError("EMBEDDING_ENDPOINT must not be blank.")
        lowered = text_value.lower()
        if not (lowered.startswith("https://") or lowered.startswith("http://")):
            raise ValueError(
                f"EMBEDDING_ENDPOINT must be an http(s) URL (got {value!r})."
            )
        return text_value

    # --- Hybrid search (keyword + semantic) tuning ---
    # Operator knobs unlike the model/dimensions above. EMBEDDING_MIN_SIMILARITY is a cosine floor
    # (1 - cosine_distance): neighbors below it are withheld, so semantic never invents relevance.
    EMBEDDING_MIN_SIMILARITY: float = Field(default=0.30, ge=-1.0, le=1.0)
    # Hard cap on semantic items before dedupe, so one vector query cannot materialise the corpus.
    EMBEDDING_SEMANTIC_TOP_K: int = Field(default=10, ge=1, le=50)
    # Shorter queries skip embedding (keyword only): a 1-2 character prefix has no semantic signal.
    EMBEDDING_MIN_QUERY_CHARS: int = Field(default=3, ge=1, le=10)
    # Per-minute embedding budgets, in-process per bucket (staff vs public). Zero disables the
    # limiter (fail-open, e.g. tests); local rather than Redis-shared so search stays fast.
    EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE: int = Field(default=30, ge=0)
    EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE: int = Field(default=60, ge=0)

    @field_validator("EMBEDDING_MIN_SIMILARITY", mode="before")
    @classmethod
    def _coerce_empty_min_similarity(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 0.30
        return value

    @field_validator("EMBEDDING_SEMANTIC_TOP_K", mode="before")
    @classmethod
    def _coerce_empty_semantic_top_k(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 10
        return value

    @field_validator("EMBEDDING_MIN_QUERY_CHARS", mode="before")
    @classmethod
    def _coerce_empty_min_query_chars(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 3
        return value

    @field_validator("EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE", mode="before")
    @classmethod
    def _coerce_empty_search_rate_limit(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 30
        return value

    @field_validator("EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE", mode="before")
    @classmethod
    def _coerce_empty_admin_search_rate_limit(cls, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip()):
            return 60
        return value

    # --- Host allowlist ---
    # Checked per request before routing: DNS rebinding and Host-header poisoning target a process
    # answering for a name it was never meant to, so anything unlisted gets a 400. Loopback covers
    # local runs; ``testserver`` is Starlette TestClient's default host, appended by the suite.
    ALLOWED_HOSTS: str = "localhost,127.0.0.1,[::1]"

    # --- Global flood brake ---
    # Requests per minute per client IP across all paths, counted in Redis so every worker shares
    # one counter. Generous by design (campus NAT); login keeps its own stricter limits. Zero or
    # negative disables it (the test suite, whose Redis is unreachable).
    GLOBAL_RATE_LIMIT_PER_MINUTE: int = 600

    # --- CORS / server ---
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8080,http://127.0.0.1:8080"
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = True

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _validate_cors_origins(cls, value: str) -> str:
        """Refuse a CORS misconfiguration at boot: a bare ``*`` with credentialed requests
        would let any site call this API with the user's cookies, and a schemeless entry
        never matches a real Origin. The runtime parser repeats this for post-boot mutations."""
        entries = [o.strip().rstrip("/") for o in value.split(",") if o.strip()]
        if not entries:
            raise ValueError("CORS_ORIGINS must name at least one origin.")
        if "*" in entries:
            raise ValueError(
                'CORS_ORIGINS must not contain "*": wildcard origins with '
                "credentials enabled would let any site call this API with the "
                "user's cookies."
            )
        for entry in entries:
            lowered = entry.lower()
            if "://" not in lowered:
                raise ValueError(
                    f"CORS_ORIGINS entry {entry!r} has no scheme: origins look like "
                    "https://app.example.com."
                )
            scheme = lowered.split("://", 1)[0]
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"CORS_ORIGINS entry {entry!r} must use http or https."
                )
        return value

    # extra="forbid": a misspelled SECRETKEY or DATABSE_URL in the shared .env would otherwise
    # be silently ignored; every legitimate key is declared, so anything else is a loud boot error.
    model_config = SettingsConfigDict(env_file=".env", extra="forbid")


settings = Settings()


def public_frontend_origin() -> str:
    """The public origin magic links point at, without a trailing slash: PUBLIC_BASE_URL when
    set, plain local HTTP otherwise, centralised so nothing drifts to hardcoded localhost."""
    return (
        (settings.PUBLIC_BASE_URL or "").strip().rstrip("/")
        or "http://localhost:5173"
    )


def cors_allow_origins() -> list[str]:
    """Parse ``CORS_ORIGINS`` into the exact origins browsers may call with cookies.

    A bare ``*`` is refused (with allow_credentials=True every website becomes a credentialed
    caller). Shared by CORS and the cookie-flow CSRF check so they agree.
    """
    origins = [o.strip().rstrip("/") for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if "*" in origins:
        raise RuntimeError(
            'CORS_ORIGINS must not contain "*": wildcard origins with credentials '
            "enabled would let any site call this API with the user's cookies."
        )
    return list(dict.fromkeys(origins))


def cookie_secure(scheme: str) -> bool:
    """Whether auth cookies are minted with the Secure flag for this request: the explicit
    override when set, otherwise the request scheme (plain HTTP never mints Secure, HTTPS
    always does)."""
    if settings.COOKIE_SECURE is not None:
        return settings.COOKIE_SECURE
    return scheme == "https"


def validate_cloudinary_settings() -> None:
    """Refuse to start if the image store cannot actually serve images.

    The three credentials are all-or-nothing: without the API secret there is no signature, and
    a signed delivery URL is the only path love note images are served over -- so an incomplete
    configuration is a broken deployment, not a degraded one (a weaker path is a privacy incident).
    """
    credentials = (settings.CLOUDINARY_CLOUD_NAME, settings.CLOUDINARY_API_KEY, settings.CLOUDINARY_API_SECRET)
    if not all(credentials):
        raise RuntimeError(
            "Cloudinary is not configured: CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and "
            "CLOUDINARY_API_SECRET must all be set to store and deliver images. The API secret "
            "is what signs private love note delivery URLs, so it is required for love notes and "
            "not just for uploads."
        )

