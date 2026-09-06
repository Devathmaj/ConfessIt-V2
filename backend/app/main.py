# app/main.py

import asyncio
import os
import secrets
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from sqlalchemy import select, text, update
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, messages
from .services.auth_service import get_current_user
from .dependencies import get_db 
from .postgres import Database, PostgresClient
from .services.storage_service import storage_service
from .models import UserDetails
import uvicorn
from .db_models import UserRecord
from .logger import get_logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Initialize logger
logger = get_logger(__name__)

# Diagnostic endpoints are opt-in so raw database/driver internals are never reachable
# from an unauthenticated request in a deployed environment.
ENABLE_DEBUG_ENDPOINTS = os.getenv("ENABLE_DEBUG_ENDPOINTS", "false").lower() in {"1", "true", "yes"}


def _init_tracing():
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    service_name = os.getenv("OTEL_SERVICE_NAME", "confessit-backend")
    resource = Resource.create({"service.name": service_name})

    provider = TracerProvider(resource=resource)
    trace.set_tracer_provider(provider)

    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))

_init_tracing()

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
    postgres = PostgresClient()
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
         "love_notes_send_enabled": False, "role": "user"},
        {"regno": "admin", "name": "Test Admin", "email": "admin@test.com", "bio": "This is a test admin.",
         "class_name": "Test_Class", "profile_picture_url": None, "gender": "male", "interests": [],
         "matchmaking_enabled": True, "notifications_enabled": True, "love_notes_receive_enabled": True,
         "love_notes_send_enabled": False, "role": "admin"},
    ]
    with postgres.sessions.begin() as session:
        session.execute(update(UserRecord).where(
            UserRecord.profile_picture_url.in_(["random_string_1", "random_string_2"])
        ).values(profile_picture_url=None))
        for seed in seeds:
            if not session.scalar(select(UserRecord.id).where(UserRecord.regno == seed["regno"])):
                session.add(UserRecord(**seed))
    logger.info("PostgreSQL storage initialized.")
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

FastAPIInstrumentor.instrument_app(app)

# ----------------------
# File Serving Routes
# ----------------------
def _paths_match(stored_path: str, object_path: str) -> bool:
    """Compare two object paths without leaking their contents through timing.

    ``compare_digest`` rejects non-ASCII input, and legacy rows may hold arbitrary text,
    so fall back to a plain comparison in that case.
    """
    try:
        return secrets.compare_digest(stored_path, object_path)
    except TypeError:
        return stored_path == object_path


def _require_owned_profile_asset(db: Database, current_user: UserDetails, object_path: str) -> None:
    """Reject any profile object that is not the caller's own currently stored asset.

    The stored value is read straight from the database rather than from the token so a
    stale or forged claim cannot widen access.
    """
    with db.sessions() as session:
        stored_url = session.scalar(
            select(UserRecord.profile_picture_url).where(UserRecord.regno == current_user.regno)
        )

    stored_path = storage_service.normalize_stored_path(stored_url, "profile")
    if not stored_path or not _paths_match(stored_path, object_path):
        raise HTTPException(status_code=404, detail="File not found")


def _redirect_to_delivery_url(route_key: str, object_path: str) -> RedirectResponse:
    """Redirect to the delivery URL for a validated object.

    Anything other than a definitive "you asked for something malformed" is reported as a
    plain 404 so the endpoint cannot be used to probe which object IDs exist.
    """
    try:
        delivery_url = storage_service.generate_temporary_view_url(route_key, object_path)
    except HTTPException as exc:
        if exc.status_code == 400:
            raise
        logger.warning("Delivery URL unavailable for %s asset: %s", route_key, exc.detail)
        raise HTTPException(status_code=404, detail="File not found")
    return RedirectResponse(url=delivery_url)


@app.get("/api/profile/files/{path:path}")
async def get_profile_file(
    path: str,
    current_user: UserDetails = Depends(get_current_user),
    db: Database = Depends(get_db),
):
    """Serve the caller's own profile file."""
    object_path = storage_service.validate_object_path(path)
    _require_owned_profile_asset(db, current_user, object_path)
    return _redirect_to_delivery_url("profile", object_path)


@app.get("/api/events/files/{path:path}")
async def get_event_file(
    path: str,
    current_user: UserDetails = Depends(get_current_user),
):
    """Serve event files to authenticated users."""
    object_path = storage_service.validate_object_path(path)
    return _redirect_to_delivery_url("events", object_path)


@app.get("/api/gallery/files/{path:path}")
async def get_gallery_file(
    path: str,
    current_user: UserDetails = Depends(get_current_user),
):
    """Serve gallery files to authenticated users."""
    object_path = storage_service.validate_object_path(path)
    return _redirect_to_delivery_url("gallery", object_path)


@app.get("/api/committee/files/{path:path}")
async def get_committee_file(
    path: str,
    current_user: UserDetails = Depends(get_current_user),
):
    """Serve committee files to authenticated users."""
    object_path = storage_service.validate_object_path(path)
    return _redirect_to_delivery_url("committee", object_path)



# ----------------------
# WebSocket Endpoints


# ----------------------
# Endpoints
# ----------------------
@app.get("/", tags=["Root"])
async def read_root():
    logger.info("Root endpoint accessed")
    return {"message": "Welcome to the ConfessIt API!"}

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
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
