"""Deterministic cooperative Case Closed gameplay.

Sessions use the event's published package and mutual-selection pairing. Actions, evidence,
accusations, timing, and scoring are enforced server-side with transactional locking.
"""

import logging
from datetime import datetime, timezone, date, time
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import or_, select, text, func
from sqlalchemy.exc import IntegrityError

logger = logging.getLogger(__name__)

from ..db_models import (
    CaseClosedEvidenceRecord,
    CaseClosedGameRecord,
    CaseClosedInvestigationRecord,
    CaseClosedPackageRecord,
    CaseClosedSessionRecord,
    CoupleGameSessionRecord,
    UserRecord,
)
from ..limits import (
    CASE_CLOSED_BUDGET_PER_PARTNER,
    CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS,
    CASE_CLOSED_DEFAULT_MAX_POINTS,
    CASE_CLOSED_SESSION_LOCK_KEY,
)
from ..models import CouplePartnerPage
from ..postgres import Database, is_valid_id

GAME_ID = "case-closed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today():
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Pure scoring (also used by league adapter + finalization)
# ---------------------------------------------------------------------------


def score_for_elapsed(
    elapsed_seconds: Optional[int],
    half_life_seconds: int = CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS,
    max_points: int = CASE_CLOSED_DEFAULT_MAX_POINTS,
) -> int:
    """Time-decay score for a correct accusation.

    score = max(1, round(max_points * 2 ** (-elapsed / half_life))).
    Faster correct always >= slower correct. Wrong/incomplete handled by
    callers (exactly zero).
    """
    if elapsed_seconds is None or elapsed_seconds < 0:
        elapsed_seconds = 0
    try:
        hl = int(half_life_seconds) or CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS
        mp = int(max_points) or CASE_CLOSED_DEFAULT_MAX_POINTS
    except (TypeError, ValueError):
        hl, mp = CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS, CASE_CLOSED_DEFAULT_MAX_POINTS
    hl = max(3600, hl)
    mp = max(1, mp)
    return max(1, round(mp * (2.0 ** (-float(elapsed_seconds) / float(hl)))))


def score_for_result(is_correct: bool, elapsed_seconds: Optional[int], game_row) -> int:
    if not is_correct:
        return 0
    hl = getattr(game_row, "scoring_half_life_seconds", CASE_CLOSED_DEFAULT_HALF_LIFE_SECONDS)
    mp = getattr(game_row, "scoring_max_points", CASE_CLOSED_DEFAULT_MAX_POINTS)
    return score_for_elapsed(elapsed_seconds, hl, mp)


# ---------------------------------------------------------------------------
# Event / couple resolution
# ---------------------------------------------------------------------------


def _latest_global_game(session) -> Optional[CaseClosedGameRecord]:
    from . import game_periods

    game = session.scalar(
        select(CaseClosedGameRecord)
        .where(CaseClosedGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )
    if game is None or game.status != "active":
        return None
    if game.case_package_id is None:
        return None
    if game.period_end_date is not None and game_periods.is_reset_due(game.period_end_date, _today()):
        return None
    return game


def _my_couple_session(session, user_id: str):
    from . import couple_gameplay

    questionnaire = couple_gameplay._latest_playable_game(session)
    if questionnaire is None:
        return None
    return session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == questionnaire.id,
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
        )
    )


def _package_for(session, package_id: str) -> dict:
    row = session.get(CaseClosedPackageRecord, package_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This case version is no longer available.",
        )
    return dict(row.package or {})


def _canonical(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted([str(a), str(b)]))  # type: ignore[return-value]


def _event_deadline(game: CaseClosedGameRecord) -> Optional[datetime]:
    """The session's actual end instant.

    Game periods use an *exclusive* end date: a ``period_end_date`` of
    January 3 is playable through January 2 and ends at 00:00 UTC on January
    3, when the scheduler is due to roll it over. Chat retention and the
    deadline shown to players share this one value, so there is no separate
    timestamp that could disagree with the scheduler.
    """
    if game.period_end_date is None:
        return None
    return datetime.combine(game.period_end_date, time.min, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Safe view construction (never leaks solution or unrevealed graph)
# ---------------------------------------------------------------------------


def _available_lead_ids(
    package: dict,
    my_path: list[str],
    my_visited: set[str],
    discovered_lead_ids: set[str],
    discovered_evidence_ids: set[str],
) -> list[str]:
    by_lead = {lead.get("id"): lead for lead in (package.get("lead_nodes") or [])}
    initials = list(package.get("initial_lead_ids") or [])
    if not my_path:
        return [lid for lid in initials if lid in by_lead]
    last = my_path[-1]
    node = by_lead.get(last, {})
    out: list[str] = []
    for nxt in (node.get("next_lead_ids") or []):
        if nxt not in by_lead or nxt in my_visited:
            continue
        target = by_lead[nxt]
        req = set(target.get("required_evidence_ids") or [])
        if not req.issubset(discovered_evidence_ids):
            continue
        out.append(nxt)
    # Fallback: unvisited initials remain enterable when the cursor dead-ends,
    # so a couple can still use all five actions across openers. Initials are
    # always enterable at any time -- no prereqs on depth-0.
    for lid in initials:
        if lid not in my_visited and lid not in out and lid in by_lead:
            # Only add if not already the cursor's child set; initials are
            # always enterable at any time (no prereqs on depth-0).
            out.append(lid)
    # Preserve order, dedupe.
    seen: set[str] = set()
    ordered: list[str] = []
    for lid in out:
        if lid not in seen:
            seen.add(lid)
            ordered.append(lid)
    return ordered


def _assemble_state(session, user, game, couple, row) -> dict:
    from ..models import UserDetails  # noqa: F401 (type only)

    package = _package_for(session, str(row.case_package_id))
    by_lead = {lead.get("id"): lead for lead in (package.get("lead_nodes") or [])}
    by_ev = {ev.get("id"): ev for ev in (package.get("evidence") or [])}
    budget = int(package.get("investigation_budget_per_partner") or CASE_CLOSED_BUDGET_PER_PARTNER)

    is_a = str(row.user_a_id) == str(user.id)
    my_used = int(row.actions_used_a if is_a else row.actions_used_b)
    partner_used = int(row.actions_used_b if is_a else row.actions_used_a)
    my_path = list(row.path_a if is_a else row.path_b)

    inv_rows = session.scalars(
        select(CaseClosedInvestigationRecord)
        .where(CaseClosedInvestigationRecord.session_id == row.id)
        .order_by(CaseClosedInvestigationRecord.seq.asc())
    ).all()
    ev_rows = session.scalars(
        select(CaseClosedEvidenceRecord)
        .where(CaseClosedEvidenceRecord.session_id == row.id)
        .order_by(CaseClosedEvidenceRecord.discovered_at.asc())
    ).all()

    my_visited = {r.lead_id for r in inv_rows if str(r.actor_user_id) == str(user.id)}
    all_visited_leads = {r.lead_id for r in inv_rows}
    discovered_lead_ids = {r.lead_id for r in ev_rows} | all_visited_leads
    discovered_evidence_ids = {r.evidence_id for r in ev_rows}
    ev_by_id = {r.evidence_id: r for r in ev_rows}

    # Suspects: public profiles only.
    suspects = [
        {"id": s.get("id"), "name": s.get("name"), "background": s.get("background"), "relationship": s.get("relationship")}
        for s in (package.get("suspects") or [])
    ]
    # Evidence board: only discovered.
    board = []
    for r in ev_rows:
        definition = by_ev.get(r.evidence_id, {})
        board.append(
            {
                "id": r.evidence_id,
                "title": str(definition.get("title") or r.evidence_id),
                "description": str(definition.get("description") or ""),
                "discovered_at": r.discovered_at,
                "discovered_by": str(r.discoverer_user_id),
                "source_lead_id": r.lead_id,
                "suspect_ids": list(definition.get("suspect_ids") or []),
                "kind": str(definition.get("kind") or "establishes_fact"),
            }
        )
    # Available leads: authorized subset only (title/description, no discovery).
    avail_ids = _available_lead_ids(package, my_path, my_visited, discovered_lead_ids, discovered_evidence_ids)
    available = []
    for lid in avail_ids:
        lead = by_lead.get(lid, {})
        available.append(
            {
                "id": lid,
                "title": str(lead.get("title") or lid),
                "description": str(lead.get("description") or ""),
                "investigated": lid in discovered_lead_ids,
                "available": True,
                "depth": int(lead.get("depth") or 0),
            }
        )

    my_proposal = str(row.proposal_a) if is_a and row.proposal_a else (str(row.proposal_b) if not is_a and row.proposal_b else None)
    partner_proposal = str(row.proposal_b) if is_a and row.proposal_b else (str(row.proposal_a) if not is_a and row.proposal_a else None)
    my_confirmed = str(row.confirmed_a) if is_a and row.confirmed_a else (str(row.confirmed_b) if not is_a and row.confirmed_b else None)
    partner_confirmed = str(row.confirmed_b) if is_a and row.confirmed_b else (str(row.confirmed_a) if not is_a and row.confirmed_a else None)

    if row.first_investigation_at is not None:
        end = row.accusation_locked_at or _now()
        elapsed = int((end - row.first_investigation_at).total_seconds())
    else:
        elapsed = None

    if row.status == "investigating":
        stage = "investigating"
    elif row.status == "accusation_locked":
        stage = "accusation_locked"
    else:
        stage = "completed"

    return {
        "stage": stage,
        "game_id": str(game.id),
        "session_id": str(row.id),
        "case_title": str(package.get("title") or ""),
        "opening_narrative": str(package.get("opening_narrative") or ""),
        "suspects": suspects,
        "available_leads": available,
        "my_path": my_path,
        "investigated_lead_ids": sorted(discovered_lead_ids),
        "evidence_board": board,
        "my_remaining": max(0, budget - my_used),
        "partner_remaining": max(0, budget - partner_used),
        "accusation": {
            "my_proposal": my_proposal,
            "partner_proposal": partner_proposal,
            "my_confirmed": my_confirmed,
            "partner_confirmed": partner_confirmed,
            "locked_suspect_id": str(row.locked_culprit_id) if row.locked_culprit_id else None,
            "locked_at": row.accusation_locked_at,
        },
        "timer": {
            "started": row.first_investigation_at is not None,
            "first_investigation_at": row.first_investigation_at,
            "locked_at": row.accusation_locked_at,
            "elapsed_seconds": row.elapsed_seconds if row.elapsed_seconds is not None else elapsed,
        },
        "session_status": row.status,
        "event_deadline": _event_deadline(game),
        "state_version": int(row.state_version or 0),
        "finalized": bool(row.finalized),
    }


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _get_or_create_session_row(session, game, user_a_id: str, user_b_id: str):
    row = session.scalar(
        select(CaseClosedSessionRecord).where(
            CaseClosedSessionRecord.case_closed_game_id == game.id,
            CaseClosedSessionRecord.user_a_id == user_a_id,
            CaseClosedSessionRecord.user_b_id == user_b_id,
        )
    )
    if row is not None:
        # Converge a session created before the column existed onto the same
        # authoritative deadline every new session gets.
        if row.expires_at is None:
            row.expires_at = _event_deadline(game)
        return row
    row = CaseClosedSessionRecord(
        case_closed_game_id=str(game.id),
        case_package_id=str(game.case_package_id),
        user_a_id=user_a_id,
        user_b_id=user_b_id,
        status="investigating",
        # The pinned event's actual end, not a duration recomputed from today.
        expires_at=_event_deadline(game),
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        # Concurrent create won; re-read the winner.
        session.rollback()
        row = session.scalar(
            select(CaseClosedSessionRecord).where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
        )
        if row is None:
            raise
    return row


def _require_membership(row, user_id: str) -> bool:
    return str(row.user_a_id) == str(user_id) or str(row.user_b_id) == str(user_id)


# ---------------------------------------------------------------------------
# Player operations
# ---------------------------------------------------------------------------


def current_state(db: Database, user) -> dict:
    with db.sessions() as session:
        game = _latest_global_game(session)
        if game is None:
            return {
                "stage": "no_game", "game_id": None, "session_id": None,
                "case_title": None, "opening_narrative": None, "suspects": [],
                "available_leads": [], "my_path": [], "investigated_lead_ids": [],
                "evidence_board": [], "my_remaining": 0, "partner_remaining": 0,
                "accusation": {"my_proposal": None, "partner_proposal": None, "my_confirmed": None, "partner_confirmed": None, "locked_suspect_id": None, "locked_at": None},
                "timer": {"started": False, "first_investigation_at": None, "locked_at": None, "elapsed_seconds": None},
                "session_status": None, "event_deadline": None, "state_version": 0, "finalized": False,
            }
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            return {
                "stage": "selecting_partner", "game_id": str(game.id), "session_id": None,
                "case_title": None, "opening_narrative": None, "suspects": [],
                "available_leads": [], "my_path": [], "investigated_lead_ids": [],
                "evidence_board": [], "my_remaining": 0, "partner_remaining": 0,
                "accusation": {"my_proposal": None, "partner_proposal": None, "my_confirmed": None, "partner_confirmed": None, "locked_suspect_id": None, "locked_at": None},
                "timer": {"started": False, "first_investigation_at": None, "locked_at": None, "elapsed_seconds": None},
                "session_status": None, "event_deadline": _event_deadline(game), "state_version": 0, "finalized": False,
            }
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = session.scalar(
            select(CaseClosedSessionRecord).where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
        )
        if row is None:
            # Reads never create rows: surface the case header + initials so
            # the page can offer Begin without a session yet.
            package = _package_for(session, str(game.case_package_id))
            budget = int(package.get("investigation_budget_per_partner") or CASE_CLOSED_BUDGET_PER_PARTNER)
            by_lead = {lead.get("id"): lead for lead in (package.get("lead_nodes") or [])}
            return {
                "stage": "selecting_partner", "game_id": str(game.id), "session_id": None,
                "case_title": str(package.get("title") or ""),
                "opening_narrative": str(package.get("opening_narrative") or ""),
                "suspects": [
                    {"id": s.get("id"), "name": s.get("name"), "background": s.get("background"), "relationship": s.get("relationship")}
                    for s in (package.get("suspects") or [])
                ],
                "available_leads": [
                    {"id": lid, "title": str(by_lead.get(lid, {}).get("title") or lid), "description": str(by_lead.get(lid, {}).get("description") or ""), "investigated": False, "available": True, "depth": 0}
                    for lid in (package.get("initial_lead_ids") or []) if lid in by_lead
                ],
                "my_path": [], "investigated_lead_ids": [], "evidence_board": [],
                "my_remaining": budget, "partner_remaining": budget,
                "accusation": {"my_proposal": None, "partner_proposal": None, "my_confirmed": None, "partner_confirmed": None, "locked_suspect_id": None, "locked_at": None},
                "timer": {"started": False, "first_investigation_at": None, "locked_at": None, "elapsed_seconds": None},
                "session_status": None, "event_deadline": _event_deadline(game), "state_version": 0, "finalized": False,
            }
        return _assemble_state(session, user, game, couple, row)


def enter_event(db: Database, user) -> dict:
    from .game_configs import lock_game_for_play

    with db.sessions.begin() as session:
        lock_game_for_play(session, GAME_ID)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CASE_CLOSED_SESSION_LOCK_KEY})
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="No Case Closed event is live.")
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = _get_or_create_session_row(session, game, user_a_id, user_b_id)
        session.flush()
        snapshot = _assemble_state(session, user, game, couple, row)
    return snapshot


def list_partners(db: Database, user, **filters) -> CouplePartnerPage:
    """One page of the questionnaire's candidate list, not a second one.

    The page, search and filters are the shared SQL ones (see
    ``couple_gameplay.list_partners`` for what each accepts).
    """
    from . import couple_gameplay

    return couple_gameplay.list_partners(db, user, **filters)


def choose_partner(db: Database, user, selected_user_id: str) -> dict:
    from . import couple_gameplay

    couple_gameplay.select_partner(db, user, selected_user_id)
    return current_state(db, user)


def investigate_lead(db: Database, user, lead_id: str, state_version: int) -> dict:
    """Resolve one investigation action deterministically (no LLM)."""
    from .game_configs import lock_game_for_play

    with db.sessions.begin() as session:
        lock_game_for_play(session, GAME_ID)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CASE_CLOSED_SESSION_LOCK_KEY})
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This event has ended.")
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = session.scalar(
            select(CaseClosedSessionRecord)
            .where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
            .with_for_update()
        )
        if row is None:
            row = _get_or_create_session_row(session, game, user_a_id, user_b_id)
            session.flush()
            row = session.scalar(
                select(CaseClosedSessionRecord)
                .where(CaseClosedSessionRecord.id == row.id)
                .with_for_update()
            )
        assert row is not None
        if not _require_membership(row, str(user.id)):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session.")
        if row.status in ("completed", "expired") or row.locked_culprit_id is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Accusation already locked.")
        if row.status not in ("investigating", "accusation_locked"):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Session is not playable.")

        package = _package_for(session, str(row.case_package_id))
        by_lead = {lead.get("id"): lead for lead in (package.get("lead_nodes") or [])}
        by_ev = {ev.get("id"): ev for ev in (package.get("evidence") or [])}
        budget = int(package.get("investigation_budget_per_partner") or CASE_CLOSED_BUDGET_PER_PARTNER)
        if lead_id not in by_lead:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown lead.")

        # Optimistic concurrency: stale version replays stored outcome when
        # this actor already visited the lead; otherwise 409.
        if int(state_version) != int(row.state_version or 0):
            prior = session.scalar(
                select(CaseClosedInvestigationRecord).where(
                    CaseClosedInvestigationRecord.session_id == row.id,
                    CaseClosedInvestigationRecord.actor_user_id == str(user.id),
                    CaseClosedInvestigationRecord.lead_id == lead_id,
                )
            )
            if prior is not None:
                snapshot = _assemble_state(session, user, game, couple, row)
                lead = by_lead[lead_id]
                return {
                    **snapshot,
                    "_replay": True,
                    "_lead_id": lead_id,
                    "_discovery_text": str(lead.get("discovery_text") or ""),
                    "_already_known": True,
                }
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Stale state; refresh and retry.")

        is_a = str(row.user_a_id) == str(user.id)
        my_visited_rows = session.scalars(
            select(CaseClosedInvestigationRecord).where(
                CaseClosedInvestigationRecord.session_id == row.id,
                CaseClosedInvestigationRecord.actor_user_id == str(user.id),
            )
        ).all()
        my_visited = {r.lead_id for r in my_visited_rows}
        if lead_id in my_visited:
            # Idempotent retry: replay without charging or bumping.
            snapshot = _assemble_state(session, user, game, couple, row)
            lead = by_lead[lead_id]
            return {
                **snapshot,
                "_replay": True,
                "_lead_id": lead_id,
                "_discovery_text": str(lead.get("discovery_text") or ""),
                "_already_known": True,
            }

        ev_rows = session.scalars(
            select(CaseClosedEvidenceRecord).where(CaseClosedEvidenceRecord.session_id == row.id)
        ).all()
        all_inv = session.scalars(
            select(CaseClosedInvestigationRecord).where(CaseClosedInvestigationRecord.session_id == row.id)
        ).all()
        discovered_lead_ids = {r.lead_id for r in ev_rows} | {r.lead_id for r in all_inv}
        discovered_evidence_ids = {r.evidence_id for r in ev_rows}
        my_path = list(row.path_a if is_a else row.path_b)
        avail = _available_lead_ids(package, my_path, my_visited, discovered_lead_ids, discovered_evidence_ids)
        if lead_id not in avail:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Lead is not available from your current path.")

        lead = by_lead[lead_id]
        globally_known = lead_id in discovered_lead_ids
        my_used = int(row.actions_used_a if is_a else row.actions_used_b)
        if not globally_known and my_used >= budget:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Investigation budget exhausted.")

        now = _now()
        if globally_known:
            # Free navigation: record the visit, advance cursor, no charge.
            seq = int(session.scalar(select(func.count()).select_from(CaseClosedInvestigationRecord).where(CaseClosedInvestigationRecord.session_id == row.id)) or 0) + 1
            session.add(
                CaseClosedInvestigationRecord(
                    session_id=str(row.id), seq=seq, lead_id=lead_id,
                    actor_user_id=str(user.id), already_known=True,
                )
            )
            if is_a:
                row.path_a = [*my_path, lead_id]
            else:
                row.path_b = [*my_path, lead_id]
            row.state_version = int(row.state_version or 0) + 1
            session.flush()
            snapshot = _assemble_state(session, user, game, couple, row)
            return {
                **snapshot,
                "_replay": False,
                "_lead_id": lead_id,
                "_discovery_text": str(lead.get("discovery_text") or ""),
                "_already_known": True,
            }

        # New discovery: charge exactly once, persist evidence, maybe start timer.
        if row.first_investigation_at is None:
            row.first_investigation_at = now
        for eid in (lead.get("revealed_evidence_ids") or []):
            if eid not in by_ev:
                continue
            if eid in discovered_evidence_ids:
                continue
            session.add(
                CaseClosedEvidenceRecord(
                    session_id=str(row.id), evidence_id=eid, lead_id=lead_id,
                    discoverer_user_id=str(user.id), discovered_at=now,
                )
            )
        seq = int(session.scalar(select(func.count()).select_from(CaseClosedInvestigationRecord).where(CaseClosedInvestigationRecord.session_id == row.id)) or 0) + 1
        session.add(
            CaseClosedInvestigationRecord(
                session_id=str(row.id), seq=seq, lead_id=lead_id,
                actor_user_id=str(user.id), already_known=False,
            )
        )
        # Ensure lead-keyed discovery is visible even when it reveals zero
        # catalogue evidence: derive from investigations as well.
        if is_a:
            row.actions_used_a = my_used + 1
            row.path_a = [*my_path, lead_id]
        else:
            row.actions_used_b = my_used + 1
            row.path_b = [*my_path, lead_id]
        row.state_version = int(row.state_version or 0) + 1
        session.flush()
        # Recompute discovered set including this lead for the response view.
        snapshot = _assemble_state(session, user, game, couple, row)
        # Patch investigated set to include evidence-less leads.
        inv_all = session.scalars(
            select(CaseClosedInvestigationRecord).where(CaseClosedInvestigationRecord.session_id == row.id)
        ).all()
        all_leads = sorted({r.lead_id for r in inv_all} | set(snapshot.get("investigated_lead_ids") or []))
        snapshot["investigated_lead_ids"] = all_leads
        return {
            **snapshot,
            "_replay": False,
            "_lead_id": lead_id,
            "_discovery_text": str(lead.get("discovery_text") or ""),
            "_already_known": False,
        }


def propose_accusation(db: Database, user, suspect_id: str) -> dict:
    from .game_configs import lock_game_for_play

    with db.sessions.begin() as session:
        lock_game_for_play(session, GAME_ID)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CASE_CLOSED_SESSION_LOCK_KEY})
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This event has ended.")
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = session.scalar(
            select(CaseClosedSessionRecord)
            .where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the event first.")
        if row.locked_culprit_id is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Accusation already locked.")
        package = _package_for(session, str(row.case_package_id))
        valid = {s.get("id") for s in (package.get("suspects") or [])}
        if suspect_id not in valid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown suspect.")
        if str(row.user_a_id) == str(user.id):
            row.proposal_a = suspect_id
        else:
            row.proposal_b = suspect_id
        row.state_version = int(row.state_version or 0) + 1
        session.flush()
        return _assemble_state(session, user, game, couple, row)


def confirm_accusation(db: Database, user, suspect_id: str) -> dict:
    """Explicit confirmation; locks only when both confirm the same suspect."""
    from .game_configs import lock_game_for_play

    with db.sessions.begin() as session:
        lock_game_for_play(session, GAME_ID)
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": CASE_CLOSED_SESSION_LOCK_KEY})
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(status_code=status.HTTP_410_GONE, detail="This event has ended.")
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Form a mutual pair first.")
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = session.scalar(
            select(CaseClosedSessionRecord)
            .where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
            .with_for_update()
        )
        if row is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Enter the event first.")
        if row.locked_culprit_id is not None:
            # Idempotent replay for the locked suspect; conflict otherwise.
            if str(row.locked_culprit_id) == str(suspect_id):
                return _assemble_state(session, user, game, couple, row)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Accusation already locked.")
        package = _package_for(session, str(row.case_package_id))
        valid = {s.get("id") for s in (package.get("suspects") or [])}
        if suspect_id not in valid:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown suspect.")
        # A client can only confirm for itself, never for the partner.
        if str(row.user_a_id) == str(user.id):
            row.confirmed_a = suspect_id
        else:
            row.confirmed_b = suspect_id
        # Lock only when both explicitly confirmed the same valid suspect.
        if row.confirmed_a is not None and row.confirmed_b is not None and str(row.confirmed_a) == str(row.confirmed_b) == str(suspect_id):
            now = _now()
            row.locked_culprit_id = suspect_id
            row.accusation_locked_at = now
            if row.first_investigation_at is not None:
                row.elapsed_seconds = int((now - row.first_investigation_at).total_seconds())
            else:
                row.elapsed_seconds = 0
            row.status = "accusation_locked"
        row.state_version = int(row.state_version or 0) + 1
        session.flush()
        return _assemble_state(session, user, game, couple, row)


def get_result(db: Database, user) -> dict:
    """Final result: hidden correctness until event finalization."""
    with db.sessions() as session:
        game = session.scalar(
            select(CaseClosedGameRecord).order_by(CaseClosedGameRecord.created_at.desc()).limit(1)
        )
        if game is None:
            return {"locked_suspect_id": None, "correct": None, "score": None, "elapsed_seconds": None, "finalized": False, "culprit_id": None, "solution_title": None, "solution_narrative": None, "why_others_ruled_out": None}
        couple = _my_couple_session(session, str(user.id))
        if couple is None:
            return {"locked_suspect_id": None, "correct": None, "score": None, "elapsed_seconds": None, "finalized": bool(game.finalized), "culprit_id": None, "solution_title": None, "solution_narrative": None, "why_others_ruled_out": None}
        user_a_id, user_b_id = _canonical(str(couple.user_a_id), str(couple.user_b_id))
        row = session.scalar(
            select(CaseClosedSessionRecord).where(
                CaseClosedSessionRecord.case_closed_game_id == game.id,
                CaseClosedSessionRecord.user_a_id == user_a_id,
                CaseClosedSessionRecord.user_b_id == user_b_id,
            )
        )
        if row is None:
            return {"locked_suspect_id": None, "correct": None, "score": None, "elapsed_seconds": None, "finalized": bool(game.finalized), "culprit_id": None, "solution_title": None, "solution_narrative": None, "why_others_ruled_out": None}
        finalized = bool(row.finalized and game.finalized)
        if not finalized:
            return {
                "locked_suspect_id": str(row.locked_culprit_id) if row.locked_culprit_id else None,
                "correct": None, "score": None,
                "elapsed_seconds": row.elapsed_seconds,
                "finalized": False,
                "culprit_id": None, "solution_title": None, "solution_narrative": None, "why_others_ruled_out": None,
            }
        package = _package_for(session, str(row.case_package_id))
        reveal = package.get("solution_reveal") or {}
        solution = package.get("solution") or {}
        return {
            "locked_suspect_id": str(row.locked_culprit_id) if row.locked_culprit_id else None,
            "correct": bool(row.is_correct) if row.is_correct is not None else None,
            "score": int(row.score or 0),
            "elapsed_seconds": row.elapsed_seconds,
            "finalized": True,
            "culprit_id": str(solution.get("culprit_id") or ""),
            "solution_title": str(reveal.get("title") or ""),
            "solution_narrative": str(reveal.get("narrative") or ""),
            "why_others_ruled_out": str(reveal.get("why_others_ruled_out") or ""),
        }


def finalize_event(db: Database, game_id: str) -> dict:
    """Idempotent event finalization: correctness + time-decay scoring + reveal.

    The call that closes the event also tells every participant who the
    culprit was. The notices go out only after the reveal itself has
    committed, and the idempotent re-entry sends none, so a retry can never
    announce a solution the game has not recorded twice.
    """
    recipients: list[str] = []
    package: dict = {}
    with db.sessions.begin() as session:
        game = session.get(CaseClosedGameRecord, game_id)
        if game is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event not found.")
        if game.finalized:
            finalized_count = int(session.scalar(select(func.count()).select_from(CaseClosedSessionRecord).where(CaseClosedSessionRecord.case_closed_game_id == game.id, CaseClosedSessionRecord.finalized.is_(True))) or 0)
            return {"game_id": str(game.id), "finalized": True, "sessions": finalized_count}
        package = _package_for(session, str(game.case_package_id)) if game.case_package_id else {}
        culprit = str((package.get("solution") or {}).get("culprit_id") or "")
        rows = session.scalars(
            select(CaseClosedSessionRecord).where(CaseClosedSessionRecord.case_closed_game_id == game.id).with_for_update()
        ).all()
        seen: set[str] = set()
        for row in rows:
            # Every session's members hear the reveal, locked or not: the
            # notification names who did it and nothing about the reader.
            for member in (row.user_a_id, row.user_b_id):
                member_id = str(member) if member else ""
                if member_id and member_id not in seen:
                    seen.add(member_id)
                    recipients.append(member_id)
            if row.finalized:
                continue
            if row.locked_culprit_id is not None and culprit:
                row.is_correct = str(row.locked_culprit_id) == culprit
                row.score = score_for_result(bool(row.is_correct), row.elapsed_seconds, game)
                row.status = "completed"
            else:
                row.is_correct = False if row.locked_culprit_id is not None else None
                # Incomplete/expired sessions score exactly zero.
                if row.locked_culprit_id is None:
                    row.is_correct = None
                row.score = 0
                if row.status == "investigating":
                    row.status = "expired"
                elif row.status == "accusation_locked" and row.locked_culprit_id is None:
                    row.status = "expired"
            row.finalized = True
            row.finalized_at = _now()
        game.finalized = True
        game.finalized_at = _now()
        game.status = "ended"
        game.ended_at = _now()
        session.flush()
        sessions_count = len(rows)
    # The reveal is committed, so the notices cannot announce a solution the
    # game still has not recorded; the send itself is best effort.
    _notify_culprit_reveal(db, recipients, package)
    return {"game_id": str(game_id), "finalized": True, "sessions": sessions_count}


def _notify_culprit_reveal(
    db: Database, recipient_ids: list[str], package: dict
) -> None:
    """Tell every participant who the culprit was, best effort.

    The notice names the culprit (and the reveal's title) and nothing about
    the reader -- not their accusation, correctness or score. When the
    package cannot name the culprit there is nothing honest to send, so
    nothing is sent. A failed write is logged, never raised: the reveal is
    already committed and the inbox is not the result.
    """
    if not recipient_ids:
        return
    solution = package.get("solution") or {}
    culprit_id = str(solution.get("culprit_id") or "")
    suspects = {
        str(suspect.get("id")): suspect for suspect in package.get("suspects") or []
    }
    culprit_name = str((suspects.get(culprit_id) or {}).get("name") or culprit_id).strip()
    if not culprit_name:
        return
    reveal_title = str((package.get("solution_reveal") or {}).get("title") or "").strip()
    # 120 and 500 are the notification columns; a long suspect name is
    # clipped rather than allowed to fail the insert.
    heading = f"Case Closed: the culprit was {culprit_name}"[:120]
    body = (
        f"{reveal_title} — open Case Closed to read the full reveal."
        if reveal_title
        else "Open Case Closed to read the full reveal."
    )[:500]

    from .notification_service import create_notification_service

    with db.sessions() as session:
        regnos = [
            str(regno)
            for (regno,) in session.execute(
                select(UserRecord.regno).where(UserRecord.id.in_(recipient_ids))
            ).all()
        ]
    for regno in regnos:
        try:
            create_notification_service(
                recipient_regno=regno, heading=heading, body=body, db=db
            )
        except Exception:
            logger.warning(
                "Failed to queue the Case Closed reveal notification for %s", regno
            )
