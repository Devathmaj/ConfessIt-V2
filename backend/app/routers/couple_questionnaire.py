# app/routers/couple_questionnaire.py

from typing import List

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    CoupleAnswerRequest,
    CoupleCurrentState,
    CouplePartnerCandidate,
    CouplePredictionRequest,
    CoupleSelectionRequest,
    UserDetails,
)
from ..postgres import Database
from ..services import couple_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/couple-questionnaire", tags=["Couple Questionnaire"])

# The player-facing surface of the feature. Every route resolves the caller as a participant
# and reads and writes only that caller's own place in the game: their selection, their
# questions, their answers, their predictions, their score. There is no way to choose for
# someone else, answer for them, or read their originals before the completed result --
# the session the caller belongs to is derived from their own id on every request, never
# from an id the client supplied. Choosing a partner sends no notification anywhere: the
# mutual-selection mechanism is discovery-based, and the choice is only visible to its
# chooser until both sides have chosen each other.


@router.get("", response_model=CoupleCurrentState)
async def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """This caller's place in the featured Couple Questionnaire.

    Reading this never creates anything: a fresh caller reads partner selection,
    a paired caller reads their questions, predictions or score depending on how
    far both sides have played. The screen calls this on every entry -- refresh,
    return, reconnect -- and resumes exactly where the stored rows say.
    """
    return couple_gameplay.current_state(db, user)


@router.get("/partners", response_model=List[CouplePartnerCandidate])
async def partner_candidates(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> List[CouplePartnerCandidate]:
    """Every participant the caller may choose, excluding only themselves.

    Declared before ``/{...}``-style routes so ``partners`` is never read as an
    id. The male/female/other default and the same-gender toggle live on the
    screen; this list carries the gender each row needs for that filter and
    decides nothing about who may be chosen.
    """
    return couple_gameplay.list_partners(db, user)


@router.post("/selection", response_model=CoupleCurrentState)
async def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Record this caller's partner choice, starting a session only if it is mutual.

    Choosing is an upsert of the caller's one selection row: a changed mind
    replaces the previous pick, and nobody is told. When the chosen user has
    already chosen this caller back, exactly one session is created holding both
    users and the featured game it belongs to. The response is the caller's new
    state, so the screen advances in one round trip.
    """
    return couple_gameplay.select_partner(db, user, payload.selected_user_id)


@router.post("/answers", response_model=CoupleCurrentState)
async def answer_question(
    payload: CoupleAnswerRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Store one of this caller's own answers.

    The question must be one of the fifteen this caller was assigned; answers
    are insert-only, so a second submit for the same question is refused rather
    than overwritten. When both sides' fifteen originals are all stored, the
    session moves to predictions in the same transaction.
    """
    return couple_gameplay.submit_answer(db, user, payload)


@router.post("/predictions", response_model=CoupleCurrentState)
async def predict_answer(
    payload: CouplePredictionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Store one prediction of the partner's answer, scored against the original.

    The question must be one of the fifteen the partner answered, and the
    verdict is computed server-side from the stored original -- nothing the
    client sends can mark itself correct. When both sides' fifteen predictions
    are all stored, the session completes in the same transaction and the
    response carries the settled score.
    """
    return couple_gameplay.submit_prediction(db, user, payload)
