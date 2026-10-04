"""The scheduler entrypoint must not let the environment become shell.

It generates a wrapper script and a cron.d file from DATABASE_URL, TZ,
APP_DIR, PYTHON_BIN, CRON_FILE, WRAPPER and CRON_USER, and both the catch-up
pass and the daily tick execute what it wrote -- the tick as appuser, via a
root cron. An unquoted expansion or an unvalidated newline would turn a value
into commands. These tests pin the three controls:

* values carrying newlines/CRs are refused before anything is written;
* `$`, backticks and backslashes are refused in paths (they stay legal in
  secrets, where single-quoting neutralises them);
* every expansion in the generated wrapper is single-quoted, proven by
  round-tripping a hostile DATABASE_URL through it byte-for-byte, and the cron
  job runs as the unprivileged user.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parent.parent / "scripts" / "game-scheduler-entrypoint.sh"


def _write_stub(path, body):
    path.write_text(body)
    os.chmod(path, 0o755)


def _run(tmp_path, **overrides):
    """Run the entrypoint with stubbed cron/runuser and an isolated filesystem.

    Returns (completed_process, paths dict). ``cron`` exits 0 without doing
    anything; ``runuser`` drops its flags and execs the command, like the real
    one minus the privilege change.
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
        "DATABASE_URL": "postgres://u:pw@db:5432/x",
        "APP_DIR": str(appdir),
        "PYTHON_BIN": "true",
        "CRON_FILE": str(cron_file),
        "WRAPPER": str(wrapper),
        "CRON_USER": "appuser",
    }
    # TZ is ambient-only when the case passes one: the wrapper omits the export
    # otherwise, which the clean-run test asserts.
    env.update({k: v for k, v in overrides.items() if v is not None})
    proc = subprocess.run(
        ["sh", str(ENTRYPOINT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, {"wrapper": wrapper, "cron_file": cron_file, "appdir": appdir}


def test_clean_run_writes_quoted_wrapper_and_unprivileged_cron(tmp_path):
    proc, paths = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    wrapper_text = paths["wrapper"].read_text()
    assert "export DATABASE_URL='postgres://u:pw@db:5432/x'" in wrapper_text
    assert f"cd '{paths['appdir']}'" in wrapper_text
    assert f"exec 'true' '{paths['appdir']}/scripts/run_game_resets.py'" in wrapper_text
    assert "export TZ" not in wrapper_text
    assert stat.S_IMODE(os.stat(paths["wrapper"]).st_mode) == 0o755

    cron_text = paths["cron_file"].read_text()
    assert f"1 0 * * * appuser {paths['wrapper']} > /proc/1/fd/1 2>&1" in cron_text
    assert " root " not in cron_text


def test_hostile_database_url_round_trips_byte_for_byte(tmp_path):
    # $, backticks, backslashes and single quotes are legal in secrets and must
    # survive single-quoting untouched -- proven by executing the wrapper, not
    # by eyeballing it. PYTHON_BIN is a stub that echoes the exported value;
    # the script path the wrapper appends is ignored by it.
    nasty = "postgres://u:p@$$w0rd!`~'\\x@db:5432/x?sslmode=require"
    showenv = tmp_path / "bin" / "showenv"
    showenv.parent.mkdir(exist_ok=True)
    showenv.write_text('#!/bin/sh\necho "GOT:$DATABASE_URL"\n')
    os.chmod(showenv, 0o755)
    proc, _paths = _run(tmp_path, DATABASE_URL=nasty, PYTHON_BIN=str(showenv))
    assert proc.returncode == 0, proc.stderr
    assert f"GOT:{nasty}" in proc.stdout.splitlines()


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
        ("DATABASE_URL", "postgres://u@h/db\nMALICIOUS=true"),
        ("DATABASE_URL", "postgres://u@h/db\rMALICIOUS=true"),
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
