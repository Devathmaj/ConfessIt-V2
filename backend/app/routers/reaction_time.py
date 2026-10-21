# app/routers/reaction_time.py

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    ReactionTimeCompleteRequest,
    ReactionTimeStartRequest,
    ReactionTimeState,
    UserDetails,
)
from ..postgres import Database
from ..services import reaction_time_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/reaction-time", tags=["Reaction Time"])

# Routes use the caller's stored attempts. The server assigns attempt numbers and
# derives best and average times from those rows.


@router.get("", response_model=ReactionTimeState)
def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ReactionTimeState:
    """This caller's place in today's featured Reaction Time game.

    Reading never creates anything: a fresh caller reads zero submissions, a
    returning one the submissions they left. The screen calls this on every
    entry and resumes exactly where the stored rows say.
    """
    return reaction_time_gameplay.current_state(db, user.id)


@router.post("/start", response_model=ReactionTimeState)
def start_attempt(
    payload: ReactionTimeStartRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ReactionTimeState:
    """Consume one submission: the blue-screen click that starts an attempt.

    The pending submission is created here, before the red wait begins -- so the
    attempt is spent even if the click that follows is too early, and a refresh
    mid-wait cannot grant another one. A caller out of submissions, or still with
    a pending attempt, is refused.
    """
    return reaction_time_gameplay.start_attempt(db, user.id, payload)


@router.post("/complete", response_model=ReactionTimeState)
def complete_attempt(
    payload: ReactionTimeCompleteRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ReactionTimeState:
    """Settle a pending attempt with its measured result, exactly once.

    ``valid`` carries the client-measured reaction time (green until click);
    ``too_early`` carries only informational elapsed time, excluded from every
    statistic. Settled rows are immutable: a second submit for the same attempt
    is refused.
    """
    return reaction_time_gameplay.complete_attempt(db, user.id, payload)
