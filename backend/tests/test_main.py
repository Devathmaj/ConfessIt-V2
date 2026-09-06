from fastapi.testclient import TestClient
from app.main import app

def test_read_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to the ConfessIt API!"}

def test_db_test_is_disabled_by_default(client: TestClient):
    # /db-test is only registered when ENABLE_DEBUG_ENDPOINTS is set, so an unauthenticated
    # request must not reach a live database connection check in a deployed environment.
    response = client.get("/db-test")
    assert response.status_code == 404
