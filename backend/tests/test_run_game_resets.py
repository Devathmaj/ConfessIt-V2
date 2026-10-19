"""The daily reset tick must work in the environment cron actually gives it.

cron(8) runs its jobs with a near-empty environment, so the tick sees only
what the generated wrapper exports -- and the tick imports the app, whose
Settings model refuses to be built without DATABASE_URL, TEST_DATABASE_URL,
REDIS_URL, SECRET_KEY and TOKEN_HASH_PEPPER. When the wrapper exported only
the database URL, the 00:01 tick died on a pydantic ValidationError before it
evaluated a single game: no session rolled over, no AI generation cycle was
enqueued, no league opportunity finalised, no expired chat was purged. The
catch-up pass at container start inherited the whole container environment and
succeeded, so a stack that restarted often looked healthy while a
continuously running one silently did nothing.

These tests pin the two halves of that fix:

* the tick succeeds with nothing but the wrapper's exports (and no .env in the
  working directory, so nothing can quietly make up for a missing export);
* a configuration failure is reported through the sanitizing handler -- exit
  1, a readable message, and no raw traceback carrying the DSN into the
  retained cron log.
"""

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = BACKEND_ROOT / "scripts" / "run_game_resets.py"
ENTRYPOINT = BACKEND_ROOT / "scripts" / "game-scheduler-entrypoint.sh"

# Strong enough for app.config.Settings to accept (>= 32 characters, no
# placeholder, enough distinct characters).
SECRET_KEY = "e359d5a93f086c7c3a90b56c390fd03a1e700708cc21c5a5ebaf7ab01436c491"
PEPPER = "bb5e97d44532f0cf881d23496899e04175c30d8db62b501c5a22057ab1200e31"


def _load_script_module():
    """Import the tick script as a module (without running main())."""
    spec = importlib.util.spec_from_file_location("run_game_resets_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_stub(path, body):
    path.write_text(body)
    os.chmod(path, 0o755)


def _wrapper_exports(tmp_path, **overrides):
    """Generate the wrapper with the entrypoint and return what it exports.

    The values come from the generated file itself, not from a list written
    here: the point is to run the tick with exactly the environment cron
    would give it.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    _write_stub(bindir / "cron", "#!/bin/sh\nexit 0\n")
    _write_stub(
        bindir / "runuser",
        "#!/bin/sh\n"
        'if [ "${1:-}" = "-u" ]; then shift 2; fi\n'
        'if [ "${1:-}" = "--" ]; then shift; fi\n'
        'exec "$@"\n',
    )
    # A stub interpreter: the entrypoint's own catch-up pass must not run a
    # second reset cycle while this test runs one.
    _write_stub(bindir / "python", "#!/bin/sh\nexit 0\n")
    appdir = tmp_path / "app"
    (appdir / "scripts").mkdir(parents=True, exist_ok=True)
    wrapper = tmp_path / "run-game-resets"
    cron_file = tmp_path / "cron.d"

    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        "APP_DIR": str(appdir),
        "PYTHON_BIN": str(bindir / "python"),
        "CRON_FILE": str(cron_file),
        "WRAPPER": str(wrapper),
        "CRON_USER": "appuser",
    }
    env.update(overrides)
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    exports = {}
    for line in wrapper.read_text().splitlines():
        match = re.match(r"^export ([A-Za-z_][A-Za-z0-9_]*)='(.*)'$", line)
        if match:
            # The wrapper single-quotes every value and turns an embedded
            # quote into '\'' -- undo that to recover the raw value.
            exports[match.group(1)] = match.group(2).replace("'\\''", "'")
    return exports


def _tick_env(exports):
    """The environment the tick runs in: the wrapper's exports, and nothing else."""
    env = dict(exports)
    if sys.platform == "win32":
        # Windows cannot start a child process without its root directory.
        # It is not configuration the wrapper supplies, so it does not weaken
        # the point of the test.
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    return env


def _test_database_url():
    """The URL of the database the suite is allowed to write to."""
    from app.config import settings

    # conftest points the app at the test database (it truncates between
    # tests), so the tick under test writes there and never at the
    # application's own data.
    return settings.DATABASE_URL


def test_tick_succeeds_with_only_the_wrapper_environment(tmp_path):
    """The audit's regression test: env -i plus the wrapper's exports, exit 0.

    The working directory is a temporary one, so no .env can stand in for a
    missing export -- the production image does not carry one either.
    """
    database_url = _test_database_url()
    exports = _wrapper_exports(
        tmp_path,
        DATABASE_URL=database_url,
        TEST_DATABASE_URL=database_url,
        REDIS_URL="redis://:pw@redis:6379",
        SECRET_KEY=SECRET_KEY,
        TOKEN_HASH_PEPPER=PEPPER,
    )
    assert "DATABASE_URL" in exports

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=_tick_env(exports),
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "result=ok" in result.stdout


def test_configuration_failure_is_reported_through_the_sanitizing_handler(tmp_path):
    """A missing setting must fail like any other failure: sanitized, exit 1.

    Before the fix the import of app.config sat outside main()'s try block, so
    the ValidationError escaped as an unhandled module-level exception: the
    raw traceback (carrying the settings the tick was handed) went to the
    container log and the handler that redacts credentials never ran.
    """
    database_url = _test_database_url()
    env = _tick_env(
        {
            "DATABASE_URL": database_url,
            "TEST_DATABASE_URL": database_url,
            "REDIS_URL": "redis://:pw@redis:6379",
            "TOKEN_HASH_PEPPER": PEPPER,
        }
    )

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 1
    # Nothing escaped unhandled: the failure was reported by the script.
    assert result.stderr == ""
    assert "[game-resets] reset cycle failed" in result.stdout
    # The failure is reported from main(), not from the script's module body:
    # an unhandled module-level exception would have printed the script's own
    # frame as "in <module>" and gone to stderr instead.
    script_frames = [
        line for line in result.stdout.splitlines() if "run_game_resets.py" in line
    ]
    assert script_frames, "the report should point at the tick script"
    assert all("in main" in line for line in script_frames), script_frames
    # The missing setting is named, and the DSN it was given is not.
    assert "SECRET_KEY" in result.stdout
    assert "postgresql+psycopg://" not in result.stdout


def test_sanitizer_redacts_connection_strings_and_secrets(monkeypatch):
    """The handler that protects the retained cron log."""
    module = _load_script_module()

    monkeypatch.setenv("SECRET_KEY", "signing-key-value-0123456789abcdef")
    text = (
        "connection failed: postgresql+psycopg://confessit:hunter2@postgres:5432/x\n"
        "SECRET_KEY is signing-key-value-0123456789abcdef"
    )
    redacted = module._sanitize_failure(text)
    assert "hunter2" not in redacted
    assert "signing-key-value-0123456789abcdef" not in redacted
    assert "://***@" in redacted
