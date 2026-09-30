"""Auth logs must identify nobody.

The auth warnings ship to OTLP/Loki, which retains them for the full retention
period. A raw regno, client id or IP on every throttled or failed login would
turn the log store into a browsable record of who signs in from where -- so
these tests pin the de-identified contract:

* regnos and client ids log as stable 8-hex-char HMAC-style tags (same actor
  correlates across lines, nothing reverses without the deployment secret);
* IPs log as their /24 (IPv4) or /64 (IPv6) network, never the host address;
* token-hash hints are DEBUG-only and 6 chars, never in a retained warning.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import auth_service as auth_service_module
from app.services.auth_service import (
    _log_identity_hash,
    _log_ip_network,
    _rate_limit_rejection,
)
from app.services.login_rate_limit import (
    SCOPE_ACCOUNT,
    SCOPE_ACCOUNT_LONG,
    SCOPE_CLIENT,
    SCOPE_IP,
    RateLimitVerdict,
)


def test_identity_hash_is_short_stable_and_distinguishing():
    first = _log_identity_hash("22/CS0455")
    assert re.fullmatch(r"[0-9a-f]{8}", first)
    assert _log_identity_hash("22/CS0455") == first
    assert _log_identity_hash("22/CS0456") != first
    assert _log_identity_hash("some-client-id") != first


def test_ip_logging_truncates_host_bits():
    assert _log_ip_network("192.168.7.23") == "192.168.7.0/24"
    assert _log_ip_network("10.0.0.1") == "10.0.0.0/24"
    assert _log_ip_network("2001:db8::dead:beef") == "2001:db8::/64"
    assert _log_ip_network("unknown") == "unparseable"
    assert _log_ip_network("") == "unparseable"
    assert _log_ip_network(None) == "unparseable"


@pytest.fixture
def captured_warnings(monkeypatch):
    lines = []
    monkeypatch.setattr(
        auth_service_module.logger,
        "warning",
        lambda message, *args, **kwargs: lines.append(message),
    )
    return lines


@pytest.mark.parametrize("scope", [SCOPE_CLIENT, SCOPE_IP, SCOPE_ACCOUNT, SCOPE_ACCOUNT_LONG])
def test_rate_limit_warnings_carry_hashes_not_identifiers(captured_warnings, scope):
    verdict = RateLimitVerdict(allowed=False, scope=scope, retry_after=42)
    exc = _rate_limit_rejection(
        verdict,
        account_type="users",
        regno="22/CS0455",
        client_id="client-abc-123",
        ip="192.168.7.23",
    )
    assert exc.status_code == 429
    assert len(captured_warnings) == 1
    line = captured_warnings[0]
    assert "22/CS0455" not in line
    assert "client-abc-123" not in line
    assert "192.168.7.23" not in line
    if scope in (SCOPE_CLIENT, SCOPE_IP):
        assert "192.168.7.0/24" in line
        assert _log_identity_hash("client-abc-123") in line
    else:
        assert _log_identity_hash("22/CS0455") in line


def test_invalid_token_warning_has_no_ip_and_hint_is_debug_only(monkeypatch):
    warnings, debugs = [], []
    monkeypatch.setattr(
        auth_service_module.logger, "warning",
        lambda message, *args, **kwargs: warnings.append(message),
    )
    monkeypatch.setattr(
        auth_service_module.logger, "debug",
        lambda message, *args, **kwargs: debugs.append(message),
    )
    response = TestClient(app).get("/auth/login/magic/verify?token=not-a-real-token")

    assert response.status_code == 400
    assert "Invalid magic link token: no matching token" in warnings
    assert not any("hash_hint" in line for line in warnings)
    # The default test client speaks as "testserver": the warning must not.
    assert not any("testserver" in line for line in warnings)
    assert any(
        re.fullmatch(
            r"Invalid magic link token: no matching token \(hash_hint=[0-9a-f]{6}\)",
            line,
        )
        for line in debugs
    )
