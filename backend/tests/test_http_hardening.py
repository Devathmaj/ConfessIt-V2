"""Baseline HTTP hardening: security headers, host allowlist, flood brake, Redis TLS.

Three independent layers, each fail-closed where it can be and each pinned through
real requests rather than config inspection:

* every response carries the security baseline (and HSTS only over HTTPS -- emitting
  it on plain HTTP would be ignored at best);
* unknown Hosts are refused before routing (poisoned Host headers must not reach URL
  building, redirects or cache keys);
* one IP gets a generous per-minute budget across all paths, counted in Redis so
  workers share it, with a Retry-After on refusal and fail-open on a Redis outage
  (the brake must not become the outage);
* a ``rediss://`` URL enforces certificate verification instead of negotiating it.
"""

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.redis_service import RedisService


def test_responses_carry_the_security_baseline(client):
    response = client.get("/")

    assert response.status_code == 200, response.text
    headers = response.headers
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["permissions-policy"] == "camera=(), microphone=(), geolocation=()"
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-site"
    assert "default-src 'self'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    # Plain HTTP here: HSTS on a non-TLS response would be ignored by browsers and
    # risks being cached by something that should not have it.
    assert "strict-transport-security" not in headers


def test_hsts_is_emitted_over_https():
    tls_client = TestClient(app, base_url="https://testserver")
    response = tls_client.get("/")

    assert response.status_code == 200, response.text
    assert "max-age=31536000" in response.headers["strict-transport-security"]


def test_unknown_host_is_refused_before_routing(client):
    response = client.get("/", headers={"Host": "evil.example.com"})

    assert response.status_code == 400, response.text


def test_listed_host_is_served(client):
    response = client.get("/", headers={"Host": "localhost:8001"})

    assert response.status_code == 200, response.text


class _FakeStore:
    def __init__(self):
        self.counts = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, ttl):
        return True


def test_flood_brake_refuses_past_the_budget_with_retry_after(client, monkeypatch):
    monkeypatch.setattr(settings, "GLOBAL_RATE_LIMIT_PER_MINUTE", 2)
    store = _FakeStore()
    monkeypatch.setattr("app.main._limiter_redis", lambda: store, raising=True)

    assert client.get("/").status_code == 200
    assert client.get("/").status_code == 200
    refused = client.get("/")
    assert refused.status_code == 429, refused.text
    assert refused.json()["detail"]
    assert int(refused.headers["Retry-After"]) > 0


def test_flood_brake_fails_open_when_redis_is_down(client, monkeypatch):
    monkeypatch.setattr(settings, "GLOBAL_RATE_LIMIT_PER_MINUTE", 2)

    class _Down:
        def incr(self, key):
            raise ConnectionError("redis down")

    monkeypatch.setattr("app.main._limiter_redis", lambda: _Down(), raising=True)

    assert client.get("/").status_code == 200


def test_rediss_url_requires_certificate_verification(monkeypatch):
    seen = {}

    class _Dummy:
        pass

    def _capture(url, **kwargs):
        seen.update(kwargs)
        return _Dummy()

    monkeypatch.setattr(settings, "REDIS_URL", "rediss://:secret@redis:6379")
    monkeypatch.setattr(
        "app.services.redis_service.redis.from_url", _capture, raising=True
    )

    RedisService()._get_client()

    assert seen.get("ssl_cert_reqs") == "required"
    assert seen.get("ssl_check_hostname") is True


def test_plain_redis_url_adds_no_tls_options(monkeypatch):
    seen = {}

    class _Dummy:
        pass

    def _capture(url, **kwargs):
        seen.update(kwargs)
        return _Dummy()

    monkeypatch.setattr(settings, "REDIS_URL", "redis://redis:6379")
    monkeypatch.setattr(
        "app.services.redis_service.redis.from_url", _capture, raising=True
    )

    RedisService()._get_client()

    assert "ssl_cert_reqs" not in seen
