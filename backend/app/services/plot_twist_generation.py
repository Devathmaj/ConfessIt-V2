"""Plot Twist two-stage AI generation pipeline, with conditional repair.

Stage A turns an administrator's configuration (difficulty plus optional
overrides) into a structured blueprint. Stage B turns the approved
blueprint into a complete playable package, in batches for large stories.
Every stage validates deterministically; failures trigger a targeted
repair request (blueprint/scene + errors + schema), bounded by
``PLOT_TWIST_MAX_REPAIR_ATTEMPTS``. An invalid package is never published.

The blueprint and all approved content are persisted, and every model
request carries the relevant constraints explicitly -- nothing depends on
hidden conversation history.
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select

from ..logger import get_logger

logger = get_logger(__name__)

from ..db_models import PlotTwistBlueprintRecord, PlotTwistPackageRecord
from ..limits import (
    PLOT_TWIST_ENDING_TYPES,
    PLOT_TWIST_MAX_BATCH_SCENES,
    PLOT_TWIST_MAX_REPAIR_ATTEMPTS,
)
from ..postgres import Database
from . import plot_twist_validation
from .plot_twist_schemas import (
    GenerationConfig,
    blueprint_json_schema,
    package_json_schema,
    scene_batch_json_schema,
)


def _now():
    return datetime.now(timezone.utc)


def _as_view(row) -> dict:
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


def _config_dict(config: GenerationConfig) -> dict:
    return config.model_dump(exclude_none=False)


def _stage_a_system() -> str:
    return (
        "You design structured game blueprints for Plot Twist, an asynchronous "
        "cooperative storytelling game for couples. Respond with a single JSON "
        "object honouring the provided schema and nothing else. Keep every id "
        "short, unique and machine-readable (lowercase letters, digits, "
        "dashes). State variables must be bounded integers with "
        "min/max/initial, or flag collections holding string tokens "
        "(initial is a list of strings, never a number or boolean; flag "
        "values elsewhere are short lowercase strings such as 'silent' or "
        "'map', never 1/0/true/false). Endings need explicit "
        "machine-evaluable conditions over declared variables, with distinct "
        "priorities and mutually exclusive outcomes so different play styles "
        "reach different endings. Reward tiers must be one of participation, "
        "bronze, silver, gold. Two-sidedness is a hard rule, and the "
        "constraints you write must say so: turns strictly alternate, so the "
        "large majority of decision scenes belong to exactly one participant "
        "(participant_one or participant_two in alternation, never the same "
        "participant twice running); either/joint scenes are rare, genuinely "
        "shared moments only. The main path is the spine of the story: list "
        "its beats in order. Interchangeable slots are optional side "
        "variations only -- never steps of the main path, never reused for "
        "two different main beats. Also write these design constraints "
        "verbatim into constraints so Stage B must honour them: "
        "'main-path scenes use slot core only; opening and every terminal "
        "scene use slot core', 'every decision scene offers at least two "
        "choices leading to different next scenes (no unanimous convergence)', "
        "'choices are tradeoffs with real costs, never one obviously-correct "
        "good option versus an obviously-wrong bad option leading to the same "
        "place', 'every choice carries a specific consequence_text distinct "
        "within its scene', 'initial_state covers every declared variable'."
    )


def _stage_a_user(config: GenerationConfig) -> str:
    lines = [
        f"Difficulty: {config.difficulty}.",
        f"Decision points wanted: {config.decision_count}.",
        f"Interchangeable encounters wanted: {config.interchangeable_count}.",
        f"Maximum scenes: {config.max_scenes}.",
    ]
    if config.theme:
        lines.append(f"Theme: {config.theme}.")
    if config.premise:
        lines.append(f"Premise: {config.premise}.")
    if config.genre:
        lines.append(f"Genre: {config.genre}.")
    if config.tone:
        lines.append(f"Tone: {config.tone}.")
    lines.append(f"Permitted ending types: {', '.join(config.ending_types)}.")
    lines.append(
        "Treasure mechanics: "
        + ("allowed; include optional discoveries." if config.allow_treasure else "not wanted.")
    )
    lines.append(
        "Define the title, description, premise, narrative rules, characters, "
        "world details, initial state variables with ranges, the main "
        "narrative path as an ordered list of beats, interchangeable scene "
        "slots with compatibility rules, required modules, encounter "
        "categories, ending definitions with conditions and priorities, "
        "reward tiers, and constraints later scenes must follow. "
        "Slot discipline: the main path beats are always slot core; "
        "interchangeable slots each hold alternative versions of ONE side "
        "moment, never two sequential main beats sharing one slot. "
        "State discipline: every declared variable needs an initial within "
        "range (flag initials are lists of strings); ending conditions must "
        "be jointly achievable through play (e.g. trust thresholds within "
        "reach of the planned increments) and mutually exclusive across "
        "endings."
    )
    return "\n".join(lines)


def _stage_b_system() -> str:
    return (
        "You write complete playable Plot Twist story packages. Respond with "
        "a single JSON object honouring the provided schema and nothing "
        "else -- no prose, no summaries, no markdown. Shape rules that always "
        "hold: `scenes` is an array of full scene objects (never strings, "
        "never ids); every scene needs a unique id, unique title, unique "
        "narrative_text (2-4 vivid sentences grounded in the blueprint world, "
        "never generic filler such as 'A quiet moment where trust can deepen', "
        "never reused across scenes), type (opening, encounter, "
        "connector, discovery or ending), actor "
        "(participant_one, participant_two, either or joint), slot, "
        "compatibility tags, choices with safe state changes (set, "
        "increment, decrement, add_flag, remove_flag over declared variables "
        "only -- flag values are short lowercase strings, never numbers or "
        "booleans), and valid next_scene_id transitions naming another scene's "
        "id. Non-terminal scenes need at least two choices; terminal scenes "
        "carry an ending_id and no choices. `endings` is an array of full "
        "ending objects with id, type, title, narrative, conditions, "
        "priority, rewards, success and reward_tier. Never invent variables, "
        "tags, endings or tiers outside the blueprint and schema. "
        "Slot discipline is a hard rule: the opening scene and every terminal "
        "scene use slot core; every main-path decision scene uses slot core; "
        "a non-core slot holds only interchangeable alternatives for ONE side "
        "moment -- never two sequential story beats sharing one slot, never "
        "the opening. Two-sidedness "
        "is a hard rule: the large majority of decision scenes belong strictly "
        "to participant_one or participant_two in alternation along every path "
        "(never the same participant twice running); either/joint only for rare "
        "genuinely shared moments. Every loop of scenes must offer a choice "
        "leading out toward an ending, or the package will be rejected. "
        "Choice design is a hard rule and the most common rejection reason: "
        "every decision scene must branch -- its choices lead to DIFFERENT "
        "next_scene_id targets, never all converging on one scene. Choices "
        "are dilemmas with real tradeoffs (trust versus secrets, safety "
        "versus treasure, speed versus care), never a polar good-versus-bad "
        "pair where one option is conventionally correct ('share/open/solve/"
        "reassure/join' gaining trust) and the other is conventionally wrong "
        "('ignore/leave/give up/stay/reject' losing trust) leading to the same "
        "place. Same-target choices are rejected. Each choice needs its own "
        "specific consequence_text (what this choice costs and changes, "
        "distinct within the scene, never generic like 'You follow the trail, "
        "curiosity drives you'). State changes must differ across the choices "
        "of a scene and matter: gate later choices and endings on them, keep "
        "initial_state complete for every declared variable, and make each "
        "ending reachable by a distinct play style."
    )


def _stage_b_user(blueprint: dict, config: GenerationConfig) -> str:
    import json as _json

    return (
        "Generate the complete playable package for this approved blueprint "
        f"(story_id: {blueprint.get('title', 'plot-twist-story')}).\n\n"
        f"BLUEPRINT:\n{_json.dumps(blueprint)}\n\n"
        f"Difficulty: {config.difficulty}. "
        f"Aim for about {config.decision_count} decision points and "
        f"{config.interchangeable_count} interchangeable encounters within "
        f"{config.max_scenes} scenes. Include the opening scene (slot core), "
        "the core story path in slot core only, decision scenes with real "
        "branches, interchangeable modules as side alternatives (one moment "
        "per slot, never sequential beats sharing a slot), optional "
        "discoveries, connectors, specific consequence_text and divergent "
        "state changes for every choice, explicit achievable ending "
        "conditions, reward definitions and compatibility rules. "
        "Before returning, self-check: no two choices of one scene share a "
        "next_scene_id; no good-vs-bad polar pair leading to the same scene; "
        "no repeated titles or narrative_text; initial_state sets every "
        "declared variable (flags as string lists); flag values are strings; "
        "each ending is reachable by a different play style."
    )


def _minimized_blueprint(blueprint: dict) -> dict:
    """The contract essentials for follow-up requests, without the prose.

    Full blueprint narratives cost thousands of tokens per request against
    small provider budgets; follow-up calls need names, ranges, slots and
    rules -- not the prose they already generated from.
    """
    return {
        "title": blueprint.get("title"),
        "difficulty": blueprint.get("difficulty"),
        "state_vars": [
            {
                "name": var.get("name"),
                "kind": var.get("kind"),
                "min_value": var.get("min_value"),
                "max_value": var.get("max_value"),
                "initial": var.get("initial"),
            }
            for var in (blueprint.get("state_vars") or [])
        ],
        "slots": [
            {"name": slot.get("name"), "accepts_tags": slot.get("accepts_tags")}
            for slot in (blueprint.get("slots") or [])
        ],
        "endings": [
            {"id": ending.get("id"), "type": ending.get("type")}
            for ending in (blueprint.get("endings") or [])
        ],
        "constraints": list(blueprint.get("constraints") or []),
    }


def _batch_user(blueprint: dict, slot: str, existing_summary: str) -> str:
    import json as _json

    return (
        f"Generate interchangeable scenes for slot {slot!r} of this approved "
        "blueprint. These are alternative versions of ONE side moment, not "
        "sequential story beats: they must be mutually substitutable, with "
        "unique titles and narrative_text that do not duplicate any existing "
        "scene. Return a single JSON object with the slot name and its "
        "scenes, where every scene is a full scene object (id, type, title, "
        "narrative_text, actor, slot, tags, choices with valid "
        "next_scene_id transitions and divergent targets) -- never a string "
        "or an id. Each scene still branches (choices lead to different next "
        "scenes) and still offers tradeoffs, never a polar good-vs-bad pair "
        "converging on one scene.\n\n"
        f"BLUEPRINT (essentials):\n{_json.dumps(_minimized_blueprint(blueprint))}\n\n"
        f"EXISTING SCENES (summaries, do not duplicate):\n{existing_summary}"
    )


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


def create_blueprint_draft(
    db: Database, config: GenerationConfig, created_by: str
) -> dict:
    """Persist a draft blueprint row from the admin configuration."""
    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title=(config.theme or "Untitled story"),
            difficulty=config.difficulty,
            config=_config_dict(config),
            blueprint={},
            status="draft",
            validation_errors=[],
            created_by=created_by,
        )
        session.add(row)
        session.flush()
        return _as_view(row)


def get_blueprint(db: Database, blueprint_id: str) -> dict:
    with db.sessions() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        return _as_view(row)


def list_blueprints(db: Database) -> list[dict]:
    with db.sessions() as session:
        rows = session.scalars(
            select(PlotTwistBlueprintRecord).order_by(PlotTwistBlueprintRecord.created_at.desc())
        ).all()
        return [_as_view(row) for row in rows]


def generate_blueprint(
    db: Database, blueprint_id: str, llm_config=None
) -> dict:
    """Run Stage A for a draft blueprint row, with bounded repair."""
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import LLMError, generate_structured_json

    with db.sessions.begin() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        try:
            config = GenerationConfig.model_validate(dict(row.config or {}))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stored generation config is invalid: {exc}",
            ) from exc
        row.status = "generating"
        session.flush()
        blueprint_id_str = str(row.id)

    try:
        payload, _meta = generate_structured_json(
            system=_stage_a_system(),
            user=_stage_a_user(config),
            json_schema=blueprint_json_schema(),
            schema_name="plot_twist_blueprint",
            operation="blueprint",
            timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
            config=llm_config,
        )
    except Exception as exc:
        _mark_blueprint_failed(db, blueprint_id_str, exc)
        raise _as_http(exc)

    report = plot_twist_validation.validate_blueprint(payload)
    attempts = 0
    while not report.ok and attempts < PLOT_TWIST_MAX_REPAIR_ATTEMPTS:
        attempts += 1
        try:
            payload, _meta = generate_structured_json(
                system=_stage_a_system(),
                user=_repair_user(
                    payload,
                    [err.model_dump() for err in report.errors],
                    "Regenerate the full blueprint object honouring its schema.",
                ),
                json_schema=blueprint_json_schema(),
                schema_name="plot_twist_blueprint",
                operation=f"blueprint-repair-{attempts}",
                timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
                config=llm_config,
            )
        except Exception as exc:
            _mark_blueprint_failed(db, blueprint_id_str, exc)
            raise _as_http(exc)
        report = plot_twist_validation.validate_blueprint(payload)

    with db.sessions.begin() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id_str)
        assert row is not None
        if report.ok:
            row.blueprint = payload
            row.title = str(payload.get("title") or row.title)
            row.status = "ready_for_review"
            row.validation_errors = []
        else:
            row.blueprint = payload
            row.status = "generation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
    return get_blueprint(db, blueprint_id_str)


def approve_blueprint(db: Database, blueprint_id: str) -> dict:
    """Move a validated blueprint to approved so Stage B may run."""
    with db.sessions() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        content = dict(row.blueprint or {})
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Blueprint has no generated content to approve.",
        )
    report = plot_twist_validation.validate_blueprint(content)
    if not report.ok:
        # Diagnostics commit in their own transaction: raising inside the
        # write would roll them back.
        with db.sessions.begin() as session:
            row = session.get(PlotTwistBlueprintRecord, blueprint_id)
            assert row is not None
            row.status = "generation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint is invalid; regenerate before approving.",
        )
    with db.sessions.begin() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        assert row is not None
        row.status = "approved"
    return get_blueprint(db, blueprint_id)


def _mark_blueprint_failed(db: Database, blueprint_id: str, exc: Exception) -> None:
    detail = getattr(exc, "detail", str(exc))
    with db.sessions.begin() as session:
        row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        if row is not None:
            row.status = "generation_failed"
            row.validation_errors = [{"code": getattr(exc, "code", "model_error"), "message": detail}]


def _as_http(exc: Exception) -> HTTPException:
    from .llm_client import LLMError

    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, LLMError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Story generation failed.")


def _next_package_version(session, blueprint_id: str) -> int:
    latest = session.scalar(
        select(func.max(PlotTwistPackageRecord.version)).where(
            PlotTwistPackageRecord.blueprint_id == blueprint_id
        )
    )
    return int(latest or 0) + 1


def get_package(db: Database, package_id: str) -> dict:
    with db.sessions() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        return _package_view(row)


def list_packages(db: Database, blueprint_id: Optional[str] = None) -> list[dict]:
    with db.sessions() as session:
        query = select(PlotTwistPackageRecord).order_by(
            PlotTwistPackageRecord.created_at.desc()
        )
        if blueprint_id is not None:
            query = query.where(PlotTwistPackageRecord.blueprint_id == blueprint_id)
        rows = session.scalars(query).all()
        return [_package_view(row) for row in rows]


def latest_published_package(db: Database) -> Optional[dict]:
    """The newest published package across all blueprints, if any."""
    with db.sessions() as session:
        row = session.scalar(
            select(PlotTwistPackageRecord)
            .where(PlotTwistPackageRecord.status == "published")
            .order_by(PlotTwistPackageRecord.published_at.desc())
            .limit(1)
        )
        return _package_view(row) if row is not None else None


def generate_package(
    db: Database, blueprint_id: str, created_by: str, llm_config=None
) -> dict:
    """Run Stage B for an approved blueprint, with bounded repair.

    Large stories generate interchangeable batches after the core package;
    every batch receives the canonical blueprint plus summaries of existing
    scenes, and each batch validates against the same contracts.
    """
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import generate_structured_json

    with db.sessions() as session:
        blueprint_row = session.get(PlotTwistBlueprintRecord, blueprint_id)
        if blueprint_row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Blueprint not found.")
        if blueprint_row.status != "approved":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Blueprint must be approved before generating the story package.",
            )
        blueprint = dict(blueprint_row.blueprint or {})
        try:
            config = GenerationConfig.model_validate(dict(blueprint_row.config or {}))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Stored generation config is invalid: {exc}",
            ) from exc

    try:
        payload, _meta = generate_structured_json(
            system=_stage_b_system(),
            user=_stage_b_user(blueprint, config),
            json_schema=package_json_schema(),
            schema_name="plot_twist_package",
            operation="package",
            timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
            max_tokens=16000,
            config=llm_config,
        )
    except Exception as exc:
        raise _as_http(exc)

    # Identity is assigned server-side, never trusted from the model: a
    # drifted blueprint_ref would detach the package from its contract.
    payload["blueprint_ref"] = blueprint_id
    payload["difficulty"] = config.difficulty
    payload.setdefault("story_id", f"plot-twist-{uuid.uuid4().hex[:8]}")

    # Batch interchangeable scenes for large stories.
    try:
        _append_interchangeable_batches(
            payload, blueprint, config, llm_config=llm_config
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _as_http(exc)

    report = plot_twist_validation.validate_package(
        payload, blueprint=blueprint, max_scenes=config.max_scenes
    )
    attempts = 0
    while not report.ok and attempts < PLOT_TWIST_MAX_REPAIR_ATTEMPTS:
        attempts += 1
        try:
            repaired, _meta = generate_structured_json(
                system=_stage_b_system(),
                user=_repair_user(
                    payload,
                    [err.model_dump() for err in report.errors],
                    "Regenerate the full story package object honouring its schema. "
                    "Every scene is a full object with id, type, title, "
                    "narrative_text, actor, slot, tags and choices.",
                    blueprint=blueprint,
                ),
                json_schema=package_json_schema(),
                schema_name="plot_twist_package",
                operation=f"package-repair-{attempts}",
                timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
                max_tokens=16000,
                config=llm_config,
            )
        except Exception as exc:
            raise _as_http(exc)
        repaired["blueprint_ref"] = blueprint_id
        repaired["difficulty"] = config.difficulty
        repaired.setdefault("story_id", payload.get("story_id"))
        payload = repaired
        report = plot_twist_validation.validate_package(
            payload, blueprint=blueprint, max_scenes=config.max_scenes
        )

    with db.sessions.begin() as session:
        version = _next_package_version(session, blueprint_id)
        payload["version"] = version
        row = PlotTwistPackageRecord(
            blueprint_id=blueprint_id,
            version=version,
            title=str(payload.get("title") or "Untitled story"),
            package=payload,
            status="ready_for_review" if report.ok else "validation_failed",
            validation_errors=[] if report.ok else [err.model_dump() for err in report.errors],
            created_by=created_by,
        )
        session.add(row)
        session.flush()
        package_id = str(row.id)
    return get_package(db, package_id)


def _append_interchangeable_batches(
    payload: dict, blueprint: dict, config: GenerationConfig, llm_config=None
) -> None:
    """Generate interchangeable batches in place for large stories.

    Skipped for small stories: when the core package already covers the
    requested interchangeable count, there is nothing to batch. Each batch
    receives the canonical blueprint and summaries of the scenes so far.
    """
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import generate_structured_json

    slots = [slot.get("name") for slot in (blueprint.get("slots") or []) if slot.get("name")]
    if not slots:
        return
    existing = list(payload.get("scenes") or [])
    interchangeable = [s for s in existing if (s.get("slot") or "core") != "core"]
    if len(interchangeable) >= max(1, config.interchangeable_count):
        return
    for slot in slots:
        slot_scenes = [s for s in existing if s.get("slot") == slot]
        if len(slot_scenes) >= PLOT_TWIST_MAX_BATCH_SCENES:
            continue
        summary = "; ".join(
            f"{s.get('id')}: {s.get('title')}" for s in existing[:20]
        )
        batch, _meta = generate_structured_json(
            system=_stage_b_system(),
            user=_batch_user(blueprint, slot, summary or "No scenes yet."),
            json_schema=scene_batch_json_schema(),
            schema_name="plot_twist_batch",
            operation=f"package-batch-{slot}",
            timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
            config=llm_config,
        )
        for scene in batch.get("scenes") or []:
            scene.setdefault("slot", slot)
            existing.append(scene)
        if len(existing) >= (config.max_scenes or 40):
            break
    payload["scenes"] = existing


def regenerate_scene(
    db: Database, package_id: str, scene_id: str, created_by: str, llm_config=None
) -> dict:
    """Regenerate one scene (or batch slot) without touching the rest.

    A failed repair leaves the previous valid draft intact: the stored
    package is only replaced when the repaired scene validates in place.
    """
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import generate_structured_json

    with db.sessions() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        if row.status == "published":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Published packages are immutable; create a new version instead.",
            )
        package = dict(row.package or {})
        blueprint_row = session.get(PlotTwistBlueprintRecord, str(row.blueprint_id))
        blueprint = dict(blueprint_row.blueprint) if blueprint_row is not None else {}
    scenes = list(package.get("scenes") or [])
    target = next((s for s in scenes if s.get("id") == scene_id), None)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scene not found in package.")
    try:
        batch, _meta = generate_structured_json(
            system=_stage_b_system(),
            user=_batch_user(
                blueprint or {"title": package.get("title")},
                target.get("slot") or "core",
                f"Regenerate scene {scene_id} ({target.get('title')}) with the same "
                "slot, tags and actor contract; keep every other scene id stable.",
            ),
            json_schema=scene_batch_json_schema(),
            schema_name="plot_twist_batch",
            operation="scene-regenerate",
            timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
            config=llm_config,
        )
    except Exception as exc:
        raise _as_http(exc)
    replacement = (batch.get("scenes") or [None])[0]
    if not replacement:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The model returned no replacement scene.",
        )
    replacement["id"] = scene_id
    replacement.setdefault("slot", target.get("slot") or "core")
    candidate = dict(package)
    candidate["scenes"] = [
        replacement if s.get("id") == scene_id else s for s in scenes
    ]
    report = plot_twist_validation.validate_package(candidate, blueprint=blueprint or None)
    with db.sessions.begin() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        assert row is not None
        if report.ok:
            row.package = candidate
            row.title = str(candidate.get("title") or row.title)
            row.status = "ready_for_review"
            row.validation_errors = []
        else:
            # Previous draft stays intact; only diagnostics are stored.
            row.status = "validation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
    _ = created_by
    return get_package(db, package_id)


def publish_package(db: Database, package_id: str) -> dict:
    """Publish a validated package version: the immutable version sessions pin."""
    with db.sessions() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        package = dict(row.package or {})
        blueprint_row = session.get(PlotTwistBlueprintRecord, str(row.blueprint_id))
        blueprint = dict(blueprint_row.blueprint) if blueprint_row is not None else None
        blueprint_approved = blueprint_row is not None and blueprint_row.status == "approved"
    report = plot_twist_validation.validate_package(package, blueprint=blueprint)
    if not report.ok:
        # Diagnostics commit in their own transaction: raising inside the
        # write would roll them back.
        with db.sessions.begin() as session:
            row = session.get(PlotTwistPackageRecord, package_id)
            assert row is not None
            row.status = "validation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Package is invalid; resolve validation errors before publishing.",
        )
    if not blueprint_approved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An approved blueprint is required before publishing.",
        )
    with db.sessions.begin() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        assert row is not None
        row.status = "published"
        row.validation_errors = []
        row.published_at = _now()
    published = get_package(db, package_id)
    # Publishing opens the event: the scheduler pins the newest published
    # version into a live period (or leaves the current one playing its own
    # pinned version). Best-effort -- the reset loop and the manual trigger
    # are the backstops -- so a scheduler fault never fails a good publish.
    try:
        from . import game_scheduler

        summary = game_scheduler.ensure_single_game(db, "plot-twist")
        logger.info(
            "Plot Twist publish opened the event",
            extra={"package_id": package_id, "scheduler": summary.get("action")},
        )
    except Exception as exc:
        logger.warning("Plot Twist publish could not open the event: %s", exc)
    return published


def update_package_content(
    db: Database, package_id: str, package: dict
) -> dict:
    """Manual admin edit of supported story fields on an unpublished package.

    Edits re-validate; invalid edits are refused and the stored draft is
    untouched. Published packages are immutable.
    """
    with db.sessions() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        if row.status == "published":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Published packages are immutable; generate a new version instead.",
            )
        blueprint_row = session.get(PlotTwistBlueprintRecord, str(row.blueprint_id))
        blueprint = dict(blueprint_row.blueprint) if blueprint_row is not None else None
        blueprint_id = str(row.blueprint_id)
        stored = dict(row.package or {})
        stored_id = str(row.id)
        stored_version = int(row.version)
    candidate = dict(package)
    candidate.setdefault("blueprint_ref", blueprint_id)
    candidate.setdefault("story_id", (stored.get("story_id") or f"plot-twist-{stored_id[:8]}"))
    candidate.setdefault("version", stored_version)
    report = plot_twist_validation.validate_package(candidate, blueprint=blueprint)
    if not report.ok:
        # Diagnostics commit in their own transaction: raising inside the
        # write would roll them back, and the stored draft stays untouched.
        with db.sessions.begin() as session:
            row = session.get(PlotTwistPackageRecord, package_id)
            assert row is not None
            row.status = "validation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Edited package is invalid; fix validation errors first.",
        )
    with db.sessions.begin() as session:
        row = session.get(PlotTwistPackageRecord, package_id)
        assert row is not None
        row.package = candidate
        row.title = str(candidate.get("title") or row.title)
        row.status = "ready_for_review"
        row.validation_errors = []
    return get_package(db, package_id)


def simulate_path(package: dict, choice_ids: list[str]) -> dict:
    """Walk choice ids from the opening scene without touching the database.

    Used by the admin preview to inspect sample decision paths and their
    outcomes. Stops at the first invalid step with valid=False.
    """
    from .plot_twist_gameplay import (
        _initial_state_with_defaults,
        apply_state_changes,
        check_conditions,
    )

    scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
    endings = {e.get("id"): e for e in (package.get("endings") or [])}
    opening_id = package.get("opening_scene_id")
    if opening_id not in scenes:
        return {"steps": [], "ending_id": None, "ending_title": None,
                "final_state": {}, "treasures": [], "valid": False,
                "error": "Opening scene is missing."}
    state = _initial_state_with_defaults(package)
    var_map = {v.get("name"): v for v in (package.get("state_vars") or [])}
    current_id = opening_id
    steps: list[dict] = []
    treasures: list[str] = []
    for choice_id in choice_ids:
        scene = scenes.get(current_id)
        if scene is None:
            return {"steps": steps, "ending_id": None, "ending_title": None,
                    "final_state": state, "treasures": treasures, "valid": False,
                    "error": f"Scene {current_id!r} does not exist."}
        if scene.get("is_terminal"):
            return {"steps": steps, "ending_id": scene.get("ending_id"),
                    "ending_title": (endings.get(scene.get("ending_id") or {}) or {}).get("title"),
                    "final_state": state, "treasures": treasures, "valid": False,
                    "error": f"Scene {current_id!r} is terminal; no further choices apply."}
        choice = next((c for c in (scene.get("choices") or []) if c.get("id") == choice_id), None)
        if choice is None:
            return {"steps": steps, "ending_id": None, "ending_title": None,
                    "final_state": state, "treasures": treasures, "valid": False,
                    "error": f"Choice {choice_id!r} is not offered in scene {current_id!r}."}
        if not check_conditions(choice.get("conditions") or [], state):
            return {"steps": steps, "ending_id": None, "ending_title": None,
                    "final_state": state, "treasures": treasures, "valid": False,
                    "error": f"Choice {choice_id!r} conditions are not met by the current state."}
        state = apply_state_changes(var_map, state, choice.get("state_changes") or [])
        if (choice.get("reward_tier") or "").lower() == "treasure" or "treasure" in (choice.get("id") or "").lower():
            treasures.append(choice.get("id"))
        steps.append({"scene_id": current_id, "choice_id": choice_id,
                      "next_scene_id": choice.get("next_scene_id")})
        current_id = choice.get("next_scene_id")
    scene = scenes.get(current_id, {})
    ending = endings.get(scene.get("ending_id") or {})
    return {"steps": steps, "ending_id": scene.get("ending_id"),
            "ending_title": ending.get("title"), "final_state": state,
            "treasures": treasures, "valid": True, "error": None}
