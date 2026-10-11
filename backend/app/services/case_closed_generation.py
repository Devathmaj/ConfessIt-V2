"""Case Closed two-stage AI generation pipeline, with bounded repair.

Stage A turns an administrator's configuration into a hidden blueprint
(truth of the mystery). Stage B turns the approved blueprint into a
playable investigation package (DAG of leads + evidence + solution).
Every stage validates deterministically; failures trigger a targeted
repair request, bounded by ``CASE_CLOSED_MAX_REPAIR_ATTEMPTS``. An invalid
package is never published.

Reuses the shared Plot Twist LLM connection
(``services.llm_client.generate_structured_json``): same provider,
credentials, model selection, strict JSON Schema + JSON-object fallback,
120s timeout. No independent client, key, base URL, or SDK.

Runtime gameplay never calls this module.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select

from ..logger import get_logger

logger = get_logger(__name__)

from ..limits import (
    CASE_CLOSED_LLM_TIMEOUT_SECONDS,
    CASE_CLOSED_MAX_REPAIR_ATTEMPTS,
)
from . import case_closed_validation
from .case_closed_schemas import (
    CaseClosedGenerationConfig,
    blueprint_json_schema,
    package_json_schema,
)


def _now():
    return datetime.now(timezone.utc)


def _sanitized_detail(exc: Exception) -> str:
    raw = str(getattr(exc, "detail", exc) or "Case generation failed.")
    lowered = raw.lower()
    for marker in ("bearer ", "api-key", "apikey", "secret"):
        if marker in lowered:
            return "Case generation failed. Check provider configuration and retry."
    return raw[:500]


def _as_http(exc: Exception) -> HTTPException:
    from .llm_client import LLMError

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LLMError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_sanitized_detail(exc))
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY, detail="Case generation failed."
    )


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def _stage_a_system() -> str:
    return (
        "You design structured mystery blueprints for Case Closed, an asynchronous "
        "cooperative detective game for couples. Respond with a single JSON object "
        "honouring the provided schema and nothing else. Keep every id short, unique "
        "and machine-readable (lowercase letters, digits, dashes; never embed suspect "
        "names in lead-like ids). Define 3-5 suspects with backgrounds, what each "
        "actually did, and claims marked truthful or not. Name exactly one culprit "
        "who is distinguishable via the timeline, facts, and deduction chain. "
        "Timelines need unique ordering; facts need ids; the deduction chain must "
        "reference existing fact ids; required_fact_ids must exist. Red herrings "
        "need explanations. The case must be solvable within five investigations "
        "per partner."
    )


def _stage_a_user(config: CaseClosedGenerationConfig) -> str:
    lines = [
        f"Difficulty: {config.difficulty}.",
        f"Suspects wanted: {config.suspect_count}.",
        f"Target lead nodes for the later package: {config.target_lead_count}.",
    ]
    if config.theme:
        lines.append(f"Theme: {config.theme}.")
    if config.premise:
        lines.append(f"Premise: {config.premise}.")
    if config.tone:
        lines.append(f"Tone: {config.tone}.")
    lines.append(
        "Define title, opening narrative, incident, suspects, culprit, motive, "
        "timeline, facts, contradictions, provable theories, red herrings, "
        "solution summary, reveal narrative, deduction chain, required facts, "
        "constraints. Example theme (do not copy unless fitting): a missing "
        "mascot costume from a locked auditorium with three present people."
    )
    return "\n".join(lines)


def _stage_b_system() -> str:
    return (
        "You write complete playable Case Closed investigation packages. Respond "
        "with a single JSON object honouring the provided schema and nothing else. "
        "`lead_nodes` holds 15-20 full lead objects (never ids): unique id/title, "
        "description, discovery_text grounded in the blueprint (no filler), "
        "revealed evidence, explicit next_lead_ids (max 3), required evidence, "
        "suspect refs, depth (initial leads depth 0, children strictly deeper). "
        "Exactly three initial_lead_ids. Convergent DAG reuse is wanted: different "
        "parents may point at the same existing lead; cycles forbidden; every "
        "edge must target an existing lead. Evidence objects need unique ids, "
        "titles, descriptions, kind (establishes_fact/contradicts_statement/"
        "supports_theory/misleading). Solution names the blueprint culprit, with "
        "motive, deduction chain referencing existing facts/evidence, and "
        "required evidence reachable from the graph. Keep "
        "investigation_budget_per_partner at 5: the couple gets five actions "
        "each. The complete evidence board must never be attainable -- spread "
        "revealed evidence so no ten investigations collect every reachable "
        "clue; give many clues their own single reveal lead so some clue stays "
        "hidden whatever sequence of choices the couple follows. Never invent "
        "facts that contradict the blueprint; never expose the culprit via ids "
        "or ordering."
    )


def _stage_b_user(blueprint: dict, config: CaseClosedGenerationConfig) -> str:
    import json as _json

    return (
        "Generate the complete playable package for this approved blueprint.\n\n"
        f"BLUEPRINT:\n{_json.dumps(blueprint)}\n\n"
        f"Difficulty: {config.difficulty}. "
        f"Aim for about {config.target_lead_count} lead nodes in a DAG "
        "(3 initial depth-0 leads, longest path at least 5 levels, up to 3 "
        "follow-ups per lead, shared convergent nodes). Self-check: valid refs, "
        "no cycles, all nodes reachable, depth consistent, solution evidence "
        "reachable, culprit matches blueprint, and the full evidence board "
        "unattainable within ten investigations (spread exclusive clues "
        "instead of clustering them)."
    )


def _minimized_blueprint(blueprint: dict) -> dict:
    return {
        "title": blueprint.get("title"),
        "difficulty": blueprint.get("difficulty"),
        "culprit_id": blueprint.get("culprit_id"),
        "suspects": [
            {"id": s.get("id"), "name": s.get("name")}
            for s in (blueprint.get("suspects") or [])
        ],
        "facts": [f.get("id") for f in (blueprint.get("facts") or [])],
        "required_fact_ids": list(blueprint.get("required_fact_ids") or []),
        "constraints": list(blueprint.get("constraints") or []),
    }


def _repair_user(
    invalid: dict,
    errors: list[dict],
    schema_hint: str,
    blueprint: Optional[dict] = None,
) -> str:
    import json as _json

    lines = [
        "The following content failed deterministic validation.",
        "Regenerate ONLY the affected parts, honouring the schema and every constraint below.",
        "",
        "INVALID CONTENT:",
        _json.dumps(invalid),
        "",
        "VALIDATION ERRORS:",
    ]
    for err in errors:
        lines.append(f"- [{err.get('code')}] {err.get('message')}")
    if blueprint:
        lines += ["", f"BLUEPRINT CONTRACT (essentials):\n{_json.dumps(_minimized_blueprint(blueprint))}"]
    lines += ["", f"SCHEMA AND CONSTRAINTS:\n{schema_hint}"]
    return "\n".join(lines)


def _config_dict(config: CaseClosedGenerationConfig) -> dict:
    return config.model_dump(exclude_none=False)


# ---------------------------------------------------------------------------
# Pure (DB-free) generation helpers — used by tests with a mocked LLM client
# ---------------------------------------------------------------------------


def generate_blueprint_content(
    config: CaseClosedGenerationConfig, llm_config=None
) -> tuple[dict, Any]:
    """Run Stage A without touching the database (repair-bounded)."""
    from .llm_client import generate_structured_json

    payload, meta = generate_structured_json(
        system=_stage_a_system(),
        user=_stage_a_user(config),
        json_schema=blueprint_json_schema(),
        schema_name="case_closed_blueprint",
        operation="case-blueprint",
        timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
        config=llm_config,
    )
    report = case_closed_validation.validate_blueprint(payload)
    attempts = 0
    while not report.ok and attempts < CASE_CLOSED_MAX_REPAIR_ATTEMPTS:
        attempts += 1
        payload, meta = generate_structured_json(
            system=_stage_a_system(),
            user=_repair_user(
                payload,
                [e.model_dump() for e in report.errors],
                "Regenerate the full blueprint object honouring its schema.",
            ),
            json_schema=blueprint_json_schema(),
            schema_name="case_closed_blueprint",
            operation=f"case-blueprint-repair-{attempts}",
            timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
            config=llm_config,
        )
        report = case_closed_validation.validate_blueprint(payload)
    return payload, report


def generate_package_content(
    blueprint: dict, config: CaseClosedGenerationConfig, llm_config=None
) -> tuple[dict, Any]:
    """Run Stage B without touching the database (repair-bounded)."""
    from .llm_client import generate_structured_json

    payload, meta = generate_structured_json(
        system=_stage_b_system(),
        user=_stage_b_user(blueprint, config),
        json_schema=package_json_schema(),
        schema_name="case_closed_package",
        operation="case-package",
        timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
        max_tokens=16000,
        config=llm_config,
    )
    payload["difficulty"] = config.difficulty
    payload.setdefault("case_id", f"case-{uuid.uuid4().hex[:8]}")
    report = case_closed_validation.validate_package(payload, blueprint=blueprint)
    attempts = 0
    while not report.ok and attempts < CASE_CLOSED_MAX_REPAIR_ATTEMPTS:
        attempts += 1
        repaired, meta = generate_structured_json(
            system=_stage_b_system(),
            user=_repair_user(
                payload,
                [e.model_dump() for e in report.errors],
                "Regenerate the full case package object honouring its schema. "
                "Every lead is a full object with id/title/description/"
                "discovery_text/evidence/next leads/depth.",
                blueprint=blueprint,
            ),
            json_schema=package_json_schema(),
            schema_name="case_closed_package",
            operation=f"case-package-repair-{attempts}",
            timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
            max_tokens=16000,
            config=llm_config,
        )
        repaired["difficulty"] = config.difficulty
        repaired.setdefault("case_id", payload.get("case_id"))
        payload = repaired
        report = case_closed_validation.validate_package(payload, blueprint=blueprint)
    return payload, report


# ---------------------------------------------------------------------------
# DB-backed admin workflow (requires Case Closed records from Step 4)
# ---------------------------------------------------------------------------


def _blueprint_view(row) -> dict:
    return {
        "id": str(row.id),
        "title": row.title,
        "difficulty": row.difficulty,
        "config": dict(row.config or {}),
        "blueprint": dict(row.blueprint or {}),
        "status": row.status,
        "validation_errors": list(row.validation_errors or []),
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _package_view(row) -> dict:
    return {
        "id": str(row.id),
        "blueprint_id": str(row.blueprint_id),
        "version": int(row.version),
        "title": row.title,
        "package": dict(row.package or {}),
        "status": row.status,
        "validation_errors": list(row.validation_errors or []),
        "published_at": row.published_at,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _records():
    from ..db_models import CaseClosedBlueprintRecord, CaseClosedPackageRecord

    return CaseClosedBlueprintRecord, CaseClosedPackageRecord


def create_blueprint_draft(db, config: CaseClosedGenerationConfig, created_by: str) -> dict:
    BlueprintRecord, _ = _records()
    with db.sessions.begin() as session:
        row = BlueprintRecord(
            title=(config.theme or "Untitled case"),
            difficulty=config.difficulty,
            config=_config_dict(config),
            blueprint={},
            status="draft",
            validation_errors=[],
            created_by=created_by,
        )
        session.add(row)
        session.flush()
        return _blueprint_view(row)


def get_blueprint(db, blueprint_id: str) -> dict:
    BlueprintRecord, _ = _records()
    with db.sessions() as session:
        row = session.get(BlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        return _blueprint_view(row)


def list_blueprints(db) -> list[dict]:
    BlueprintRecord, _ = _records()
    with db.sessions() as session:
        rows = session.scalars(
            select(BlueprintRecord).order_by(BlueprintRecord.created_at.desc())
        ).all()
        return [_blueprint_view(r) for r in rows]


def generate_blueprint(db, blueprint_id: str, llm_config=None) -> dict:
    BlueprintRecord, _ = _records()
    with db.sessions.begin() as session:
        row = session.get(BlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        try:
            config = CaseClosedGenerationConfig.model_validate(dict(row.config or {}))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stored generation config is invalid: {exc}",
            ) from exc
        row.status = "generating"
        session.flush()
        bid = str(row.id)

    try:
        from .llm_client import generate_structured_json

        # Stage A via the shared client (same contract as Plot Twist).
        with db.sessions() as session:
            row = session.get(BlueprintRecord, bid)
            assert row is not None
            config = CaseClosedGenerationConfig.model_validate(dict(row.config or {}))
        payload, _meta = generate_structured_json(
            system=_stage_a_system(),
            user=_stage_a_user(config),
            json_schema=blueprint_json_schema(),
            schema_name="case_closed_blueprint",
            operation="case-blueprint",
            timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
            config=llm_config,
        )
        report = case_closed_validation.validate_blueprint(payload)
        attempts = 0
        while not report.ok and attempts < CASE_CLOSED_MAX_REPAIR_ATTEMPTS:
            attempts += 1
            payload, _meta = generate_structured_json(
                system=_stage_a_system(),
                user=_repair_user(
                    payload,
                    [e.model_dump() for e in report.errors],
                    "Regenerate the full blueprint object honouring its schema.",
                ),
                json_schema=blueprint_json_schema(),
                schema_name="case_closed_blueprint",
                operation=f"case-blueprint-repair-{attempts}",
                timeout_s=CASE_CLOSED_LLM_TIMEOUT_SECONDS,
                config=llm_config,
            )
            report = case_closed_validation.validate_blueprint(payload)
    except Exception as exc:
        with db.sessions.begin() as session:
            row = session.get(BlueprintRecord, bid)
            if row is not None:
                row.status = "generation_failed"
                row.validation_errors = [
                    {"code": getattr(exc, "code", "model_error"), "message": _sanitized_detail(exc)}
                ]
        raise _as_http(exc)

    with db.sessions.begin() as session:
        row = session.get(BlueprintRecord, bid)
        assert row is not None
        if report.ok:
            row.blueprint = payload
            row.title = str(payload.get("title") or row.title)
            row.status = "ready_for_review"
            row.validation_errors = []
        else:
            row.blueprint = payload
            row.status = "generation_failed"
            row.validation_errors = [e.model_dump() for e in report.errors]
    return get_blueprint(db, bid)


def approve_blueprint(db, blueprint_id: str) -> dict:
    BlueprintRecord, _ = _records()
    with db.sessions() as session:
        row = session.get(BlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        content = dict(row.blueprint or {})
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Blueprint has no generated content to approve.",
        )
    report = case_closed_validation.validate_blueprint(content)
    if not report.ok:
        with db.sessions.begin() as session:
            row = session.get(BlueprintRecord, blueprint_id)
            assert row is not None
            row.status = "generation_failed"
            row.validation_errors = [e.model_dump() for e in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint is invalid; regenerate before approving.",
        )
    with db.sessions.begin() as session:
        row = session.get(BlueprintRecord, blueprint_id)
        assert row is not None
        row.status = "approved"
    return get_blueprint(db, blueprint_id)


def _next_package_version(session, blueprint_id: str) -> int:
    _, PackageRecord = _records()
    latest = session.scalar(
        select(func.max(PackageRecord.version)).where(PackageRecord.blueprint_id == blueprint_id)
    )
    return int(latest or 0) + 1


def get_package(db, package_id: str) -> dict:
    _, PackageRecord = _records()
    with db.sessions() as session:
        row = session.get(PackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case package not found.")
        return _package_view(row)


def list_packages(db, blueprint_id: Optional[str] = None) -> list[dict]:
    _, PackageRecord = _records()
    with db.sessions() as session:
        query = select(PackageRecord).order_by(PackageRecord.created_at.desc())
        if blueprint_id is not None:
            query = query.where(PackageRecord.blueprint_id == blueprint_id)
        return [_package_view(r) for r in session.scalars(query).all()]


def latest_published_package(db) -> Optional[dict]:
    _, PackageRecord = _records()
    with db.sessions() as session:
        row = session.scalar(
            select(PackageRecord)
            .where(PackageRecord.status == "published")
            .order_by(PackageRecord.published_at.desc())
            .limit(1)
        )
        return _package_view(row) if row is not None else None


def generate_package(db, blueprint_id: str, created_by: str, llm_config=None) -> dict:
    BlueprintRecord, PackageRecord = _records()
    with db.sessions() as session:
        brow = session.get(BlueprintRecord, blueprint_id)
        if brow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        if brow.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Blueprint must be approved before generating the case package.",
            )
        blueprint = dict(brow.blueprint or {})
        try:
            config = CaseClosedGenerationConfig.model_validate(dict(brow.config or {}))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stored generation config is invalid: {exc}",
            ) from exc

    try:
        payload, report = generate_package_content(blueprint, config, llm_config=llm_config)
        payload["blueprint_ref"] = blueprint_id
    except Exception as exc:
        raise _as_http(exc)

    with db.sessions.begin() as session:
        version = _next_package_version(session, blueprint_id)
        payload["version"] = version
        row = PackageRecord(
            blueprint_id=blueprint_id,
            version=version,
            title=str(payload.get("title") or "Untitled case"),
            package=payload,
            status="ready_for_review" if report.ok else "validation_failed",
            validation_errors=[] if report.ok else [e.model_dump() for e in report.errors],
            created_by=created_by,
        )
        session.add(row)
        session.flush()
        pid = str(row.id)
    return get_package(db, pid)


def publish_package(db, package_id: str) -> dict:
    _, PackageRecord = _records()
    with db.sessions() as session:
        row = session.get(PackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case package not found.")
        content = dict(row.package or {})
        blueprint_id = str(row.blueprint_id)
    # Re-validate; never publish invalid.
    blueprint = None
    try:
        BlueprintRecord, _ = _records()
        with db.sessions() as session:
            brow = session.get(BlueprintRecord, blueprint_id)
            blueprint = dict(brow.blueprint) if brow is not None else None
    except Exception:
        blueprint = None
    report = case_closed_validation.validate_package(content, blueprint=blueprint)
    if not report.ok:
        with db.sessions.begin() as session:
            row = session.get(PackageRecord, package_id)
            assert row is not None
            row.status = "validation_failed"
            row.validation_errors = [e.model_dump() for e in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Case package is invalid; regenerate before publishing.",
        )
    with db.sessions.begin() as session:
        row = session.get(PackageRecord, package_id)
        assert row is not None
        if row.status == "published":
            return _package_view(row)
        row.status = "published"
        row.published_at = _now()
        row.validation_errors = []
    return get_package(db, package_id)
