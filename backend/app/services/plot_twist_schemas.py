"""Pydantic schemas and strict JSON Schema conversions for Plot Twist packages."""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..limits import (
    PLOT_TWIST_ACTORS,
    PLOT_TWIST_CONDITION_OPS,
    PLOT_TWIST_DIFFICULTIES,
    PLOT_TWIST_ENDING_TYPES,
    PLOT_TWIST_MAX_BATCH_SCENES,
    PLOT_TWIST_SCENE_TYPES,
    PLOT_TWIST_STATE_OPS,
)

SceneType = Literal["opening", "encounter", "connector", "discovery", "ending"]
Actor = Literal["participant_one", "participant_two", "either", "joint"]
StateOp = Literal["set", "increment", "decrement", "add_flag", "remove_flag"]
ConditionOp = Literal["eq", "gte", "lte", "has_flag", "missing_flag"]
Difficulty = Literal["easy", "medium", "hard"]
EndingType = Literal[
    "escape", "failure", "treasure", "capture", "mystery_solved", "twist"
]


class GenerationConfig(BaseModel):
    """Administrator configuration for Stage A story generation."""

    model_config = ConfigDict(extra="forbid")

    difficulty: Difficulty = "medium"
    theme: Optional[str] = None
    premise: Optional[str] = None
    genre: Optional[str] = None
    tone: Optional[str] = None
    decision_count: int = Field(default=8, ge=3, le=24)
    interchangeable_count: int = Field(default=4, ge=0, le=12)
    ending_types: list[str] = Field(default_factory=lambda: ["escape", "failure"])
    allow_treasure: bool = True
    max_scenes: int = Field(default=24, ge=5, le=40)


class StateVarDef(BaseModel):
    """One declared game-state variable and its permitted range."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    kind: Literal["int", "flag"] = "int"
    min_value: Optional[int] = None
    max_value: Optional[int] = None
    initial: Any = None
    description: Optional[str] = None


class StateChange(BaseModel):
    """One safe, predefined state mutation attached to a choice."""

    model_config = ConfigDict(extra="forbid")

    op: StateOp
    variable: str = Field(min_length=1, max_length=64)
    value: Optional[Any] = None


class ChoiceCondition(BaseModel):
    """When a choice is offered: a machine-evaluable predicate over state."""

    model_config = ConfigDict(extra="forbid")

    variable: str = Field(min_length=1, max_length=64)
    op: ConditionOp
    value: Optional[Any] = None


class ChoiceDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=500)
    consequence_text: Optional[str] = Field(default=None, max_length=1000)
    conditions: list[ChoiceCondition] = Field(default_factory=list)
    state_changes: list[StateChange] = Field(default_factory=list)
    next_scene_id: str = Field(min_length=1, max_length=64)
    reward_tier: Optional[str] = None


class SceneDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    type: SceneType
    title: str = Field(min_length=1, max_length=200)
    narrative_text: str = Field(min_length=1, max_length=4000)
    actor: Actor = "either"
    slot: str = Field(default="core", min_length=1, max_length=64)
    requires_tags: list[str] = Field(default_factory=list)
    provides_tags: list[str] = Field(default_factory=list)
    forbidden_tags: list[str] = Field(default_factory=list)
    entry_conditions: list[ChoiceCondition] = Field(default_factory=list)
    choices: list[ChoiceDef] = Field(default_factory=list)
    is_terminal: bool = False
    ending_id: Optional[str] = None
    reward_tier: Optional[str] = None


class EndingDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    type: EndingType
    title: str = Field(min_length=1, max_length=200)
    narrative: str = Field(min_length=1, max_length=4000)
    conditions: list[ChoiceCondition] = Field(default_factory=list)
    priority: int = Field(default=0, ge=0, le=100)
    rewards: dict[str, Any] = Field(default_factory=dict)
    success: bool = False
    reward_tier: Optional[str] = None


class SlotDef(BaseModel):
    """One interchangeable scene slot: what may fill it and what it guarantees."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    accepts_tags: list[str] = Field(default_factory=list)
    provides_tags: list[str] = Field(default_factory=list)
    max_alternatives: int = Field(default=3, ge=1, le=6)
    allow_repeat: bool = False


class RewardTierDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tier: str = Field(min_length=1, max_length=32)
    points: int = Field(ge=0, le=100)
    description: Optional[str] = None


class BlueprintDef(BaseModel):
    """The approved contract every later scene-generation request must follow."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    premise: str = Field(min_length=1, max_length=2000)
    narrative_rules: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    world_details: list[str] = Field(default_factory=list)
    state_vars: list[StateVarDef] = Field(default_factory=list)
    main_path: list[str] = Field(default_factory=list)
    slots: list[SlotDef] = Field(default_factory=list)
    required_modules: list[str] = Field(default_factory=list)
    encounter_categories: list[str] = Field(default_factory=list)
    endings: list[EndingDef] = Field(default_factory=list)
    reward_tiers: list[RewardTierDef] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    genre: Optional[str] = None
    tone: Optional[str] = None
    difficulty: str = "medium"


class StoryPackageDef(BaseModel):
    """The complete playable package, referencing its approved blueprint."""

    model_config = ConfigDict(extra="forbid")

    story_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    genre: Optional[str] = None
    tone: Optional[str] = None
    difficulty: str = "medium"
    blueprint_ref: str = Field(min_length=1, max_length=64)
    # Maximum decisions allowed before concluding playthrough at best ending.
    max_decisions: int = Field(default=24, ge=5, le=40)
    initial_state: dict[str, Any] = Field(default_factory=dict)
    state_vars: list[StateVarDef] = Field(default_factory=list)
    opening_scene_id: str = Field(min_length=1, max_length=64)
    scenes: list[SceneDef] = Field(default_factory=list)
    endings: list[EndingDef] = Field(default_factory=list)
    rewards: dict[str, Any] = Field(default_factory=dict)


class SceneBatchDef(BaseModel):
    """One interchangeable batch for large stories: scenes for named slots."""

    model_config = ConfigDict(extra="forbid")

    slot: str = Field(min_length=1, max_length=64)
    scenes: list[SceneDef] = Field(
        default_factory=list, max_length=PLOT_TWIST_MAX_BATCH_SCENES
    )


_ANY_BRANCHES: list[dict] = [
    {"type": "string"},
    {"type": "integer"},
    {"type": "boolean"},
    {"type": "array", "items": {"type": "string"}},
    {"type": "null"},
]


def _has_type(node: dict) -> bool:
    return bool({"type", "anyOf", "oneOf", "$ref", "enum", "const"} & set(node))


def _is_free_form_map(node: dict) -> bool:
    """Check if field is a free-form dictionary unrepresentable in strict schema mode."""
    return (
        node.get("type") == "object"
        and not isinstance(node.get("properties"), dict)
        and node.get("additionalProperties") is not False
    )


def _strict_node(node: Any) -> Any:
    """Adapt JSON schema node for strict Structured Outputs compliance."""
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    if _is_free_form_map(node):
        return None
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in ("properties", "$defs") and isinstance(value, dict):
            container: dict[str, Any] = {}
            for name, sub in value.items():
                item = _strict_node(sub)
                if item is not None:
                    container[name] = item
            out[key] = container
        elif key == "anyOf" and isinstance(value, list):
            branches: list[dict] = []
            for branch in value:
                item = _strict_node(branch)
                if item is None or not _has_type(item):
                    branches.extend(dict(extra) for extra in _ANY_BRANCHES)
                else:
                    branches.append(item)
            unique: list[dict] = []
            for branch in branches:
                if branch not in unique:
                    unique.append(branch)
            out[key] = unique
        elif isinstance(value, (dict, list)):
            item = _strict_node(value)
            out[key] = {} if item is None else item
        else:
            out[key] = value
    if not _has_type(out):
        return {"anyOf": [dict(extra) for extra in _ANY_BRANCHES]}
    if isinstance(out.get("properties"), dict):
        out["required"] = sorted(out["properties"].keys())
        out["additionalProperties"] = False
    elif out.get("type") == "object":
        out["additionalProperties"] = False
    return out


def strict_schema_for(model: Any) -> dict:
    """The provider-facing strict JSON Schema for a Pydantic model."""
    import copy

    return _strict_node(copy.deepcopy(model.model_json_schema()))


def blueprint_json_schema() -> dict:
    """The strict JSON Schema sent to the provider for Stage A."""
    return strict_schema_for(BlueprintDef)


def package_json_schema(
    blueprint: Optional[dict] = None, max_scenes: Optional[int] = None
) -> dict:
    """Strict JSON schema for Stage B investigation packages."""
    schema = strict_schema_for(StoryPackageDef)
    if max_scenes:
        scenes = schema.get("properties", {}).get("scenes")
        if isinstance(scenes, dict):
            scenes["maxItems"] = int(max_scenes)
    if blueprint:
        properties: dict[str, Any] = {}
        for var in blueprint.get("state_vars") or []:
            name = var.get("name")
            if not name:
                continue
            if var.get("kind") == "flag":
                properties[name] = {"type": "array", "items": {"type": "string"}}
            else:
                node: dict[str, Any] = {"type": "integer"}
                if var.get("min_value") is not None:
                    node["minimum"] = var["min_value"]
                if var.get("max_value") is not None:
                    node["maximum"] = var["max_value"]
                properties[name] = node
        if properties:
            schema["properties"]["initial_state"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": sorted(properties),
            }
            schema["required"] = sorted(set(schema.get("required") or []) | {"initial_state"})
    return schema


def scene_batch_json_schema() -> dict:
    """The strict JSON Schema for interchangeable batch requests."""
    return strict_schema_for(SceneBatchDef)


# Re-export vocabularies so prompts and validators share one source.
__all__ = [
    "Actor",
    "BlueprintDef",
    "ChoiceCondition",
    "ChoiceDef",
    "ConditionOp",
    "Difficulty",
    "EndingDef",
    "EndingType",
    "GenerationConfig",
    "RewardTierDef",
    "SceneBatchDef",
    "SceneDef",
    "SceneType",
    "SlotDef",
    "StateChange",
    "StateOp",
    "StateVarDef",
    "StoryPackageDef",
    "blueprint_json_schema",
    "package_json_schema",
    "scene_batch_json_schema",
    "strict_schema_for",
]
