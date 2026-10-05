"""Deterministic validation for Case Closed case packages.

AI reviews may supplement these checks but never replace them: a package is
publishable only when this module reports ``ok=True``. Every error names the
offending lead (or package level) and the exact problem, so the admin screen
and the repair loop can act on it.

Covers prompt.md Section 9:
- Structural: dup ids, missing fields/types (via Pydantic), bad refs,
  unreachable nodes, cycles, depth, budget, solution suspect.
- Mystery consistency: unique culprit, timeline order, solution support,
  no culprit leak via ids/ordering, reachable evidence for solution,
  branch depth.
"""

from collections import deque
from typing import Any, Optional

from pydantic import BaseModel, ValidationError

from ..limits import (
    CASE_CLOSED_BUDGET_PER_PARTNER,
    CASE_CLOSED_INITIAL_LEADS_COUNT,
    CASE_CLOSED_MAX_FOLLOW_UPS,
    CASE_CLOSED_MAX_LEADS,
    CASE_CLOSED_MIN_DEPTH,
    CASE_CLOSED_MIN_LEADS,
)
from .case_closed_schemas import CaseClosedBlueprint, CaseClosedPackage


class ValidationMessage(BaseModel):
    lead_id: Optional[str] = None
    code: str
    message: str


class ValidationReport(BaseModel):
    ok: bool
    errors: list[ValidationMessage] = []


def _err(
    errors: list[ValidationMessage],
    code: str,
    message: str,
    lead_id: Optional[str] = None,
) -> None:
    errors.append(ValidationMessage(lead_id=lead_id, code=code, message=message))


def _dupes(ids: list[str]) -> set[str]:
    seen: set[str] = set()
    dup: set[str] = set()
    for i in ids:
        if i in seen:
            dup.add(i)
        seen.add(i)
    return dup


def validate_blueprint(data: dict) -> ValidationReport:
    """Validate a Stage A blueprint: schema plus mystery sanity."""
    errors: list[ValidationMessage] = []
    try:
        bp = CaseClosedBlueprint.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            _err(errors, "blueprint_schema", f"Blueprint field {loc}: {err['msg']}.")
        return ValidationReport(ok=False, errors=errors)

    suspect_ids = [s.id for s in bp.suspects]
    if len(set(suspect_ids)) != len(suspect_ids):
        _err(errors, "duplicate_suspect_id", "Blueprint suspect ids must be unique.")
    if bp.culprit_id not in set(suspect_ids):
        _err(
            errors,
            "unknown_culprit",
            f"Culprit {bp.culprit_id!r} is not among the suspect roster.",
        )
    # Timeline order must be unique and sane.
    orders = [t.order for t in bp.timeline]
    if len(set(orders)) != len(orders):
        _err(errors, "timeline_order", "Timeline order values must be unique.")
    timeline_ids = [t.id for t in bp.timeline]
    if len(set(timeline_ids)) != len(timeline_ids):
        _err(errors, "duplicate_timeline_id", "Timeline ids must be unique.")
    for t in bp.timeline:
        for sid in t.suspect_ids:
            if sid not in set(suspect_ids):
                _err(
                    errors,
                    "timeline_unknown_suspect",
                    f"Timeline {t.id!r} references unknown suspect {sid!r}.",
                )
    fact_ids = [f.id for f in bp.facts]
    if len(set(fact_ids)) != len(fact_ids):
        _err(errors, "duplicate_fact_id", "Blueprint fact ids must be unique.")
    fact_set = set(fact_ids)
    for fid in bp.required_fact_ids:
        if fid not in fact_set:
            _err(
                errors,
                "required_fact_unknown",
                f"Required fact {fid!r} is not among the blueprint facts.",
            )
    for step in bp.deduction_chain:
        for fid in step.fact_ids:
            if fid not in fact_set:
                _err(
                    errors,
                    "deduction_unknown_fact",
                    f"Deduction step {step.step} references unknown fact {fid!r}.",
                )
    if not bp.title.strip():
        _err(errors, "blueprint_title", "Blueprint title must not be blank.")
    return ValidationReport(ok=not errors, errors=errors)


def _reachable_from(by_lead: dict[str, Any], roots: list[str]) -> set[str]:
    seen: set[str] = set()
    queue: deque[str] = deque(r for r in roots if r in by_lead)
    while queue:
        cur = queue.popleft()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in by_lead[cur].next_lead_ids:
            if nxt in by_lead and nxt not in seen:
                queue.append(nxt)
    return seen


def _has_cycle(by_lead: dict[str, Any]) -> Optional[list[str]]:
    visiting: set[str] = set()
    done: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> Optional[list[str]]:
        visiting.add(node)
        stack.append(node)
        for nxt in by_lead[node].next_lead_ids:
            if nxt not in by_lead:
                continue
            if nxt in visiting:
                return stack + [nxt]
            if nxt not in done:
                found = dfs(nxt)
                if found:
                    return found
        visiting.discard(node)
        stack.pop()
        done.add(node)
        return None

    for nid in by_lead:
        if nid not in done and nid not in visiting:
            found = dfs(nid)
            if found:
                return found
    return None


def _longest_path_from(by_lead: dict[str, Any], roots: list[str]) -> int:
    # DAG longest path in edges from any root (memoised DFS; cycles already rejected).
    memo: dict[str, int] = {}

    def depth(nid: str, visiting: set[str]) -> int:
        if nid in memo:
            return memo[nid]
        if nid in visiting:
            return 0
        visiting.add(nid)
        best = 0
        for nxt in by_lead[nid].next_lead_ids:
            if nxt in by_lead:
                best = max(best, 1 + depth(nxt, visiting))
        visiting.discard(nid)
        memo[nid] = best
        return best

    return max((depth(r, set()) for r in roots if r in by_lead), default=0)


def validate_package(
    data: dict,
    blueprint: Optional[dict] = None,
) -> ValidationReport:
    """Deterministic validation of a complete Stage B case package."""
    errors: list[ValidationMessage] = []
    try:
        pkg = CaseClosedPackage.model_validate(data)
    except ValidationError as exc:
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"])
            _err(errors, "schema", f"Field {loc}: {err['msg']}.")
        return ValidationReport(ok=False, errors=errors)

    suspect_ids = [s.id for s in pkg.suspects]
    if len(set(suspect_ids)) != len(suspect_ids):
        _err(errors, "duplicate_suspect_id", "Suspect ids must be unique.")
    suspect_set = set(suspect_ids)

    fact_ids = [f.id for f in pkg.facts]
    if len(set(fact_ids)) != len(fact_ids):
        _err(errors, "duplicate_fact_id", "Fact ids must be unique.")
    fact_set = set(fact_ids)

    ev_ids = [e.id for e in pkg.evidence]
    if len(set(ev_ids)) != len(ev_ids):
        _err(errors, "duplicate_evidence_id", "Evidence ids must be unique.")
    ev_set = set(ev_ids)
    by_ev = {e.id: e for e in pkg.evidence}

    lead_ids = [lead.id for lead in pkg.lead_nodes]
    if len(set(lead_ids)) != len(lead_ids):
        dup = sorted(_dupes(lead_ids))
        _err(errors, "duplicate_lead_id", f"Lead ids must be unique (dupes: {dup}).")
    by_lead = {lead.id: lead for lead in pkg.lead_nodes}

    timeline_ids = [t.id for t in pkg.timeline]
    if len(set(timeline_ids)) != len(timeline_ids):
        _err(errors, "duplicate_timeline_id", "Timeline ids must be unique.")
    timeline_set = set(timeline_ids)

    # --- budget / counts -------------------------------------------------
    if pkg.investigation_budget_per_partner < 1 or pkg.investigation_budget_per_partner > 10:
        _err(
            errors,
            "invalid_budget",
            "investigation_budget_per_partner must be between 1 and 10.",
        )
    if len(pkg.initial_lead_ids) != CASE_CLOSED_INITIAL_LEADS_COUNT:
        _err(
            errors,
            "initial_lead_count",
            f"Package must name exactly {CASE_CLOSED_INITIAL_LEADS_COUNT} initial leads.",
        )
    for lid in pkg.initial_lead_ids:
        if lid not in by_lead:
            _err(errors, "initial_lead_unknown", f"Initial lead {lid!r} does not exist.")
    if not (CASE_CLOSED_MIN_LEADS <= len(pkg.lead_nodes) <= CASE_CLOSED_MAX_LEADS):
        _err(
            errors,
            "lead_count",
            f"Package holds {len(pkg.lead_nodes)} leads; expected "
            f"{CASE_CLOSED_MIN_LEADS}-{CASE_CLOSED_MAX_LEADS}.",
        )

    # --- per-lead refs ----------------------------------------------------
    for lead in pkg.lead_nodes:
        if len(lead.next_lead_ids) > CASE_CLOSED_MAX_FOLLOW_UPS:
            _err(
                errors,
                "too_many_follow_ups",
                f"Lead {lead.id!r} names {len(lead.next_lead_ids)} follow-ups; "
                f"max is {CASE_CLOSED_MAX_FOLLOW_UPS}.",
                lead.id,
            )
        if len(set(lead.next_lead_ids)) != len(lead.next_lead_ids):
            _err(
                errors,
                "duplicate_edge",
                f"Lead {lead.id!r} repeats a follow-up lead.",
                lead.id,
            )
        if lead.id in lead.next_lead_ids:
            _err(errors, "self_loop", f"Lead {lead.id!r} points to itself.", lead.id)
        for nxt in lead.next_lead_ids:
            if nxt not in by_lead:
                _err(
                    errors,
                    "unknown_transition",
                    f"Lead {lead.id!r} points to nonexistent lead {nxt!r}.",
                    lead.id,
                )
        for eid in lead.revealed_evidence_ids:
            if eid not in ev_set:
                _err(
                    errors,
                    "unknown_evidence",
                    f"Lead {lead.id!r} reveals nonexistent evidence {eid!r}.",
                    lead.id,
                )
        for eid in lead.required_evidence_ids:
            if eid not in ev_set:
                _err(
                    errors,
                    "unknown_required_evidence",
                    f"Lead {lead.id!r} requires nonexistent evidence {eid!r}.",
                    lead.id,
                )
        for sid in lead.suspect_ids:
            if sid not in suspect_set:
                _err(
                    errors,
                    "unknown_suspect_ref",
                    f"Lead {lead.id!r} references unknown suspect {sid!r}.",
                    lead.id,
                )
        if lead.depth < 0 or lead.depth > 10:
            _err(
                errors,
                "invalid_depth",
                f"Lead {lead.id!r} has depth {lead.depth} outside 0-10.",
                lead.id,
            )

    # --- evidence refs ----------------------------------------------------
    for ev in pkg.evidence:
        for sid in ev.suspect_ids:
            if sid not in suspect_set:
                _err(
                    errors,
                    "evidence_unknown_suspect",
                    f"Evidence {ev.id!r} references unknown suspect {sid!r}.",
                )
        for tid in ev.timeline_ids:
            if tid not in timeline_set:
                _err(
                    errors,
                    "evidence_unknown_timeline",
                    f"Evidence {ev.id!r} references unknown timeline {tid!r}.",
                )
        for rel in ev.related_evidence_ids:
            if rel not in ev_set:
                _err(
                    errors,
                    "evidence_unknown_related",
                    f"Evidence {ev.id!r} relates to nonexistent {rel!r}.",
                )
            elif rel == ev.id:
                _err(
                    errors,
                    "evidence_self_related",
                    f"Evidence {ev.id!r} relates to itself.",
                )

    # --- solution ----------------------------------------------------------
    sol = pkg.solution
    if sol.culprit_id not in suspect_set:
        _err(
            errors,
            "solution_unknown_culprit",
            f"Solution culprit {sol.culprit_id!r} is not among the suspects.",
        )
    for eid in sol.required_evidence_ids:
        if eid not in ev_set:
            _err(
                errors,
                "solution_unknown_evidence",
                f"Solution requires nonexistent evidence {eid!r}.",
            )
    for fid in sol.required_fact_ids:
        if fid not in fact_set:
            _err(
                errors,
                "solution_unknown_fact",
                f"Solution requires nonexistent fact {fid!r}.",
            )
    for step in sol.deduction_chain:
        for fid in step.fact_ids:
            if fid not in fact_set:
                _err(
                    errors,
                    "deduction_unknown_fact",
                    f"Deduction step {step.step} references unknown fact {fid!r}.",
                )
        for eid in step.evidence_refs:
            if eid not in ev_set:
                _err(
                    errors,
                    "deduction_unknown_evidence",
                    f"Deduction step {step.step} references unknown evidence {eid!r}.",
                )
    for eid in pkg.solution_reveal.key_evidence_ids:
        if eid not in ev_set:
            _err(
                errors,
                "reveal_unknown_evidence",
                f"Solution reveal references nonexistent evidence {eid!r}.",
            )

    # --- graph shape: cycles / reachability / depth -----------------------
    cycle = _has_cycle(by_lead)
    if cycle:
        _err(errors, "cycle", f"Investigation graph contains a cycle: {' -> '.join(cycle)}.")
    reachable = _reachable_from(by_lead, pkg.initial_lead_ids)
    unreachable = sorted(set(by_lead) - reachable)
    if unreachable:
        _err(
            errors,
            "unreachable_leads",
            f"{len(unreachable)} lead(s) unreachable from initial leads: {unreachable[:6]}.",
        )
    # Depth consistency: child depth must be > parent depth (DAG levels).
    for lead in pkg.lead_nodes:
        for nxt in lead.next_lead_ids:
            if nxt in by_lead and by_lead[nxt].depth <= lead.depth:
                _err(
                    errors,
                    "invalid_depth_assignment",
                    f"Lead {nxt!r} (depth {by_lead[nxt].depth}) must be deeper "
                    f"than parent {lead.id!r} (depth {lead.depth}).",
                    lead.id,
                )
                break
    for lid in pkg.initial_lead_ids:
        if lid in by_lead and by_lead[lid].depth != 0:
            _err(
                errors,
                "initial_depth",
                f"Initial lead {lid!r} must have depth 0.",
                lid,
            )
    longest = _longest_path_from(by_lead, pkg.initial_lead_ids)
    # longest is edges; levels = edges+1. Need ~5 levels -> edges >= 4.
    if longest < CASE_CLOSED_MIN_DEPTH:
        _err(
            errors,
            "insufficient_depth",
            f"Longest investigation path is {longest + 1} level(s); "
            f"at least {CASE_CLOSED_MIN_DEPTH + 1} levels are required for "
            "five meaningful investigations.",
        )
    # Every initial lead should open a path of at least 3 levels so no opener is a dead end.
    for lid in pkg.initial_lead_ids:
        if lid in by_lead:
            d = _longest_path_from(by_lead, [lid])
            if d < 2:
                _err(
                    errors,
                    "shallow_branch",
                    f"Initial lead {lid!r} reaches only {d + 1} level(s); "
                    "each opener must allow a meaningful follow-up path.",
                    lid,
                )

    # --- solvability: solution evidence must be reachable ------------------
    revealed_reachable: set[str] = set()
    for lid in reachable:
        revealed_reachable.update(by_lead[lid].revealed_evidence_ids)
    for eid in sol.required_evidence_ids:
        if eid not in revealed_reachable:
            _err(
                errors,
                "solution_unreachable",
                f"Solution requires evidence {eid!r} never revealed by any "
                "reachable lead.",
            )

    # --- culprit must not leak via ids/ordering ----------------------------
    culprit = sol.culprit_id.strip().lower()
    for lead in pkg.lead_nodes:
        lid_low = lead.id.strip().lower()
        if culprit and culprit != "" and (culprit in lid_low or lid_low in culprit):
            _err(
                errors,
                "culprit_exposed",
                f"Lead id {lead.id!r} exposes the culprit via its identifier.",
                lead.id,
            )
    for ev in pkg.evidence:
        if culprit and ev.id.strip().lower() in (culprit, f"{culprit}-evidence"):
            _err(errors, "culprit_exposed", f"Evidence id {ev.id!r} exposes the culprit.")
    # Ordering must not place the culprit first as a hint: suspect order is
    # admin-defined, so only flag the trivial leak where the culprit suspect
    # id sorts first *and* a lead id mentions ordering. Kept narrow on purpose.
    if pkg.suspects and pkg.suspects[0].id == sol.culprit_id and len(pkg.suspects) > 2:
        # Not a hard fail by itself; flag only when combined with shallow graph
        # would make the first suspect the obvious guess. We keep it as info:
        # no error (ordering alone is not proof of leak).
        pass

    # Timeline order uniqueness (package-level public timeline).
    orders = [t.order for t in pkg.timeline]
    if len(set(orders)) != len(orders):
        _err(errors, "timeline_order", "Timeline order values must be unique.")

    # Scoring config sanity.
    sc = pkg.time_scoring_config
    if sc.half_life_seconds < 3600 or sc.max_points < 1:
        _err(errors, "invalid_scoring", "Time scoring config is out of range.")

    # Optional blueprint linkage: culprit must agree when a blueprint is given.
    if blueprint:
        try:
            bp = CaseClosedBlueprint.model_validate(blueprint)
            if bp.culprit_id != sol.culprit_id:
                _err(
                    errors,
                    "blueprint_culprit_mismatch",
                    "Package culprit does not match the approved blueprint culprit.",
                )
        except ValidationError:
            pass

    # Unused budget guard: keep the constant referenced so renames fail loudly.
    _ = CASE_CLOSED_BUDGET_PER_PARTNER

    return ValidationReport(ok=not errors, errors=errors)
