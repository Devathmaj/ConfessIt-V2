# app/main.py

import asyncio
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from .routers import auth, profile, matchmaking, confessions, love_notes, conversations, notifications, admin, admin_love_notes, messages
from .services.auth_service import get_current_user
from .dependencies import get_db 
from pymongo.database import Database
from .services.storage_service import storage_service
import uvicorn
import pymongo
from .config import settings
from .models import UserDetails
from .logger import get_logger

# Initialize logger
logger = get_logger(__name__)

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
    """
    Used to run the initial database setup on application start.
    """
    logger.info("Running initial database setup...")
    client = None
    try:
        # Wait / retry for MongoDB to become ready. Docker may start the backend
        # before Mongo has finished initializing, so poll with a short timeout.
        max_wait = 60  # seconds total to wait for Mongo
        waited = 0
        interval = 2  # seconds between retries
        while waited < max_wait:
            try:
                # Use a short serverSelectionTimeout so the ping fails fast
                client = pymongo.MongoClient(settings.MONGO_URI, serverSelectionTimeoutMS=2000)
                # Force a round-trip to ensure the server is available
                client.admin.command("ping")
                logger.info("Connected to MongoDB")
                break
            except Exception as e:
                logger.warning(f"MongoDB not ready yet (waited={waited}s): {e}")
                await asyncio.sleep(interval)
                waited += interval

        if not client:
            raise pymongo.errors.ConnectionFailure(f"Could not connect to MongoDB after {max_wait} seconds")

        db = client[settings.DATABASE_NAME]
        user_details_collection = db["UserDetails"]

        users_to_create = [
            UserDetails(
                Regno="user",
                Name="Test User",
                email="user@test.com",
                emoji=None,
                bio="This is a test user.",
                which_class="Test_Class",
                profile_picture_id="random_string_1",
                gender="male",
                interests=["coding", "reading"],
                isMatchmaking=True,
                isNotifications=True,
                isLovenotesRecieve=True,
                isLovenotesSend=False,
                reported_count=0,
                last_login_time=None,
                last_login_ip=None,
                user_role="user",
                last_matchmaking_time=None
            ),
            UserDetails(
                Regno="admin",
                Name="Test Admin",
                email="admin@test.com",
                emoji=None,
                bio="This is a test admin.",
                which_class="Test_Class",
                profile_picture_id="random_string_2",
                gender="male",
                interests=["management", "monitoring"],
                isMatchmaking=True,
                isNotifications=True,
                isLovenotesRecieve=True,
                isLovenotesSend=False,
                reported_count=0,
                last_login_time=None,
                last_login_ip=None,
                user_role="admin",
                last_matchmaking_time=None
            ),
        ]

        for user_data in users_to_create:
            existing_user = user_details_collection.find_one({"Regno": user_data.Regno})
            if not existing_user:
                user_details_collection.insert_one(user_data.dict(by_alias=True))
                logger.info(f"Inserted user: {user_data.Name}")
            else:
                logger.info(f"User with Regno '{user_data.Regno}' already exists. Skipping.")

        # Create database indexes for performance optimization
        logger.info("Creating database indexes...")
        try:
            # Confessions indexes
            db["Confessions"].create_index([("timestamp", pymongo.DESCENDING)], name="idx_timestamp_desc")
            db["Confessions"].create_index([("user_id", pymongo.ASCENDING)], name="idx_user_id")
            db["Confessions"].create_index([("is_approved", pymongo.ASCENDING)], name="idx_is_approved")
            
            # UserDetails indexes
            db["UserDetails"].create_index([("Regno", pymongo.ASCENDING)], unique=True, name="idx_regno_unique")
            db["UserDetails"].create_index([("email", pymongo.ASCENDING)], name="idx_email")
            db["UserDetails"].create_index([("user_role", pymongo.ASCENDING)], name="idx_user_role")
            
            # LoginTokens indexes
            db["LoginTokens"].create_index([("expires_at", pymongo.ASCENDING)], name="idx_expires_at")
            db["LoginTokens"].create_index([("consumed_at", pymongo.ASCENDING)], name="idx_consumed_at")
            db["LoginTokens"].create_index([("user_regno", pymongo.ASCENDING)], name="idx_user_regno")
            
            # Matches indexes
            db["matches"].create_index([
                ("user_1_regno", pymongo.ASCENDING),
                ("user_2_regno", pymongo.ASCENDING)
            ], name="idx_user_pair")
            db["matches"].create_index([("expires_at", pymongo.ASCENDING)], name="idx_match_expires")
            db["matches"].create_index([("created_at", pymongo.DESCENDING)], name="idx_match_created")
            
            # Notifications indexes
            db["notifications"].create_index([
                ("user_id", pymongo.ASCENDING),
                ("timestamp", pymongo.DESCENDING)
            ], name="idx_user_notifications")
            db["notifications"].create_index([("read", pymongo.ASCENDING)], name="idx_read_status")
            
            # Conversations indexes
            db["conversations"].create_index([("match_id", pymongo.ASCENDING)], name="idx_match_id")
            db["conversations"].create_index([
                ("participants", pymongo.ASCENDING),
                ("last_message_at", pymongo.DESCENDING)
            ], name="idx_participants_last_message")
            
            # LoveNotes indexes
            db["LoveNotes"].create_index([("recipient_id", pymongo.ASCENDING)], name="idx_recipient")
            db["LoveNotes"].create_index([("sender_id", pymongo.ASCENDING)], name="idx_sender")
            db["LoveNotes"].create_index([("status", pymongo.ASCENDING)], name="idx_status")
            db["LoveNotes"].create_index([("created_at", pymongo.DESCENDING)], name="idx_lovenote_created")
            
            # ConfessionComments indexes
            db["ConfessionComments"].create_index([("confession_id", pymongo.ASCENDING)], name="idx_confession_id")
            db["ConfessionComments"].create_index([("timestamp", pymongo.DESCENDING)], name="idx_comment_timestamp")
            
            # Reports indexes
            db["Reports"].create_index([("content_id", pymongo.ASCENDING)], name="idx_content_id")
            db["Reports"].create_index([("reporter_id", pymongo.ASCENDING)], name="idx_reporter_id")
            db["Reports"].create_index([("status", pymongo.ASCENDING)], name="idx_report_status")
            
            # Messages indexes (new)
            db["messages"].create_index([
                ("conversation_id", pymongo.ASCENDING),
                ("timestamp", pymongo.ASCENDING)
            ], name="idx_conversation_messages")
            db["messages"].create_index([("timestamp", pymongo.ASCENDING)], name="idx_message_timestamp")
            db["messages"].create_index([("sender_id", pymongo.ASCENDING)], name="idx_sender_id")
            db["messages"].create_index([("receiver_id", pymongo.ASCENDING)], name="idx_receiver_id")
            db["messages"].create_index([("read", pymongo.ASCENDING)], name="idx_read_status")
            
            # Message reports indexes (new)
            db["message_reports"].create_index([("message_id", pymongo.ASCENDING)], name="idx_message_id")
            db["message_reports"].create_index([
                ("message_id", pymongo.ASCENDING),
                ("reporter_id", pymongo.ASCENDING)
            ], unique=True, name="idx_unique_message_report")
            db["message_reports"].create_index([("reporter_id", pymongo.ASCENDING)], name="idx_message_reporter")
            db["message_reports"].create_index([("status", pymongo.ASCENDING)], name="idx_message_report_status")
            
            logger.info("Database indexes created successfully.")
        except Exception as e:
            logger.warning(f"Error creating indexes (may already exist): {e}")
        
        logger.info("Initial database setup complete.")
    except pymongo.errors.ConnectionFailure as e:
        logger.error(f"Could not connect to MongoDB during startup setup: {e}")
    except Exception as e:
        logger.error(f"An error occurred during startup setup: {e}")
    finally:
        if client:
            client.close()

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
