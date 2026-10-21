# app/routers/couple_questionnaire.py

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_db
from ..limits import PAGE_OFFSET_MAX, USER_DEPARTMENT_MAX, USER_YEAR_MAX, USER_YEAR_MIN
from ..models import (
    CoupleAnswerRequest,
    CoupleCurrentState,
    CouplePartnerPage,
    CouplePredictionRequest,
    CoupleSelectionRequest,
    UserDetails,
)
from ..postgres import Database
from ..services import couple_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/couple-questionnaire", tags=["Couple Questionnaire"])

# The player-facing surface of the feature. Every route resolves the caller as a
# participant and reads and writes only that caller's own place in the game -- nothing
# on someone else's behalf and no reading of others' originals before the completed
# result. The session belongs to the caller's own id on every request, never a
# client-supplied one. Choosing a partner sends no notification: mutual selection is
# discovery-based, and a choice is visible only to its chooser until both have chosen.


@router.get("", response_model=CoupleCurrentState)
def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """This caller's place in the featured Couple Questionnaire.

    Reading never creates anything: a fresh caller reads partner selection, a
    paired caller their questions, predictions or score. The screen calls this
    on every entry and resumes exactly where the stored rows say.
    """
    return couple_gameplay.current_state(db, user)


@router.get("/partners", response_model=CouplePartnerPage)
def partner_candidates(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching participants to skip."),
    limit: int = Query(
        50,
        ge=1,
        le=couple_gameplay.COUPLE_PARTNERS_MAX_LIMIT,
        description="How many matching participants to return. Clamped so one request can never materialise the whole participant table.",
    ),
    q: Optional[str] = Query(
        None,
        max_length=100,
        description="Case-insensitive substring match on the candidate's name, run in SQL.",
    ),
    gender_mode: Literal["different", "same"] = Query(
        "different",
        description="'different' hides the caller's own gender; 'same' shows only it. A discovery aid, never a rule about who may be chosen.",
    ),
    year: Optional[int] = Query(
        None, ge=USER_YEAR_MIN, le=USER_YEAR_MAX, description="Exact year of study, half of the cohort filter."
    ),
    department: Optional[str] = Query(
        None, max_length=USER_DEPARTMENT_MAX, description="Exact department, half of the cohort filter, case-insensitive."
    ),
) -> CouplePartnerPage:
    """One page of participants the caller may choose, excluding only themselves.

    Declared before ``/{...}``-style routes so ``partners`` is never read as an id.
    Every filter is a SQL WHERE, the page a SQL LIMIT/OFFSET and the search a SQL
    ILIKE, so the screen pages downwards instead of filtering in the browser. The
    gender default and same-gender toggle stay discovery aids, never rules about
    who may be chosen; picking an id is validated on its own.
    """
    return couple_gameplay.list_partners(
        db,
        user,
        skip=skip,
        limit=limit,
        query=q,
        gender_mode=gender_mode,
        year=year,
        department=department,
    )


@router.post("/selection", response_model=CoupleCurrentState)
def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Record this caller's partner choice, starting a session only if it is mutual.

    Choosing is an upsert of the caller's one selection row: a changed mind replaces
    the previous pick, and nobody is told. When the chosen user has already chosen
    back, exactly one session holding both users and the featured game is created.
    The response is the caller's new state, so the screen advances in one round trip.
    """
    return couple_gameplay.select_partner(db, user, payload.selected_user_id)


@router.post("/answers", response_model=CoupleCurrentState)
def answer_question(
    payload: CoupleAnswerRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Store one of this caller's own answers.

    The question must be one of the fifteen this caller was assigned; answers are
    insert-only, so a second submit is refused rather than overwritten. When both
    sides' originals are all stored, the session moves to predictions in the same
    transaction.
    """
    return couple_gameplay.submit_answer(db, user, payload)


@router.post("/predictions", response_model=CoupleCurrentState)
def predict_answer(
    payload: CouplePredictionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> CoupleCurrentState:
    """Store one prediction of the partner's answer, scored against the original.

    The question must be one of the fifteen the partner answered, and the verdict
    is computed server-side from the stored original -- nothing the client sends
    can mark itself correct. When both sides' predictions are all stored, the
    session completes and the response carries the settled score.
    """
    return couple_gameplay.submit_prediction(db, user, payload)
