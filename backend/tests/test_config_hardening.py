"""Settings fail fast on typos and insecure values (see app.config).

With ``extra="allow"`` a misspelled ``SECRETKEY`` or ``DATABSE_URL`` would be
silently ignored while the deployment ran on defaults, and a ``CORS_ORIGINS``
of ``*`` or a negative ``MAX_*`` would only surface as misbehaviour. These
tests pin the fail-closed contract: unknown keys, wrong-scheme URLs, wildcard
or schemeless origins, and non-positive ceilings all refuse to construct.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings

# A well-formed 64-hex-character key, distinct from the suite's own pinned key.
VALID_KEY = "b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1"

# Everything required, all valid, so each test fails (or passes) on the one
# value it varies -- never on an unrelated missing setting.
_BASE_KWARGS = {
    "SECRET_KEY": VALID_KEY,
    "TOKEN_HASH_PEPPER": VALID_KEY,
    "DATABASE_URL": "postgresql+psycopg://u:p@db:5432/ConfessIt",
    "TEST_DATABASE_URL": "postgresql+psycopg://u:p@db:5432/ConfessIt_Test",
    "REDIS_URL": "redis://db:6379",
}


def _settings(monkeypatch, tmp_path, **overrides):
    """Build Settings isolated from the real environment and .env file."""
    for name in (
        "SECRET_KEY", "TOKEN_HASH_PEPPER", "DATABASE_URL", "TEST_DATABASE_URL",
        "REDIS_URL", "CORS_ORIGINS", "MAX_USER_COUNT", "MAX_ADMIN_COUNT",
        "MAX_MODERATOR_COUNT", "MAX_IMAGE_SIZE_MB", "MAX_IMAGE_DIMENSION",
        "MAX_IMAGE_PIXELS", "MAX_REQUEST_BODY_BYTES", "MAX_CONCURRENCY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    kwargs = dict(_BASE_KWARGS)
    kwargs.update(overrides)
    return Settings(**kwargs)


def test_misspelled_and_unknown_keys_are_refused(monkeypatch, tmp_path):
    # The typo from the finding: SECRETKEY must not be silently ignored.
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, SECRETKEY="whatever-this-is-ignored")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, DATABSE_URL="postgresql+psycopg://u:p@db:5432/x")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGNS="https://app.example.com")


def test_database_urls_require_the_psycopg_driver(monkeypatch, tmp_path):
    for field in ("DATABASE_URL", "TEST_DATABASE_URL"):
        with pytest.raises(ValidationError):
            _settings(monkeypatch, tmp_path, **{field: "postgresql://u:p@db:5432/x"})
        with pytest.raises(ValidationError):
            _settings(monkeypatch, tmp_path, **{field: "sqlite:///x.db"})
        with pytest.raises(ValidationError):
            _settings(monkeypatch, tmp_path, **{field: "   "})


def test_redis_url_requires_a_redis_scheme(monkeypatch, tmp_path):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, REDIS_URL="http://db:6379")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, REDIS_URL="")
    assert _settings(monkeypatch, tmp_path, REDIS_URL="redis://db:6379").REDIS_URL.startswith(
        "redis://"
    )
    assert _settings(monkeypatch, tmp_path, REDIS_URL="rediss://db:6379").REDIS_URL.startswith(
        "rediss://"
    )


def test_cors_origins_reject_wildcards_and_schemeless_entries(monkeypatch, tmp_path):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGINS="*")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGINS="https://app.example.com,*")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGINS="app.example.com")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGINS="ftp://app.example.com")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, CORS_ORIGINS="   ")
    parsed = _settings(
        monkeypatch, tmp_path, CORS_ORIGINS="https://app.example.com/,http://localhost:5173"
    )
    assert parsed.CORS_ORIGINS == "https://app.example.com/,http://localhost:5173"


@pytest.mark.parametrize("field", ["MAX_USER_COUNT", "MAX_ADMIN_COUNT", "MAX_MODERATOR_COUNT"])
def test_account_caps_reject_negatives_but_allow_zero(monkeypatch, tmp_path, field):
    # Zero is a real configuration ("this kind cannot be created"); negatives are not.
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, **{field: -1})
    assert getattr(_settings(monkeypatch, tmp_path, **{field: 0}), field) == 0


@pytest.mark.parametrize(
    "field",
    ["MAX_IMAGE_SIZE_MB", "MAX_IMAGE_DIMENSION", "MAX_IMAGE_PIXELS", "MAX_REQUEST_BODY_BYTES"],
)
def test_optional_ceilings_must_be_positive_when_set(monkeypatch, tmp_path, field):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, **{field: 0})
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, **{field: -5})
    assert getattr(_settings(monkeypatch, tmp_path, **{field: None}), field) is None


def test_concurrency_ceiling_must_be_positive(monkeypatch, tmp_path):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, tmp_path, MAX_CONCURRENCY=0)
    assert _settings(monkeypatch, tmp_path, MAX_CONCURRENCY=1).MAX_CONCURRENCY == 1

def test_cloud_transport_gate_ignores_local_runs(monkeypatch):
    """Plaintext transports stay legal off CLOUD: local dev is the posture they describe."""
    from app.config import settings, validate_transport_security

    monkeypatch.setattr(settings, "CLOUD", False)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ConfessIt")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://db:6379")
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_DATA_TRANSPORT", False)
    validate_transport_security()  # must not raise


def test_cloud_refuses_cleartext_data_transport(monkeypatch):
    """SEC-004: a cloud boot on redis:// + bare postgres refuses to start."""
    from app.config import settings, validate_transport_security

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ConfessIt")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://:pw@db:6379")
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_DATA_TRANSPORT", False)
    with pytest.raises(RuntimeError):
        validate_transport_security()


@pytest.mark.parametrize("sslmode", ["require", "verify-ca", "verify-full"])
def test_cloud_accepts_encrypted_data_transport(monkeypatch, sslmode):
    """SEC-004: rediss:// + an enforcing sslmode boots clean."""
    from app.config import settings, validate_transport_security

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"postgresql+psycopg://u:p@db:5432/ConfessIt?sslmode={sslmode}"
    )
    monkeypatch.setattr(settings, "REDIS_URL", "rediss://:pw@db:6379")
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_DATA_TRANSPORT", False)
    validate_transport_security()  # must not raise


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer"])
def test_cloud_refuses_downgradable_sslmodes(monkeypatch, sslmode):
    """SEC-004: allow/prefer silently cleartext against a lax server -- refused like absent."""
    from app.config import settings, validate_transport_security

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(
        settings, "DATABASE_URL", f"postgresql+psycopg://u:p@db:5432/ConfessIt?sslmode={sslmode}"
    )
    monkeypatch.setattr(settings, "REDIS_URL", "rediss://:pw@db:6379")
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_DATA_TRANSPORT", False)
    with pytest.raises(RuntimeError):
        validate_transport_security()


def test_cloud_plaintext_acknowledgment_warns_instead_of_booting(monkeypatch, caplog):
    """SEC-004: the single-host opt-out is a loud warning, never silent."""
    import logging

    from app.config import settings, validate_transport_security

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+psycopg://u:p@db:5432/ConfessIt")
    monkeypatch.setattr(settings, "REDIS_URL", "redis://:pw@db:6379")
    monkeypatch.setattr(settings, "ALLOW_PLAINTEXT_DATA_TRANSPORT", True)
    with caplog.at_level(logging.WARNING, logger="app.config"):
        validate_transport_security()  # must not raise
    assert "plaintext" in caplog.text.lower()

def test_cloud_cookie_gate_ignores_local_runs(monkeypatch):
    """SEC-005: plain HTTP on loopback keeps working with no knobs set."""
    from app.config import settings, validate_cookie_transport

    monkeypatch.setattr(settings, "CLOUD", False)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOSTS", "")
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    validate_cookie_transport()  # must not raise


def test_cloud_without_proxy_trust_or_override_refuses_to_boot(monkeypatch):
    """SEC-005: behind a TLS proxy with nothing configured, Secure silently drops."""
    from app.config import settings, validate_cookie_transport

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOSTS", "")
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    with pytest.raises(RuntimeError):
        validate_cookie_transport()


def test_cloud_accepts_either_resolution(monkeypatch):
    """SEC-005: a Secure override or trusted proxies each restore the flag."""
    from app.config import settings, validate_cookie_transport

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOSTS", "")
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    validate_cookie_transport()  # must not raise

    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOSTS", "10.0.0.0/8")
    monkeypatch.setattr(settings, "COOKIE_SECURE", None)
    validate_cookie_transport()  # must not raise


def test_cloud_refuses_an_explicit_insecure_override(monkeypatch):
    """SEC-005: COOKIE_SECURE=false describes nothing a public deployment serves."""
    from app.config import settings, validate_cookie_transport

    monkeypatch.setattr(settings, "CLOUD", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOSTS", "10.0.0.0/8")
    monkeypatch.setattr(settings, "COOKIE_SECURE", False)
    with pytest.raises(RuntimeError):
        validate_cookie_transport()
