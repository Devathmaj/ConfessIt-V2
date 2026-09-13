# app/main.py

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from sqlalchemy import select, text, update
from .config import settings
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, messages
from .services.auth_service import oauth2_scheme, verify_token_service, load_active_user_service
from .dependencies import get_db 
from .postgres import Database, close_postgres_client, get_postgres_client
from .services.storage_service import storage_service
from .services import asset_delivery_service
from .models import UserDetails
import uvicorn
from .db_models import UserRecord
from .roles import Role
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
ENABLE_DEBUG_ENDPOINTS = os.getenv("ENABLE_DEBUG_ENDPOINTS", "false").lower() in {"1", "true", "yes"}

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
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "confessit-backend")
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
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "confessit-backend")
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

    postgres.initialize()

    seeds = [
        {"regno": "user", "name": "Test User", "email": "user@test.com", "bio": "This is a test user.",
         "class_name": "Test_Class", "profile_picture_url": None, "gender": "male", "interests": [],
         "matchmaking_enabled": True, "notifications_enabled": True, "love_notes_receive_enabled": True,
         "love_notes_send_enabled": False, "role": Role.USER.value},
        {"regno": "admin", "name": "Test Admin", "email": "admin@test.com", "bio": "This is a test admin.",
         "class_name": "Test_Class", "profile_picture_url": None, "gender": "male", "interests": [],
         "matchmaking_enabled": True, "notifications_enabled": True, "love_notes_receive_enabled": True,
         "love_notes_send_enabled": False, "role": Role.ADMIN.value},
    ]
    with postgres.sessions.begin() as session:
        session.execute(update(UserRecord).where(
            UserRecord.profile_picture_url.in_(["random_string_1", "random_string_2"])
        ).values(profile_picture_url=None))
        for seed in seeds:
            if not session.scalar(select(UserRecord.id).where(UserRecord.regno == seed["regno"])):
                session.add(UserRecord(**seed))
    logger.info("PostgreSQL storage initialized.")

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

# ----------------------
# CORS Middleware
# ----------------------
origins = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8080",
    "http://127.0.0.1:8080",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
# Asset Delivery Routes
# ----------------------
# These are the only paths user-uploaded images are served from. The backend mints the
# URLs it hands out (see asset_delivery_service) and re-checks entitlement on every
# request, so an image URL is never a standalone bearer capability.
def _resolve_asset_requester(
    db: Database,
    kind: str,
    object_path: str,
    query_token: Optional[str],
    bearer_token: Optional[str],
) -> UserDetails:
    """Identify the caller of an asset route.

    A scoped query token is preferred because ``<img src>`` cannot send an Authorization
    header; a normal bearer token is still accepted for programmatic callers.
    """
    if query_token:
        regno = asset_delivery_service.verify_asset_token(
            query_token, kind=kind, object_path=object_path
        )
        return load_active_user_service(db, regno)
    if bearer_token:
        return verify_token_service(bearer_token, db)
    raise HTTPException(
        status_code=401,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _serve_asset(
    kind: str,
    path: str,
    query_token: Optional[str],
    bearer_token: Optional[str],
    db: Database,
) -> Response:
    """Validate, authorise and stream one stored object."""
    # Defined as a sync route so the blocking origin fetch runs in FastAPI's threadpool.
    object_path = storage_service.validate_object_path(path)
    requester = _resolve_asset_requester(db, kind, object_path, query_token, bearer_token)

    if not asset_delivery_service.is_entitled_to_asset(
        db, kind=kind, object_path=object_path, user=requester
    ):
        raise HTTPException(status_code=404, detail="File not found")

    try:
        origin_url = storage_service.get_origin_url(kind, object_path)
    except HTTPException:
        raise HTTPException(status_code=404, detail="File not found")

    payload, content_type = asset_delivery_service.fetch_asset_bytes(origin_url)
    return Response(
        content=payload,
        media_type=content_type,
        headers={
            # Private so the bytes are only reused by the browser that was entitled.
            "Cache-Control": f"private, max-age={asset_delivery_service.asset_token_ttl_seconds()}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/profile/files/{path:path}")
def get_profile_file(
    path: str,
    t: Optional[str] = Query(default=None, description="Scoped asset delivery token"),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db: Database = Depends(get_db),
):
    """Serve a profile image to any signed-in account.

    Profiles appear across matchmaking, confessions and chat, so these are not
    owner-only; love note images remain restricted to the note's parties.
    """
    return _serve_asset("profile", path, t, bearer_token, db)


@app.get("/api/love-notes/files/{path:path}")
def get_love_note_file(
    path: str,
    t: Optional[str] = Query(default=None, description="Scoped asset delivery token"),
    bearer_token: Optional[str] = Depends(oauth2_scheme),
    db: Database = Depends(get_db),
):
    """Serve a love note image to its recipient (once approved), its sender, or an admin."""
    return _serve_asset("love-notes", path, t, bearer_token, db)



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
        host="0.0.0.0",
        port=8001,
        reload=True,
        limit_concurrency=settings.MAX_CONCURRENCY,
        h11_max_incomplete_event_size=16 * 1024,
    )
