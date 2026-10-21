# app/main.py

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import ClientDisconnect, Request
from starlette.responses import PlainTextResponse
from sqlalchemy import text
from .config import (
    settings,
    cors_allow_origins,
    validate_cloudinary_settings,
    validate_cookie_transport,
    validate_public_base_url,
    validate_transport_security,
)
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, staff_notifications, admin, admin_love_notes, admin_wordle, admin_anagram, admin_couple, admin_connections, admin_trivia_drop, admin_games, admin_plot_twist, admin_case_closed, admin_reaction_time, messages, wordle, anagram, connections, trivia_drop, couple_questionnaire, plot_twist, case_closed, reaction_time, games, two_key_escape, leaderboards
from .dependencies import get_db
from .postgres import Database, close_postgres_client, get_postgres_client
from .services.storage_service import storage_service
from .services.redis_service import redis_service
from .services.matchmaking_service import MatchmakingRefusal
from .models import AccountDetails
import uvicorn
from .logger import get_logger, install_query_credential_redaction, otlp_transport
from .telemetry import redact_client_url, redact_query_strings
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.instrumentation.urllib3 import URLLib3Instrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.semconv.attributes.http_attributes import (
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
)
from opentelemetry.semconv.attributes.network_attributes import (
    NETWORK_PROTOCOL_VERSION,
)
from opentelemetry.semconv.attributes.url_attributes import (
    URL_SCHEME,
)

# Initialize logger
logger = get_logger(__name__)

# Debug endpoints are opt-in; database and driver details must not be exposed publicly.
ENABLE_DEBUG_ENDPOINTS = bool(settings.ENABLE_DEBUG_ENDPOINTS)

# Allowlist http.server metric dimensions to bound cardinality and fail closed on unknown labels.
# Keep only modern conventions; ``http.route`` is a route template, not a request URL.
BOUNDED_HTTP_METRIC_ATTRIBUTES = {
    HTTP_REQUEST_METHOD,
    HTTP_RESPONSE_STATUS_CODE,
    HTTP_ROUTE,
    URL_SCHEME,
    NETWORK_PROTOCOL_VERSION,
}


def _init_tracing():
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return

    service_name = settings.OTEL_SERVICE_NAME
    resource = Resource.create({"service.name": service_name})

    insecure, credentials = otlp_transport()

    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=insecure, credentials=credentials)
    provider.add_span_processor(BatchSpanProcessor(exporter))

_init_tracing()


def _init_metrics():
    """Register the meter provider before FastAPIInstrumentor binds its HTTP instruments."""
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return

    service_name = settings.OTEL_SERVICE_NAME
    resource = Resource.create({"service.name": service_name})

    insecure, credentials = otlp_transport()

    exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure, credentials=credentials)
    # Match the 15-second Prometheus scrape interval; the SDK's 60-second default can miss a scrape.
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=15000)
    metrics.set_meter_provider(
        MeterProvider(
            resource=resource,
            metric_readers=[reader],
            views=[
                View(
                    instrument_name="http.server.*",
                    attribute_keys=BOUNDED_HTTP_METRIC_ATTRIBUTES,
                )
            ],
        )
    )

_init_metrics()


# Prometheus scrapes these process-health series because pushed OTLP metrics alone cannot
# distinguish a dead backend from a healthy collector. Do not re-export HTTP instruments here:
# a second job would double-count them. The small exposition stays hand-written to avoid a dependency.

_PROCESS_START_TIME_SECONDS = time.time()

# Declare the exposition version and charset so Starlette does not append a second charset.
_METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _metrics_exposition() -> str:
    """Render this process's liveness in the Prometheus text exposition format."""
    return (
        "# HELP confessit_backend_up 1 while this process can serve a request.\n"
        "# TYPE confessit_backend_up gauge\n"
        "confessit_backend_up 1\n"
        "# HELP confessit_backend_start_time_seconds Process start time since the Unix epoch.\n"
        "# TYPE confessit_backend_start_time_seconds gauge\n"
        f"confessit_backend_start_time_seconds {_PROCESS_START_TIME_SECONDS:.3f}\n"
    )


def _init_dependency_instrumentation(postgres: Database) -> None:
    """Instrument the three dependencies the server span used to sit on top of.

    Without these, a request produced a single ``POST /confessions`` span and the
    time spent in PostgreSQL, Redis and Cloudinary was unattributed, so a slow
    endpoint could not be told apart from a slow query.

    SQLAlchemy is instrumented against this one shared engine rather than globally,
    because the engine is created per :class:`~app.postgres.PostgresClient` and
    the one the process serves traffic from is the one that matters. Redis and urllib3
    patch their clients globally, which is why they need no arguments; the urllib3
    hook is what keeps a signed Cloudinary query string out of the span.

    Which database tracing runs is decided by ``telemetry.sqlalchemy_instrumentation``:
    the contrib instrumentor refuses SQLAlchemy >= 2.1, in which case the in-house
    Engine adapter in the same module takes over (same CLIENT spans, same db.*
    names) instead of silently emitting nothing.

    Called from ``on_startup`` rather than at import because it needs a live engine,
    and after that event's own schema work so that booting the process does not emit
    a burst of spans that belong to no request.
    """
    from .telemetry import instrument_database_engine, sqlalchemy_instrumentation

    if sqlalchemy_instrumentation() == "manual":
        logger.info(
            "Contrib SQLAlchemy instrumentor does not support the installed "
            "SQLAlchemy; using the in-house Engine adapter for database spans."
        )
        instrument_database_engine(postgres.engine)
    else:
        SQLAlchemyInstrumentor().instrument(engine=postgres.engine)
    RedisInstrumentor().instrument()
    URLLib3Instrumentor().instrument(request_hook=redact_client_url)


app = FastAPI(
    title="ConfessIt API",
    description="Backend for the anonymous confession and matchmaking application.",
    version="0.1.0"
)

# The process-local handle for the automated AI session generation worker,
# started on startup when ``AI_GENERATION_WORKER_ENABLED`` is set. One per
# process; the queue itself lives in Postgres, so extra processes only poll.
_ai_generation_worker = None

# The process-local handle for the task that publishes the game-reset heartbeat.
# One per process; the heartbeat itself lives in Postgres, so the workers all
# publish the same value.
_reset_heartbeat_task = None

# How often the heartbeat is published. The metric it produces is a timestamp, so
# the only thing staleness costs is how long the alert takes to notice a change --
# and the tick itself runs once a day.
RESET_HEARTBEAT_PUBLISH_INTERVAL_SECONDS = 60


async def _publish_reset_heartbeat_forever(postgres: Database) -> None:
    """Publish the game-reset heartbeat as a metric, for as long as the process runs.

    The tick records its outcome in the database; this reads that record and turns
    it into the two series the alerting rules read. A read that fails is logged
    and retried on the next tick of the loop: the last published value stays
    standing, so the alert keeps saying "the last success was at <time>", which is
    still true, and the age keeps growing.
    """
    from .services.reset_heartbeat import publish_reset_heartbeat

    while True:
        try:
            publish_reset_heartbeat(postgres)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Could not publish the game-reset heartbeat", exc_info=True)
        await asyncio.sleep(RESET_HEARTBEAT_PUBLISH_INTERVAL_SECONDS)

# ----------------------
# Startup Event
# ----------------------
@app.on_event("startup")
async def on_startup():
    # Uvicorn's own loggers are configured before this module is imported, but a
    # different launcher (or a test harness) may re-configure them later. Reinstall
    # the idempotent query-credential filter once the process is actually serving.
    install_query_credential_redaction()
    logger.info("Initializing PostgreSQL storage...")
    # The process-wide client, so startup and every request share one pool.
    postgres = get_postgres_client()
    for attempt in range(30):
        try:
            with postgres.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(2)

    # Validate private image delivery before schema work so the app cannot start in a broken mode.
    validate_cloudinary_settings()
    logger.info(
        "Cloudinary image delivery validated; love note images are private signed-delivery "
        "objects. Note that these signatures do not expire: expiry would require Cloudinary "
        "authentication tokens and the account-wide Auth token key that is not available on "
        "this plan. Entitlement is therefore re-checked on every response that mints a URL."
    )

    # Before any login can be minted: a cloud deployment without PUBLIC_BASE_URL would print
    # localhost magic links for every prod sign-in. Same fail-fast shape as the check above.
    validate_public_base_url()

    # Before any credential traverses a wire: a cloud deployment on cleartext database
    # and cache connections leaks everything it stores at the network layer. Same
    # fail-fast shape as the checks above, with an explicit single-host opt-out.
    validate_transport_security()

    # Before any session cookie is minted: a cloud deployment that cannot know the
    # client-facing scheme (no trusted proxies, no Secure override) mints bearer
    # cookies without Secure on an https site. Same fail-fast shape as above.
    validate_cookie_transport()

    # Validate the AES-256-GCM key before accepting chat. The scheduler process does not handle chat.
    from .services.chat_encryption import validate_startup_configuration

    validate_startup_configuration()
    logger.info("Chat message encryption configuration validated.")

    postgres.initialize()
    # Account seeding is explicit via app/test.py. Couple Prediction's required
    # question bank is the exception and is seeded idempotently at startup.
    from .services.couple_service import seed_couple_questions
    seeded = seed_couple_questions(postgres)
    logger.info("Couple game questions seeded: %s inserted.", seeded)
    logger.info("PostgreSQL storage initialized.")

    # Ensure sessions at boot using the same idempotent reset path as the daily cron.
    # Best-effort so scheduler failures do not prevent the app from starting.
    try:
        from .services.game_scheduler import ensure_game_sessions

        boot_resets = ensure_game_sessions(postgres, startup=True)
        logger.info("Game sessions ensured at boot: %s", boot_resets)
    except Exception as exc:
        logger.warning("Game session boot ensure skipped: %s", exc)

    # Trivia Drop has no period: a question once set stays the pipeline's
    # content, so start-up is the one moment worth checking that anything is
    # live or scheduled. Run after the boot ensure so a question due today has
    # already been released. Best-effort like the game ensure above.
    try:
        from .services.staff_notification_service import notify_trivia_drop_empty

        notify_trivia_drop_empty(postgres)
    except Exception as exc:
        logger.warning("Trivia Drop boot notification skipped: %s", exc)

    # Start the shared-lock AI worker for queued Plot Twist and Case Closed generation.
    global _ai_generation_worker
    if settings.AI_GENERATION_WORKER_ENABLED:
        try:
            from .services.ai_game_automation import GenerationWorker

            _ai_generation_worker = GenerationWorker(postgres)
            _ai_generation_worker.start()
            logger.info("AI generation worker started.")
        except Exception as exc:
            logger.warning("AI generation worker not started: %s", exc)

    # Initialize matchmaking slots: fetch all classes and create initial slot
    from .services.matchmaking_slot_service import init_matchmaking_slot_service
    init_result = init_matchmaking_slot_service(postgres)
    logger.info(
        "Matchmaking slots initialized: %s - %s",
        init_result.get("message"),
        init_result.get("classes_count", 0),
    )

    # Last, so the schema work above is not traced against no request.
    _init_dependency_instrumentation(postgres)

# Refresh and export the reset heartbeat gauge after database startup.
# Skip polling when no metrics exporter is configured.
    global _reset_heartbeat_task
    if _reset_heartbeat_task is None and settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        _reset_heartbeat_task = asyncio.create_task(_publish_reset_heartbeat_forever(postgres))


@app.on_event("shutdown")
def on_shutdown():
    # Hand the pool's connections back so a reload or restart does not leak them until
    # Postgres itself times them out. Redis pool is closed explicitly for the same
    # reason: PubSub connections otherwise linger past shutdown.
    global _ai_generation_worker
    if _ai_generation_worker is not None:
        # Best-effort stop: a pass mid-provider-call keeps the lock until its
        # process dies, and the stale-job reclaim picks the cycle up again.
        _ai_generation_worker.stop()
        _ai_generation_worker = None
    global _reset_heartbeat_task
    if _reset_heartbeat_task is not None:
        # The task only reads the database and sets a gauge, so cancelling it at
        # shutdown is enough; there is nothing to flush on the way out.
        _reset_heartbeat_task.cancel()
        _reset_heartbeat_task = None
    close_postgres_client()
    try:
        redis_service.close()
    except Exception as exc:
        logger.warning("Redis close on shutdown failed: %s", exc)

# Request body size limit
# Enforced before framework buffering; Content-Length and receive counting cover fixed and chunked bodies.


# Unencodable input in a 422
# Escape invalid UTF-8 in validation errors so refused requests still return 422.


def _make_encodable(value):
    """Return ``value`` with any un-encodable string repaired, recursively."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {key: _make_encodable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_encodable(item) for item in value]
    return value


def _request_validation_response(request: Request, exc: RequestValidationError) -> Response:
    return JSONResponse(
        {"detail": _make_encodable(jsonable_encoder(exc.errors()))},
        status_code=422,
    )


# A draw that was refused
# Keep the human-readable detail and a stable code for client branching.
def _matchmaking_refusal_response(request: Request, exc: MatchmakingRefusal) -> Response:
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code},
        status_code=exc.status_code,
    )


def _game_unavailable_response(request: Request, exc) -> Response:
    """Render a game availability refusal with its client-branchable code.

    ``detail`` stays the plain sentence toasts already show; ``code`` is what
    the screens branch on (``game_disabled`` vs ``game_hidden``). Registered for
    the subclass so ordinary HTTPExceptions keep the default body.
    """
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code},
        status_code=exc.status_code,
    )


class SecurityHeadersMiddleware:
    """Stamp every HTTP response with the baseline security headers.

    Pure ASGI with no dependencies, so it cannot fail a request: header-setting is
    infallible, and anything raised downstream travels through `send` untouched.

    * ``X-Content-Type-Options: nosniff`` -- a JSON body must never be sniffed into an
      executable type, which is what turns a stored string into someone else's script.
    * ``X-Frame-Options: DENY`` plus ``frame-ancestors 'none'`` -- nothing here is
      framed, so nothing may be (belt and suspenders across old and new browsers).
    * ``Referrer-Policy: no-referrer`` -- responses carry bearer URLs (signed images,
      magic-link deadlines); a later navigation must not deposit them in a third
      party's logs. The SPA's own document carries the same policy in ``index.html``.
    * ``Permissions-Policy`` -- this API uses no camera, microphone or geolocation, so
      none is granted, including to any document that somehow embeds it.
    * ``Cross-Origin-Opener-Policy: same-origin`` -- our window cannot be adopted by a
      cross-origin one. Safe: no flow here opens popups.
    * ``Cross-Origin-Resource-Policy: same-site`` -- no-cors cross-site reads (an
      ``<img>`` or ``<script>`` pointed at the API) are refused. Safe for the same
      reason ``SameSite=Lax`` cookies work here at all: every supported deployment is
      same-site, and a cross-site one would already be broken for auth. CORS remains
      the access control for fetch-mode callers.
    * ``Content-Security-Policy`` -- tight because the API serves no HTML, images or
      scripts of its own: scripts, objects and frames have nowhere legitimate to come
      from. (The SPA document's own, broader policy lives in the Nginx example: it
      has fonts, images and sockets to allow.)
    * ``Strict-Transport-Security`` -- only on HTTPS responses. Emitting it on plain
      HTTP would be ignored by browsers, and gating it here (rather than trusting the
      socket) keeps it correct behind the TLS proxy too.
    """

    _POLICY = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"
                headers["Referrer-Policy"] = "no-referrer"
                headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Resource-Policy"] = "same-site"
                headers["Content-Security-Policy"] = self._POLICY
                if scope.get("scheme") == "https":
                    headers["Strict-Transport-Security"] = (
                        "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, send_with_headers)


class TrustedHostMiddleware:
    """Refuse requests for hostnames this deployment does not serve.

    DNS rebinding and Host-header poisoning both aim at making this process answer for
    a name it was never meant to -- password-reset links, cache keys and absolute
    redirects built from ``Host`` then go somewhere hostile. The allowlist is read
    per request from ``ALLOWED_HOSTS`` so tests and operators change it without a
    restart. Runs inside the proxy middleware, so the name checked is the
    client-facing one the proxy reported, not the proxy-to-app hop.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            raw_host = Headers(scope=scope).get("host", "")
            if raw_host.startswith("["):
                bare = raw_host[1:].split("]", 1)[0]
            else:
                bare = raw_host.split(":")[0]
            allowed = {
                h.strip().lower()
                for h in settings.ALLOWED_HOSTS.split(",")
                if h.strip()
            }
            if bare.lower() not in allowed:
                response = PlainTextResponse("Invalid host", status_code=400)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _limiter_redis():
    """The Redis client for the flood brake, factored out so tests can replace it."""
    return redis_service.client


class GlobalRateLimitMiddleware:
    """A per-IP flood brake across all paths, counted in Redis.

    One fixed window per minute per client IP, shared by every worker through a single
    ``INCR``/``EXPIRE`` pair. Deliberately generous (see
    ``GLOBAL_RATE_LIMIT_PER_MINUTE``): shared campus NATs put many legitimate users
    behind one address, and the login endpoints keep their own stricter, layered
    limits -- this catches floods, not people. Zero or negative disables it. A Redis
    outage fails open with a warning: the brake must not become the outage, and the
    per-endpoint limits still hold.

    Runs inside the proxy middleware so the IP is the client-facing one the proxy
    reported (leftmost ``X-Forwarded-For`` entry when proxies are configured,
    otherwise the socket peer).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = settings.GLOBAL_RATE_LIMIT_PER_MINUTE
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        client = scope.get("client")
        ip = client[0] if client else "unknown"
        if settings.TRUSTED_PROXY_HOSTS.strip():
            forwarded = Headers(scope=scope).get("x-forwarded-for")
            if forwarded and forwarded.split(",")[0].strip():
                ip = forwarded.split(",")[0].strip()

        window = int(time.time() // 60)
        try:
            store = _limiter_redis()
            count = int(store.incr(f"rl:global:{ip}:{window}"))
            if count == 1:
                store.expire(f"rl:global:{ip}:{window}", 70)
        except Exception as exc:
            logger.warning("Global rate limiter unavailable, allowing request: %s", exc)
            await self.app(scope, receive, send)
            return

        if count > limit:
            retry_after = 60 - int(time.time() % 60)
            response = JSONResponse(
                {"detail": "Too many requests. Please try again shortly."},
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequestBodyLimitMiddleware:
    """Reject a request whose body exceeds the configured ceiling, declared or not."""
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        # Non-HTTP scopes (the websocket routes) carry no body and are left alone.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                length = int(declared)
            except ValueError:
                # A malformed length is not something to guess at; let the server's own
                # parser reject it.
                pass
            else:
                if length > self.max_bytes:
                    logger.warning(
                        "Refused declared body of %s bytes (limit %s) for %s %s",
                        length, self.max_bytes, scope.get("method"), scope.get("path"),
                    )
                    response = PlainTextResponse(
                        "Request body too large", status_code=413
                    )
                    await response(scope, receive, send)
                    return

        received = 0
        answered_413 = False

        async def counted_receive():
            nonlocal received, answered_413
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes and not answered_413:
                    answered_413 = True
                    logger.warning(
                        "Refused undeclared body past %s bytes for %s %s",
                        self.max_bytes, scope.get("method"), scope.get("path"),
                    )
# Send 413 directly, stop body parsing, and let outer middleware add standard headers.
                    response = PlainTextResponse(
                        "Request body too large", status_code=413
                    )
                    await response(scope, receive, send)
                    return {"type": "http.disconnect"}
            return message

        try:
            await self.app(scope, counted_receive, send)
        except ClientDisconnect:
            # Our own doing: the parser saw the disconnect above and aborted, and the
            # 413 already went out on this connection. Swallowing only this case keeps
            # an abuse attempt from becoming an error traceback in the logs; a
            # disconnect the client caused itself still propagates.
            if answered_413:
                return
            raise


def request_body_limit() -> int:
    """Resolve the body ceiling, defaulting to the largest legitimate body.

    A love note is the biggest request this app accepts: a base64 image plus a JSON
    envelope. Sizing the default off the image limits rather than hardcoding a number
    keeps the ceiling correct if MAX_IMAGE_SIZE_MB is raised, and the extra headroom
    covers multipart boundaries and form fields.
    """
    if settings.MAX_REQUEST_BODY_BYTES:
        return int(settings.MAX_REQUEST_BODY_BYTES)
    largest_payload = max(
        storage_service.max_base64_chars,
        storage_service.max_image_bytes,
    )
    return largest_payload + 256 * 1024


# Registered before CORS so that CORS ends up outermost: a 413 answered here still has to
# be readable by the browser, and a response without the CORS headers surfaces as an
# opaque network error instead of the status the client needs to see.
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=request_body_limit())
app.add_exception_handler(RequestValidationError, _request_validation_response)
# Registered for the subclass, so it does not shadow the ordinary HTTPException handler: FastAPI
# walks the exception's bases, so a plain HTTPException still gets the default body.
app.add_exception_handler(MatchmakingRefusal, _matchmaking_refusal_response)
from .services.game_configs import GameUnavailable as _GameUnavailable

app.add_exception_handler(_GameUnavailable, _game_unavailable_response)

# ----------------------
# CORS Middleware
# ----------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(),
    allow_credentials=True,
    # Exactly what the frontend sends: credentialed JSON reads and writes. No HEAD,
    # TRACE or anything else exists on any router, so the preflight answer does not
    # offer them. `OPTIONS` is the preflight itself.
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    # Exactly what the frontend sends: the bearer scheme, the JSON content type (and
    # its multipart form on uploads), and the default Accept. Anything else fails
    # preflight instead of reaching a handler.
    allow_headers=["Authorization", "Content-Type", "Accept"],
# Expose Retry-After to browser clients so the login screen can show cooldown timing.
    expose_headers=["Retry-After"],
    # Cache a successful preflight for ten minutes so browsers do not re-ask on every
    # call. The answer only changes on redeploy, which restarts the process anyway.
    max_age=600,
)

# Added here so both run inside the proxy middleware below: the rate limiter must count
# the client-facing IP the proxy reported, and the host check must see the
# client-facing name. Both read their settings per request, so operators change them
# without a restart.
app.add_middleware(GlobalRateLimitMiddleware)
app.add_middleware(TrustedHostMiddleware)

# Trust forwarded scheme and host only from configured proxies; spoofed headers could
# otherwise affect secure-cookie decisions and client identity.
_proxy_hosts = [h.strip() for h in settings.TRUSTED_PROXY_HOSTS.split(",") if h.strip()]
if _proxy_hosts:
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_proxy_hosts)

# Added last so it runs outermost: every response -- routed, refused, or errored --
# leaves with the baseline headers, including preflights and rate-limit refusals.
app.add_middleware(SecurityHeadersMiddleware)

# ----------------------
# Routers
# ----------------------
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(matchmaking.router)
app.include_router(confessions.router)
app.include_router(love_notes.router)
app.include_router(conversations.router)
app.include_router(messages.router)
app.include_router(notifications.router)
app.include_router(staff_notifications.router)
app.include_router(admin.router)
app.include_router(admin_love_notes.router)
app.include_router(admin_wordle.router)
app.include_router(admin_anagram.router)
app.include_router(admin_couple.router)
app.include_router(admin_connections.router)
app.include_router(admin_trivia_drop.router)
app.include_router(admin_reaction_time.router)
app.include_router(admin_plot_twist.router)
app.include_router(admin_case_closed.router)
app.include_router(admin_games.router)
app.include_router(wordle.router)
app.include_router(anagram.router)
app.include_router(connections.router)
app.include_router(trivia_drop.router)
app.include_router(couple_questionnaire.router)
app.include_router(plot_twist.router)
app.include_router(case_closed.router)
app.include_router(reaction_time.router)
app.include_router(two_key_escape.router)
app.include_router(games.router)
app.include_router(leaderboards.router)

# Redact URL queries before export because asset tokens use a query parameter. Exclude
# Prometheus's self-scrape from tracing and HTTP metrics; the regex matches its full URL.
EXCLUDED_INSTRUMENTED_URLS = r"://[^/]+/metrics/?(?:\?.*)?$"

FastAPIInstrumentor.instrument_app(
    app,
    server_request_hook=redact_query_strings,
    excluded_urls=EXCLUDED_INSTRUMENTED_URLS,
)

# Images are served directly by Cloudinary. Love-note URLs are durable bearer capabilities,
# so mint them only for entitled readers, disable caching/referrers, and delete or rotate
# assets when access is revoked.


# ----------------------
# WebSocket Endpoints


# ----------------------
# Endpoints
# ----------------------
@app.get("/", tags=["Root"])
def read_root():
    logger.info("Root endpoint accessed")
    return {"message": "Welcome to the ConfessIt API!"}

@app.get("/api/time", tags=["Root"])
def read_server_time():
    """Report the server's own UTC time so clients need not trust the device clock.

    The application stores and computes everything in UTC, and this is the reference the
    browser measures itself against. A device with a wrong clock would otherwise decide on
    its own that a conversation has expired, or that a cooldown has lifted, and no amount
    of correct timestamps on the wire would stop it.

    Carries an explicit offset and is marked no-store, so an intermediary cannot hand back
    a stale "now" and have the client believe it.
    """
    now = datetime.now(timezone.utc)
    return JSONResponse(
        {
            "now": now.isoformat(),
            "epoch_ms": int(now.timestamp() * 1000),
        },
        headers={"Cache-Control": "no-store"},
    )

@app.get("/metrics", include_in_schema=False)
def read_metrics():
    """Expose process liveness for Prometheus to scrape.

    The target of the ``confessit-backend`` job in prometheus.yml, and what makes
    ``up{job="confessit-backend"}`` a real signal about this process rather than about
    the collector. See the block comment above ``_metrics_exposition`` for why this
    carries liveness only and not the OTel instruments.
    """
    return PlainTextResponse(_metrics_exposition(), media_type=_METRICS_CONTENT_TYPE)

if ENABLE_DEBUG_ENDPOINTS:
    @app.get("/db-test", include_in_schema=False)
    def db_test(db: Database = Depends(get_db)):
        """Used to test the database connection. Registered only when debug endpoints are enabled."""
        try:
            with db.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.info("Database connection test successful")
            return {"status": "success", "message": "Successfully connected to the database"}
        except Exception as e:
            # The exception text can embed DSNs, hostnames and driver internals, so it is
            # logged server-side only and never returned to the caller.
            logger.error(f"Failed to connect to the database: {e}")
            return {"status": "error", "message": "Failed to connect to the database"}


# ----------------------
# Main entrypoint
# ----------------------
if __name__ == "__main__":
# Run from the backend directory with ``uvicorn app.main:app --reload --host 0.0.0.0 --port 8001``.
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        limit_concurrency=settings.MAX_CONCURRENCY,
        h11_max_incomplete_event_size=16 * 1024,
    )
