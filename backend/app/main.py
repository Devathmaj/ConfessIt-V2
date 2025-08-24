# app/main.py

from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from .routers import auth, profile, matchmaking, confessions, love_notes
from .dependencies import get_db
from pymongo.database import Database
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
# Static Files
# ----------------------
# The path is relative to this file's location
BASE_DIR = Path(__file__).resolve().parent
app.mount(
    "/profile_pictures",
    StaticFiles(directory=BASE_DIR / "profile_pictures"),
    name="profile_pictures"
)

# ----------------------
# Startup Event
# ----------------------
@app.on_event("startup")
def on_startup():
    """
    Used to run the initial database setup on application start.
    """
    logger.info("Running initial database setup...")
    client = None
    try:
        client = pymongo.MongoClient(settings.MONGO_URI)
        db = client[settings.MONGO_DB_NAME]
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
                user_role="user"
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
                user_role="admin"
            ),
        ]

        for user_data in users_to_create:
            existing_user = user_details_collection.find_one({"Regno": user_data.Regno})
            if not existing_user:
                user_details_collection.insert_one(user_data.dict(by_alias=True))
                logger.info(f"Inserted user: {user_data.Name}")
            else:
                logger.info(f"User with Regno '{user_data.Regno}' already exists. Skipping.")

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
