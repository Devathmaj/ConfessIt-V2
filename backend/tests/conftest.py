import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.db_models import Base
from app.postgres import get_postgres_client

# Point the app at the test database before anything builds the shared engine, so tests
# exercise the real dependency wiring (including the confession router's provider) against
# an isolated database. clear_db truncates, so refuse to run if that is not a separate
# database from the one the app is configured to use.
_configured_database_url = settings.DATABASE_URL
if not settings.TEST_DATABASE_URL:
    raise RuntimeError("TEST_DATABASE_URL must be set to run the test suite")
if settings.TEST_DATABASE_URL == _configured_database_url:
    raise RuntimeError(
        "TEST_DATABASE_URL is identical to DATABASE_URL; refusing to run a suite that "
        "truncates the application's own database"
    )
settings.DATABASE_URL = settings.TEST_DATABASE_URL

# Keep the suite on a small pool so a leak inside the tests exhausts it quickly and
# visibly, instead of quietly ratcheting up against the server's connection ceiling.
settings.DATABASE_POOL_SIZE = 2
settings.DATABASE_MAX_OVERFLOW = 2

from app.main import app
from app.services.auth_service import get_current_user

test_db = get_postgres_client()
test_db.initialize()


def override_get_current_user():
    return {
        "regno": "testuser",
        "name": "Test User",
        "email": "test@user.com",
        "role": "user"
    }

def override_get_admin_user():
    return {
        "regno": "adminuser",
        "name": "Admin User",
        "email": "admin@user.com",
        "role": "admin"
    }

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
    """Empty every application table so tests cannot leak state into each other."""
    with test_db.engine.begin() as connection:
        tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
        connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    yield
