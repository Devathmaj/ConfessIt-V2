import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.dependencies import get_db
from app.services.auth_service import get_current_user
from app.postgres import PostgresClient
from app.config import settings

# Ensure test cleanup can never target the application's configured database.
settings.DATABASE_URL = settings.TEST_DATABASE_URL
test_client = PostgresClient()
test_client.initialize()
test_db = test_client[settings.DATABASE_NAME]

def override_get_db():
    try:
        yield test_db
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
    return test_db

@pytest.fixture(autouse=True)
def clear_db():
    test_db.clear_all()
    yield
