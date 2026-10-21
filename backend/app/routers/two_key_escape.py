# app/routers/two_key_escape.py

from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_db
from ..limits import PAGE_OFFSET_MAX, USER_DEPARTMENT_MAX, USER_YEAR_MAX, USER_YEAR_MIN
from ..models import (
    CouplePartnerPage,
    CoupleSelectionRequest,
    TwoKeyEscapeKeyRequest,
    TwoKeyEscapeLeaderboardEntry,
    TwoKeyEscapeMoveRequest,
    TwoKeyEscapeRoleRequest,
    TwoKeyEscapeState,
    UserDetails,
)
from ..postgres import Database
from ..services import two_key_escape_gameplay
from ..services.authorization import require_participant
from ..services.couple_gameplay import COUPLE_PARTNERS_MAX_LIMIT

router = APIRouter(prefix="/games/two-key-escape", tags=["Two-Key Escape"])

# The caller's mutual-selection session determines the couple; client IDs are ignored.
# Reads are passive, and the client polls for updates after writes and while waiting.


@router.get("", response_model=TwoKeyEscapeState)
def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """This caller's place in the couple's maze.

    Reading never creates anything: a caller with no couple session reads
    ``no_couple``, a couple that never confirmed a role reads ``selecting_roles``
    with the shared template, and a playing couple reads the same authoritative
    overlay on every load -- refresh, return or reconnect.
    """
    return two_key_escape_gameplay.current_state(db, user)


@router.get("/partners", response_model=CouplePartnerPage)
def partner_candidates(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching participants to skip."),
    limit: int = Query(
        50,
        ge=1,
        le=COUPLE_PARTNERS_MAX_LIMIT,
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
    The questionnaire's own candidate list: settling the pairing here or there
    chooses from the same people under the same rules -- SQL page, search and
    filters (see ``couple_gameplay.list_partners``).
    """
    return two_key_escape_gameplay.partner_candidates(
        db,
        user,
        skip=skip,
        limit=limit,
        query=q,
        gender_mode=gender_mode,
        year=year,
        department=department,
    )


@router.post("/selection", response_model=TwoKeyEscapeState)
def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Record this caller's partner choice through the questionnaire's selection.

    A changed mind replaces the previous pick; a mutual pick creates the one session
    for the pair -- visible in both games at once, both reading the same session row.
    Answers the caller's escape state, so the screen advances in one round trip.
    """
    return two_key_escape_gameplay.choose_partner(db, user, payload.selected_user_id)


@router.post("/role-submit", response_model=TwoKeyEscapeState)
def submit_role(
    payload: TwoKeyEscapeRoleRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Record this caller's confirmed role choice.

    Writes only the caller's own submission, never the partner's. When both
    submissions are stored and distinct, the assignment finalizes atomically in
    the same transaction; identical ones finalize nothing and the response carries
    the conflict both sides resolve. Safe to retry: a finalized assignment is
    returned unchanged.
    """
    return two_key_escape_gameplay.submit_role(db, user, payload.role)


@router.post("/role-change", response_model=TwoKeyEscapeState)
def change_role(
    payload: TwoKeyEscapeRoleRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Replace this caller's confirmed choice before finalization.

    Validated against the partner's stored choice on the backend: a
    now-distinct pair finalizes atomically. Refused once finalized, so the
    control the page removes is gone server-side as well.
    """
    return two_key_escape_gameplay.change_role(db, user, payload.role)


@router.post("/move", response_model=TwoKeyEscapeState)
def move(
    payload: TwoKeyEscapeMoveRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Advance the character one cell for the assigned Navigator.

    Validated server-side against the stored board; refusals change nothing
    at all.
    """
    return two_key_escape_gameplay.move(db, user, payload.direction)


@router.post("/key", response_model=TwoKeyEscapeState)
def use_key(
    payload: TwoKeyEscapeKeyRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Use one key for the assigned Key Operator.

    Keys are reusable: every use costs the cooldown, never a charge. Opens the
    *different* lock the maze's hidden mapping assigns to the clicked lock -- never
    the clicked itself -- sends any previously opened lock back, then blocks a
    different eligible cell after verifying the maze stays solvable. Refusals change
    nothing: only a successful activation stamps the server-side cooldown.
    """
    return two_key_escape_gameplay.use_key(db, user, payload.key_index)


@router.get("/leaderboard", response_model=List[TwoKeyEscapeLeaderboardEntry])
def get_leaderboard(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> List[TwoKeyEscapeLeaderboardEntry]:
    """This period's couples in rank order: completed first, then by play."""
    return two_key_escape_gameplay.leaderboard(db, user)
