"""Deterministic validation for Plot Twist story packages.

AI-generated reviews may supplement these checks but never replace them:
a package is publishable only when this module reports ``ok=True``.
Every error names the offending scene (or package level) and the exact
problem, so the admin screen and the repair loop can act on it.

Checks:
- schema parse into StoryPackageDef;
- unique scene / choice / ending ids;
- every transition target and ending reference exists;
- state variables referenced by conditions and changes are declared;
- state-change ops / types / ranges are legal (no expressions);
- interchangeable modules are compatible with their slots;
- reachability of endings from the opening scene (graph traversal);
- every non-terminal scene has a valid route forward (no dead ends);
- ending conditions use defined variables; conflicting ending conditions
  resolve by priority (duplicate priority + overlapping conditions flagged);
- reward tiers within configured limits;
- scene / decision ceilings honoured.
"""

from collections import deque
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..limits import (
    PLOT_TWIST_MAX_DECISIONS,
    PLOT_TWIST_MAX_SCENES,
    PLOT_TWIST_REWARD_TIERS,
)
from .plot_twist_schemas import BlueprintDef, StoryPackageDef


class ValidationMessage(BaseModel):
    scene_id: Optional[str] = None
    code: str
    message: str


class ValidationReport(BaseModel):
    ok: bool
    errors: list[ValidationMessage] = []


def _err(
    errors: list[ValidationMessage],
    code: str,
    message: str,
    scene_id: Optional[str] = None,
) -> None:
    errors.append(ValidationMessage(scene_id=scene_id, code=code, message=message))


def _var_map(package: StoryPackageDef) -> dict[str, Any]:
    return {var.name: var for var in package.state_vars}


def _check_condition(
    errors: list[ValidationMessage],
    cond: Any,
    var_map: dict[str, Any],
    where: str,
    scene_id: Optional[str] = None,
) -> None:
    var = var_map.get(cond.variable)
    if var is None:
        _err(
            errors,
            "undefined_variable",
            f"{where} references undefined variable {cond.variable!r}.",
            scene_id,
        )
        return
    if var.kind == "flag" and cond.op in ("eq", "gte", "lte"):
        _err(
            errors,
            "condition_type_mismatch",
            f"{where} uses numeric comparison {cond.op!r} on flag {cond.variable!r}; "
            "use has_flag / missing_flag.",
            scene_id,
        )
    if var.kind == "int" and cond.op in ("has_flag", "missing_flag"):
        _err(
            errors,
            "condition_type_mismatch",
            f"{where} uses flag comparison {cond.op!r} on integer {cond.variable!r}.",
            scene_id,
        )
    if cond.op in ("has_flag", "missing_flag") and not isinstance(cond.value, str):
        _err(
            errors,
            "flag_value_type",
            f"{where} uses flag comparison with non-string value "
            f"{cond.value!r}; flag tokens must be short strings.",
            scene_id,
        )


def _check_state_change(
    errors: list[ValidationMessage],
    change: Any,
    var_map: dict[str, Any],
    where: str,
    scene_id: Optional[str] = None,
) -> None:
    var = var_map.get(change.variable)
    if var is None:
        _err(
            errors,
            "undefined_variable",
            f"{where} mutates undefined variable {change.variable!r}.",
            scene_id,
        )
        return
    if var.kind == "flag" and change.op in ("set", "increment", "decrement"):
        # `set` on a flag is allowed only with a boolean value.
        if change.op == "set" and isinstance(change.value, bool):
            return
        _err(
            errors,
            "state_change_type_mismatch",
            f"{where} applies numeric op {change.op!r} to flag {change.variable!r}; "
            "use add_flag / remove_flag.",
            scene_id,
        )
        return
    if var.kind == "int" and change.op in ("add_flag", "remove_flag"):
        _err(
            errors,
            "state_change_type_mismatch",
            f"{where} applies flag op {change.op!r} to integer {change.variable!r}.",
            scene_id,
        )
        return
    if change.op in ("add_flag", "remove_flag") and not isinstance(change.value, str):
        _err(
            errors,
            "flag_value_type",
            f"{where} uses flag op {change.op!r} with non-string value "
            f"{change.value!r}; flag tokens must be short strings.",
            scene_id,
        )
        return
    if change.op in ("increment", "decrement"):
        if not isinstance(change.value, int):
            _err(
                errors,
                "state_change_bad_value",
                f"{where} uses {change.op!r} with non-integer value "
                f"{change.value!r}; only bounded integers are permitted.",
                scene_id,
            )
    if change.op == "set" and var.kind == "int":
        if not isinstance(change.value, int):
            _err(
                errors,
                "state_change_bad_value",
                f"{where} sets integer {change.variable!r} to non-integer "
                f"{change.value!r}.",
                scene_id,
            )
        elif var.min_value is not None and change.value < var.min_value:
            _err(
                errors,
                "state_change_out_of_range",
                f"{where} sets {change.variable!r} to {change.value}, below "
                f"minimum {var.min_value}.",
                scene_id,
            )
        elif var.max_value is not None and change.value > var.max_value:
            _err(
                errors,
                "state_change_out_of_range",
                f"{where} sets {change.variable!r} to {change.value}, above "
                f"maximum {var.max_value}.",
                scene_id,
            )


def validate_blueprint(data: dict) -> ValidationReport:
    """Validate a Stage A blueprint: schema plus structural sanity."""
    errors: list[ValidationMessage] = []
    try:
        blueprint = BlueprintDef.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            _err(errors, "blueprint_schema", f"Blueprint field {loc}: {err['msg']}.")
        return ValidationReport(ok=False, errors=errors)
    if not blueprint.title.strip():
        _err(errors, "blueprint_title", "Blueprint title must not be blank.")
    if not blueprint.main_path:
        _err(errors, "blueprint_main_path", "Blueprint must name its main narrative path.")
    if not blueprint.endings:
        _err(errors, "blueprint_endings", "Blueprint must define at least one ending.")
    ending_ids = [ending.id for ending in blueprint.endings]
    if len(set(ending_ids)) != len(ending_ids):
        _err(errors, "blueprint_ending_ids", "Blueprint ending ids must be unique.")
    var_names = [var.name for var in blueprint.state_vars]
    if len(set(var_names)) != len(var_names):
        _err(errors, "blueprint_var_ids", "Blueprint state variable names must be unique.")
    return ValidationReport(ok=not errors, errors=errors)


def validate_package(
    data: dict,
    blueprint: Optional[dict] = None,
    max_scenes: Optional[int] = None,
    max_decisions: Optional[int] = None,
) -> ValidationReport:
    """Deterministic validation of a complete Stage B story package."""
    errors: list[ValidationMessage] = []
    try:
        package = StoryPackageDef.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            _err(errors, "schema", f"Field {loc}: {err['msg']}.")
        return ValidationReport(ok=False, errors=errors)

    scene_ceiling = max_scenes or PLOT_TWIST_MAX_SCENES
    decision_ceiling = max_decisions or PLOT_TWIST_MAX_DECISIONS
    if len(package.scenes) > scene_ceiling:
        _err(
            errors,
            "too_many_scenes",
            f"Package holds {len(package.scenes)} scenes; the ceiling is {scene_ceiling}.",
        )
    decision_total = sum(len(scene.choices) for scene in package.scenes)
    if decision_total > decision_ceiling * 4:
        _err(
            errors,
            "too_many_decisions",
            f"Package holds {decision_total} choices; the decision budget is "
            f"{decision_ceiling} decision points.",
        )

    scene_ids = [scene.id for scene in package.scenes]
    if len(set(scene_ids)) != len(scene_ids):
        _err(errors, "duplicate_scene_id", "Scene ids must be unique.")
    by_scene = {scene.id: scene for scene in package.scenes}

    if package.opening_scene_id not in by_scene:
        _err(
            errors,
            "missing_opening",
            f"Opening scene {package.opening_scene_id!r} is not among the scenes.",
        )
    else:
        opening = by_scene[package.opening_scene_id]
        if opening.type != "opening":
            _err(
                errors,
                "opening_type",
                f"Opening scene {opening.id!r} has type {opening.type!r}; "
                "it must be 'opening'.",
                opening.id,
            )
        if (opening.slot or "core") != "core":
            _err(
                errors,
                "opening_not_core",
                f"Opening scene {opening.id!r} uses slot {opening.slot!r}; "
                "the opening must use slot 'core' so every couple starts "
                "the same story instead of a slot-collapsed loop.",
                opening.id,
            )

    ending_ids = [ending.id for ending in package.endings]
    if len(set(ending_ids)) != len(ending_ids):
        _err(errors, "duplicate_ending_id", "Ending ids must be unique.")
    if not package.endings:
        _err(errors, "no_endings", "Package must define at least one ending.")
    by_ending = {ending.id: ending for ending in package.endings}

    var_map = _var_map(package)
    for name, value in package.initial_state.items():
        if name not in var_map:
            _err(errors, "initial_state_unknown_var", f"Initial state sets undeclared variable {name!r}.")
    for var_name, var in var_map.items():
        if var_name not in package.initial_state:
            _err(
                errors,
                "missing_initial_state",
                f"Initial state omits declared variable {var_name!r}; "
                "gameplay would start it from zero instead of its designed initial.",
            )
            continue
        value = package.initial_state[var_name]
        if var.kind == "int":
            if not isinstance(value, int) or isinstance(value, bool):
                _err(
                    errors,
                    "initial_state_bad_type",
                    f"Initial state sets integer {var_name!r} to non-integer {value!r}.",
                )
            elif (var.min_value is not None and value < var.min_value) or (
                var.max_value is not None and value > var.max_value
            ):
                _err(
                    errors,
                    "initial_state_out_of_range",
                    f"Initial state sets {var_name!r} to {value}, outside "
                    f"[{var.min_value}, {var.max_value}].",
                )
        else:
            if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                _err(
                    errors,
                    "initial_state_bad_type",
                    f"Initial state sets flag {var_name!r} to {value!r}; "
                    "flag state must be a list of string tokens.",
                )

    # Content variety: repeated titles or narratives play as deja vu, the
    # exact complaint behind looped histories.
    _check_content_variety(package, errors, by_scene)

    # Per-scene checks.
    for scene in package.scenes:
        if set(scene.requires_tags) & set(scene.forbidden_tags):
            _err(
                errors,
                "tag_conflict",
                f"Scene lists tags {sorted(set(scene.requires_tags) & set(scene.forbidden_tags))} "
                "as both required and forbidden.",
                scene.id,
            )
        for cond in scene.entry_conditions:
            _check_condition(errors, cond, var_map, "Entry condition", scene.id)
        if scene.is_terminal:
            if scene.ending_id is not None and scene.ending_id not in by_ending:
                _err(
                    errors,
                    "unknown_ending_ref",
                    f"Terminal scene references unknown ending {scene.ending_id!r}.",
                    scene.id,
                )
            if scene.type != "ending" and scene.ending_id is None:
                _err(
                    errors,
                    "terminal_without_ending",
                    "Terminal scene has no ending_id and is not of type 'ending'.",
                    scene.id,
                )
            if scene.choices:
                _err(
                    errors,
                    "terminal_with_choices",
                    "Terminal scene must not offer choices.",
                    scene.id,
                )
            if (scene.slot or "core") != "core":
                _err(
                    errors,
                    "terminal_not_core",
                    f"Terminal scene {scene.id!r} uses slot {scene.slot!r}; "
                    "terminals must use slot 'core' so module selection can "
                    "never shadow or duplicate an ending.",
                    scene.id,
                )
            continue
        if not scene.choices:
            _err(errors, "dead_end", "Non-terminal scene offers no choices.", scene.id)
            continue
        choice_ids = [choice.id for choice in scene.choices]
        if len(set(choice_ids)) != len(choice_ids):
            _err(errors, "duplicate_choice_id", "Choice ids must be unique within a scene.", scene.id)
        if len(scene.choices) >= 2:
            targets = {choice.next_scene_id for choice in scene.choices}
            if len(targets) == 1:
                _err(
                    errors,
                    "convergent_choices",
                    f"All {len(scene.choices)} choices lead to "
                    f"{next(iter(targets))!r}: a decision with one outcome is "
                    "not a decision. Branch to different next scenes with "
                    "different tradeoffs instead of a polar good-vs-bad pair "
                    "converging on the same place.",
                    scene.id,
                )
            texts = [str(choice.text).strip().lower() for choice in scene.choices]
            if len(set(texts)) != len(texts):
                _err(
                    errors,
                    "duplicate_choice_text",
                    "Choice texts must be distinct within a scene.",
                    scene.id,
                )
        for choice in scene.choices:
            where = f"Choice {choice.id!r}"
            if choice.next_scene_id not in by_scene:
                _err(
                    errors,
                    "unknown_transition",
                    f"{where} points at unknown scene {choice.next_scene_id!r}.",
                    scene.id,
                )
            for cond in choice.conditions:
                _check_condition(errors, cond, var_map, where, scene.id)
            for change in choice.state_changes:
                _check_state_change(errors, change, var_map, where, scene.id)
            if choice.reward_tier is not None and choice.reward_tier not in PLOT_TWIST_REWARD_TIERS:
                _err(
                    errors,
                    "unknown_reward_tier",
                    f"{where} names unknown reward tier {choice.reward_tier!r}.",
                    scene.id,
                )
        if scene.ending_id is not None and scene.ending_id not in by_ending:
            _err(
                errors,
                "unknown_ending_ref",
                f"Scene references unknown ending {scene.ending_id!r}.",
                scene.id,
            )

    # Ending checks.
    seen_priorities: dict[int, list[str]] = {}
    for ending in package.endings:
        for cond in ending.conditions:
            _check_condition(errors, cond, var_map, f"Ending {ending.id!r}")
        seen_priorities.setdefault(ending.priority, []).append(ending.id)
        if ending.reward_tier is not None and ending.reward_tier not in PLOT_TWIST_REWARD_TIERS:
            _err(errors, "unknown_reward_tier", f"Ending {ending.id!r} names unknown reward tier {ending.reward_tier!r}.")
    for priority, ids in seen_priorities.items():
        if len(ids) > 1:
            unconditional = [i for i in ids if not by_ending[i].conditions]
            if unconditional:
                _err(
                    errors,
                    "ending_conflict",
                    f"Endings {unconditional} share priority {priority} with no "
                    "distinguishing conditions; the winner would be arbitrary.",
                )

    # Slot compatibility: every interchangeable scene's slot must be declared
    # on the blueprint (when one is supplied), and its required tags must be
    # satisfiable by tags provided somewhere in the package.
    provided_union: set[str] = set()
    for scene in package.scenes:
        provided_union.update(scene.provides_tags)
    slot_names: set[str] = set()
    if blueprint is not None:
        try:
            parsed_blueprint = BlueprintDef.model_validate(blueprint)
            slot_names = {slot.name for slot in parsed_blueprint.slots}
        except ValidationError:
            parsed_blueprint = None  # type: ignore[assignment]
    for scene in package.scenes:
        if scene.slot == "core":
            continue
        if slot_names and scene.slot not in slot_names:
            _err(
                errors,
                "unknown_slot",
                f"Scene uses slot {scene.slot!r}, which the blueprint does not declare.",
                scene.id,
            )
        missing = [tag for tag in scene.requires_tags if tag not in provided_union]
        if missing:
            _err(
                errors,
                "unsatisfiable_tags",
                f"Scene requires tags {missing}, which no scene in the package provides.",
                scene.id,
            )

    # Slot-collapse guard: two scenes sharing one non-core slot must be
    # alternatives for the same moment, never sequential beats. If one can
    # reach the other, per-couple module selection collapses the whole path
    # into a loop (the Echoes of Trust failure: slot-1 held scene-1 and
    # scene-5, so every couple replayed 4 scenes 11 times).
    _check_slot_reuse_on_path(by_scene, errors)

    # Two-sidedness: turns strictly alternate at runtime, so the package
    # must read that way too -- mostly individual scenes, and never two
    # consecutive scenes for the same member.
    non_terminal = [scene for scene in package.scenes if not scene.is_terminal]
    if non_terminal:
        shared = [
            scene for scene in non_terminal if (scene.actor or "either") in ("either", "joint")
        ]
        from ..limits import PLOT_TWIST_SHARED_SCENE_FRACTION

        if len(shared) / len(non_terminal) > PLOT_TWIST_SHARED_SCENE_FRACTION:
            _err(
                errors,
                "shared_scene_heavy",
                f"{len(shared)} of {len(non_terminal)} decision scenes are shared "
                "(either/joint): most decisions must belong to one participant, "
                "with shared moments as the exception.",
            )
    for scene in package.scenes:
        if scene.is_terminal or not scene.choices:
            continue
        for choice in scene.choices:
            target = by_scene.get(choice.next_scene_id)
            if target is None or target.is_terminal:
                continue
            if (scene.actor or "either") in ("participant_one", "participant_two") and (
                target.actor or "either"
            ) == scene.actor:
                _err(
                    errors,
                    "repeated_strict_actor",
                    f"Choice {choice.id!r} hands {scene.actor} a second consecutive "
                    "scene: turns must alternate, so consecutive scenes must not "
                    "name the same participant.",
                    scene.id,
                )

    # Inescapable cycles: a strongly connected set of scenes with no choice
    # leading out toward an ending replays forever. Edges are the authored
    # transitions (module selection may redirect inside a slot, but never
    # invents an exit the author did not write).
    _check_escapable_cycles(by_scene, errors)

    # Graph traversal from the opening: reachability + forward progress.
    if package.opening_scene_id in by_scene:
        reachable: set[str] = set()
        queue: deque[str] = deque([package.opening_scene_id])
        while queue:
            current_id = queue.popleft()
            if current_id in reachable:
                continue
            reachable.add(current_id)
            current = by_scene[current_id]
            if current.is_terminal:
                continue
            for choice in current.choices:
                if choice.next_scene_id in by_scene and choice.next_scene_id not in reachable:
                    queue.append(choice.next_scene_id)
        unreachable = [sid for sid in by_scene if sid not in reachable]
        if unreachable:
            _err(
                errors,
                "unreachable_scenes",
                f"Scenes unreachable from the opening: {sorted(unreachable)}.",
            )
        # Every reachable ending must be reachable; equivalently, at least one
        # terminal scene must be reachable.
        terminals = [sid for sid in reachable if by_scene[sid].is_terminal]
        if not terminals:
            _err(
                errors,
                "no_reachable_ending",
                "No terminal scene is reachable from the opening scene.",
            )
        # Bounded simulation: walk up to 200 steps of blind choices to shake
        # out loops that never terminate.
        _check_livelock(errors, by_scene, package.opening_scene_id)

    return ValidationReport(ok=not errors, errors=errors)


def _check_content_variety(package: Any, errors: list[ValidationMessage], by_scene: dict) -> None:
    """Flag copy-paste stories: duplicate titles read as loops in histories.

    Narrative text is deliberately not enforced here: synthetic engine tests
    reuse short placeholders like "Onward." across corridor scenes, while the
    prompt already asks the model for distinct vivid beats. Titles are what
    the history screen repeats, so they stay a hard rule.
    """
    titles: dict[str, list[str]] = {}
    for scene in package.scenes:
        key = str(scene.title or "").strip().lower()
        if key:
            titles.setdefault(key, []).append(scene.id)
    for key, ids in titles.items():
        if len(ids) > 1:
            _err(
                errors,
                "duplicate_scene_title",
                f"Scenes {sorted(ids)} share the title {key!r}; every scene "
                "needs its own moment so histories do not repeat.",
            )


def _reachable_from(by_scene: dict, start_id: str) -> set[str]:
    """All scene ids reachable from ``start_id`` via authored transitions."""
    seen: set[str] = set()
    queue: deque[str] = deque([start_id])
    while queue:
        current = queue.popleft()
        if current in seen or current not in by_scene:
            continue
        seen.add(current)
        scene = by_scene[current]
        if scene.is_terminal:
            continue
        for choice in scene.choices or []:
            if choice.next_scene_id in by_scene:
                queue.append(choice.next_scene_id)
    return seen


def _check_slot_reuse_on_path(by_scene: dict, errors: list[ValidationMessage]) -> None:
    """Flag non-core slots reused for sequential beats (selection collapse)."""
    by_slot: dict[str, list[str]] = {}
    for scene_id, scene in by_scene.items():
        slot = scene.slot or "core"
        if slot == "core":
            continue
        by_slot.setdefault(slot, []).append(scene_id)
    for slot, ids in by_slot.items():
        if len(ids) < 2:
            continue
        for source in ids:
            reachable = _reachable_from(by_scene, source) - {source}
            collided = sorted(set(ids) & reachable)
            if collided:
                _err(
                    errors,
                    "slot_reuse_on_path",
                    f"Slot {slot!r} holds scenes {sorted(ids)} where "
                    f"{source!r} can reach {collided}: interchangeable "
                    "alternatives must never be sequential beats, or module "
                    "selection collapses the story into a repeating loop.",
                    source,
                )
                break


def _check_escapable_cycles(by_scene: dict, errors: list[ValidationMessage]) -> None:
    """Flag scene loops with no authored way out toward an ending."""
    edges: dict[str, list[str]] = {}
    for scene_id, scene in by_scene.items():
        edges[scene_id] = [
            choice.next_scene_id
            for choice in (scene.choices or [])
            if choice.next_scene_id in by_scene
        ]
    # Nodes that can reach a terminal scene (reverse walk from terminals).
    can_finish: set[str] = set()
    queue: deque[str] = deque(
        [sid for sid, scene in by_scene.items() if scene.is_terminal]
    )
    reverse: dict[str, list[str]] = {sid: [] for sid in by_scene}
    for sid, outs in edges.items():
        for target in outs:
            reverse[target].append(sid)
    while queue:
        current = queue.popleft()
        if current in can_finish:
            continue
        can_finish.add(current)
        queue.extend(reverse[current])
    # Strongly connected components (Tarjan): loops are components with
    # more than one scene, or one scene pointing at itself.
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = [0]

    def strongconnect(node: str) -> None:
        index_of[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for target in edges.get(node, []):
            if target not in index_of:
                strongconnect(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index_of[target])
        if low[node] == index_of[node]:
            component = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            cyclic = len(component) > 1 or node in edges.get(node, [])
            if not cyclic:
                return
            exits = sorted(
                {
                    target
                    for member in component
                    for target in edges.get(member, [])
                    if target not in component and target in can_finish
                }
            )
            if not exits:
                _err(
                    errors,
                    "inescapable_cycle",
                    f"Scenes {sorted(component)} loop with no choice leading "
                    "toward an ending: the couple would replay them forever.",
                )

    for scene_id in by_scene:
        if scene_id not in index_of:
            strongconnect(scene_id)


def _check_livelock(
    errors: list[ValidationMessage], by_scene: dict, opening_id: str
) -> None:
    """Flag a package whose blind walk never reaches a terminal scene."""
    current_id: Optional[str] = opening_id
    seen: dict[str, int] = {}
    for _ in range(200):
        if current_id is None or current_id not in by_scene:
            return
        current = by_scene[current_id]
        if current.is_terminal:
            return
        if not current.choices:
            return  # already reported as dead_end
        seen[current_id] = seen.get(current_id, 0) + 1
        if seen[current_id] > 20:
            _err(
                errors,
                "possible_livelock",
                f"Blind walk revisits scene {current_id!r} without terminating; "
                "check that loops can exit.",
                current_id,
            )
            return
        current_id = current.choices[0].next_scene_id
