"""SECRET_KEY must be required, long and unguessable (see app.config).

A random-per-process default used to live in the settings: every worker, restart and
container minted its own key, so restarts invalidated all JWTs and multi-worker
deployments 401'd intermittently. These tests pin the fail-closed contract --
missing, short, placeholder or low-entropy keys refuse to start -- so no future edit
can silently reintroduce a weak default.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings, settings

# A well-formed 64-hex-character key, distinct from the suite's own pinned key.
VALID_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
# Exactly the 32-character minimum, with enough distinct characters to pass.
BOUNDARY_KEY = "a1b2c3d4e5f60718293a4b5c6d7e8f90"


def test_missing_secret_key_refuses_to_start(monkeypatch, tmp_path):
    # Both sources gone: nothing in the process environment and no .env file to read,
    # so the required field has nowhere to come from.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError):
        Settings()


def test_short_secret_key_is_refused():
    with pytest.raises(ValidationError):
        Settings(SECRET_KEY="short")
    with pytest.raises(ValidationError):
        Settings(SECRET_KEY="a" * 31)


@pytest.mark.parametrize(
    "weak",
    [
        "changeme-changeme-changeme-00000000",
        "test" * 8,
        "secret" * 6,
        "password" * 4,
        "a" * 64,
        "ab" * 32,
    ],
)
def test_placeholder_or_low_entropy_key_is_refused(weak):
    # Each is at least 32 characters, so length alone would accept it; the validator
    # must still refuse placeholder words and repeated patterns.
    assert len(weak) >= 32
    with pytest.raises(ValidationError):
        Settings(SECRET_KEY=weak)


def test_valid_key_is_accepted_verbatim():
    assert Settings(SECRET_KEY=VALID_KEY).SECRET_KEY == VALID_KEY
    assert Settings(SECRET_KEY=BOUNDARY_KEY).SECRET_KEY == BOUNDARY_KEY


def test_same_environment_yields_same_key_across_instances():
    # The multi-worker regression in one assertion: the key comes from shared
    # configuration, never from per-process randomness, so two instances built from
    # the same environment agree exactly.
    assert Settings().SECRET_KEY == Settings().SECRET_KEY


def test_configured_suite_key_passes_validation():
    # Guards the pinned test key itself: if someone replaces it with a placeholder,
    # this fails before any session test can pass on a weak key.
    assert len(settings.SECRET_KEY) >= 32
