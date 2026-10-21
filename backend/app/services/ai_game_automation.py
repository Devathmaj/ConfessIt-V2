"""Automated, schedule-driven session generation for the two AI-powered games.

Plot Twist and Case Closed ride the game scheduler that already runs at 00:01 UTC
(and at boot, and from ``POST /admin/games/run-resets``), but the scheduler only
*enqueues* a cycle: one ``game_generation_jobs`` row. A background worker --
in-process, safe on every uvicorn worker and replica -- claims the job under one
shared advisory lock, runs the existing two-stage generation pipeline, validates
the result, and only then opens the playable session, all inside the game's own
lifecycle lock.

Why it is shaped this way:

* **00:01 UTC is a checkpoint, not a deadline.** Queueing is a fast database write;
  the model calls happen later and never block the scheduling pass.
* **One cycle per game per boundary.** ``(game_id, cycle_start_date)`` is unique
  and the window is fixed at enqueue, so repeated scheduler runs find the existing
  job instead of starting a second generation, and slow generation can never shift
  the configured frequency.
* **One protected pipeline.** Every worker runs under the single
  ``AI_GENERATION_LOCK_KEY`` advisory lock, so the two games can never call the
  provider concurrently, in one process or across replicas.
* **Publication is atomic with completion.** The package's ``published`` status,
  the old session's end, the new session's start and the job's terminal update
  commit in one transaction under the game lock: a crash can leave an unpublished
  (retryable) package, but never a published session without its job or vice versa.
* **Revocation wins races.** ``game_session_revocations`` ends the live session,
  suppresses replacement generation until the next boundary, cancels a running job
  in the same locked transaction, and is re-checked inside the publish transaction
  so a stale job can never resurrect a revoked session.
"""

from __future__ import annotations

import threading
import time as _time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterator, Optional

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db_models import (
    CaseClosedBlueprintRecord,
    CaseClosedGameRecord,
    CaseClosedPackageRecord,
    GameGenerationJobRecord,
    GameSessionRevocationRecord,
    PlotTwistBlueprintRecord,
    PlotTwistGameRecord,
    PlotTwistPackageRecord,
)
from ..limits import (
    AI_GAME_IDS,
    AI_GENERATION_HISTORY_DEFAULT_LIMIT,
    AI_GENERATION_HISTORY_MAX_LIMIT,
    AI_GENERATION_LOCK_KEY,
    AI_GENERATION_MAX_ATTEMPTS,
    AI_GENERATION_RETRY_BACKOFF_SECONDS,
    AI_GENERATION_STALE_SECONDS,
)
from ..logger import get_logger
from ..postgres import Database
from . import game_periods
from .game_configs import game_lock_key, get_config

logger = get_logger(__name__)

# The session model, the pinned-package foreign key and the pipeline models
# for each AI game. Kept as one table so a new AI game cannot half-wire the
# automation: everything below (scheduler, worker, revoke, history) reads it.
_AI_GAME_REGISTRY: dict[str, dict[str, Any]] = {
    "plot-twist": {
        "session": PlotTwistGameRecord,
        "package": PlotTwistPackageRecord,
        "blueprint": PlotTwistBlueprintRecord,
        "package_field": "story_package_id",
    },
    "case-closed": {
        "session": CaseClosedGameRecord,
        "package": CaseClosedPackageRecord,
        "blueprint": CaseClosedBlueprintRecord,
        "package_field": "case_package_id",
    },
}

_TERMINAL_STATUSES = ("published", "failed", "cancelled")
_LIVE_STATUSES = ("queued", "running", "retry_wait")


class _GenerationFailed(RuntimeError):
    """The pipeline ran but produced content that must not be published."""

    def __init__(self, message: str, *, code: str = "generation_failed",
                 blueprint_id: Optional[str] = None,
                 package_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.blueprint_id = blueprint_id
        self.package_id = package_id


class _GenerationAborted(RuntimeError):
    """The cycle became ineligible while it ran (revoked, hidden, superseded)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def _validate_ai_game(game_id: str) -> str:
    if game_id not in AI_GAME_IDS:
        raise ValueError(f"Not an automatically generated game: {game_id!r}")
    return game_id


def _utc(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _current_row(session, model):
    """The one active-or-pending session row, or None (same invariant as the scheduler)."""
    return session.scalar(
        select(model).where(model.status.in_(("active", "pending"))).limit(1)
    )


def _end_session(db: Database, game_id: str, row, moment: datetime) -> None:
    """End one live session through the existing ``ended`` convention.

    Case Closed first finalizes -- closing the case reveals the solution and
    scores every participant -- and a failure there is logged, not raised,
    exactly like the scheduler's boundary close: the stranded scan retries
    whatever this misses.
    """
    if game_id == "case-closed":
        _finalize_case_closed_safely(db, str(row.id))
    row.status = "ended"
    row.ended_at = moment


def _finalize_case_closed_safely(db: Database, game_id: str) -> None:
    """Reveal one closed Case Closed event, logging rather than raising."""
    try:
        from . import case_closed_gameplay

        case_closed_gameplay.finalize_event(db, game_id)
    except Exception:
        logger.exception("Case Closed finalization failed for event %s", game_id)


def _finalize_stranded_case_closed(db: Database) -> None:
    """Finish Case Closed events that ended without ever revealing the case.

    An event dying between ending and finalizing would otherwise keep its
    solution -- and every participant's reveal -- hidden forever. The scan
    runs inside the case-closed lock, so two scheduler passes cannot race
    the same reveal.
    """
    with db.sessions() as session:
        stranded = [
            str(game_id)
            for (game_id,) in session.execute(
                select(CaseClosedGameRecord.id).where(
                    CaseClosedGameRecord.status == "ended",
                    CaseClosedGameRecord.finalized.is_(False),
                )
            ).all()
        ]
    for game_id in stranded:
        _finalize_case_closed_safely(db, game_id)


# ---------------------------------------------------------------------------
# Scheduler integration: evaluate the cycle and enqueue when due
# ---------------------------------------------------------------------------


def ensure_cycle(
    db: Database, game_id: str, now: Optional[datetime] = None, *, startup: bool = False
) -> dict:
    """Evaluate one AI game's cycle boundary and enqueue generation when due.

    The delegated entry point behind ``game_scheduler.ensure_single_game``. It
    never generates content itself: it closes a session whose boundary has
    passed (Case Closed reveals and scores there), respects a live or recently
    revoked cycle, and otherwise writes one idempotent generation job.

    Actions: ``skipped_hidden``, ``generating`` (a job is already live),
    ``current`` (the session is inside its period), ``revoked_waiting``,
    ``generation_queued``, ``generation_requeued`` (a failed same-cycle job
    gets another attempt), ``generation_failed`` and ``generation_cancelled``.
    """
    _validate_ai_game(game_id)
    moment = _utc(now)
    today = moment.date()
    model = _AI_GAME_REGISTRY[game_id]["session"]

    try:
        with db.sessions.begin() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key(game_id)},
            )
            # Case Closed owes its reveal to every ended-but-unfinalized event
            # first, hidden or not -- the same scan and ordering the old
            # scheduler branch held.
            if game_id == "case-closed":
                _finalize_stranded_case_closed(db)
            config = get_config(session, game_id)
            if config.is_hidden:
                return {"game_id": game_id, "action": "skipped_hidden"}

            if startup:
                today_session = session.scalar(
                    select(model)
                    .where(
                        model.period_start_date <= today,
                        model.period_end_date >= today,
                    )
                    .order_by(model.period_end_date.desc())
                    .limit(1)
                )
                reset_boundary = datetime.combine(
                    today, datetime.min.time(), timezone.utc
                ) + timedelta(minutes=1)
                session_is_current = today_session is not None and (
                    today_session.period_end_date > today or moment < reset_boundary
                )
                if session_is_current:
                    return {
                        "game_id": game_id,
                        "action": "session_exists_today",
                        "session_id": str(today_session.id),
                    }

            current = _current_row(session, model)
            if current is not None and game_periods.is_reset_due(
                current.period_end_date, today
            ):
                # The boundary arrived: close the expired period now. The new
                # session opens at publication, never before.
                _end_session(db, game_id, current, moment)
                current = None

            live_job = _live_job(session, game_id)
            if live_job is not None:
                # Generation already runs (or waits) for this cycle: a second
                # run must not start a duplicate.
                return {
                    "game_id": game_id,
                    "action": "generating",
                    "job_id": str(live_job.id),
                    "job_status": live_job.status,
                }

            if current is not None:
                return {
                    "game_id": game_id,
                    "action": "current",
                    "session_id": str(current.id),
                }

            revoked_until = _active_revocation_until(session, game_id, today)
            if revoked_until is not None:
                return {
                    "game_id": game_id,
                    "action": "revoked_waiting",
                    "revoked_until": revoked_until.isoformat(),
                }

            existing = _job_for_cycle(session, game_id, today)
            if existing is not None:
                if existing.status == "failed" and existing.attempt < existing.max_attempts:
                    existing.status = "queued"
                    existing.next_attempt_at = None
                    existing.finished_at = None
                    session.flush()
                    return {
                        "game_id": game_id,
                        "action": "generation_requeued",
                        "job_id": str(existing.id),
                    }
                return {
                    "game_id": game_id,
                    "action": (
                        "generation_cancelled"
                        if existing.status == "cancelled"
                        else "generation_failed"
                    ),
                    "job_id": str(existing.id),
                    "error": existing.last_error or "",
                }

            start, end = game_periods.period_for_start(today, config.reset_cadence)
            job = GameGenerationJobRecord(
                game_id=game_id,
                cycle_start_date=start,
                cycle_end_date=end,
                difficulty=(config.difficulty or "medium"),
                status="queued",
                attempt=0,
                max_attempts=AI_GENERATION_MAX_ATTEMPTS,
                requested_at=moment,
                created_by="scheduler",
            )
            session.add(job)
            session.flush()
            logger.info(
                "AI generation cycle queued",
                extra={"game_id": game_id, "job_id": str(job.id), "cycle_end": end.isoformat()},
            )
            return {
                "game_id": game_id,
                "action": "generation_queued",
                "job_id": str(job.id),
                "cycle_start": start.isoformat(),
                "cycle_end": end.isoformat(),
                "difficulty": job.difficulty,
            }
    except IntegrityError as exc:
        # Only losing the (game_id, cycle_start_date) race is benign: a
        # concurrent scheduler run wrote the cycle first, so settle on it.
        from psycopg.errors import UniqueViolation

        if not isinstance(exc.orig, UniqueViolation):
            raise
        with db.sessions() as session:
            job = _live_job(session, game_id) or _job_for_cycle(session, game_id, today)
            return {
                "game_id": game_id,
                "action": "generation_queued",
                "job_id": str(job.id) if job is not None else None,
            }


def _live_job(session, game_id: str):
    return session.scalars(
        select(GameGenerationJobRecord)
        .where(
            GameGenerationJobRecord.game_id == game_id,
            GameGenerationJobRecord.status.in_(_LIVE_STATUSES),
        )
        .order_by(GameGenerationJobRecord.requested_at.desc())
        .limit(1)
    ).first()


def _job_for_cycle(session, game_id: str, day: date):
    return session.scalars(
        select(GameGenerationJobRecord)
        .where(
            GameGenerationJobRecord.game_id == game_id,
            GameGenerationJobRecord.cycle_start_date == day,
        )
        .order_by(GameGenerationJobRecord.created_at.desc())
        .limit(1)
    ).first()


def _active_revocation_until(session, game_id: str, today: date) -> Optional[date]:
    """The latest still-active revocation date for ``game_id``, or None.

    A revocation expires by the calendar: it is active only while its
    ``revoked_until`` is strictly later than today. Past rows stay for the
    audit trail and are never consulted again.
    """
    row = session.scalars(
        select(GameSessionRevocationRecord)
        .where(
            GameSessionRevocationRecord.game_id == game_id,
            GameSessionRevocationRecord.revoked_until > today,
        )
        .order_by(GameSessionRevocationRecord.revoked_until.desc())
        .limit(1)
    ).first()
    return row.revoked_until if row is not None else None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def revoke_current_session(
    db: Database, game_id: str, revoked_by: str, now: Optional[datetime] = None
) -> dict:
    """End the live session (or cancel its generation) and hold the next cycle.

    Returns ``action: "revoked"`` with the ended session and the date
    generation may resume, or ``action: "none"`` when nothing live existed, so
    repeated revokes are safe and never extend the suppression. The marker
    always reaches at least tomorrow, so revocation can never be followed by
    an immediate replacement in the same cycle; for longer cadences the
    session's own period end (the next boundary) is later and wins.
    """
    _validate_ai_game(game_id)
    moment = _utc(now)
    today = moment.date()
    model = _AI_GAME_REGISTRY[game_id]["session"]

    with db.sessions.begin() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": game_lock_key(game_id)},
        )
        current = _current_row(session, model)
        live_job = _live_job(session, game_id)
        if current is None and live_job is None:
            return {
                "game_id": game_id,
                "action": "none",
                "session_id": None,
                "revoked_until": None,
            }

        session_id: Optional[str] = None
        until: Optional[date] = None
        if current is not None:
            _end_session(db, game_id, current, moment)
            session_id = str(current.id)
            until = current.period_end_date
        if live_job is not None:
            live_job.status = "cancelled"
            live_job.error_code = "revoked"
            live_job.last_error = "Revoked by an administrator before publishing."
            live_job.finished_at = moment
            if until is None or live_job.cycle_end_date > until:
                until = live_job.cycle_end_date

        minimum = today + timedelta(days=1)
        if until is None or until < minimum:
            until = minimum
        session.add(
            GameSessionRevocationRecord(
                game_id=game_id,
                session_id=session_id,
                revoked_until=until,
                revoked_by=revoked_by,
            )
        )
        session.flush()
        logger.info(
            "AI game session revoked",
            extra={
                "game_id": game_id,
                "session_id": session_id,
                "revoked_until": until.isoformat(),
            },
        )
        return {
            "game_id": game_id,
            "action": "revoked",
            "session_id": session_id,
            "revoked_until": until.isoformat(),
        }


# ---------------------------------------------------------------------------
# Worker: claim one job, run the protected pipeline
# ---------------------------------------------------------------------------


@contextmanager
def _generation_lock(db: Database, *, wait_seconds: Optional[float] = None) -> Iterator[bool]:
    """Try to hold the one shared generation lockdown, waiting up to a budget.

    Session-level (not transaction-level) on purpose: the protected workflow
    spans several transactions -- blueprint, package, publication -- and the
    lock must survive all of them. It is released explicitly on the way out,
    and by PostgreSQL itself if the process dies with the connection open, so
    a crashed worker can never wedge the pipeline.
    """
    budget = (
        float(settings.AI_GENERATION_LOCK_WAIT_SECONDS)
        if wait_seconds is None
        else float(wait_seconds)
    )
    connection = db.engine.connect()
    acquired = False
    try:
        deadline = _time.monotonic() + max(0.0, budget)
        while True:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": AI_GENERATION_LOCK_KEY},
                ).scalar()
            )
            if acquired or _time.monotonic() >= deadline:
                break
            _time.sleep(0.25)
        yield acquired
    finally:
        try:
            if acquired:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": AI_GENERATION_LOCK_KEY},
                )
        except Exception:
            logger.warning("Could not release the AI generation lock", exc_info=True)
        finally:
            connection.close()


def process_due_jobs(
    db: Database,
    now: Optional[datetime] = None,
    *,
    max_jobs: int = 1,
    wait_seconds: Optional[float] = None,
) -> dict:
    """One worker pass: hold the shared lock and process queued generation.

    The lock is taken *before* claiming, so even when both games became due
    at the same checkpoint their workflows run sequentially -- the second
    job waits its turn in the queue rather than starting concurrently.
    """
    moment = _utc(now)
    processed: list[dict] = []
    with _generation_lock(db, wait_seconds=wait_seconds) as acquired:
        if not acquired:
            return {"action": "busy", "processed": []}
        for _ in range(max(1, int(max_jobs))):
            job = claim_next_job(db, moment)
            if job is None:
                break
            processed.append(run_claimed_job(db, job["id"], moment))
    return {"action": "processed" if processed else "idle", "processed": processed}


def claim_next_job(db: Database, now: Optional[datetime] = None) -> Optional[dict]:
    """Claim the oldest due job, marking it running and consuming one attempt.

    Three sources feed a claim: a fresh ``queued`` job, a ``retry_wait`` job
    whose backoff has elapsed, and a ``running`` job whose worker stopped
    touching it past the stale window (a crashed process). ``FOR UPDATE SKIP
    LOCKED`` lets concurrent passes pick different jobs instead of blocking,
    and the shared pipeline lock above them keeps only one workflow active.
    """
    moment = _utc(now)
    stale_before = moment - timedelta(seconds=AI_GENERATION_STALE_SECONDS)
    with db.sessions.begin() as session:
        for _ in range(5):
            row = session.scalars(
                select(GameGenerationJobRecord)
                .where(
                    or_(
                        (GameGenerationJobRecord.status == "queued")
                        & (GameGenerationJobRecord.attempt < GameGenerationJobRecord.max_attempts),
                        (GameGenerationJobRecord.status == "retry_wait")
                        & (GameGenerationJobRecord.attempt < GameGenerationJobRecord.max_attempts)
                        & or_(
                            GameGenerationJobRecord.next_attempt_at.is_(None),
                            GameGenerationJobRecord.next_attempt_at <= moment,
                        ),
                        (GameGenerationJobRecord.status == "running")
                        & or_(
                            GameGenerationJobRecord.heartbeat_at.is_(None),
                            GameGenerationJobRecord.heartbeat_at < stale_before,
                        ),
                    )
                )
                .order_by(
                    GameGenerationJobRecord.requested_at.asc(),
                    GameGenerationJobRecord.created_at.asc(),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if row is None:
                return None
            if int(row.attempt or 0) >= int(row.max_attempts or 1):
                # A crash consumed the final attempt: park it so it can never
                # wedge the queue, then look for the next claimable job.
                row.status = "failed"
                row.error_code = "worker_stopped"
                row.last_error = "The generation worker stopped before finishing the final attempt."
                row.finished_at = moment
                session.flush()
                continue
            row.status = "running"
            row.attempt = int(row.attempt or 0) + 1
            row.started_at = moment
            row.heartbeat_at = moment
            row.finished_at = None
            row.worker_id = _worker_id()
            session.flush()
            view = _job_view(row)
            logger.info(
                "AI generation job claimed",
                extra={
                    "game_id": row.game_id,
                    "job_id": str(row.id),
                    "attempt": row.attempt,
                },
            )
            return view
    return None


def _worker_id() -> str:
    import os

    return f"{os.getpid()}:{threading.current_thread().name}"[:128]


def run_claimed_job(db: Database, job_id: str, now: Optional[datetime] = None) -> dict:
    """Run one claimed job's full generation-to-publication lifecycle.

    Failures are recorded and bounded-retried (``retry_wait`` with backoff)
    or parked as ``failed``; eligibility loss (revoked, hidden) cancels the
    job. Nothing here raises for an expected pipeline failure: the caller is
    a background pass that must keep draining the queue.
    """
    moment = _utc(now)
    job = get_job(db, job_id)
    if job is None or job["status"] != "running":
        return {"job_id": str(job_id), "action": "not_running"}

    try:
        if job["game_id"] == "plot-twist":
            blueprint_id, package_id = _generate_plot_twist(db, job)
        elif job["game_id"] == "case-closed":
            blueprint_id, package_id = _generate_case_closed(db, job)
        else:  # defensive: the registry validated this at enqueue time
            raise _GenerationFailed(f"Unknown AI game {job['game_id']!r}", code="unknown_game")
        outcome = _publish_and_open(db, job_id=str(job["id"]), package_id=package_id, now=moment)
        logger.info(
            "AI generation cycle published",
            extra={
                "game_id": job["game_id"],
                "job_id": str(job["id"]),
                "package_id": package_id,
                "session_id": outcome.get("session_id"),
            },
        )
        return {
            "job_id": str(job["id"]),
            "action": "published",
            "game_id": job["game_id"],
            "blueprint_id": blueprint_id,
            "package_id": package_id,
            "session_id": outcome.get("session_id"),
        }
    except _GenerationAborted as exc:
        _finish_job(
            db,
            str(job["id"]),
            status="cancelled",
            error_code=exc.code,
            error=str(exc),
            now=moment,
        )
        logger.info(
            "AI generation cycle cancelled: %s (game=%s job=%s)",
            exc,
            job["game_id"],
            job["id"],
        )
        return {"job_id": str(job["id"]), "action": "cancelled", "error": str(exc)}
    except Exception as exc:  # provider failures, validation exhaustion, DB faults
        detail = _error_detail(exc)
        outcome = _fail_or_retry(db, str(job["id"]), detail, moment)
        logger.warning(
            "AI generation attempt failed (game=%s job=%s attempt outcome=%s): %s",
            job["game_id"],
            job["id"],
            outcome["action"],
            detail,
        )
        return {"job_id": str(job["id"]), **outcome, "error": detail}


def _generate_plot_twist(db: Database, job: dict) -> tuple[str, str]:
    """Run the existing Plot Twist Stage A -> approve -> Stage B pipeline."""
    from . import plot_twist_generation
    from .plot_twist_schemas import GenerationConfig

    config = GenerationConfig.model_validate({"difficulty": job["difficulty"]})
    draft = plot_twist_generation.create_blueprint_draft(
        db, config, created_by="scheduler"
    )
    blueprint_id = str(draft["id"])
    blueprint = plot_twist_generation.generate_blueprint(db, blueprint_id)
    if blueprint["status"] != "ready_for_review":
        raise _GenerationFailed(
            _validation_summary(blueprint, "The blueprint failed validation."),
            code="blueprint_invalid",
            blueprint_id=blueprint_id,
        )
    blueprint = plot_twist_generation.approve_blueprint(db, blueprint_id)
    if blueprint["status"] != "approved":
        raise _GenerationFailed(
            "The generated blueprint could not be approved.",
            code="blueprint_not_approved",
            blueprint_id=blueprint_id,
        )
    package = plot_twist_generation.generate_package(
        db, blueprint_id, created_by="scheduler"
    )
    if package["status"] != "ready_for_review":
        raise _GenerationFailed(
            _validation_summary(package, "The story package failed validation."),
            code="package_invalid",
            blueprint_id=blueprint_id,
            package_id=str(package["id"]),
        )
    return blueprint_id, str(package["id"])


def _generate_case_closed(db: Database, job: dict) -> tuple[str, str]:
    """Run the existing Case Closed Stage A -> approve -> Stage B pipeline."""
    from . import case_closed_generation
    from .case_closed_schemas import CaseClosedGenerationConfig

    config = CaseClosedGenerationConfig.model_validate({"difficulty": job["difficulty"]})
    draft = case_closed_generation.create_blueprint_draft(
        db, config, created_by="scheduler"
    )
    blueprint_id = str(draft["id"])
    blueprint = case_closed_generation.generate_blueprint(db, blueprint_id)
    if blueprint["status"] != "ready_for_review":
        raise _GenerationFailed(
            _validation_summary(blueprint, "The case blueprint failed validation."),
            code="blueprint_invalid",
            blueprint_id=blueprint_id,
        )
    blueprint = case_closed_generation.approve_blueprint(db, blueprint_id)
    if blueprint["status"] != "approved":
        raise _GenerationFailed(
            "The generated case blueprint could not be approved.",
            code="blueprint_not_approved",
            blueprint_id=blueprint_id,
        )
    package = case_closed_generation.generate_package(
        db, blueprint_id, created_by="scheduler"
    )
    if package["status"] != "ready_for_review":
        raise _GenerationFailed(
            _validation_summary(package, "The case package failed validation."),
            code="package_invalid",
            blueprint_id=blueprint_id,
            package_id=str(package["id"]),
        )
    return blueprint_id, str(package["id"])


def _validation_summary(view: dict, fallback: str) -> str:
    errors = list(view.get("validation_errors") or [])
    if not errors:
        return fallback
    first = errors[0]
    code = str(first.get("code") or "invalid")
    message = str(first.get("message") or "")
    return f"{fallback} [{code}] {message}".strip()[:500]


def _publish_and_open(
    db: Database, *, job_id: str, package_id: str, now: datetime
) -> dict:
    """Publish a validated package and open its session, atomically.

    Everything commits together under the game's lifecycle lock: the package
    status flip, the old session's end, the new session's start and the job's
    terminal update. The eligibility checks are re-run inside the lock, so a
    revocation, hide or supersession that happened while the model calls ran
    still wins and the cycle is abandoned without publishing a session.
    """
    from . import league_finalization, plot_twist_validation, case_closed_validation

    job = get_job(db, job_id)
    if job is None:
        raise _GenerationAborted("The generation job no longer exists.", code="job_missing")
    game_id = job["game_id"]
    registry = _AI_GAME_REGISTRY[game_id]
    model = registry["session"]
    package_model = registry["package"]
    blueprint_model = registry["blueprint"]
    validator = (
        plot_twist_validation.validate_package
        if game_id == "plot-twist"
        else case_closed_validation.validate_package
    )

    with db.sessions.begin() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": game_lock_key(game_id)},
        )
        job_row = session.get(GameGenerationJobRecord, job_id, with_for_update=True)
        if job_row is None:
            raise _GenerationAborted("The generation job no longer exists.", code="job_missing")
        if job_row.status == "published":
            return {"game_id": game_id, "action": "already_published", "session_id": None}
        if job_row.status != "running":
            raise _GenerationAborted(
                "The generation cycle is no longer current.", code="job_not_running"
            )

        config = get_config(session, game_id)
        if config.is_hidden:
            raise _GenerationAborted(
                "The game was hidden while generation ran.", code="game_hidden"
            )
        revoked_until = _active_revocation_until(session, game_id, now.date())
        if revoked_until is not None:
            raise _GenerationAborted(
                "The session was revoked while generation ran.", code="session_revoked"
            )

        package_row = session.get(package_model, package_id, with_for_update=True)
        if package_row is None:
            raise _GenerationFailed("The generated package no longer exists.", code="package_missing")
        if package_row.status == "published":
            # A previous attempt already published this exact package; finish
            # the job bookkeeping instead of publishing something else.
            _complete_job(session, job_row, package_row, now)
            return {"game_id": game_id, "action": "already_published", "session_id": None}

        blueprint_row = session.get(blueprint_model, str(package_row.blueprint_id))
        if blueprint_row is None or blueprint_row.status != "approved":
            raise _GenerationFailed(
                "An approved blueprint is required before publishing.",
                code="blueprint_not_approved",
                package_id=str(package_row.id),
            )
        report = validator(
            dict(package_row.package or {}), blueprint=dict(blueprint_row.blueprint or {})
        )
        if not report.ok:
            package_row.status = "validation_failed"
            package_row.validation_errors = [err.model_dump() for err in report.errors]
            raise _GenerationFailed(
                "The package failed validation at publication; nothing was published.",
                code="package_invalid",
                blueprint_id=str(blueprint_row.id),
                package_id=str(package_row.id),
            )

        # Validation passed: publish, close the old period and open the new
        # one for the window starting today under the game's own cadence.
        package_row.status = "published"
        package_row.published_at = now
        package_row.validation_errors = []
        for live in session.scalars(
            select(model).where(model.status.in_(("active", "pending")))
        ).all():
            _end_session(db, game_id, live, now)
        start, end = game_periods.period_for_start(now.date(), config.reset_cadence)
        opened = _open_session_row(
            game_id, package_row, start, end, now
        )
        session.add(opened)
        session.flush()
        opened_id = str(opened.id)
        league_finalization.snapshot_opened_safely(
            session, game_id=game_id, opportunity_id=opened_id
        )
        _complete_job(session, job_row, package_row, now)
        outcome = {
            "game_id": game_id,
            "action": "created",
            "session_id": opened_id,
            "package_id": str(package_row.id),
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }

    # The publication deliberately closed the previous period, so its league
    # awards settle now rather than waiting for the next due scan. Best
    # effort: the scan retries anything this misses.
    try:
        league_finalization.finalize_game_safely(db, game_id)
    except Exception:
        logger.warning("AI game publication could not finalize the ended period", exc_info=True)
    return outcome


def _open_session_row(game_id: str, package_row, start: date, end: date, now: datetime):
    """Build the session row pinned to a just-published package."""
    if game_id == "plot-twist":
        return PlotTwistGameRecord(
            game_date=start,
            status="active",
            period_start_date=start,
            period_end_date=end,
            story_package_id=str(package_row.id),
            seed=uuid.uuid4().hex,
            started_at=now,
            created_by="scheduler",
        )
    package = dict(package_row.package or {})
    scoring = package.get("time_scoring_config") or {}
    return CaseClosedGameRecord(
        game_date=start,
        status="active",
        period_start_date=start,
        period_end_date=end,
        case_package_id=str(package_row.id),
        seed=uuid.uuid4().hex,
        scoring_half_life_seconds=int(scoring.get("half_life_seconds") or 43200),
        scoring_max_points=int(scoring.get("max_points") or 100),
        started_at=now,
        created_by="scheduler",
    )


def _complete_job(session, job_row, package_row, now: datetime) -> None:
    job_row.status = "published"
    job_row.package_id = str(package_row.id)
    job_row.blueprint_id = str(package_row.blueprint_id)
    job_row.finished_at = now
    job_row.heartbeat_at = now
    job_row.error_code = None
    job_row.last_error = None


def _fail_or_retry(db: Database, job_id: str, detail: str, moment: datetime) -> dict:
    """Record one failed attempt: bounded backoff retry, or park as failed."""
    with db.sessions.begin() as session:
        row = session.get(GameGenerationJobRecord, job_id, with_for_update=True)
        if row is None or row.status != "running":
            return {"action": "not_running"}
        row.heartbeat_at = moment
        if int(row.attempt or 0) < int(row.max_attempts or 1):
            backoff = AI_GENERATION_RETRY_BACKOFF_SECONDS * max(1, int(row.attempt or 1))
            row.status = "retry_wait"
            row.next_attempt_at = moment + timedelta(seconds=backoff)
            row.error_code = "attempt_failed"
            row.last_error = detail
            return {"action": "retry_wait", "retry_in_seconds": backoff}
        row.status = "failed"
        row.finished_at = moment
        row.error_code = "attempts_exhausted"
        row.last_error = detail
        return {"action": "failed"}


def _finish_job(
    db: Database,
    job_id: str,
    *,
    status: str,
    error_code: Optional[str],
    error: Optional[str],
    now: datetime,
) -> None:
    with db.sessions.begin() as session:
        row = session.get(GameGenerationJobRecord, job_id, with_for_update=True)
        if row is None or row.status in _TERMINAL_STATUSES:
            return
        row.status = status
        row.finished_at = now
        row.heartbeat_at = now
        row.error_code = error_code
        row.last_error = error


def _error_detail(exc: Exception) -> str:
    """A safe, bounded diagnostic for storage and logs.

    Provider errors can echo request material; anything resembling a
    credential marker is replaced, and the stored text is clipped so a huge
    traceback can never bloat the job row.
    """
    raw = str(getattr(exc, "detail", exc) or type(exc).__name__)
    lowered = raw.lower()
    for marker in ("bearer ", "api-key", "apikey", "authorization", "secret"):
        if marker in lowered:
            return "Generation failed. Check the provider configuration and retry."
    return raw.strip()[:500]


# ---------------------------------------------------------------------------
# Read surfaces for the admin consoles
# ---------------------------------------------------------------------------


def get_job(db: Database, job_id: str) -> Optional[dict]:
    with db.sessions() as session:
        row = session.get(GameGenerationJobRecord, job_id)
        return _job_view(row) if row is not None else None


def latest_generation_job(db: Database, game_id: str) -> Optional[dict]:
    """The newest generation job for ``game_id``, whatever its state."""
    with db.sessions() as session:
        row = session.scalars(
            select(GameGenerationJobRecord)
            .where(GameGenerationJobRecord.game_id == game_id)
            .order_by(GameGenerationJobRecord.requested_at.desc())
            .limit(1)
        ).first()
        return _job_view(row) if row is not None else None


def _job_view(row) -> dict:
    return {
        "id": str(row.id),
        "game_id": row.game_id,
        "cycle_start_date": row.cycle_start_date.isoformat() if row.cycle_start_date else None,
        "cycle_end_date": row.cycle_end_date.isoformat() if row.cycle_end_date else None,
        "difficulty": row.difficulty,
        "status": row.status,
        "attempt": int(row.attempt or 0),
        "max_attempts": int(row.max_attempts or 0),
        "requested_at": row.requested_at,
        "started_at": row.started_at,
        "finished_at": row.finished_at,
        "next_attempt_at": row.next_attempt_at,
        "blueprint_id": str(row.blueprint_id) if row.blueprint_id else None,
        "package_id": str(row.package_id) if row.package_id else None,
        "error_code": row.error_code,
        "last_error": row.last_error,
        "created_by": row.created_by,
    }


def current_session_overview(db: Database, game_id: str) -> dict:
    """The active session's inspectable content plus the latest generation state.

    ``session`` is the real published-session row pinned to its package --
    never a blueprint or an unpublished draft. When none is active (generation
    in flight, revoked, failed, or the very first cycle) it is ``None`` and
    ``generation`` carries the simple status or last error an admin needs to
    understand why.
    """
    _validate_ai_game(game_id)
    registry = _AI_GAME_REGISTRY[game_id]
    model = registry["session"]
    package_model = registry["package"]
    package_field = registry["package_field"]
    with db.sessions() as session:
        current = _current_row(session, model)
        summary: Optional[dict] = None
        if current is not None:
            package_id = getattr(current, package_field)
            package_row = (
                session.get(package_model, str(package_id)) if package_id else None
            )
            summary = _session_summary(game_id, current, package_row)
        generation = latest_generation_job(db, game_id)
    return {"game_id": game_id, "session": summary, "generation": generation}


def _session_summary(game_id: str, row, package_row) -> dict:
    package = dict(package_row.package or {}) if package_row is not None else {}
    summary = {
        "id": str(row.id),
        "status": row.status,
        "period_start_date": row.period_start_date.isoformat() if row.period_start_date else None,
        "period_end_date": row.period_end_date.isoformat() if row.period_end_date else None,
        "package_id": (
            str(getattr(row, "story_package_id" if game_id == "plot-twist" else "case_package_id"))
            if getattr(row, "story_package_id" if game_id == "plot-twist" else "case_package_id")
            else None
        ),
        "title": package_row.title if package_row is not None else None,
        "package": package,
        "created_by": row.created_by,
        "started_at": row.started_at,
        "ended_at": row.ended_at,
        "created_at": row.created_at,
    }
    if game_id == "case-closed":
        summary["finalized"] = bool(row.finalized)
        summary["finalized_at"] = row.finalized_at
        summary["scoring_half_life_seconds"] = row.scoring_half_life_seconds
        summary["scoring_max_points"] = row.scoring_max_points
    return summary


def list_sessions(db: Database, game_id: str, *, skip: int = 0, limit: int = 50) -> dict:
    """One backend page of session history, newest first, with the total.

    The page is a SQL OFFSET/LIMIT; package JSON is never loaded for the
    list -- only titles -- so navigating history stays cheap however long the
    game has been running. Revoked rows are flagged from the audit table.
    """
    _validate_ai_game(game_id)
    registry = _AI_GAME_REGISTRY[game_id]
    model = registry["session"]
    package_model = registry["package"]
    package_field = registry["package_field"]
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or AI_GENERATION_HISTORY_DEFAULT_LIMIT), AI_GENERATION_HISTORY_MAX_LIMIT))

    with db.sessions() as session:
        total = int(session.scalar(select(func.count()).select_from(model)) or 0)
        rows = session.scalars(
            select(model)
            .order_by(model.created_at.desc(), model.id.desc())
            .offset(skip)
            .limit(limit)
        ).all()
        package_ids = [
            str(getattr(row, package_field))
            for row in rows
            if getattr(row, package_field)
        ]
        titles: dict[str, str] = {}
        if package_ids:
            titles = {
                str(package_id): title
                for package_id, title in session.execute(
                    select(package_model.id, package_model.title).where(
                        package_model.id.in_(package_ids)
                    )
                ).all()
            }
        session_ids = [str(row.id) for row in rows]
        revoked_ids: set[str] = set()
        if session_ids:
            revoked_ids = {
                str(session_id)
                for (session_id,) in session.execute(
                    select(GameSessionRevocationRecord.session_id).where(
                        GameSessionRevocationRecord.session_id.in_(session_ids)
                    )
                ).all()
            }
        entries = []
        for row in rows:
            package_id = getattr(row, package_field)
            entry = {
                "id": str(row.id),
                "status": row.status,
                "period_start_date": row.period_start_date.isoformat() if row.period_start_date else None,
                "period_end_date": row.period_end_date.isoformat() if row.period_end_date else None,
                "package_id": str(package_id) if package_id else None,
                "title": titles.get(str(package_id)) if package_id else None,
                "revoked": str(row.id) in revoked_ids,
                "created_by": row.created_by,
                "started_at": row.started_at,
                "ended_at": row.ended_at,
                "created_at": row.created_at,
            }
            if game_id == "case-closed":
                entry["finalized"] = bool(row.finalized)
            entries.append(entry)
        return {"sessions": entries, "total": total, "skip": skip, "limit": limit}


# ---------------------------------------------------------------------------
# In-process worker thread
# ---------------------------------------------------------------------------


class GenerationWorker:
    """A daemon thread that drains the generation queue one pass at a time.

    Safe to run in every process: the shared pipeline lock and the
    ``SKIP LOCKED`` claim mean extra threads poll cheaply and can never start
    a second concurrent generation. Tests disable it
    (``AI_GENERATION_WORKER_ENABLED``) and drive ``process_due_jobs``
    directly.
    """

    def __init__(self, db: Database, *, poll_seconds: Optional[float] = None) -> None:
        self._db = db
        self._poll_seconds = float(
            poll_seconds
            if poll_seconds is not None
            else settings.AI_GENERATION_POLL_SECONDS
        )
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name="ai-generation-worker", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                process_due_jobs(self._db, max_jobs=2)
            except Exception:
                logger.exception("AI generation worker pass failed; retrying next interval")
            if self._stop.wait(self._poll_seconds):
                break

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
