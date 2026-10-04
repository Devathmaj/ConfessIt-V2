# app/routers/plot_twist.py

from typing import List

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    CouplePartnerCandidate,
    CoupleSelectionRequest,
    PlotTwistChoiceSubmitRequest,
    PlotTwistCurrentState,
    UserDetails,
)
from ..postgres import Database
from ..services import plot_twist_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/plot-twist", tags=["Plot Twist"])

# The player-facing surface of the feature. Every route resolves the caller
# as a participant and reads and writes only that caller's own place in the
# story: their selection, their couple's session, their assigned turns.
# Pairing reuses the questionnaire's mutual selection; the story executes a
# published, versioned package deterministically -- the model is never
# called during gameplay.


@router.get("", response_model=PlotTwistCurrentState)
def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> PlotTwistCurrentState:
    """This caller's place in the active Plot Twist event.

    Reading never creates gameplay rows: a fresh caller reads partner
    selection, a paired caller reads their current scene or wait state,
    and a finished caller reads the settled outcome. The screen calls this
    on every entry and polls it while waiting for the partner.
    """
    return plot_twist_gameplay.current_state(db, user)


@router.get("/partners", response_model=List[CouplePartnerCandidate])
def partner_candidates(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> List[CouplePartnerCandidate]:
    """Every participant the caller may choose, excluding only themselves.

    The questionnaire's candidate list, not a second one: pairing here and
    pairing there choose from the same people under the same rules.
    """
    return plot_twist_gameplay.list_partners(db, user)


@router.post("/selection", response_model=PlotTwistCurrentState)
def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> PlotTwistCurrentState:
    """Record this caller's partner choice through the shared selection.

    A mutual pick starts the couple's story session (with its deterministic
    module selection persisted up front) when a Plot Twist event is live.
    """
    return plot_twist_gameplay.choose_partner(db, user, payload.selected_user_id)


@router.post("/start", response_model=PlotTwistCurrentState)
def start_event(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> PlotTwistCurrentState:
    """Enter the active Plot Twist event.

    Creates this couple's story session (with its deterministic module
    selection persisted up front) when the pair is mutual and an event is
    live, or fetches it when it already exists. Pairs formed while no
    event was live reach the story through here instead of re-picking.
    """
    return plot_twist_gameplay.enter_event(db, user)


@router.post("/choices", response_model=PlotTwistCurrentState)
def submit_choice(
    payload: PlotTwistChoiceSubmitRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> PlotTwistCurrentState:
    """Submit the assigned decision for the current scene.

    The choice must be offered in the caller's current scene and the turn
    must belong to the caller; the version must match the stored one, so a
    stale screen cannot overwrite newer game state. A repeat submit of an
    already-recorded turn answers the stored outcome without applying
    consequences twice.
    """
    return plot_twist_gameplay.submit_choice(db, user, payload.choice_id, payload.state_version)
