# app/main.py

import asyncio
import os
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from sqlalchemy import select, text, update
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, messages
from .services.auth_service import oauth2_scheme, verify_token_service, load_active_user_service
from .dependencies import get_db 
from .postgres import Database, close_postgres_client, get_postgres_client
from .services.storage_service import storage_service
from .services import asset_delivery_service
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


@app.on_event("shutdown")
async def on_shutdown():
    # Hand the pool's connections back so a reload or restart does not leak them until
    # Postgres itself times them out.
    close_postgres_client()

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
