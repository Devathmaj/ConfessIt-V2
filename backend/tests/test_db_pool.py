"""Guards the single-engine invariant.

Every PostgresClient owns a connection pool, so a client created per request multiplies
the connection count by the request rate and exhausts Postgres' max_connections. These
tests fail if a second engine can appear anywhere in the request path.
"""
import inspect

from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import postgres as postgres_module
from app.dependencies import get_db
from app.main import app
from app.postgres import PostgresClient, close_postgres_client, get_postgres_client
from app.routers import confessions
from app.services.confession_service import ConfessionService


def test_get_postgres_client_is_a_singleton():
    assert get_postgres_client() is get_postgres_client()


def test_get_db_is_not_a_generator():
    # A generator dependency would be torn down per request; the shared pool must
    # outlive any single request.
    assert not inspect.isgeneratorfunction(get_db)
    assert get_db() is get_postgres_client()


def test_confession_service_never_builds_its_own_engine():
    # Two services must land on the shared client; two engines would mean two pools.
    first, second = ConfessionService(), ConfessionService()
    assert first.client is second.client
    assert first.client is get_postgres_client()
    assert first.sessions.kw["bind"] is get_postgres_client().engine


def test_confession_service_accepts_an_injected_client():
    injected = PostgresClient(pool_size=1, max_overflow=0)
    try:
        service = ConfessionService(injected)
        assert service.client is injected
        assert service.sessions.kw["bind"] is injected.engine
    finally:
        injected.close()


def test_confession_router_provider_reuses_the_request_client():
    client = get_postgres_client()
    assert confessions.get_confession_service(client).client is client


def test_no_engine_is_created_while_serving_requests(monkeypatch):
    """The regression that mattered: N requests must not mean N engines."""
    created = []
    real_create_engine = create_engine

    def counting_create_engine(*args, **kwargs):
        created.append(kwargs.get("url", args[0] if args else None))
        return real_create_engine(*args, **kwargs)

    # Patch the name the app module uses, so any engine built during a request is seen.
    monkeypatch.setattr(postgres_module, "create_engine", counting_create_engine)
    # The shared client already exists (built by conftest), so a correct request path
    # builds nothing. The pre-fix code built a fresh PostgresClient per ConfessionService,
    # and so would have recorded one engine per request here.
    with TestClient(app) as test_client:
        for _ in range(3):
            test_client.get("/confessions/")
            test_client.get("/confessions/total_count")

    assert created == [], f"serving requests created extra engines: {created}"


def test_close_postgres_client_disposes_and_clears(monkeypatch):
    # Isolated from the process-wide client so this cannot disturb the rest of the suite.
    throwaway = PostgresClient(pool_size=1, max_overflow=0)
    monkeypatch.setattr(postgres_module, "_client", throwaway)
    try:
        close_postgres_client()
        assert postgres_module._client is None
        assert throwaway.engine.pool.checkedin() == 0
    finally:
        throwaway.close()
        monkeypatch.setattr(postgres_module, "_client", get_postgres_client())


def test_connection_budget_is_within_postgres_limit():
    from app.config import settings

    budget = (settings.DATABASE_POOL_SIZE + settings.DATABASE_MAX_OVERFLOW) * max(
        settings.WEB_WORKERS, 1
    )
    assert budget <= 100, f"connection budget {budget} exceeds Postgres max_connections"
