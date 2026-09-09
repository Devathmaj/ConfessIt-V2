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
from app.models import UserDetails
from app.services.auth_service import get_current_user

test_db = get_postgres_client()
test_db.initialize()


def override_get_current_user():
    """A real UserDetails, not a dict.

    Endpoints are annotated with UserDetails and reach for attributes such as .regno and
    .id. A dict stub is only ever truthy enough for the dependency to resolve, so it lets
    a handler fail on an AttributeError instead of exercising the code it is supposed to.
    """
    return UserDetails(
        regno="testuser",
        name="Test User",
        email="test@user.com",
        class_name="Test Class",
        gender="other",
        matchmaking_enabled=True,
        notifications_enabled=True,
        role="user",
    )


def override_get_admin_user():
    """A real UserDetails carrying the admin role."""
    return override_get_current_user().model_copy(
        update={"regno": "adminuser", "name": "Admin User", "email": "admin@user.com", "role": "admin"}
    )

app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(autouse=True)
def authenticated_user():
    """Re-install the auth override before every test.

    A test that needs to check the unauthenticated path pops this override itself. That
    used to leak: once popped it stayed popped, so every test module collected afterwards
    ran with no auth at all and every authenticated endpoint answered 401. Re-applying it
    per test makes the authenticated default independent of collection order while still
    letting an individual test opt out.
    """
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield
    app.dependency_overrides[get_current_user] = override_get_current_user


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_client():
    """A client whose ``get_current_user`` resolves to an admin.

    ``admin_required`` depends on ``get_current_user``, so overriding that single
    dependency is enough to reach the admin routes.
    """
    app.dependency_overrides[get_current_user] = override_get_admin_user
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


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
