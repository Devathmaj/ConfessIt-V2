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

from ..db_models import PlotTwistBlueprintRecord, PlotTwistGameRecord, PlotTwistPackageRecord
from ..limits import (
    PLOT_TWIST_ENDING_TYPES,
    PLOT_TWIST_MAX_BATCH_SCENES,
    PLOT_TWIST_MAX_PATCH_SCENES,
    PLOT_TWIST_MAX_REPAIR_ATTEMPTS,
    PLOT_TWIST_MAX_STUCK_REGENERATIONS,
    PLOT_TWIST_SESSION_LOCK_KEY,
)
from ..postgres import Database, get_row, is_valid_id
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
        "dashes). State variables are bounded integers (min/max/initial) or "
        "flag token lists (initial is a string list; flag values are short "
        "lowercase strings, never 1/0/true/false). Endings need explicit "
        "conditions over declared variables, distinct priorities, mutually "
        "exclusive outcomes. Reward tiers: participation, bronze, silver, "
        "gold. Constraints must state: turns strictly alternate "
        "(participant_one/participant_two in alternation, never same twice; "
        "either/joint only for rare shared moments); main-path beats use slot "
        "core only (opening and terminals always core); interchangeable slots "
        "are side alternatives for one moment, never sequential beats; "
        "choices are tradeoffs with distinct targets, never polar good-vs-bad "
        "converging on one scene; initial_state covers every variable."
    )


def _stage_a_user(config: GenerationConfig) -> str:
    lines = [
        f"Difficulty: {config.difficulty}.",
        f"Decision points wanted: {config.decision_count}.",
        f"Interchangeable encounters wanted: {config.interchangeable_count}.",
        f"Maximum scenes: {config.max_scenes}.",
        f"Main path: about {config.decision_count} beats and never more than "
        f"{config.decision_count + 2} -- each beat becomes one core scene in the "
        "playable package, so merge story moments into beats instead of listing "
        "them all.",
        f"Slots: core plus about {config.interchangeable_count} side slots, and "
        f"never more than {config.interchangeable_count + 2} in total.",
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
        "Define title, description, premise, rules, characters, world, state "
        "vars with ranges/initials, ordered main path, slot rules, modules, "
        "encounter categories, endings with conditions/priorities, reward "
        "tiers, constraints. Main path is slot core; side slots are "
        "alternatives for one moment, never sequential beats. Every var needs "
        "an in-range initial (flags: string lists); endings achievable and "
        "mutually exclusive."
    )
    return "\n".join(lines)


def _stage_b_system() -> str:
    return (
        "You write complete playable Plot Twist story packages. Respond with "
        "a single JSON object honouring the provided schema and nothing else. "
        "`scenes` holds full scene objects (never ids): unique id/title, "
        "vivid 2-4 sentence narrative_text grounded in the blueprint (no "
        "filler, no reuse), type, actor, slot, tags, choices with safe state "
        "changes over declared variables only (flag values are short strings, "
        "never numbers/booleans), valid next_scene_id targets. Integer "
        "variables take set/increment/decrement with numeric values; flag "
        "variables take only add_flag/remove_flag with short string tokens "
        "(never set/increment on a flag -- the turn flag flips with "
        "remove_flag then add_flag). Terminals carry "
        "ending_id and no choices. Never invent variables, tags, endings, tiers. "
        "Slots: opening, terminals and main-path scenes are slot core; side "
        "slots hold alternatives for one moment, never sequential beats, and "
        "interchangeable scenes of one slot must never lead to each other. "
        "Tags: requires_tags must be satisfied by another scene's "
        "provides_tags -- never require a tag nothing provides. "
        "Turns alternate participant_one/participant_two along every path "
        "(never same twice): a participant_two scene's choices must lead to "
        "participant_one scenes and vice versa; either/joint rarely. Loops "
        "must offer an exit to an ending, and each scene's FIRST choice must "
        "keep advancing toward an ending -- the engine walks first choices to "
        "check a run can finish, so put looping or risky options after the "
        "advancing one. Branching is the top rejection "
        "reason: multi-choice scenes must use DIFFERENT next_scene_id targets, "
        "never unanimous convergence, and never insert detour or variant "
        "scenes whose choices all funnel into one successor; choices are "
        "tradeoffs (trust vs secrets/safety vs treasure), never polar "
        "good-vs-bad to the same scene. Each choice needs distinct specific "
        "consequence_text. State changes differ per choice and gate endings; "
        "initial_state covers every variable; each ending fits a play style."
    )


def _stage_b_user(blueprint: dict, config: GenerationConfig) -> str:
    import json as _json

    return (
        "Generate the complete playable package for this approved blueprint "
        f"(story_id: {blueprint.get('title', 'plot-twist-story')}).\n\n"
        f"BLUEPRINT:\n{_json.dumps(blueprint)}\n\n"
        f"Difficulty: {config.difficulty}. "
        f"Render about {config.decision_count} core decision scenes and "
        f"{config.interchangeable_count} side encounters, and keep the whole "
        f"package to at most {config.max_scenes} scenes in total (opening, core, "
        "sides, terminals and alternates counted together; the schema enforces "
        "the cap). The blueprint's main path is a guide, not a scene list: merge "
        "or drop listed beats instead of creating one scene per beat. "
        "Self-check: distinct targets per scene, no polar "
        "same-target pair, no single-successor detour nodes, alternating "
        "actors per edge, unique titles, complete initial_state, string flag "
        "values, distinct reachable endings."
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
            {
                "name": slot.get("name"),
                "accepts_tags": slot.get("accepts_tags"),
                "provides_tags": slot.get("provides_tags"),
            }
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
        f"Generate interchangeable scenes for slot {slot!r}: alternatives for "
        "ONE side moment, mutually substitutable, unique titles/narratives. "
        "Return one JSON object with slot and full scene objects (choices use "
        "distinct targets, tradeoffs not polar same-target pairs).\n\n"
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
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
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
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
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

    budgets = {
        "decision_count": config.decision_count,
        "interchangeable_count": config.interchangeable_count,
    }
    report = plot_twist_validation.validate_blueprint(payload, **budgets)
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
        report = plot_twist_validation.validate_blueprint(payload, **budgets)

    with db.sessions.begin() as session:
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id_str)
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
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
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
            row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
            assert row is not None
            row.status = "generation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Blueprint is invalid; regenerate before approving.",
        )
    with db.sessions.begin() as session:
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
        assert row is not None
        row.status = "approved"
    return get_blueprint(db, blueprint_id)


def _mark_blueprint_failed(db: Database, blueprint_id: str, exc: Exception) -> None:
    detail = getattr(exc, "detail", str(exc))
    with db.sessions.begin() as session:
        row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
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
        row = get_row(session, PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        return _package_view(row)


def list_packages(db: Database, blueprint_id: Optional[str] = None) -> list[dict]:
    if blueprint_id is not None and not is_valid_id(blueprint_id):
        # A malformed id names no blueprint, and it must not reach a uuid column to
        # find that out: the filter compares against one, and PostgreSQL raises
        # ``invalid input syntax for type uuid`` on a value that is not a uuid.
        # A blueprint that does not exist has no packages, which is the answer.
        return []
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


def _patch_targets(payload: dict, errors: list) -> list[str]:
    """Scene ids implicated by validation errors, plus their direct targets.

    Actor hand-offs and convergence live on edges, so a culprit scene is
    patched together with the scenes its choices lead to. Errors without a
    scene (scene count, initial state) are not patchable and fall back to a
    full regeneration.
    """
    by_id = {str(scene.get("id")): scene for scene in payload.get("scenes") or []}
    targets: list[str] = []
    for error in errors:
        scene_id = str(getattr(error, "scene_id", None) or "")
        if scene_id in by_id and scene_id not in targets:
            targets.append(scene_id)
    expanded = list(targets)
    for scene_id in targets:
        for choice in by_id[scene_id].get("choices") or []:
            successor = str(choice.get("next_scene_id") or "")
            if successor in by_id and successor not in expanded:
                expanded.append(successor)
    return expanded[:PLOT_TWIST_MAX_PATCH_SCENES]


def _scene_fix_user(
    blueprint: dict, scene_map: str, selected: list[dict], errors: list[dict]
) -> str:
    import json as _json

    return (
        "A generated Plot Twist package failed validation. Correct the listed "
        "scenes and return one JSON object with `slot` and the corrected full "
        "scene objects in `scenes`. You may also return any other existing "
        "scene you had to change (e.g. to wire an unreachable scene in or "
        "provide a missing tag); scenes you do not return stay unchanged.\n\n"
        "Rules: turns alternate along every edge -- a participant_one choice "
        "must lead to a participant_two scene and vice versa, never the same "
        "participant twice in a row; a scene's choices must lead to DIFFERENT "
        "existing scenes; interchangeable scenes of one slot must never lead "
        "into each other; a scene may require only tags that some scene's "
        "provides_tags supplies (add the tag to a listed scene's provides_tags "
        "when needed, or drop the requirement); flag variables take only "
        "add_flag/remove_flag with short string tokens; terminals carry "
        "ending_id and no choices; every non-terminal scene keeps 1-3 choices; "
        "every scene must be reachable from the opening -- when a scene is "
        "unreachable, retarget or add a choice in another existing scene so "
        "the story can enter it. Keep each scene's id and slot, and keep "
        "choices targeting ids from the scene list.\n\n"
        f"BLUEPRINT (essentials):\n{_json.dumps(_minimized_blueprint(blueprint))}\n\n"
        f"ALL SCENES (id | actor | slot | terminal | choice targets):\n{scene_map}\n\n"
        f"SCENES TO CORRECT:\n{_json.dumps(selected)}\n\n"
        f"VALIDATION ERRORS TO RESOLVE:\n{_json.dumps(errors)}"
    )


def _patch_scenes(
    payload: dict,
    blueprint: dict,
    target_ids: list[str],
    errors: list[dict],
    llm_config=None,
) -> Optional[dict]:
    """Regenerate only the implicated scenes and splice them back in.

    Repairs used to regenerate every scene, which fixed one error and could
    regress another. A patch leaves every untouched scene byte-identical;
    ``None`` means the request failed and the caller should fall back to a
    full regeneration.
    """
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import generate_structured_json

    by_id = {str(scene.get("id")): scene for scene in payload.get("scenes") or []}
    selected = [by_id[scene_id] for scene_id in target_ids if scene_id in by_id]
    if not selected:
        return None
    scene_map = "\n".join(
        f"{scene.get('id')} | actor={scene.get('actor')} | "
        f"slot={scene.get('slot') or 'core'} | "
        f"terminal={bool(scene.get('is_terminal'))} | "
        f"choices -> {[choice.get('next_scene_id') for choice in (scene.get('choices') or [])]} | "
        f"req={scene.get('requires_tags') or []} | prov={scene.get('provides_tags') or []}"
        for scene in payload.get("scenes") or []
    )
    relevant = [
        error for error in errors if str(error.get("scene_id") or "") in set(target_ids)
    ]
    try:
        batch, _meta = generate_structured_json(
            system=_stage_b_system(),
            user=_scene_fix_user(blueprint, scene_map, selected, relevant or errors[:12]),
            json_schema=scene_batch_json_schema(),
            schema_name="plot_twist_scene_fix",
            operation="package-scene-fix",
            timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
            max_tokens=14000,
            config=llm_config,
        )
    except Exception:
        return None
    replacements = {
        str(scene.get("id")): scene
        for scene in (batch.get("scenes") or [])
        if isinstance(scene, dict) and str(scene.get("id")) in by_id
    }
    if not replacements:
        return None
    patched: list[dict] = []
    for scene in payload.get("scenes") or []:
        replacement = replacements.get(str(scene.get("id")))
        if replacement is None:
            patched.append(scene)
            continue
        replacement["id"] = scene.get("id")
        replacement["slot"] = scene.get("slot") or "core"
        patched.append(replacement)
    candidate = dict(payload)
    candidate["scenes"] = patched
    return candidate


def generate_package(
    db: Database, blueprint_id: str, created_by: str, llm_config=None
) -> dict:
    """Run Stage B for an approved blueprint, with bounded repair.

    Large stories generate interchangeable batches after the core package;
    every batch receives the canonical blueprint plus summaries of existing
    scenes, and each batch validates against the same contracts.
    """
    from ..limits import PLOT_TWIST_LLM_TIMEOUT_SECONDS
    from .llm_client import LLMError, generate_structured_json

    with db.sessions() as session:
        blueprint_row = get_row(session, PlotTwistBlueprintRecord, blueprint_id)
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

    package_call = dict(
        system=_stage_b_system(),
        user=_stage_b_user(blueprint, config),
        json_schema=package_json_schema(blueprint, max_scenes=config.max_scenes),
        schema_name="plot_twist_package",
        operation="package",
        timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
        max_tokens=20000,
        config=llm_config,
    )
    try:
        payload, _meta = generate_structured_json(**package_call)
    except Exception as exc:
        if not (isinstance(exc, LLMError) and exc.code == "truncated"):
            raise _as_http(exc)
        # A cut-off answer is a stochastic overrun (hidden reasoning plus a
        # long package), not a broken contract: retry once against the cap.
        logger.warning(
            "Plot Twist package answer was cut off by the output-token budget; retrying once."
        )
        try:
            payload, _meta = generate_structured_json(**package_call)
        except Exception as retry_exc:
            raise _as_http(retry_exc)

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
    force_full = False
    stuck = 0
    while not report.ok and attempts < PLOT_TWIST_MAX_REPAIR_ATTEMPTS:
        attempts += 1
        progressed = False
        if not force_full:
            targets = _patch_targets(payload, report.errors)
            if targets:
                candidate = _patch_scenes(
                    payload,
                    blueprint,
                    targets,
                    [err.model_dump() for err in report.errors],
                    llm_config=llm_config,
                )
                if candidate is not None:
                    candidate_report = plot_twist_validation.validate_package(
                        candidate, blueprint=blueprint, max_scenes=config.max_scenes
                    )
                    if len(candidate_report.errors) < len(report.errors):
                        logger.info(
                            "Plot Twist patch fixed %s of %s validation errors.",
                            len(report.errors) - len(candidate_report.errors),
                            len(report.errors),
                        )
                        payload, report = candidate, candidate_report
                        progressed = True
                        stuck = 0
                    else:
                        force_full = True
                else:
                    force_full = True
        if report.ok:
            break
        if progressed:
            continue
        # Whole-package regeneration: what remains is structural (scene count,
        # initial state) or the patch stopped helping.
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
                json_schema=package_json_schema(blueprint, max_scenes=config.max_scenes),
                schema_name="plot_twist_package",
                operation=f"package-repair-{attempts}",
                timeout_s=PLOT_TWIST_LLM_TIMEOUT_SECONDS,
                max_tokens=20000,
                config=llm_config,
            )
        except LLMError as exc:
            if exc.code == "truncated":
                # A cut-off answer is a stochastic overrun, not a broken
                # contract: spend the attempt on the same repair instead of
                # failing the whole generation.
                logger.warning(
                    "Plot Twist repair %s was cut off by the output-token budget; retrying.",
                    attempts,
                )
                continue
            raise _as_http(exc)
        except Exception as exc:
            raise _as_http(exc)
        repaired["blueprint_ref"] = blueprint_id
        repaired["difficulty"] = config.difficulty
        repaired.setdefault("story_id", payload.get("story_id"))
        repaired_report = plot_twist_validation.validate_package(
            repaired, blueprint=blueprint, max_scenes=config.max_scenes
        )
        if len(repaired_report.errors) <= len(report.errors):
            payload, report = repaired, repaired_report
            stuck = 0
        else:
            # A regeneration that adds errors must not erase progress the
            # patches already made; keep the better draft as the working state.
            stuck += 1
            logger.info(
                "Plot Twist regeneration produced %s validation errors against "
                "%s; keeping the better draft.",
                len(repaired_report.errors),
                len(report.errors),
            )
            if stuck >= PLOT_TWIST_MAX_STUCK_REGENERATIONS:
                logger.info(
                    "Plot Twist regeneration is not finding a better structure; "
                    "ending the loop on the best draft."
                )
                break
        force_full = False

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
        ceiling = config.max_scenes or 40
        room = max(0, ceiling - len(existing))
        for scene in (batch.get("scenes") or [])[:room]:
            scene.setdefault("slot", slot)
            existing.append(scene)
        if len(existing) >= ceiling:
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
        row = get_row(session, PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        if row.status == "published":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Published packages are immutable; create a new version instead.",
            )
        package = dict(row.package or {})
        blueprint_row = get_row(session, PlotTwistBlueprintRecord, str(row.blueprint_id))
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
        row = get_row(session, PlotTwistPackageRecord, package_id)
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


def _rotate_event_to_package(db: Database, package_id: str) -> dict:
    """End the live Plot Twist period and open a fresh one on ``package_id``.

    Terminate-then-create, like a Wordle/Anagram Update: the current global
    row (if any) is stamped ended and a new active row pinned to the just
    published package opens for today under the game's reset cadence. An
    update at 23:58 does not buy a fresh 24 hours -- the period still runs
    under the cadence.

    Per-couple sessions belong to their global id, so every couple starts
    fresh on the new story (created lazily on entering); unfinished sessions
    of the ended period stay unfinished and score nothing at finalization
    (only ``completed`` sessions rank). The ended period's awards finalize
    now rather than waiting for the next reset run.
    """
    import uuid as _uuid_mod
    from sqlalchemy import text as _text
    from sqlalchemy.exc import IntegrityError

    from . import game_periods
    from .game_configs import game_lock_key, get_config

    try:
        with db.sessions.begin() as session:
            # ENG-001 order: lifecycle lock before the pair lock, as in
            # gameplay submits, so a publish racing a choice cannot deadlock
            # it and cannot leave two live global rows.
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("plot-twist")},
            )
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": PLOT_TWIST_SESSION_LOCK_KEY},
            )
            moment = _now()
            today = moment.date()
            config = get_config(session, "plot-twist")
            live = session.scalars(
                select(PlotTwistGameRecord).where(
                    PlotTwistGameRecord.status.in_(("active", "pending"))
                )
            ).all()
            for row in live:
                row.status = "ended"
                row.ended_at = moment
            start, end = game_periods.period_for_start(today, config.reset_cadence)
            opened = PlotTwistGameRecord(
                game_date=today,
                status="active",
                period_start_date=start,
                period_end_date=end,
                story_package_id=package_id,
                seed=_uuid_mod.uuid4().hex,
                started_at=moment,
                created_by="publish",
            )
            session.add(opened)
            session.flush()
            opened_id = str(opened.id)
            from . import league_finalization

            league_finalization.snapshot_opened_safely(
                session, game_id="plot-twist", opportunity_id=opened_id
            )
            summary = {
                "game_id": "plot-twist",
                "action": "rotated",
                "session_id": opened_id,
                "story_package_id": package_id,
                "period_start": start.isoformat(),
                "period_end": end.isoformat(),
            }
    except IntegrityError as exc:
        # A concurrent publish won the insert past the lock and the partial
        # unique index refused the duplicate: the package is still published,
        # so settle on the surviving live row instead of failing the publish.
        from psycopg.errors import UniqueViolation

        if not isinstance(exc.orig, UniqueViolation):
            raise
        with db.sessions() as session:
            winner = session.scalar(
                select(PlotTwistGameRecord)
                .where(PlotTwistGameRecord.status.in_(("active", "pending")))
                .limit(1)
            )
            return {
                "game_id": "plot-twist",
                "action": "current",
                "session_id": str(winner.id) if winner is not None else None,
            }
    # The rotation deliberately closed the previous period, so its awards
    # finalize now rather than waiting for the next reset run. Best-effort:
    # the due scan retries anything this misses on its next pass.
    try:
        from . import league_finalization

        league_finalization.finalize_game_safely(db, "plot-twist")
    except Exception as exc:
        logger.warning("Plot Twist rotation could not finalize the ended period: %s", exc)
    logger.info(
        "Plot Twist publish rotated the event",
        extra={"package_id": package_id, "scheduler": summary.get("action")},
    )
    return summary


def publish_package(db: Database, package_id: str) -> dict:
    """Publish a validated package version and rotate the live event onto it.

    Publishing ends the current global period (if any) and opens a fresh one
    pinned to this package, like a Wordle/Anagram Update: couples start the
    new story from scratch and unfinished playthroughs of the old story stay
    unfinished, scoring nothing. Retrying a publish is safe and idempotent.
    """
    with db.sessions() as session:
        row = get_row(session, PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        package = dict(row.package or {})
        blueprint_row = get_row(session, PlotTwistBlueprintRecord, str(row.blueprint_id))
        blueprint = dict(blueprint_row.blueprint) if blueprint_row is not None else None
        blueprint_approved = blueprint_row is not None and blueprint_row.status == "approved"
    report = plot_twist_validation.validate_package(package, blueprint=blueprint)
    if not report.ok:
        # Diagnostics commit in their own transaction: raising inside the
        # write would roll them back.
        with db.sessions.begin() as session:
            row = get_row(session, PlotTwistPackageRecord, package_id)
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
        row = get_row(session, PlotTwistPackageRecord, package_id)
        assert row is not None
        row.status = "published"
        row.validation_errors = []
        row.published_at = _now()
    published = get_package(db, package_id)
    # Publishing rotates the event onto this package (terminate-then-create).
    # Rotation errors propagate -- unlike a scheduler fault, a failed rotation
    # would leave the new story unpublished-in-effect -- and retrying the
    # publish is safe: the package stays published and the rotation converges
    # on the surviving live row.
    _rotate_event_to_package(db, package_id)
    return published


def update_package_content(
    db: Database, package_id: str, package: dict
) -> dict:
    """Manual admin edit of supported story fields on an unpublished package.

    Edits re-validate; invalid edits are refused and the stored draft is
    untouched. Published packages are immutable.
    """
    with db.sessions() as session:
        row = get_row(session, PlotTwistPackageRecord, package_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Story package not found.")
        if row.status == "published":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Published packages are immutable; generate a new version instead.",
            )
        blueprint_row = get_row(session, PlotTwistBlueprintRecord, str(row.blueprint_id))
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
            row = get_row(session, PlotTwistPackageRecord, package_id)
            assert row is not None
            row.status = "validation_failed"
            row.validation_errors = [err.model_dump() for err in report.errors]
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Edited package is invalid; fix validation errors first.",
        )
    with db.sessions.begin() as session:
        row = get_row(session, PlotTwistPackageRecord, package_id)
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
    ending_id = scene.get("ending_id")
    ending = endings.get(ending_id or "")
    return {"steps": steps, "ending_id": ending_id,
            "ending_title": (ending or {}).get("title"), "final_state": state,
            "treasures": treasures, "valid": True, "error": None}
