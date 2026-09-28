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
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from sqlalchemy import text
from .config import (
    settings,
    cors_allow_origins,
    validate_cloudinary_settings,
    validate_public_base_url,
)
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, admin_wordle, admin_anagram, admin_couple, admin_connections, admin_trivia_drop, admin_games, admin_reaction_time, messages, wordle, anagram, connections, trivia_drop, couple_questionnaire, reaction_time, games, two_key_escape, leaderboards
from .services.auth_service import verify_token_service
from .dependencies import get_db
from .postgres import Database, close_postgres_client, get_postgres_client
from .services.storage_service import storage_service
from .services.redis_service import redis_service
from .services.matchmaking_service import MatchmakingRefusal
from .models import AccountDetails
import uvicorn
from .logger import get_logger, otlp_transport
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

# Diagnostic endpoints are opt-in so raw database/driver internals are never reachable
# from an unauthenticated request in a deployed environment.
ENABLE_DEBUG_ENDPOINTS = bool(settings.ENABLE_DEBUG_ENDPOINTS)

# The dimensions the http.server.* instruments are allowed to keep. The
# instrumentation's own modern attribute set for the duration histogram is
# ['error.type', 'http.request.method', 'http.response.status_code', 'http.route',
# 'network.protocol.version', 'url.scheme']; the body-size histograms and
# http.server.active_requests use subsets of it.
#
# The bound is still worth having even in modern-only mode. The middleware builds
# its attribute list per stability mode, so http.server_name and the other Host-
# derived names are not currently among the dimensions it records -- but they were
# the reason this View exists, and the cost of keeping the allowlist is that it
# keeps failing closed if a future release puts one of them back. The View is also
# what guarantees the bound rather than relying on the current default.
#
# An allowlist rather than a denylist because the SDK's View offers no
# drop_attribute_keys, which is how "drop exactly this one key" would normally be
# written. It fails closed: an attribute a future instrumentation release starts
# emitting is excluded until someone decides it belongs here.
#
# Only the current conventions are listed, because the backend runs with
# OTEL_SEMCONV_STABILITY_OPT_IN=http (see docker-compose.yml) and the pre-1.20
# names are no longer emitted at all. Listing them would be dead configuration.
#
# error.type is left out even though the instrumentation's attribute set includes
# it: nothing in this stack queries it, and the error-rate rule keys off
# http.response.status_code. Excluding it keeps the emitted label set to exactly
# what the derivations in README.md need.
#
# The route dimension is http.route, the route *template* the middleware reads from
# route.path_format, so one series covers every confession id rather than one per
# id. It is not a request target and cannot reintroduce the query string that
# app.telemetry removes from spans.
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
    """Register the MeterProvider that the HTTP instrumentation records through.

    Without this the SDK leaves a NoOp MeterProvider in place, so the middleware's
    instruments are created against a provider that discards every measurement. The
    ASGI middleware already builds the standard http.server.* instruments (duration,
    request/response size, active requests) and takes a meter when
    FastAPIInstrumentor runs, so registering the provider is all the HTTP metrics
    need: there is no counter to add per route and no new dependency.

    Metrics go out over the same OTLP gRPC endpoint and the same resource as the
    traces, so the collector's existing metrics pipeline carries them to the
    prometheus exporter on :8889, which prometheus.yml already scrapes. Nothing in
    the collector configuration had to change.

    This must run before FastAPIInstrumentor.instrument_app below. The instrumentor
    resolves the global meter provider when it runs, not when a measurement is
    recorded, so a provider registered afterwards would never be seen and the
    instruments would stay bound to the NoOp one.
    """
    endpoint = settings.OTEL_EXPORTER_OTLP_ENDPOINT
    if not endpoint:
        return

    service_name = settings.OTEL_SERVICE_NAME
    resource = Resource.create({"service.name": service_name})

    insecure, credentials = otlp_transport()

    exporter = OTLPMetricExporter(endpoint=endpoint, insecure=insecure, credentials=credentials)
    # 15s matches the scrape interval in prometheus.yml and the default datasource
    # timeInterval. The SDK default of 60s would mean a point is often exported
    # after the scrape that would have collected it, so a restarted backend looks
    # empty for a minute.
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


# ---------------------------------------------------------------------------
# Scrape endpoint
# ---------------------------------------------------------------------------
# Why this exists. The backend's metrics are *pushed* over OTLP to the collector, and
# the collector re-exports them on :8889 for Prometheus to scrape. A pushed pipeline has
# no "is the producer alive?" series anywhere in it: the only thing that observes the
# backend is the collector, and the collector is perfectly healthy when the backend that
# feeds it is dead. Until this endpoint existed the availability rule could only watch
# the collector, so "the backend died" and "the collector died" looked identical, and the
# comment in alerting-rules.yml had to admit it could not tell them apart.
#
# What it deliberately does NOT do is re-export the OTel instruments. Adding a
# PrometheusMetricReader to the same MeterProvider would serve a second copy of every
# http_server_* series under a different job label, so the same request would be counted
# twice by any query that does not pin exported_job. Two sources for one measurement is a
# worse problem than the one being solved, so the application metrics stay on the OTLP
# path and this endpoint carries only what that path structurally cannot provide.
#
# Three series, written out by hand in the Prometheus text exposition format 0.0.4 rather
# than by a client library. The format is three lines per series and is stable; pulling
# in prometheus_client, or the opentelemetry-exporter-prometheus package that would drag
# the whole MeterProvider along, would add a dependency to produce a payload this small.
# A test asserts the body parses and that the OTLP-only series are absent, so the
# hand-written part cannot quietly drift.
#
# confessit_backend_up is 1 by construction -- it exists to be *absent* when the process
# cannot answer, which Prometheus records as up{job="confessit-backend"} == 0. Keeping
# the explicit gauge as well means the signal is legible from the series itself, and not
# only from the scrape bookkeeping.
#
# confessit_backend_start_time_seconds is what makes a *restart* visible: a series that
# silently resets to 1 is easy to miss in a dashboard, a start time that jumps is not.

_PROCESS_START_TIME_SECONDS = time.time()

# The exposition content type. The version is part of the media type because that is how
# a scrape declares the format it is speaking; Prometheus rejects a body it cannot parse,
# and charset is stated explicitly so Starlette does not append a second one.
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
    because the engine is created per :class:`~app.postgres.PostgresClient` and the
    one the process serves traffic from is the one that matters. Redis and urllib3
    patch their clients globally, which is why they need no arguments; the urllib3
    hook is what keeps a signed Cloudinary query string out of the span.

    Called from ``on_startup`` rather than at import because it needs a live engine,
    and after that event's own schema work so that booting the process does not emit
    a burst of spans that belong to no request.
    """
    SQLAlchemyInstrumentor().instrument(engine=postgres.engine)
    RedisInstrumentor().instrument()
    URLLib3Instrumentor().instrument(request_hook=redact_client_url)


app = FastAPI(
    title="ConfessIt API",
    description="Backend for the anonymous confession and matchmaking application.",
    version="0.1.0"
)

# ----------------------
# Startup Event
# ----------------------
@app.on_event("startup")
async def on_startup():
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

    # Before any schema work: a deployment that cannot deliver love note images should not come up
    # looking healthy and then 503 in the inbox of the person who was sent one. The failure is
    # raised, not logged and continued past, because there is no degraded mode for a private image
    # store -- serving love notes from anywhere weaker than a private signed-delivery object is not
    # a fallback this application has.
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

    postgres.initialize()
    # No accounts are created here. Booting the application used to seed a test participant
    # and a test admin, which meant a fresh deployment -- or a dropped-and-recreated
    # development database -- came up holding a ``Test Admin`` account whose regno is
    # guessable, and it fought the seed script for the same regnos. Seeding is a deliberate
    # act with a deliberate owner, so it lives in app/test.py and runs when asked for.
    #
    # The Couple Prediction question bank is the deliberate exception: fifty static
    # templates the game cannot run without, seeded idempotently from the committed
    # list. The database remains the source of truth after the first boot -- the
    # seed inserts only what is missing, never a duplicate and never an overwrite.
    from .services.couple_service import seed_couple_questions
    seeded = seed_couple_questions(postgres)
    logger.info("Couple game questions seeded: %s inserted.", seeded)
    logger.info("PostgreSQL storage initialized.")

    # Open today's game sessions idempotently, so a fresh boot is playable
    # without waiting for the 00:01 UTC cron. The same function backs
    # POST /admin/games/run-resets, which is what a daily 00:01 UTC cron calls
    # to roll each game into its next calendar period. Best-effort at boot: a
    # scheduler failure must not take the whole application down.
    try:
        from .services.game_scheduler import ensure_game_sessions

        boot_resets = ensure_game_sessions(postgres)
        logger.info("Game sessions ensured at boot: %s", boot_resets)
    except Exception as exc:
        logger.warning("Game session boot ensure skipped: %s", exc)

    # Initialize matchmaking slots: fetch all classes and create initial slot
    from .services.matchmaking_slot_service import init_matchmaking_slot_service
    init_result = await init_matchmaking_slot_service(postgres)
    logger.info(
        "Matchmaking slots initialized: %s - %s",
        init_result.get("message"),
        init_result.get("classes_count", 0),
    )

    # Last, so the schema work above is not traced against no request.
    _init_dependency_instrumentation(postgres)


@app.on_event("shutdown")
async def on_shutdown():
    # Hand the pool's connections back so a reload or restart does not leak them until
    # Postgres itself times them out.
    close_postgres_client()

# ----------------------
# Request body size limit
# ----------------------
# A request body is buffered by the framework, not by the handler, so a per-handler size
# check runs only after the memory has already been spent. This refuses the body at the
# edge instead. It is deliberately a raw ASGI middleware rather than BaseHTTPMiddleware:
# the check is on a single header, and wrapping every request in an extra task and
# response cycle to read one integer would cost more than it saves.
#
# Two bounds, because either one alone is bypassable. The declared Content-Length turns
# away an oversize body before a byte of it is read, and the receive wrapper counts what
# actually arrives, which is the only bound a chunked request -- which declares no
# length at all -- can be caught by. Counting also bounds the multipart spool, which
# otherwise writes an unbounded body to a temp file before any handler sees it.


class RequestBodyTooLarge(Exception):
    """Raised from the receive channel once more than the cap has actually arrived."""


async def _body_too_large_response(request: Request, exc: RequestBodyTooLarge) -> Response:
    return PlainTextResponse("Request body too large", status_code=413)


# ----------------------
# Unencodable input in a 422
# ----------------------
# A validation error quotes the offending value back to the caller, and that value is
# arbitrary user input. If it is not valid UTF-8 -- an unpaired surrogate, which a JSON
# ``\udXXX`` escape can produce and which ``str.encode('utf-8')`` refuses -- then rendering
# the 422 itself raises UnicodeEncodeError and the client gets a 500 for a request the
# server had already correctly rejected. The rejection was right; only the way it was
# reported was broken.
#
# So the 422 is rendered with backslashreplace, which is already the escape these
# characters use on the wire. The response keeps FastAPI's usual shape and the message is
# still legible; only the unencodable bytes change representation. This is not a place to
# sanitize the request itself -- by the time this runs the value has been refused.


def _make_encodable(value):
    """Return ``value`` with any un-encodable string repaired, recursively."""
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {key: _make_encodable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_make_encodable(item) for item in value]
    return value


async def _request_validation_response(request: Request, exc: RequestValidationError) -> Response:
    return JSONResponse(
        {"detail": _make_encodable(jsonable_encoder(exc.errors()))},
        status_code=422,
    )


# ----------------------
# A draw that was refused
# ----------------------
# A matchmaking refusal is a 404 whose ``detail`` is a sentence written for a person, and the
# page has to decide what to *show* as well as what to say in a toast. Those are different
# sentences: a draw refused because nobody was available and a draw refused because the cohort has
# no slot are both "no match", but only the second one is permanent, and telling that user to try
# again would be as wrong as telling them they are on a cooldown.
#
# So the code travels beside the sentence rather than inside it. ``detail`` stays a plain string,
# which keeps ``describeApiError`` and the toast exactly as they were; the client branches on
# ``code`` and falls back to the sentence alone if it meets a server that does not send one.
async def _matchmaking_refusal_response(request: Request, exc: MatchmakingRefusal) -> Response:
    return JSONResponse(
        {"detail": exc.detail, "code": exc.code},
        status_code=exc.status_code,
    )


async def _game_unavailable_response(request: Request, exc) -> Response:
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

        async def counted_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    logger.warning(
                        "Refused undeclared body past %s bytes for %s %s",
                        self.max_bytes, scope.get("method"), scope.get("path"),
                    )
                    raise RequestBodyTooLarge
            return message

        await self.app(scope, counted_receive, send)


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


# Registered before CORS so that CORS ends up outermost: a 413 raised here still has to
# be readable by the browser, and a response without the CORS headers surfaces as an
# opaque network error instead of the status the client needs to see.
app.add_middleware(RequestBodyLimitMiddleware, max_bytes=request_body_limit())
app.add_exception_handler(RequestBodyTooLarge, _body_too_large_response)
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
    # `Retry-After` is a plain response header, so a cross-origin fetch cannot read it
    # unless it is named here: the browser withholds non-safelisted response headers
    # from JS unless the server publicly lists them. The login page turns the value it
    # carries into "You can try again in about N minutes", so without this line that
    # toast could never know how long the wait is. One name is listed, not a wildcard:
    # the `*` value is not honoured with credentials, and this is the one header the
    # client actually reads.
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

# Added last so it runs outermost: every middleware and handler below sees the
# client-facing scheme and host the proxy reported, not the proxy-to-app hop. Only
# added when TRUSTED_PROXY_HOSTS names the proxies -- trusting X-Forwarded-* from
# anyone would let a direct client declare its own scheme as https (upgrading its own
# cookies to Secure) or spoof its address. With no proxies configured the app is
# reached directly and the socket already tells the truth, so nothing is added.
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
app.include_router(admin.router)
app.include_router(admin_love_notes.router)
app.include_router(admin_wordle.router)
app.include_router(admin_anagram.router)
app.include_router(admin_couple.router)
app.include_router(admin_connections.router)
app.include_router(admin_trivia_drop.router)
app.include_router(admin_reaction_time.router)
app.include_router(admin_games.router)
app.include_router(wordle.router)
app.include_router(anagram.router)
app.include_router(connections.router)
app.include_router(trivia_drop.router)
app.include_router(couple_questionnaire.router)
app.include_router(reaction_time.router)
app.include_router(two_key_escape.router)
app.include_router(games.router)
app.include_router(leaderboards.router)

# The hook drops the query string from the span's URL attributes before the span is
# exported, because the middleware's http.url otherwise carries the raw query string
# and the asset delivery routes below authenticate on a scoped token passed as `t`.
# See app.telemetry for why the hook is used rather than a span processor.
#
# excluded_urls keeps /metrics out of both the spans and the HTTP server metrics. That
# endpoint is Prometheus's own liveness probe and Prometheus scrapes it every 15s, so
# instrumenting it makes the scrape the loudest thing in the application: measured at
# 92% of all request rate and ~3,800 spans/day, and it filled Tempo's search results
# with nothing but `GET /metrics`. Every "top routes" and "request rate" view is then
# dominated by the monitoring system measuring itself, which is the opposite of useful.
# The series that matter for liveness are served from that endpoint and scraped
# directly as the `confessit-backend` job, so nothing is lost by not tracing it.
#
# The pattern is anchored to the authority rather than to the start of the string on
# purpose. 0.66b0 does not match this against the path: it splits the argument on
# commas, joins the parts into one alternation, and `re.search`es the result against the
# full `scheme://host:port/path?query` string. So `^/metrics$` matches nothing at all,
# and an unanchored `/metrics$` is nearly as bad in the other direction -- it also
# swallows `/api/metrics`. `://[^/]+` consumes the authority and then requires
# `/metrics` to be the entire remaining path. tests/test_metrics_endpoint.py asserts
# this against real requests, because a pattern that silently stops matching leaves the
# scrape noise behind with nothing to indicate it.
EXCLUDED_INSTRUMENTED_URLS = r"://[^/]+/metrics/?(?:\?.*)?$"

FastAPIInstrumentor.instrument_app(
    app,
    server_request_hook=redact_query_strings,
    excluded_urls=EXCLUDED_INSTRUMENTED_URLS,
)

# ----------------------
# Image delivery
# ----------------------
# There are no image routes here, and that is the design rather than a gap.
#
# Profile pictures are served straight from Cloudinary's CDN as ordinary public HTTPS URLs, and
# love note images are served from the same CDN as ``authenticated`` objects behind a signed
# delivery URL that this application mints only after deciding the viewer is entitled. In both
# cases the bytes never pass through this process: there is no origin fetch, no per-request
# entitlement re-check on an image GET, and no second copy of every picture in this process's
# memory.
#
# The signature is *not* short-lived: Cloudinary's expiring mechanism is the authentication
# token, which needs an account-wide Auth token key that is not available on this plan (see
# ``config``). An issued love note URL is therefore a durable bearer capability, and the
# mitigations live around that fact rather than pretending otherwise:
#
# * the responses that mint URLs carry ``Cache-Control: no-store`` plus
#   ``Referrer-Policy: no-referrer``, and the frontend renders them through ``<img>``
#   with ``referrerpolicy="no-referrer"``, so the capability is neither cached nor
#   leaked sideways into referrers;
# * taking a note down destroys its image at the store (report removal and permanent
#   delete alike), so previously minted URLs stop resolving;
# * blocking an account rotates its live notes' images (destroy plus re-upload under the
#   same name), which bumps the object version old signatures were minted against, so
#   those URLs stop verifying while entitled viewers keep working through fresh ones.
#
# What moved here instead of out of the codebase is the *authorisation*, and it moved to the point
# where a URL is minted -- inside the services that build these responses. That is the only place
# it can be enforced now: a love note delivery URL is a capability, and unlike a URL naming this
# API it is consumed by somebody else's server, so nothing on our side can later re-check it.


# ----------------------
# WebSocket Endpoints


# ----------------------
# Endpoints
# ----------------------
@app.get("/", tags=["Root"])
async def read_root():
    logger.info("Root endpoint accessed")
    return {"message": "Welcome to the ConfessIt API!"}

@app.get("/api/time", tags=["Root"])
async def read_server_time():
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
async def read_metrics():
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
    # To run this app correctly, use the command:
    # uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
    # This should be run from the parent directory containing the 'app' package.
    # The same body/concurrency limits the container image sets are applied here, so a
    # developer running this entrypoint is not testing a less protected configuration.
    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
        limit_concurrency=settings.MAX_CONCURRENCY,
        h11_max_incomplete_event_size=16 * 1024,
    )
