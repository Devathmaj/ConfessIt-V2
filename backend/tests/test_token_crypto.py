"""Peppered, domain-separated token hashes and a fixed JWT algorithm.

Two properties pinned here. First, what is stored for a token is useless without the
deployment secret and bound to one column: the same raw value hashes differently
under the magic and refresh domains, deterministically within each, and the legacy
unpeppered SHA-256 matches neither -- so rotating the pepper (or upgrading from the
unpeppered scheme) invalidates every outstanding session and link at once, by
construction rather than by cleanup. Second, JWT verification speaks exactly HS256
with the identity claims required: ``none``, foreign-algorithm and claim-stripped
tokens are refused before any lookup, and every refusal is the generic 401 callers
already handle.
"""

import hashlib

import jwt
import pytest
from pydantic import ValidationError

from app.config import Settings, settings
from app.services.auth_service import (
    TOKEN_HASH_DOMAIN_MAGIC,
    TOKEN_HASH_DOMAIN_REFRESH,
    _account_from_bearer_token,
    create_access_token,
    hash_token,
)


def test_same_value_hashes_differently_per_domain():
    assert hash_token("raw-token", domain=TOKEN_HASH_DOMAIN_MAGIC) != hash_token(
        "raw-token", domain=TOKEN_HASH_DOMAIN_REFRESH
    )


def test_hash_is_deterministic_within_a_domain():
    assert hash_token("raw-token", domain=TOKEN_HASH_DOMAIN_MAGIC) == hash_token(
        "raw-token", domain=TOKEN_HASH_DOMAIN_MAGIC
    )


def test_legacy_unpeppered_hash_matches_neither_domain():
    """The pre-pepper scheme is dead on arrival: its outputs verify nowhere."""
    legacy = hashlib.sha256(b"raw-token").hexdigest()
    assert legacy != hash_token("raw-token", domain=TOKEN_HASH_DOMAIN_MAGIC)
    assert legacy != hash_token("raw-token", domain=TOKEN_HASH_DOMAIN_REFRESH)


def test_hash_requires_an_explicit_domain():
    with pytest.raises(TypeError):
        hash_token("raw-token")  # type: ignore[call-arg]


def test_missing_pepper_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.delenv("TOKEN_HASH_PEPPER", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        Settings(
            SECRET_KEY="a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
            DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt",
            TEST_DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt_Test",
            REDIS_URL="redis://db:6379",
        )


def test_weak_pepper_refuses_to_start(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        Settings(
            SECRET_KEY="a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
            TOKEN_HASH_PEPPER="testtesttesttesttesttesttesttest",
            DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt",
            TEST_DATABASE_URL="postgresql+psycopg://u:p@db:5432/ConfessIt_Test",
            REDIS_URL="redis://db:6379",
        )


def _unsigned_token(**claims):
    return jwt.encode(claims, None, algorithm="none")


def _valid_claims(**overrides):
    import time

    claims = {
        "sub": "someone",
        "account_id": "11111111-1111-1111-1111-111111111111",
        "account_type": "users",
        "session_id": "22222222-2222-2222-2222-222222222222",
        "exp": int(time.time()) + 600,
    }
    claims.update(overrides)
    return claims


def test_none_algorithm_token_is_refused_before_any_lookup(db):
    assert _account_from_bearer_token(_unsigned_token(**_valid_claims()), db) is None


def test_foreign_algorithm_token_is_refused(db):
    other = jwt.encode(_valid_claims(), settings.SECRET_KEY, algorithm="HS384")
    assert _account_from_bearer_token(other, db) is None


def test_token_missing_expiry_is_refused(db):
    claims = _valid_claims()
    del claims["exp"]
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
    assert _account_from_bearer_token(token, db) is None


def test_token_missing_identity_claims_is_refused(db):
    claims = _valid_claims()
    del claims["account_id"]
    token = jwt.encode(claims, settings.SECRET_KEY, algorithm="HS256")
    assert _account_from_bearer_token(token, db) is None


def test_well_formed_token_decodes_with_identity_intact():
    payload = jwt.decode(
        create_access_token({"sub": "someone"}),
        settings.SECRET_KEY,
        algorithms=["HS256"],
    )
    assert payload["sub"] == "someone"
    assert payload["exp"]
