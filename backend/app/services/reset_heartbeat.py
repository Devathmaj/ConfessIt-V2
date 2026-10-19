# app/services/reset_heartbeat.py

"""The heartbeat of the daily game-reset tick: written by the tick, read by the backend.

The 00:01 UTC game-reset tick runs in the scheduler sidecar -- a separate process
that starts, does one pass and exits. Nothing it does outlives it, so a tick that
stopped running left no trace at all: the cron entry broke, the sidecar would not
start, the pass died before it could say anything, and the games simply stopped
rolling over. The only symptom was a stale leaderboard days later, and nothing in
the stack was watching for it.

This module is the trace. The tick records the outcome of every pass in one row,
and the backend reads that row and publishes it as a metric, so "the daily job has
not succeeded since <time>" is a thing an alert can see. The alert itself lives in
``observability/alerting-rules.yml``.

Two rules decide what the heartbeat means:

* **A failure does not erase the last success.** A failed pass records the attempt
  and the error and leaves ``last_success_at`` alone. The age of the last success
  is what says how long the job has really been broken, and one bad night should
  not look like the job never ran at all.
* **Only the scheduled tick moves it.** The boot-time ensure and the manual
  ``POST /admin/games/run-resets`` trigger roll the games over too, but they are
  different events with different cadence: a deploy is not a daily job, and an
  administrator pressing a button is not a schedule. The heartbeat answers "did the
  scheduled tick run?", and only the scheduled tick answers it.
"""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db_models import GameResetHeartbeatRecord
from ..logger import get_logger
from ..postgres import Database

logger = get_logger(__name__)

# The job this heartbeat is about. The primary key of the row, so a second job
# would get its own row rather than overwriting this one.
RESET_JOB = "game-reset"

# The width of the ``last_error`` column. The error text is clipped to it rather
# than to a number of our own, so the clip and the column cannot drift apart.
MAX_ERROR_CHARS = 1000

# The metric names. They become ``confessit_game_reset_last_success_seconds`` and
# ``confessit_game_reset_last_attempt_seconds`` in Prometheus: the collector's
# Prometheus exporter turns the dots into underscores and appends the unit.
LAST_SUCCESS_METRIC = "confessit.game.reset.last_success"
LAST_ATTEMPT_METRIC = "confessit.game.reset.last_attempt"


def record_reset_outcome(
    db: Database,
    *,
    succeeded: bool,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
) -> None:
    """Record the outcome of one reset pass.

    ``succeeded`` says whether the pass finished without a failure. On success the
    last-success stamp moves to now and the stored error is cleared; on failure the
    attempt stamp moves, the error is stored, and the last success is left exactly
    where it was.

    The write is an upsert on the job name, so the first pass creates the row and
    every later pass updates it. It is a single statement rather than a read
    followed by a write, so two passes landing at once cannot both decide the row
    does not exist yet and one of them fail on the primary key.
    """
    moment = now or datetime.now(timezone.utc)
    values = {
        "job": RESET_JOB,
        "last_attempt_at": moment,
        "last_success_at": moment if succeeded else None,
        "last_error": None if succeeded else (error or "")[:MAX_ERROR_CHARS] or None,
        "updated_at": moment,
    }
    with db.sessions.begin() as session:
        statement = pg_insert(GameResetHeartbeatRecord).values(**values)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[GameResetHeartbeatRecord.job],
                set_={
                    "last_attempt_at": values["last_attempt_at"],
                    # COALESCE, so a failed attempt does not erase the last success:
                    # the age of that stamp is what says how long the job has been
                    # broken, and one bad night is not a broken job.
                    "last_success_at": func.coalesce(
                        statement.excluded.last_success_at,
                        GameResetHeartbeatRecord.last_success_at,
                    ),
                    "last_error": values["last_error"],
                    "updated_at": values["updated_at"],
                },
            )
        )


def read_reset_heartbeat(db: Database) -> Optional[dict]:
    """The recorded heartbeat, or None when no pass has ever been recorded.

    The four fields are what the metric and the alert need: when the last attempt
    was, when the last success was, and what the last failure said.
    """
    with db.sessions() as session:
        row = session.scalar(
            select(GameResetHeartbeatRecord).where(GameResetHeartbeatRecord.job == RESET_JOB)
        )
        if row is None:
            return None
        return {
            "job": row.job,
            "last_attempt_at": row.last_attempt_at,
            "last_success_at": row.last_success_at,
            "last_error": row.last_error,
        }


# The gauge handles are built once and kept: the meter resolves the global
# provider when the instrument is created, and creating a new instrument on every
# publication would register a second copy of the same series.
_last_success_gauge = None
_last_attempt_gauge = None


def _gauges():
    """The two gauges, created on first use.

    Created lazily rather than at import because the meter resolves the global
    provider when the instrument is made, and the provider is registered during
    application start-up. A gauge bound to the provider that was in place at import
    time would be the no-op one, and the series would never appear.
    """
    global _last_success_gauge, _last_attempt_gauge
    if _last_success_gauge is None or _last_attempt_gauge is None:
        from opentelemetry import metrics

        meter = metrics.get_meter("confessit.backend")
        _last_success_gauge = meter.create_gauge(
            LAST_SUCCESS_METRIC,
            unit="s",
            description="Unix time of the last successful daily game-reset tick.",
        )
        _last_attempt_gauge = meter.create_gauge(
            LAST_ATTEMPT_METRIC,
            unit="s",
            description="Unix time of the last daily game-reset tick, successful or not.",
        )
    return _last_success_gauge, _last_attempt_gauge


def publish_reset_heartbeat(db: Database) -> Optional[float]:
    """Publish the heartbeat as metrics. Returns the last-success stamp published.

    Nothing is published when there is no heartbeat to publish: a stack whose tick
    has never run has no series, and the alert for that is written against the
    absence of the series rather than against a sentinel value. A sentinel would be
    indistinguishable from a real timestamp the moment anybody read the series by
    hand, and "no successful reset has ever been recorded" is a different fact from
    "the last successful reset was at the epoch".
    """
    heartbeat = read_reset_heartbeat(db)
    if heartbeat is None:
        return None

    success_gauge, attempt_gauge = _gauges()
    attempt = heartbeat.get("last_attempt_at")
    if attempt is not None:
        attempt_gauge.set(attempt.timestamp())
    success = heartbeat.get("last_success_at")
    if success is None:
        return None
    success_gauge.set(success.timestamp())
    return success.timestamp()
