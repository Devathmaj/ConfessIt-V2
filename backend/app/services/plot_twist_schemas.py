"""Plot Twist story schemas: the versioned, structured representation of a story.

AI-generated prose is never executed as game logic. Every generated package
is parsed into these Pydantic models first; anything that does not parse is
rejected before validation. The same models produce the strict JSON Schemas
sent to the provider as Structured Outputs, so the contract the model is
asked to honour and the contract the backend enforces are one definition,
not two.

State changes use a closed set of operations (set / increment / decrement /
add_flag / remove_flag) over declared variables. There is deliberately no
expression language: a model that cannot name an operator cannot smuggle one
in.
"""

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
    """What the administrator supplies before Stage A.

    Only ``difficulty`` is required (and the only knob the admin UI offers);
    everything else defaults so a story can always be generated, while tests
    and power users may pin the rest.
    """

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


def _strict_node(node: Any) -> Any:
    """Rewrite one schema node for strict Structured Outputs.

    Strict mode requires ``required`` to name every property, and refuses
    ``anyOf`` optionality: ``Optional[X]`` (``anyOf: [X, null]``) collapses
    to ``X``. Local Pydantic validation still enforces the real models
    afterwards, so this only tightens what the provider is asked to emit.
    """
    if isinstance(node, list):
        return [_strict_node(item) for item in node]
    if not isinstance(node, dict):
        return node
    choices = node.get("anyOf")
    if isinstance(choices, list):
        non_null = [c for c in choices if not (isinstance(c, dict) and c.get("type") == "null")]
        if len(non_null) == len(choices) - 1 and len(non_null) == 1:
            return _strict_node(non_null[0])
    out = {
        key: (_strict_node(value) if isinstance(value, (dict, list)) else value)
        for key, value in node.items()
    }
    if isinstance(out.get("properties"), dict):
        out["required"] = sorted(out["properties"].keys())
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
    return out


def strict_schema_for(model: Any) -> dict:
    """The provider-facing strict JSON Schema for a Pydantic model."""
    import copy

    return _strict_node(copy.deepcopy(model.model_json_schema()))


def blueprint_json_schema() -> dict:
    """The strict JSON Schema sent to the provider for Stage A."""
    return strict_schema_for(BlueprintDef)


def package_json_schema() -> dict:
    """The strict JSON Schema sent to the provider for Stage B."""
    return strict_schema_for(StoryPackageDef)


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
