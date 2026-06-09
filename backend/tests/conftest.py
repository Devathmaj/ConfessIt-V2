import os
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.dependencies import get_db
from app.services.auth_service import get_current_user
import mongomock
import pymongo

# If running in CI, use the real MongoDB service, otherwise use mongomock
if os.environ.get("CI"):
    mock_client = pymongo.MongoClient(os.environ.get("MONGO_URI", "mongodb://localhost:27017/"))
    mock_db = mock_client["ConfessDB_Test"]
else:
    mock_client = mongomock.MongoClient()
    mock_db = mock_client.test_database

def override_get_db():
    try:
        yield mock_db
    finally:
        pass

def override_get_current_user():
    return {
        "Regno": "testuser",
        "Name": "Test User",
        "email": "test@user.com",
        "user_role": "user"
    }

def override_get_admin_user():
    return {
        "Regno": "adminuser",
        "Name": "Admin User",
        "email": "admin@user.com",
        "user_role": "admin"
    }

app.dependency_overrides[get_db] = override_get_db
app.dependency_overrides[get_current_user] = override_get_current_user

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c

@pytest.fixture(scope="module")
def db():
    return mock_db

@pytest.fixture(autouse=True)
def clear_db():
    # Clear all collections before each test
    for collection_name in mock_db.list_collection_names():
        mock_db[collection_name].delete_many({})
    yield
