# app/routers/case_closed.py

from typing import List

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    CaseClosedConfirmRequest,
    CaseClosedInvestigationSubmitRequest,
    CaseClosedProposeRequest,
    CouplePartnerCandidate,
    CoupleSelectionRequest,
)
from ..postgres import Database
from ..services import case_closed_gameplay
from ..services.authorization import require_participant
from ..services.case_closed_schemas import (
    CaseClosedCurrentState,
    CaseClosedResultView,
)

router = APIRouter(prefix="/games/case-closed", tags=["Case Closed"])


@router.get("", response_model=CaseClosedCurrentState)
def current_state(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    """This caller's safe place in the active Case Closed event."""
    return CaseClosedCurrentState(**case_closed_gameplay.current_state(db, user))


@router.get("/partners", response_model=List[CouplePartnerCandidate])
def partner_candidates(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> List[CouplePartnerCandidate]:
    return case_closed_gameplay.list_partners(db, user)


@router.post("/selection", response_model=CaseClosedCurrentState)
def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.choose_partner(db, user, payload.selected_user_id)
    )


@router.post("/start", response_model=CaseClosedCurrentState)
def start_event(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(**case_closed_gameplay.enter_event(db, user))


@router.post("/investigate")
def investigate(
    payload: CaseClosedInvestigationSubmitRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
):
    """Investigate one available lead (deterministic, no LLM).

    Returns the safe session view plus the lead's discovery text.
    Retries of an already-visited lead replay the stored discovery.
    """
    result = case_closed_gameplay.investigate_lead(
        db, user, payload.lead_id, payload.state_version
    )
    replay = bool(result.pop("_replay", False))
    lead_id = result.pop("_lead_id", payload.lead_id)
    discovery = result.pop("_discovery_text", "")
    already_known = bool(result.pop("_already_known", False))
    state = CaseClosedCurrentState(**result)
    return {
        "state": state,
        "lead_id": lead_id,
        "discovery_text": discovery,
        "already_known": already_known,
        "replay": replay,
    }


@router.post("/accusation/propose", response_model=CaseClosedCurrentState)
def propose_accusation(
    payload: CaseClosedProposeRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.propose_accusation(db, user, payload.suspect_id)
    )


@router.post("/accusation/confirm", response_model=CaseClosedCurrentState)
def confirm_accusation(
    payload: CaseClosedConfirmRequest,
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedCurrentState:
    return CaseClosedCurrentState(
        **case_closed_gameplay.confirm_accusation(db, user, payload.suspect_id)
    )


@router.get("/result", response_model=CaseClosedResultView)
def get_result(
    db: Database = Depends(get_db),
    user=Depends(require_participant),
) -> CaseClosedResultView:
    return CaseClosedResultView(**case_closed_gameplay.get_result(db, user))
