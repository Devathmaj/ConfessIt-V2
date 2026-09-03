# app/main.py

import asyncio
import os
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, messages
from .services.auth_service import get_current_user
from .dependencies import get_db 
from .postgres import Database, PostgresClient
from .services.storage_service import storage_service
import uvicorn
from .config import settings
from .models import UserDetails
from .logger import get_logger
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Initialize logger
logger = get_logger(__name__)

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
    from .postgres import PostgresClient
    db = PostgresClient()[settings.DATABASE_NAME]
    for attempt in range(30):
        try:
            db.command("ping")
            break
        except Exception:
            if attempt == 29:
                raise
            await asyncio.sleep(2)

    users = db["UserDetails"]
    seeds = [
        UserDetails(Regno="user", Name="Test User", email="user@test.com", bio="This is a test user.", which_class="Test_Class", profile_picture_id="random_string_1", gender="male", interests=["coding", "reading"], isMatchmaking=True, isNotifications=True, isLovenotesRecieve=True, isLovenotesSend=False, user_role="user"),
        UserDetails(Regno="admin", Name="Test Admin", email="admin@test.com", bio="This is a test admin.", which_class="Test_Class", profile_picture_id="random_string_2", gender="male", interests=["management", "monitoring"], isMatchmaking=True, isNotifications=True, isLovenotesRecieve=True, isLovenotesSend=False, user_role="admin"),
    ]
    for user in seeds:
        if not users.find_one({"Regno": user.Regno}):
            users.insert_one(user.model_dump(by_alias=True))
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
@app.get("/api/profile/files/{path:path}")
async def get_profile_file(path: str):
    """Serve profile files with signed URLs."""
    try:
        signed_url = storage_service.generate_temporary_view_url("profile", path)
        return RedirectResponse(url=signed_url)
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found")

@app.get("/api/events/files/{path:path}")
async def get_event_file(path: str):
    """Serve event files with signed URLs."""
    try:
        signed_url = storage_service.generate_temporary_view_url("events", path)
        return RedirectResponse(url=signed_url)
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found")

@app.get("/api/gallery/files/{path:path}")
async def get_gallery_file(path: str):
    """Serve gallery files with signed URLs."""
    try:
        signed_url = storage_service.generate_temporary_view_url("gallery", path)
        return RedirectResponse(url=signed_url)
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found")

@app.get("/api/committee/files/{path:path}")
async def get_committee_file(path: str):
    """Serve committee files with signed URLs."""
    try:
        signed_url = storage_service.generate_temporary_view_url("committee", path)
        return RedirectResponse(url=signed_url)
    except Exception as e:
        raise HTTPException(status_code=404, detail="File not found")



# ----------------------
# WebSocket Endpoints


# ----------------------
# Endpoints
# ----------------------
@app.get("/", tags=["Root"])
async def read_root():
    logger.info("Root endpoint accessed")
    return {"message": "Welcome to the ConfessIt API!"}

@app.get("/db-test")
def db_test(db: Database = Depends(get_db)):
    """Used to test the database connection."""
    try:
        db.command("ping")
        logger.info("Database connection test successful")
        return {"status": "success", "message": "Successfully connected to the database"}
    except Exception as e:
        logger.error(f"Failed to connect to the database: {e}")
        return {"status": "error", "message": f"Failed to connect to the database: {e}"}

# ----------------------
# Main entrypoint
# ----------------------
if __name__ == "__main__":
    # To run this app correctly, use the command:
    # uvicorn app.main:app --reload --host 0.0.0.0 --port 8001
    # This should be run from the parent directory containing the 'app' package.
    uvicorn.run("app.main:app", host="0.0.0.0", port=8001, reload=True)
