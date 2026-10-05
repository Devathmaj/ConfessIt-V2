"""Case Closed mystery schemas: versioned, structured representation of a case.

Mirrors the Plot Twist pattern: AI-generated prose is never executed as game
logic. Every generated blueprint/package is parsed into these Pydantic models
first; anything that does not parse is rejected before validation. The same
models produce the strict JSON Schemas sent to the provider as Structured
Outputs (via the shared ``llm_client``), so the contract the model is asked
to honour and the contract the backend enforces are one definition, not two.

Gameplay is deterministic and never calls the LLM: it executes an immutable,
published ``CaseClosedPackage``.

Conventions (prompt.md Sections 8-9):
- String identifiers for suspects, leads, facts, evidence.
- DAG of leads: edges explicit via ``next_lead_ids``.
- Three initial leads, ~15-20 nodes, depth ~5, up to 3 follow-ups.
- Budgets enforced server-side (default 5 per partner).
- Player-facing models never carry the hidden solution or unrevealed graph.
"""

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

CaseClosedDifficulty = Literal["easy", "medium", "hard"]
EvidenceKind = Literal[
    "establishes_fact",
    "contradicts_statement",
    "supports_theory",
    "misleading",
]

CASE_CLOSED_SCHEMA_VERSION = 1
CASE_CLOSED_DEFAULT_BUDGET_PER_PARTNER = 5
CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS = 43200  # 12 hours
CASE_CLOSED_DEFAULT_MAX_POINTS = 100


# ---------------------------------------------------------------------------
# Stage A: generation config + blueprint (hidden authoritative truth)
# ---------------------------------------------------------------------------


class CaseClosedGenerationConfig(BaseModel):
    """What the administrator supplies before Stage A."""

    model_config = ConfigDict(extra="forbid")

    difficulty: CaseClosedDifficulty = "medium"
    theme: Optional[str] = Field(default=None, max_length=200)
    premise: Optional[str] = Field(default=None, max_length=2000)
    tone: Optional[str] = Field(default=None, max_length=200)
    suspect_count: int = Field(default=3, ge=3, le=5)
    target_lead_count: int = Field(default=18, ge=15, le=20)


class CaseClosedSuspectClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=1000)
    truthful: bool = False


class CaseClosedBlueprintSuspect(BaseModel):
    """Full suspect truth (Stage A only, never player-visible)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    background: str = Field(min_length=1, max_length=2000)
    relationship: str = Field(min_length=1, max_length=1000)
    actual_actions: str = Field(min_length=1, max_length=2000)
    claims: list[CaseClosedSuspectClaim] = Field(default_factory=list)


class CaseClosedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=1000)


class CaseClosedTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=200)
    order: int = Field(ge=0, le=1000)
    description: str = Field(min_length=1, max_length=2000)
    suspect_ids: list[str] = Field(default_factory=list)


class CaseClosedRedHerring(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=1000)
    explanation: str = Field(min_length=1, max_length=1000)


class CaseClosedDeductionStep(BaseModel):
    """One link in the canonical deduction chain."""

    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1, le=50)
    conclusion: str = Field(min_length=1, max_length=1000)
    fact_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class CaseClosedBlueprint(BaseModel):
    """Hidden authoritative truth of the mystery (Stage A output)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    opening_narrative: str = Field(min_length=1, max_length=4000)
    difficulty: CaseClosedDifficulty = "medium"
    incident_description: str = Field(min_length=1, max_length=4000)
    suspects: list[CaseClosedBlueprintSuspect] = Field(min_length=3, max_length=6)
    culprit_id: str = Field(min_length=1, max_length=64)
    motive: str = Field(min_length=1, max_length=2000)
    timeline: list[CaseClosedTimelineEvent] = Field(min_length=2, max_length=30)
    facts: list[CaseClosedFact] = Field(min_length=3, max_length=60)
    contradictions: list[str] = Field(default_factory=list)
    provable_theories: list[str] = Field(default_factory=list)
    red_herrings: list[CaseClosedRedHerring] = Field(default_factory=list)
    solution_summary: str = Field(min_length=1, max_length=2000)
    reveal_narrative: str = Field(min_length=1, max_length=4000)
    deduction_chain: list[CaseClosedDeductionStep] = Field(min_length=2, max_length=20)
    required_fact_ids: list[str] = Field(min_length=1, max_length=30)
    constraints: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage B: playable package (immutable once published)
# ---------------------------------------------------------------------------


class CaseClosedSuspect(BaseModel):
    """Player-visible suspect profile (no hidden truth)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    background: str = Field(min_length=1, max_length=2000)
    relationship: str = Field(min_length=1, max_length=1000)


class CaseClosedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    suspect_ids: list[str] = Field(default_factory=list)
    timeline_ids: list[str] = Field(default_factory=list)
    kind: EvidenceKind = "establishes_fact"
    related_evidence_ids: list[str] = Field(default_factory=list)


class CaseClosedLead(BaseModel):
    """One investigation node in the DAG. Edges are explicit."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    discovery_text: str = Field(min_length=1, max_length=3000)
    revealed_evidence_ids: list[str] = Field(default_factory=list)
    next_lead_ids: list[str] = Field(default_factory=list, max_length=3)
    required_evidence_ids: list[str] = Field(default_factory=list)
    suspect_ids: list[str] = Field(default_factory=list)
    depth: int = Field(ge=0, le=10)


class CaseClosedSolution(BaseModel):
    """Hidden canonical solution (server-side only)."""

    model_config = ConfigDict(extra="forbid")

    culprit_id: str = Field(min_length=1, max_length=64)
    motive: str = Field(min_length=1, max_length=2000)
    deduction_chain: list[CaseClosedDeductionStep] = Field(min_length=2, max_length=20)
    required_evidence_ids: list[str] = Field(min_length=1, max_length=30)
    required_fact_ids: list[str] = Field(default_factory=list)


class CaseClosedSolutionReveal(BaseModel):
    """Player-facing explanation, shown only after event finalization."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    narrative: str = Field(min_length=1, max_length=4000)
    why_others_ruled_out: str = Field(min_length=1, max_length=4000)
    key_evidence_ids: list[str] = Field(default_factory=list)


class CaseClosedTimeScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    half_life_seconds: int = Field(
        default=CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS, ge=3600, le=7 * 24 * 3600
    )
    max_points: int = Field(default=CASE_CLOSED_DEFAULT_MAX_POINTS, ge=1, le=1000)


class CaseClosedPackage(BaseModel):
    """The complete playable package, referencing its approved blueprint."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=CASE_CLOSED_SCHEMA_VERSION, ge=1, le=10)
    case_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    difficulty: CaseClosedDifficulty = "medium"
    opening_narrative: str = Field(min_length=1, max_length=4000)
    initial_lead_ids: list[str] = Field(min_length=3, max_length=3)
    suspects: list[CaseClosedSuspect] = Field(min_length=3, max_length=6)
    facts: list[CaseClosedFact] = Field(min_length=1, max_length=60)
    timeline: list[CaseClosedTimelineEvent] = Field(default_factory=list, max_length=30)
    evidence: list[CaseClosedEvidence] = Field(min_length=3, max_length=80)
    lead_nodes: list[CaseClosedLead] = Field(min_length=15, max_length=24)
    solution: CaseClosedSolution
    solution_reveal: CaseClosedSolutionReveal
    investigation_budget_per_partner: int = Field(
        default=CASE_CLOSED_DEFAULT_BUDGET_PER_PARTNER, ge=1, le=10
    )
    time_scoring_config: CaseClosedTimeScoringConfig = Field(
        default_factory=CaseClosedTimeScoringConfig
    )
    blueprint_ref: Optional[str] = Field(default=None, max_length=64)
    authoring_metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Strict JSON Schema helpers (same pattern as Plot Twist)
# ---------------------------------------------------------------------------


def _strict_node(node: Any) -> Any:
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
    import copy

    return _strict_node(copy.deepcopy(model.model_json_schema()))


def blueprint_json_schema() -> dict:
    return strict_schema_for(CaseClosedBlueprint)


def package_json_schema() -> dict:
    return strict_schema_for(CaseClosedPackage)


# ---------------------------------------------------------------------------
# Player-facing / API schemas (never carry hidden solution or full graph)
# ---------------------------------------------------------------------------


class CaseClosedLeadView(BaseModel):
    id: str
    title: str
    description: str
    investigated: bool = False
    available: bool = False
    depth: int = 0


class CaseClosedEvidenceView(BaseModel):
    id: str
    title: str
    description: str
    discovered_at: Optional[datetime] = None
    discovered_by: Optional[str] = None
    source_lead_id: Optional[str] = None
    suspect_ids: list[str] = []
    kind: str = "establishes_fact"


class CaseClosedSuspectView(BaseModel):
    id: str
    name: str
    background: str
    relationship: str


class CaseClosedInvestigationSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lead_id: str = Field(min_length=1, max_length=64)
    state_version: int = Field(ge=0)


class CaseClosedInvestigationResponse(BaseModel):
    lead_id: str
    discovery_text: str
    revealed_evidence: list[CaseClosedEvidenceView] = []
    already_known: bool = False
    my_remaining: int = 0
    partner_remaining: int = 0
    state_version: int = 0
    available_leads: list[CaseClosedLeadView] = []


class CaseClosedAccusationProposeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspect_id: str = Field(min_length=1, max_length=64)


class CaseClosedAccusationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suspect_id: str = Field(min_length=1, max_length=64)


class CaseClosedAccusationState(BaseModel):
    my_proposal: Optional[str] = None
    partner_proposal: Optional[str] = None
    my_confirmed: Optional[str] = None
    partner_confirmed: Optional[str] = None
    locked_suspect_id: Optional[str] = None
    locked_at: Optional[datetime] = None


class CaseClosedTimerState(BaseModel):
    started: bool = False
    first_investigation_at: Optional[datetime] = None
    locked_at: Optional[datetime] = None
    elapsed_seconds: Optional[int] = None


CaseClosedStage = Literal[
    "no_game",
    "selecting_partner",
    "investigating",
    "accusation_locked",
    "completed",
]


class CaseClosedCurrentState(BaseModel):
    """Safe player view: no solution, no unrevealed leads/evidence."""

    stage: str = "no_game"
    game_id: Optional[str] = None
    session_id: Optional[str] = None
    case_title: Optional[str] = None
    opening_narrative: Optional[str] = None
    suspects: list[CaseClosedSuspectView] = []
    available_leads: list[CaseClosedLeadView] = []
    my_path: list[str] = []
    investigated_lead_ids: list[str] = []
    evidence_board: list[CaseClosedEvidenceView] = []
    my_remaining: int = 0
    partner_remaining: int = 0
    accusation: CaseClosedAccusationState = Field(
        default_factory=CaseClosedAccusationState
    )
    timer: CaseClosedTimerState = Field(default_factory=CaseClosedTimerState)
    session_status: Optional[str] = None
    event_deadline: Optional[datetime] = None
    state_version: int = 0
    finalized: bool = False


class CaseClosedResultView(BaseModel):
    locked_suspect_id: Optional[str] = None
    correct: Optional[bool] = None
    score: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    finalized: bool = False
    culprit_id: Optional[str] = None
    solution_title: Optional[str] = None
    solution_narrative: Optional[str] = None
    why_others_ruled_out: Optional[str] = None


__all__ = [
    "CaseClosedGenerationConfig",
    "CaseClosedBlueprint",
    "CaseClosedBlueprintSuspect",
    "CaseClosedSuspectClaim",
    "CaseClosedSuspect",
    "CaseClosedFact",
    "CaseClosedTimelineEvent",
    "CaseClosedRedHerring",
    "CaseClosedDeductionStep",
    "CaseClosedEvidence",
    "CaseClosedLead",
    "CaseClosedPackage",
    "CaseClosedSolution",
    "CaseClosedSolutionReveal",
    "CaseClosedTimeScoringConfig",
    "blueprint_json_schema",
    "package_json_schema",
    "strict_schema_for",
    "CaseClosedLeadView",
    "CaseClosedEvidenceView",
    "CaseClosedSuspectView",
    "CaseClosedInvestigationSubmitRequest",
    "CaseClosedInvestigationResponse",
    "CaseClosedAccusationProposeRequest",
    "CaseClosedAccusationConfirmRequest",
    "CaseClosedAccusationState",
    "CaseClosedTimerState",
    "CaseClosedCurrentState",
    "CaseClosedResultView",
    "CASE_CLOSED_SCHEMA_VERSION",
    "CASE_CLOSED_DEFAULT_BUDGET_PER_PARTNER",
    "CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS",
    "CASE_CLOSED_DEFAULT_MAX_POINTS",
]
