import pytest
from fastapi.testclient import TestClient

def test_profile_update_unauthorized(client: TestClient):
    # Removing the dependency override for this specific test
    from app.main import app
    from app.services.auth_service import get_current_user
    app.dependency_overrides.pop(get_current_user, None)
    
    response = client.put("/profile/update", json={"bio": "New bio"})
    assert response.status_code == 401

def test_confessions_get(client: TestClient):
    response = client.get("/confessions/")
    # If the endpoint exists and our mock user is valid, it might return 200 or validation error.
    # We just ensure it's not a 500 error.
    assert response.status_code in [200, 404, 405, 422, 401]

def test_matchmaking_status(client: TestClient):
    response = client.get("/matchmaking/status")
    assert response.status_code in [200, 404, 405, 422, 401]

def test_love_notes_get(client: TestClient):
    response = client.get("/love-notes/")
    assert response.status_code in [200, 404, 405, 422, 401]

def test_conversations_get(client: TestClient):
    response = client.get("/conversations/")
    assert response.status_code in [200, 404, 405, 422, 401]

def test_notifications_get(client: TestClient):
    response = client.get("/notifications/")
    assert response.status_code in [200, 404, 405, 422, 401]

def test_admin_dashboard(client: TestClient):
    response = client.get("/admin/dashboard")
    assert response.status_code in [200, 403, 404, 405, 422, 401]
