"""Automated, schedule-driven session generation for Plot Twist and Case Closed.

The two AI games ride the existing 00:01 UTC scheduler, but their sessions come
from an asynchronous pipeline: the boundary tick closes the expired session and
enqueues one generation cycle; a background worker claims it under a single
shared advisory lock, runs blueprint -> approve -> package -> validate, and
opens the playable session only when the validated package is published. These
tests pin the properties a careless implementation would get quietly wrong:

* one cycle per game per boundary, however many times the scheduler runs;
* the full lifecycle runs unattended, and content only exists after publication;
* Plot Twist and Case Closed never generate concurrently -- not even when two
  workers race the same queue;
* a provider failure retries with backoff, then parks without blocking the
  other game or the next cycle;
* revoking ends the live session, cancels in-flight generation and suppresses
  replacement until the next eligible scheduled cycle -- and a stale job can
  never publish over a revocation;
* hiding a game cancels publication and enqueues nothing;
* difficulty and cadence are snapshotted per cycle, so mid-flight changes apply
  from the next session;
* the admin console sees the published story/choices/result through paginated
  backend reads, and never a blueprint or draft.
"""

import copy
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.db_models import (
    CaseClosedBlueprintRecord,
    CaseClosedGameRecord,
    CaseClosedPackageRecord,
    GameGenerationJobRecord,
    GameSessionRevocationRecord,
    PlotTwistBlueprintRecord,
    PlotTwistGameRecord,
    PlotTwistPackageRecord,
)
from app.limits import AI_GENERATION_LOCK_KEY
from app.services import ai_game_automation, game_configs, game_scheduler
from test_case_closed import valid_blueprint as case_closed_blueprint
from test_case_closed import valid_package as case_closed_package
from test_plot_twist import BLUEPRINT as plot_twist_blueprint
from test_plot_twist import story_package as plot_twist_package

UTC = timezone.utc
NOW = datetime(2026, 10, 19, 0, 1, tzinfo=UTC)

# game_id -> (blueprint payload builder, package payload builder, session model,
# package model, blueprint model, pinned-package column, schema-name prefix)
GAMES = {
    "plot-twist": {
        "blueprint": lambda: copy.deepcopy(plot_twist_blueprint),
        "package": plot_twist_package,
        "session_model": PlotTwistGameRecord,
        "package_model": PlotTwistPackageRecord,
        "blueprint_model": PlotTwistBlueprintRecord,
        "package_field": "story_package_id",
        "prefix": "plot_twist",
    },
    "case-closed": {
        "blueprint": case_closed_blueprint,
        "package": case_closed_package,
        "session_model": CaseClosedGameRecord,
        "package_model": CaseClosedPackageRecord,
        "blueprint_model": CaseClosedBlueprintRecord,
        "package_field": "case_package_id",
        "prefix": "case_closed",
    },
}


def mock_provider(monkeypatch, game_id, *, on_call=None):
    """Answer the shared LLM client with valid two-stage payloads for one game.

    Blueprint calls return the valid blueprint, batch calls an empty batch (the
    fixture stories need no interchangeable filler), and every other call the
    valid package -- so a mocked cycle exercises the real prompts, schemas,
    validation and repair bounds while never touching a provider.
    """
    from app.services import llm_client

    spec = GAMES[game_id]

    def fake(**kwargs):
        name = str(kwargs.get("schema_name") or "")
        if on_call is not None:
            on_call(name)
        if name.endswith("_batch"):
            payload = {"scenes": []}
        elif name.endswith("_blueprint"):
            payload = spec["blueprint"]()
        else:
            payload = spec["package"]()
        return payload, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake)
    return fake


def jobs_for(db, game_id):
    with db.sessions() as session:
        return list(
            session.scalars(
                select(GameGenerationJobRecord)
                .where(GameGenerationJobRecord.game_id == game_id)
                .order_by(GameGenerationJobRecord.created_at)
            ).all()
        )


def sessions_for(db, game_id):
    spec = GAMES[game_id]
    with db.sessions() as session:
        return list(session.scalars(select(spec["session_model"])))


def live_session(db, game_id):
    return next((row for row in sessions_for(db, game_id) if row.status == "active"), None)


def queue_cycle(db, game_id, now=NOW):
    outcome = game_scheduler.ensure_single_game(db, game_id, now)
    assert outcome["action"] in ("generation_queued", "generation_requeued"), outcome
    return outcome


def run_cycle(db, game_id, now=NOW):
    queue_cycle(db, game_id, now)
    return ai_game_automation.process_due_jobs(db, now, max_jobs=1)


# --- scheduling: one cycle per boundary, generation is asynchronous ----------


def test_the_tick_enqueues_one_cycle_without_opening_a_session(db):
    """00:01 UTC is a checkpoint: a due AI game queues generation and returns."""
    outcome = game_scheduler.ensure_single_game(db, "plot-twist", NOW)
    assert outcome["action"] == "generation_queued"

    rows = jobs_for(db, "plot-twist")
    assert len(rows) == 1
    job = rows[0]
    assert job.status == "queued" and job.attempt == 0
    assert job.cycle_start_date == NOW.date()
    assert job.cycle_end_date == NOW.date() + timedelta(days=1)  # default daily
    # Nothing playable exists before the content is generated and published.
    assert live_session(db, "plot-twist") is None


def test_a_repeated_tick_finds_the_live_cycle_instead_of_duplicating_it(db):
    queue_cycle(db, "plot-twist", NOW)
    again = game_scheduler.ensure_single_game(db, "plot-twist", NOW + timedelta(minutes=30))
    assert again["action"] == "generating"
    assert len(jobs_for(db, "plot-twist")) == 1


def test_generation_completes_asynchronously_and_only_then_opens_the_session(db, monkeypatch):
    """The whole lifecycle runs without an admin: blueprint -> approve -> package
    -> validate -> publish -> playable session."""
    mock_provider(monkeypatch, "plot-twist")
    queue_cycle(db, "plot-twist", NOW)

    # Still nothing playable the moment the tick returned.
    assert live_session(db, "plot-twist") is None

    result = ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    assert [entry["action"] for entry in result["processed"]] == ["published"]
    # Publication can land later than 00:01: process the queue at a different
    # wall-clock moment and the session still opens on the configured cadence.
    later = NOW + timedelta(hours=6)

    spec = GAMES["plot-twist"]
    row = live_session(db, "plot-twist")
    assert row is not None
    assert row.period_start_date == NOW.date()
    assert row.period_end_date == NOW.date() + timedelta(days=1)
    package_id = getattr(row, spec["package_field"])
    with db.sessions() as session:
        package = session.get(spec["package_model"], str(package_id))
        blueprint = session.get(spec["blueprint_model"], str(package.blueprint_id))
    assert package is not None and package.status == "published"
    assert blueprint is not None and blueprint.status == "approved"
    job = jobs_for(db, "plot-twist")[0]
    assert job.status == "published" and job.package_id == str(package_id)
    assert job.attempt == 1

    # A session now exists and is inside its period: the next tick keeps it.
    kept = game_scheduler.ensure_single_game(db, "plot-twist", later)
    assert kept["action"] == "current"
    assert len(jobs_for(db, "plot-twist")) == 1


@pytest.mark.parametrize("game_id", ["plot-twist", "case-closed"])
def test_each_ai_game_runs_its_own_full_lifecycle(db, monkeypatch, game_id):
    mock_provider(monkeypatch, game_id)
    run_cycle(db, game_id)
    row = live_session(db, game_id)
    assert row is not None and row.status == "active"
    assert row.created_by == "scheduler"
    assert jobs_for(db, game_id)[0].status == "published"


def test_the_configured_cadence_sets_the_cycle_window(db, monkeypatch):
    mock_provider(monkeypatch, "case-closed")
    game_configs.update_config(db, "case-closed", reset_cadence="every_3_days")
    queue_cycle(db, "case-closed", NOW)
    # The enqueued cycle itself carries the configured window; generation is
    # not tied to the wall-clock moment the worker gets to it.
    job = jobs_for(db, "case-closed")[0]
    assert job.cycle_end_date == NOW.date() + timedelta(days=3)

    ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    row = live_session(db, "case-closed")
    assert row is not None
    assert row.period_start_date == NOW.date()
    assert row.period_end_date == NOW.date() + timedelta(days=3)


# --- serialization across both games ----------------------------------------


def test_two_workers_never_generate_concurrently(db, monkeypatch):
    """Both games due at one checkpoint: queued work runs one workflow at a time.

    Two passes race the queue the way two uvicorn workers (or replicas) do.
    The shared advisory lock is what makes the second wait rather than start a
    concurrent provider call, and both cycles still publish.
    """
    from app.services import llm_client

    in_flight = {"count": 0, "peak": 0}
    guard = threading.Lock()

    def fake(**kwargs):
        name = str(kwargs.get("schema_name") or "")
        spec = GAMES["case-closed" if name.startswith("case_closed") else "plot-twist"]
        with guard:
            in_flight["count"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["count"])
        try:
            time.sleep(0.05)
            if name.endswith("_batch"):
                return {"scenes": []}, {"strict_schema": True}
            if name.endswith("_blueprint"):
                return spec["blueprint"](), {"strict_schema": True}
            return spec["package"](), {"strict_schema": True}
        finally:
            with guard:
                in_flight["count"] -= 1

    monkeypatch.setattr(llm_client, "generate_structured_json", fake)
    queue_cycle(db, "plot-twist", NOW)
    queue_cycle(db, "case-closed", NOW)

    results = []

    def pass_queue():
        results.append(ai_game_automation.process_due_jobs(db, NOW, max_jobs=1, wait_seconds=10))

    workers = [threading.Thread(target=pass_queue) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)

    published = {
        job.game_id for job in (jobs_for(db, "plot-twist") + jobs_for(db, "case-closed"))
        if job.status == "published"
    }
    assert published == {"plot-twist", "case-closed"}
    assert in_flight["peak"] == 1, "two workflows used the protected pipeline at once"


def test_a_held_pipeline_lock_leaves_the_cycle_queued(db, monkeypatch):
    """The lock is shared infrastructure, not an in-memory flag: an external
    holder (another process) keeps the worker out, and the job stays queued."""
    mock_provider(monkeypatch, "plot-twist")
    queue_cycle(db, "plot-twist", NOW)

    with db.engine.connect() as connection:
        assert connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": AI_GENERATION_LOCK_KEY}
        ).scalar() is True
        try:
            blocked = ai_game_automation.process_due_jobs(db, NOW, wait_seconds=0)
            assert blocked["action"] == "busy"
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": AI_GENERATION_LOCK_KEY}
            )

    assert jobs_for(db, "plot-twist")[0].status == "queued"
    assert live_session(db, "plot-twist") is None

    winner = ai_game_automation.process_due_jobs(db, NOW, max_jobs=1, wait_seconds=1)
    assert [entry["action"] for entry in winner["processed"]] == ["published"]


def test_a_failure_does_not_block_the_other_game_or_the_queue(db, monkeypatch):
    from app.services import llm_client

    def fake(**kwargs):
        name = str(kwargs.get("schema_name") or "")
        if name.startswith("plot_twist"):
            raise RuntimeError("provider exploded")
        if name.endswith("_batch"):
            return {"scenes": []}, {"strict_schema": True}
        if name.endswith("_blueprint"):
            return case_closed_blueprint(), {"strict_schema": True}
        return case_closed_package(), {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake)
    queue_cycle(db, "plot-twist", NOW)
    queue_cycle(db, "case-closed", NOW)

    result = ai_game_automation.process_due_jobs(db, NOW, max_jobs=2)
    actions = sorted(entry["action"] for entry in result["processed"])
    assert actions == ["published", "retry_wait"]

    assert jobs_for(db, "plot-twist")[0].status == "retry_wait"
    assert jobs_for(db, "plot-twist")[0].last_error
    assert live_session(db, "case-closed") is not None


def test_provider_failure_recovers_on_the_bounded_retry(db, monkeypatch):
    from app.services import llm_client

    calls = {"n": 0}

    def fake(**kwargs):
        name = str(kwargs.get("schema_name") or "")
        if name.endswith("_blueprint"):
            return plot_twist_blueprint, {"strict_schema": True}
        if name.endswith("_batch"):
            return {"scenes": []}, {"strict_schema": True}
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("provider hiccup")
        return plot_twist_package(), {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake)

    queue_cycle(db, "plot-twist", NOW)
    first = ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    assert [entry["action"] for entry in first["processed"]] == ["retry_wait"]
    job = jobs_for(db, "plot-twist")[0]
    assert job.status == "retry_wait" and job.next_attempt_at is not None
    assert live_session(db, "plot-twist") is None

    # The backoff has elapsed by the next pass: the retry publishes.
    with db.sessions.begin() as session:
        row = session.get(GameGenerationJobRecord, str(job.id))
        row.next_attempt_at = NOW - timedelta(minutes=1)
    second = ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    assert [entry["action"] for entry in second["processed"]] == ["published"]
    assert live_session(db, "plot-twist") is not None


def test_exhausted_attempts_park_the_cycle_and_the_next_day_starts_fresh(db, monkeypatch):
    from app.services import llm_client

    def always_fail(**kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_client, "generate_structured_json", always_fail)
    queue_cycle(db, "plot-twist", NOW)

    for _ in range(3):
        with db.sessions.begin() as session:
            row = session.scalars(select(GameGenerationJobRecord)).first()
            row.next_attempt_at = NOW - timedelta(minutes=1)
        ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)

    job = jobs_for(db, "plot-twist")[0]
    assert job.status == "failed" and job.attempt == job.max_attempts
    assert job.last_error

    # Re-running the scheduler for the same boundary must not start a second
    # generation loop for a cycle whose attempts are spent.
    refused = game_scheduler.ensure_single_game(db, "plot-twist", NOW)
    assert refused["action"] == "generation_failed"
    assert len(jobs_for(db, "plot-twist")) == 1

    # The next scheduled cycle starts clean and succeessfully.
    mock_provider(monkeypatch, "plot-twist")
    next_day = game_scheduler.ensure_single_game(db, "plot-twist", NOW + timedelta(days=1))
    assert next_day["action"] == "generation_queued"
    result = ai_game_automation.process_due_jobs(db, NOW + timedelta(days=1), max_jobs=1)
    assert [entry["action"] for entry in result["processed"]] == ["published"]


def test_an_abandoned_job_is_reclaimed_after_the_stale_window(db, monkeypatch):
    """A worker crash between claim and publish must not wedge the queue."""
    mock_provider(monkeypatch, "plot-twist")
    queue_cycle(db, "plot-twist", NOW)
    first = ai_game_automation.claim_next_job(db, NOW)
    assert first is not None and first["attempt"] == 1

    with db.sessions.begin() as session:
        row = session.get(GameGenerationJobRecord, first["id"])
        row.heartbeat_at = NOW - timedelta(hours=2)
        row.started_at = NOW - timedelta(hours=2)

    reclaimed = ai_game_automation.claim_next_job(db, NOW)
    assert reclaimed is not None and reclaimed["id"] == first["id"]
    assert reclaimed["attempt"] == 2
    outcome = ai_game_automation.run_claimed_job(db, reclaimed["id"], NOW)
    assert outcome["action"] == "published"
    assert len([row for row in sessions_for(db, "plot-twist") if row.status == "active"]) == 1


# --- revocation --------------------------------------------------------------


def test_revoke_ends_the_session_and_waits_for_the_next_cycle(db, monkeypatch):
    mock_provider(monkeypatch, "plot-twist")
    run_cycle(db, "plot-twist", NOW)
    session_id = str(live_session(db, "plot-twist").id)

    revoked = ai_game_automation.revoke_current_session(
        db, "plot-twist", "adminuser", NOW + timedelta(hours=1)
    )
    assert revoked["action"] == "revoked"
    assert revoked["session_id"] == session_id
    assert revoked["revoked_until"] is not None

    with db.sessions() as session:
        row = session.get(PlotTwistGameRecord, session_id)
        assert row is not None and row.status == "ended" and row.ended_at is not None
    assert live_session(db, "plot-twist") is None

    # The same scheduling cycle must not replace it...
    held = game_scheduler.ensure_single_game(db, "plot-twist", NOW + timedelta(hours=6))
    assert held["action"] == "revoked_waiting"
    assert len(jobs_for(db, "plot-twist")) == 1

    # ...and a repeated revoke with nothing live is a safe no-op.
    assert ai_game_automation.revoke_current_session(
        db, "plot-twist", "adminuser", NOW + timedelta(hours=6)
    )["action"] == "none"

    # Once the next eligible scheduled cycle arrives, generation resumes.
    resumed = game_scheduler.ensure_single_game(db, "plot-twist", NOW + timedelta(days=1))
    assert resumed["action"] == "generation_queued"
    assert len(jobs_for(db, "plot-twist")) == 2


def test_a_revoked_session_stays_in_history_for_audit(db, monkeypatch):
    mock_provider(monkeypatch, "case-closed")
    run_cycle(db, "case-closed", NOW)
    session_id = str(live_session(db, "case-closed").id)
    ai_game_automation.revoke_current_session(db, "case-closed", "adminuser", NOW)

    page = ai_game_automation.list_sessions(db, "case-closed", skip=0, limit=10)
    entry = next(row for row in page["sessions"] if row["id"] == session_id)
    assert entry["status"] == "ended" and entry["revoked"] is True
    with db.sessions() as session:
        revocations = list(session.scalars(select(GameSessionRevocationRecord)))
    assert len(revocations) == 1 and revocations[0].revoked_by == "adminuser"


def test_revoking_during_generation_cancels_the_job_and_blocks_publication(db, monkeypatch):
    """An admin revoke while the model calls are in flight must win the race:
    the cycle is cancelled and no session is opened from it."""
    queue_cycle(db, "plot-twist", NOW)
    claimed = ai_game_automation.claim_next_job(db, NOW)
    assert claimed is not None

    def revoke_mid_generation(name):
        if revoke_mid_generation.done:
            return
        revoke_mid_generation.done = True
        revoked = ai_game_automation.revoke_current_session(db, "plot-twist", "adminuser", NOW)
        assert revoked["action"] == "revoked"

    revoke_mid_generation.done = False
    mock_provider(monkeypatch, "plot-twist", on_call=revoke_mid_generation)

    outcome = ai_game_automation.run_claimed_job(db, claimed["id"], NOW)
    assert outcome["action"] == "cancelled"

    job = jobs_for(db, "plot-twist")[0]
    assert job.status == "cancelled"
    assert job.error_code == "revoked"
    assert live_session(db, "plot-twist") is None
    with db.sessions() as session:
        packages = list(session.scalars(select(PlotTwistPackageRecord)))
    # Content may have been generated, but nothing was published as a session.
    assert all(package.status != "published" for package in packages)

    same_day = game_scheduler.ensure_single_game(db, "plot-twist", NOW + timedelta(hours=3))
    assert same_day["action"] == "revoked_waiting"
    assert len(jobs_for(db, "plot-twist")) == 1


# --- hide / disable ----------------------------------------------------------


def test_hidden_games_enqueue_nothing(db, monkeypatch):
    mock_provider(monkeypatch, "case-closed")
    game_configs.update_config(db, "case-closed", is_hidden=True)
    outcome = game_scheduler.ensure_single_game(db, "case-closed", NOW)
    assert outcome["action"] == "skipped_hidden"
    assert jobs_for(db, "case-closed") == []
    assert live_session(db, "case-closed") is None


def test_hiding_a_game_mid_generation_cancels_publication(db, monkeypatch):
    queue_cycle(db, "case-closed", NOW)

    def hide_mid_generation(name):
        if hide_mid_generation.done:
            return
        hide_mid_generation.done = True
        game_configs.update_config(db, "case-closed", is_hidden=True)

    hide_mid_generation.done = False
    mock_provider(monkeypatch, "case-closed", on_call=hide_mid_generation)

    result = ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    assert [entry["action"] for entry in result["processed"]] == ["cancelled"]
    job = jobs_for(db, "case-closed")[0]
    assert job.status == "cancelled" and job.error_code == "game_hidden"
    assert live_session(db, "case-closed") is None

    assert game_scheduler.ensure_single_game(db, "case-closed", NOW)["action"] == "skipped_hidden"
    game_configs.update_config(db, "case-closed", is_hidden=False)
    # The spent cycle is not silently regenerated the same day; the next
    # boundary is the next eligible cycle.
    state = game_scheduler.ensure_single_game(db, "case-closed", NOW)
    assert state["action"] == "generation_cancelled"


def test_disabling_a_game_keeps_the_session_lifecycle_running(db, monkeypatch):
    """Disable refuses play, not generation: the same semantics as every other
    game's enabled switch."""
    mock_provider(monkeypatch, "plot-twist")
    game_configs.update_config(db, "plot-twist", enabled=False)
    run_cycle(db, "plot-twist", NOW)
    assert live_session(db, "plot-twist") is not None


# --- difficulty snapshots ----------------------------------------------------


def test_difficulty_is_snapshotted_per_cycle(db, monkeypatch):
    mock_provider(monkeypatch, "plot-twist")
    game_configs.update_config(db, "plot-twist", difficulty="hard")
    queue_cycle(db, "plot-twist", NOW)
    assert jobs_for(db, "plot-twist")[0].difficulty == "hard"

    # A change while the cycle runs applies from the next cycle, never to the
    # content already being generated.
    game_configs.update_config(db, "plot-twist", difficulty="easy")
    ai_game_automation.process_due_jobs(db, NOW, max_jobs=1)
    job = jobs_for(db, "plot-twist")[0]
    assert job.difficulty == "hard"
    with db.sessions() as session:
        blueprint = session.get(PlotTwistBlueprintRecord, job.blueprint_id)
    assert blueprint is not None and blueprint.difficulty == "hard"


def test_difficulty_is_refused_for_other_games_and_unknown_values(db):
    import fastapi

    with pytest.raises(fastapi.HTTPException):
        game_configs.update_config(db, "wordle", difficulty="hard")
    with pytest.raises(fastapi.HTTPException):
        game_configs.update_config(db, "plot-twist", difficulty="impossible")


# --- admin surfaces ----------------------------------------------------------


def test_admin_reads_the_published_content_and_can_revoke(db, admin_client, monkeypatch):
    mock_provider(monkeypatch, "case-closed")
    run_cycle(db, "case-closed", NOW)

    overview = admin_client.get("/admin/case-closed/session")
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["generation"]["status"] == "published"
    session = body["session"]
    assert session is not None and session["status"] == "active"
    package = session["package"]
    # The published scenario, its choices (leads) and the hidden solution are
    # all inspectable; nothing here is a blueprint or an unpublished draft.
    assert package["opening_narrative"]
    assert package["lead_nodes"] and any(lead["next_lead_ids"] for lead in package["lead_nodes"])
    assert package["solution"]["culprit_id"] == "s1"

    history = admin_client.get("/admin/case-closed/sessions", params={"limit": 1}).json()
    assert history["total"] == 1 and len(history["sessions"]) == 1
    assert history["sessions"][0]["id"] == session["id"]
    assert history["sessions"][0]["finalized"] is False

    revoked = admin_client.post("/admin/case-closed/session/revoke").json()
    assert revoked["action"] == "revoked"
    after = admin_client.get("/admin/case-closed/session").json()
    assert after["session"] is None
    assert after["generation"]["status"] == "published"


def test_plot_twist_admin_sees_the_story_choices_and_outcome(db, admin_client, monkeypatch):
    mock_provider(monkeypatch, "plot-twist")
    run_cycle(db, "plot-twist", NOW)

    body = admin_client.get("/admin/plot-twist/session").json()
    session = body["session"]
    assert session is not None
    package = session["package"]
    assert package["title"] == "Locked In"
    assert package["scenes"], "the story scenes are inspectable"
    assert any(scene["choices"] for scene in package["scenes"]), "the choices are inspectable"
    assert package["endings"], "the outcomes are inspectable"


def test_empty_state_reports_generation_status_not_a_removed_workflow(db, admin_client):
    body = admin_client.get("/admin/plot-twist/session").json()
    assert body["session"] is None
    assert body["generation"] is None

    game_scheduler.ensure_single_game(db, "plot-twist", NOW)
    body = admin_client.get("/admin/plot-twist/session").json()
    assert body["session"] is None
    assert body["generation"]["status"] == "queued"


def test_history_is_paginated_by_the_backend(db, admin_client):
    """Twenty sessions must cost exactly the requested page, never the table."""
    spec = GAMES["plot-twist"]
    base = datetime(2026, 10, 1, tzinfo=UTC)
    with db.sessions.begin() as session:
        for index in range(20):
            session.add(
                spec["session_model"](
                    game_date=base.date(),
                    status="ended",
                    period_start_date=base.date(),
                    period_end_date=base.date() + timedelta(days=1),
                    seed=f"seed-{index}",
                    created_by="scheduler",
                    created_at=base + timedelta(hours=index),
                )
            )
    first = admin_client.get("/admin/plot-twist/sessions", params={"skip": 0, "limit": 5}).json()
    second = admin_client.get("/admin/plot-twist/sessions", params={"skip": 5, "limit": 5}).json()
    assert first["total"] == 20
    assert len(first["sessions"]) == 5 and len(second["sessions"]) == 5
    assert {row["id"] for row in first["sessions"]}.isdisjoint(row["id"] for row in second["sessions"])
    # Newest first: nothing on the second page may predate the first page's rump.
    assert first["sessions"][-1]["created_at"] >= second["sessions"][0]["created_at"]

    over = admin_client.get("/admin/plot-twist/sessions", params={"limit": 1000})
    assert over.status_code == 422


# --- the worker thread itself ------------------------------------------------


def test_the_worker_thread_drains_the_queue(db, monkeypatch):
    mock_provider(monkeypatch, "plot-twist")
    queue_cycle(db, "plot-twist", NOW)

    worker = ai_game_automation.GenerationWorker(db, poll_seconds=0.05)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if jobs_for(db, "plot-twist")[0].status == "published":
                break
            time.sleep(0.05)
    finally:
        worker.stop()

    assert jobs_for(db, "plot-twist")[0].status == "published"
    assert live_session(db, "plot-twist") is not None
