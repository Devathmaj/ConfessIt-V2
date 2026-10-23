"""The API's cache boundary.

Sensitive reads must never be stored; dynamic reads revalidate with an ETag so a
conditional request costs a 304; writes are no-store. These are policy tests on
a tiny app, independent of the database-backed suite.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.http_cache import HttpCacheMiddleware


def build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(HttpCacheMiddleware)
    calls: list[int] = []
    app.state.confession_calls = calls

    @app.get("/confessions")
    def confessions():
        calls.append(1)
        return {"rows": [{"id": "1", "body": "hello"}]}

    @app.get("/admin/reports")
    def reports():
        return {"rows": []}

    @app.get("/assets/app-123.js")
    def asset():
        return {"built": True}

    @app.post("/confessions")
    def create():
        return {"id": "2"}

    @app.get("/api/time")
    def server_time():
        return JSONResponse({"now": 1}, headers={"Cache-Control": "no-store"})

    return app


def test_dynamic_read_revalidates_with_an_etag():
    client = TestClient(build_app())

    first = client.get("/confessions")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, max-age=0, must-revalidate"
    etag = first.headers["etag"]
    assert etag.startswith('W/"')

    second = client.get("/confessions", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert second.content == b""
    assert second.headers["etag"] == etag


def test_conditional_304_still_executes_the_route():
    app = build_app()
    client = TestClient(app)

    first = client.get("/confessions")
    etag = first.headers["etag"]
    second = client.get("/confessions", headers={"If-None-Match": etag})

    assert second.status_code == 304
    # The ETag is computed after the route has run. A 304 saves response bytes,
    # not the database work behind the answer.
    assert app.state.confession_calls == [1, 1]


def test_sensitive_reads_are_no_store_and_never_etagged():
    client = TestClient(build_app())

    response = client.get("/admin/reports")
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_writes_are_no_store():
    client = TestClient(build_app())

    response = client.post("/confessions")
    assert response.headers["cache-control"] == "no-store"


def test_an_endpoint_no_store_is_preserved():
    client = TestClient(build_app())

    response = client.get("/api/time")
    assert response.headers["cache-control"] == "no-store"
    assert "etag" not in response.headers


def test_versioned_assets_are_immutable():
    client = TestClient(build_app())

    response = client.get("/assets/app-123.js")
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
