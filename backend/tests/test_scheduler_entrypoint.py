"""The scheduler entrypoint must not let the environment become shell.

It generates a wrapper script and a cron.d file from the PG* connection
settings, TZ, APP_DIR, PYTHON_BIN, CRON_FILE, WRAPPER and CRON_USER, and both the catch-up
pass and the daily tick execute what it wrote -- the tick as appuser, via a
root cron. An unquoted expansion or an unvalidated newline would turn a value
into commands. These tests pin the three controls:

* values carrying newlines/CRs are refused before anything is written;
* `$`, backticks and backslashes are refused in paths (they stay legal in
  secrets, where single-quoting neutralises them);
* every expansion in the generated wrapper is single-quoted, proven by
  round-tripping a hostile DATABASE_URL through it byte-for-byte, and the cron
  job runs as the unprivileged user.

The fourth control is about what the wrapper carries rather than how it quotes
it. cron(8) runs its jobs with a near-empty environment, so the wrapper has to
export every setting app.config.Settings requires: exporting only the
database URL left the 00:01 tick dying on a pydantic ValidationError before
it evaluated a single game -- no rollover, no AI generation cycle, no league
finalisation, no chat retention -- while the catch-up pass at container start
(which does inherit the container environment) kept succeeding and made the
stack look healthy. So the wrapper's exports are asserted against the app's
required settings, and the wrapper is executed with nothing but those exports
to prove the app's settings can still be built from them.
"""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "scripts" / "game-scheduler-entrypoint.sh"
BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Values the app's Settings model accepts (strong enough secrets). The
# entrypoint itself only checks that a required setting is present and free of
# line breaks, but the wrapper is proved below to build the app's settings, and
# that needs values the app would accept. PostgreSQL is configured through the
# PG* variables the Azure deployment supplies.
VALID_PG_SETTINGS = {
    "PGHOST": "db",
    "PGPORT": "5432",
    "PGUSER": "u",
    "PGPASSWORD": "pw",
    "PGDATABASE": "ConfessIt",
    "PGSSLMODE": "disable",
}
VALID_REDIS_URL = "redis://:pw@redis:6379"
VALID_SECRET_KEY = "e359d5a93f086c7c3a90b56c390fd03a1e700708cc21c5a5ebaf7ab01436c491"
VALID_PEPPER = "bb5e97d44532f0cf881d23496899e04175c30d8db62b501c5a22057ab1200e31"


def _write_stub(path, body):
    path.write_text(body)
    os.chmod(path, 0o755)


def _run(tmp_path, unset=(), **overrides):
    """Run the entrypoint with stubbed cron/runuser and an isolated filesystem.

    Returns (completed_process, paths dict). ``cron`` exits 0 without doing
    anything; ``runuser`` drops its flags and execs the command, like the real
    one minus the privilege change. ``unset`` names environment variables to
    withhold (the scheduler refuses to start without the required ones);
    ``overrides`` replaces them.
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
    appdir = tmp_path / "app"
    (appdir / "scripts").mkdir(parents=True, exist_ok=True)
    wrapper = tmp_path / "run-game-resets"
    cron_file = tmp_path / "cron.d"

    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        **VALID_PG_SETTINGS,
        # The rest of the required settings: cron(8) will not pass the
        # container environment to the tick, so the wrapper has to carry them.
        "REDIS_URL": VALID_REDIS_URL,
        "SECRET_KEY": VALID_SECRET_KEY,
        "TOKEN_HASH_PEPPER": VALID_PEPPER,
        "APP_DIR": str(appdir),
        "PYTHON_BIN": "true",
        "CRON_FILE": str(cron_file),
        "WRAPPER": str(wrapper),
        "CRON_USER": "appuser",
    }
    # TZ is ambient-only when the case passes one: the wrapper omits the export
    # otherwise, which the clean-run test asserts.
    env.update({k: v for k, v in overrides.items() if v is not None})
    for name in unset:
        env.pop(name, None)
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, {"wrapper": wrapper, "cron_file": cron_file, "appdir": appdir}


def _exported_names(wrapper_text):
    """The setting names the wrapper exports."""
    return set(re.findall(r"^export ([A-Za-z_][A-Za-z0-9_]*)=", wrapper_text, flags=re.MULTILINE))


def test_clean_run_writes_quoted_wrapper_and_unprivileged_cron(tmp_path):
    proc, paths = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    wrapper_text = paths["wrapper"].read_text()
    assert "export PGHOST='db'" in wrapper_text
    assert f"cd '{paths['appdir']}'" in wrapper_text
    assert f"exec 'true' '{paths['appdir']}/scripts/run_game_resets.py'" in wrapper_text
    assert "export TZ" not in wrapper_text
    assert stat.S_IMODE(os.stat(paths["wrapper"]).st_mode) == 0o644

    cron_text = paths["cron_file"].read_text()
    assert f"1 0 * * * appuser /bin/sh {paths['wrapper']} > /proc/1/fd/1 2>&1" in cron_text
    assert " root " not in cron_text


def test_wrapper_exports_every_setting_the_app_requires(tmp_path):
    """The tick imports the app, so the wrapper must carry the app's settings.

    A required field the wrapper does not export aborts the whole 00:01 pass
    before one game is evaluated -- the failure that stopped every rollover,
    every AI generation cycle, every league finalisation and the chat
    retention purge from ever running. The list of required fields is read
    from the app itself, so adding one to app.config.Settings without
    teaching the wrapper about it fails here.
    """
    from app.config import Settings

    required = {
        name for name, field in Settings.model_fields.items() if field.is_required()
    }
    assert required, "the app is expected to have required settings"

    proc, paths = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    exported = _exported_names(paths["wrapper"].read_text())
    assert required <= exported, (
        "the generated wrapper must export every required setting; missing: "
        f"{sorted(required - exported)}"
    )


@pytest.mark.parametrize(
    "missing",
    [
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
        "REDIS_URL",
        "SECRET_KEY",
        "TOKEN_HASH_PEPPER",
    ],
)
def test_missing_required_setting_refuses_to_start(tmp_path, missing):
    """A scheduler that cannot build its settings must say so, not tick blind.

    Starting anyway would reproduce the original defect in a quieter form:
    the daily tick would die at 00:01 UTC with nothing in the log but a
    non-zero exit, long after the operator stopped watching the container.
    """
    proc, paths = _run(tmp_path, unset=[missing])
    assert proc.returncode != 0
    assert "refusing to start" in proc.stderr
    assert missing in proc.stderr
    assert not paths["wrapper"].exists()
    assert not paths["cron_file"].exists()


def test_wrapper_alone_builds_the_app_settings(tmp_path):
    """The wrapper's exports alone must be enough to build the app's settings.

    This is the environment cron(8) hands the tick: essentially empty. The
    probe runs in the wrapper's working directory, which holds no .env, so
    nothing but the exported settings can satisfy the app -- if the wrapper
    forgot one, the probe fails exactly as the 00:01 tick did.
    """
    appdir = tmp_path / "app"
    appdir.mkdir(exist_ok=True)
    probe = tmp_path / "bin" / "settings-probe"
    probe.parent.mkdir(exist_ok=True)
    probe.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.path.insert(0, {str(BACKEND_ROOT)!r})\n"
        "import app.config\n"
        "print('SETTINGS_OK')\n"
    )
    os.chmod(probe, 0o755)

    proc, paths = _run(
        tmp_path,
        PYTHON_BIN=str(probe),
        APP_DIR=str(appdir),
    )
    assert proc.returncode == 0, proc.stderr

    # Now run the wrapper the way cron(8) does: with an essentially empty
    # environment, where the wrapper's own exports are the only source of
    # configuration. This is the environment the 00:01 tick actually gets.
    cron_env = {"PATH": os.environ.get("PATH", "")}
    result = subprocess.run(
        ["sh", str(paths["wrapper"])],
        env=cron_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    # The probe reports that the app's settings object was built from the
    # wrapper's exports alone.
    assert "SETTINGS_OK" in result.stdout


def test_hostile_database_url_round_trips_byte_for_byte(tmp_path):
    # $, backticks, backslashes and single quotes are legal in secrets and must
    # survive single-quoting untouched -- proven by executing the wrapper, not
    # by eyeballing it. PYTHON_BIN is a stub that echoes the exported value;
    # the script path the wrapper appends is ignored by it.
    nasty = "p@$$w0rd!`~'\\x"
    showenv = tmp_path / "bin" / "showenv"
    showenv.parent.mkdir(exist_ok=True)
    showenv.write_text('#!/bin/sh\necho "GOT:$PGPASSWORD"\n')
    os.chmod(showenv, 0o755)
    proc, _paths = _run(tmp_path, PGPASSWORD=nasty, PYTHON_BIN=str(showenv))
    assert proc.returncode == 0, proc.stderr
    assert f"GOT:{nasty}" in proc.stdout.splitlines()


def test_secret_with_a_line_break_is_refused(tmp_path):
    # A newline in any exported setting would split the generated wrapper into
    # extra commands; the secrets are exported the same way the URL is.
    proc, paths = _run(tmp_path, SECRET_KEY="abc\nrm -rf /")
    assert proc.returncode != 0
    assert "refusing to start" in proc.stderr
    assert not paths["wrapper"].exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("APP_DIR", "/tmp/x\ntouch /tmp/pwned"),
        ("APP_DIR", "/tmp/$(touch /tmp/pwned)"),
        ("APP_DIR", "/tmp/`touch /tmp/pwned`"),
        ("APP_DIR", "/tmp/with $dollar"),
        ("APP_DIR", "/tmp/with\\backslash"),
        ("PYTHON_BIN", "/usr/bin/py\ntouch /tmp/pwned"),
        ("PYTHON_BIN", "/usr/bin/`id`"),
        ("PGPASSWORD", "hunter2\nMALICIOUS=true"),
        ("PGPASSWORD", "hunter2\rMALICIOUS=true"),
        ("CRON_FILE", "/tmp/cron\n* * * * * root evil"),
        ("WRAPPER", "/tmp/w\nevil"),
        ("CRON_USER", "appuser\nmalicious"),
        ("CRON_USER", "app$user"),
        ("TZ", "UTC\nEVIL=1"),
    ],
)
def test_hostile_values_are_refused_before_anything_is_written(tmp_path, field, value):
    proc, paths = _run(tmp_path, **{field: value})
    assert proc.returncode != 0
    assert "refusing to start" in proc.stderr
    assert not paths["wrapper"].exists()
    assert not paths["cron_file"].exists()
    assert not (tmp_path / "pwned").exists()


def test_catch_up_retries_a_failing_pass_then_continues(tmp_path):
    # A bring-up race (database still empty) fails the first passes: the
    # entrypoint retries instead of waiting a whole day for the next tick.
    # The stub python fails twice, then succeeds; the delay is zero here.
    flaky = tmp_path / "bin" / "flaky-python"
    counter = tmp_path / "attempts"
    flaky.parent.mkdir(exist_ok=True)
    flaky.write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{counter}" 2>/dev/null || echo 0)\n'
        f'echo $((n + 1)) > "{counter}"\n'
        '[ "$n" -ge 2 ] || exit 1\n'
        "exit 0\n"
    )
    os.chmod(flaky, 0o755)
    proc, _paths = _run(
        tmp_path, PYTHON_BIN=str(flaky), CATCHUP_RETRY_DELAY_SECONDS="0"
    )
    assert proc.returncode == 0, proc.stderr
    assert counter.read_text().strip() == "3"
    assert "retrying" in proc.stderr


def test_catch_up_gives_up_after_five_attempts_but_keeps_cron(tmp_path):
    proc, paths = _run(
        tmp_path, PYTHON_BIN="false", CATCHUP_RETRY_DELAY_SECONDS="0"
    )
    # The daemon still starts: a failed catch-up must not take cron down.
    assert proc.returncode == 0, proc.stderr
    assert paths["cron_file"].exists()


def test_non_digit_retry_delay_is_refused(tmp_path):
    proc, paths = _run(tmp_path, CATCHUP_RETRY_DELAY_SECONDS="15; evil")
    assert proc.returncode != 0
    assert "refusing to start" in proc.stderr
