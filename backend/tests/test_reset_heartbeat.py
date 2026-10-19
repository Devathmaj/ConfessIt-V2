"""A daily job that stops is a job nobody notices until a leaderboard is days stale.

The 00:01 UTC game-reset tick runs in the scheduler sidecar: a process that starts,
does one pass and exits. Nothing it does outlives it, so a tick that stopped running --
a broken cron entry, a sidecar that would not start, a pass that died before it could
say anything -- left no trace anywhere. The games simply stopped rolling over.

These tests pin the trace. The tick records the outcome of every pass in one row, and
the backend publishes that row as the metric the alerting rules read, so "the daily
job has not succeeded since <time>" is a thing an alert can see.

Three rules decide what the heartbeat means, and each of them is a rule the old
behaviour broke:

* **A failure does not erase the last success.** One bad night is not a broken job,
  and the age of the last success is what says how long the job has really been
  broken.
* **Only the scheduled tick moves it.** The boot-time ensure and the manual admin
  trigger roll the games over too, but a deploy is not a daily job and an
  administrator pressing a button is not a schedule.
* **Nothing published is better than a sentinel.** A stack whose tick has never run
  has no series, and the alert for that is written against the absence of the series
  rather than against a value that would read like a real timestamp.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db_models import GameResetHeartbeatRecord
from app.limits import GAME_IDS
from app.services import game_scheduler, reset_heartbeat
from app.services.game_scheduler import run_reset_cycle
from app.services.reset_heartbeat import (
    LAST_ATTEMPT_METRIC,
    LAST_SUCCESS_METRIC,
    RESET_JOB,
    record_reset_outcome,
    read_reset_heartbeat,
)

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeGauge:
    """A gauge that remembers what it was set to."""

    def __init__(self):
        self.values: list[float] = []

    def set(self, value, attributes=None):
        self.values.append(value)


@pytest.fixture
def gauges(monkeypatch):
    """The two gauges, stood in for, so a test can read what was published."""
    success = FakeGauge()
    attempt = FakeGauge()
    monkeypatch.setattr(
        reset_heartbeat, "_gauges", lambda: (success, attempt), raising=True
    )
    return {"success": success, "attempt": attempt}


# ------------------------------------------------------------------ the row


def test_a_successful_pass_records_the_heartbeat(db):
    """The pass says it happened."""
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat is not None
    assert heartbeat["job"] == RESET_JOB
    assert heartbeat["last_success_at"] == datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert heartbeat["last_attempt_at"] == datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert heartbeat["last_error"] is None


def test_a_failed_pass_records_the_attempt_and_the_error(db):
    """A failure says what happened."""
    record_reset_outcome(
        db, succeeded=False, error="wordle: RuntimeError: boom", now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    )

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat["last_success_at"] is None
    assert heartbeat["last_attempt_at"] == datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert "wordle" in heartbeat["last_error"]


def test_a_failure_does_not_erase_the_last_success(db):
    """The rule the age of the success depends on.

    One bad night must not look like the job never ran at all: the alert reads the
    age of the last success, and a failure that erased it would report every
    transient fault as a job that had never worked.
    """
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))
    record_reset_outcome(
        db, succeeded=False, error="boom", now=datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    )

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat["last_success_at"] == datetime(2026, 10, 8, 0, 1, tzinfo=UTC)
    assert heartbeat["last_attempt_at"] == datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    assert heartbeat["last_error"] == "boom"


def test_a_later_success_clears_the_error(db):
    """The job recovered, so the row says so."""
    record_reset_outcome(db, succeeded=False, error="boom", now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 9, 0, 1, tzinfo=UTC))

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat["last_error"] is None
    assert heartbeat["last_success_at"] == datetime(2026, 10, 9, 0, 1, tzinfo=UTC)


def test_the_row_is_a_single_row_for_the_job(db):
    """A second pass updates the row; it does not add another one."""
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 9, 0, 1, tzinfo=UTC))

    with db.sessions() as session:
        rows = session.scalars(select(GameResetHeartbeatRecord)).all()
        assert len(rows) == 1
        assert rows[0].job == RESET_JOB


def test_a_very_long_error_is_clipped(db):
    """The error is clipped to the width of the column it lands in."""
    record_reset_outcome(db, succeeded=False, error="x" * 5000, now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    heartbeat = read_reset_heartbeat(db)
    assert len(heartbeat["last_error"]) == reset_heartbeat.MAX_ERROR_CHARS


def test_nothing_recorded_reads_as_nothing(db):
    """A stack whose tick has never run has no heartbeat."""
    assert read_reset_heartbeat(db) is None


# ------------------------------------------------------------------ the tick records it


def test_the_reset_cycle_records_its_own_outcome(db):
    """The tick is the writer, so the tick is what is tested.

    A successful pass leaves a success behind.
    """
    run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat is not None
    assert heartbeat["last_success_at"] is not None
    assert heartbeat["last_error"] is None


def test_a_failing_pass_records_the_failure_and_keeps_the_last_success(db, monkeypatch):
    """One game's failure is recorded, and the previous success survives it."""
    run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))
    first_success = read_reset_heartbeat(db)["last_success_at"]

    real_ensure = game_scheduler.ensure_single_game

    def flaky(game_db, game_id, now=None):
        if game_id == "wordle":
            raise RuntimeError("simulated word-list outage")
        return real_ensure(game_db, game_id, now)

    monkeypatch.setattr(game_scheduler, "ensure_single_game", flaky)
    run_reset_cycle(db, datetime(2026, 10, 9, 0, 1, tzinfo=UTC))

    heartbeat = read_reset_heartbeat(db)
    assert heartbeat["last_success_at"] == first_success
    assert heartbeat["last_attempt_at"] == datetime(2026, 10, 9, 0, 1, tzinfo=UTC)
    assert "wordle" in heartbeat["last_error"]
    assert "RuntimeError" in heartbeat["last_error"]


def test_the_pass_still_succeeds_when_the_heartbeat_cannot_be_written(db, monkeypatch):
    """A heartbeat that could not be written must not fail the rollover.

    The pass has already done its work by the time the heartbeat is written, and a
    missing heartbeat is itself a signal -- the alert fires on an absent series too.
    """
    def _boom(*args, **kwargs):
        raise RuntimeError("the heartbeat table is missing")

    # The scheduler imports the writer inside the function, so the patch belongs on
    # the module it is imported from.
    monkeypatch.setattr(
        "app.services.reset_heartbeat.record_reset_outcome", _boom, raising=True
    )

    summary = run_reset_cycle(db, datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    assert set(summary["games"]) == set(GAME_IDS)
    assert all(info["action"] != "error" for info in summary["games"].values()), summary


# ------------------------------------------------------------------ the metric


def test_the_heartbeat_is_published_as_a_metric(db, gauges):
    """The row becomes the series the alert reads."""
    record_reset_outcome(db, succeeded=True, now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    published = reset_heartbeat.publish_reset_heartbeat(db)

    assert published == datetime(2026, 10, 8, 0, 1, tzinfo=UTC).timestamp()
    assert gauges["success"].values == [published]
    assert gauges["attempt"].values == [published]


def test_a_failed_attempt_is_published_as_an_attempt(db, gauges):
    """The attempt is published too, so "the tick is running and failing" is visible."""
    record_reset_outcome(db, succeeded=False, error="boom", now=datetime(2026, 10, 8, 0, 1, tzinfo=UTC))

    published = reset_heartbeat.publish_reset_heartbeat(db)

    assert published is None, "there is no success to publish"
    assert gauges["success"].values == []
    assert gauges["attempt"].values == [datetime(2026, 10, 8, 0, 1, tzinfo=UTC).timestamp()]


def test_nothing_is_published_without_a_heartbeat(db, gauges):
    """No sentinel: an absent series is the fact, not a value of zero."""
    published = reset_heartbeat.publish_reset_heartbeat(db)

    assert published is None
    assert gauges["success"].values == []
    assert gauges["attempt"].values == []


def test_the_metric_names_survive_the_prometheus_translation():
    """The names the alerting rules read.

    The collector's Prometheus exporter turns the dots into underscores and appends
    the unit, so the instrument names below are the series names in the rule file.
    If either side drifts, the rule quietly matches nothing -- an alert that can
    never fire is worse than no alert, because it looks like coverage.
    """
    rules = (REPO_ROOT / "observability" / "alerting-rules.yml").read_text(encoding="utf-8")

    assert LAST_SUCCESS_METRIC == "confessit.game.reset.last_success"
    assert LAST_ATTEMPT_METRIC == "confessit.game.reset.last_attempt"
    assert "confessit_game_reset_last_success_seconds" in rules
    assert "confessit_game_reset_last_attempt_seconds" in rules


# ------------------------------------------------------------------ the alert


def test_the_alerting_rules_cover_the_stalled_tick():
    """There is an alert for a tick that has not succeeded.

    Two days rather than one: a single missed night is a deploy that took the sidecar
    down at 00:01, not a broken schedule.
    """
    rules = (REPO_ROOT / "observability" / "alerting-rules.yml").read_text(encoding="utf-8")

    assert "ConfessItGameResetStalled" in rules
    assert "172800" in rules, "the stalled rule waits two days"
    assert "absent(confessit_game_reset_last_success_seconds" in rules


def test_the_alerting_rules_are_valid_prometheus_rules():
    """The file still parses as Prometheus rules.

    A syntax error in the rule file takes every rule in it down, so this is checked
    against the same parser Prometheus uses -- when it is available.
    """
    import shutil
    import subprocess

    if not shutil.which("promtool"):
        pytest.skip("promtool is not installed here")

    result = subprocess.run(
        ["promtool", "check", "rules", str(REPO_ROOT / "observability" / "alerting-rules.yml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
