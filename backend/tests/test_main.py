from fastapi.testclient import TestClient
from app.main import app

def test_read_root(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to the ConfessIt API!"}

def test_db_test(client: TestClient):
    response = client.get("/db-test")
    # The test fixture supplies a PostgreSQL-backed test database.
    # but the endpoint is at least reachable.
    assert response.status_code == 200
    assert "status" in response.json()
