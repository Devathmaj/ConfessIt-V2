# app/routers/two_key_escape.py

from typing import List

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    CouplePartnerCandidate,
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

router = APIRouter(prefix="/games/two-key-escape", tags=["Two-Key Escape"])

# The player-facing surface of the feature. Every route resolves the caller
# as a participant and derives the couple from the caller's own
# mutual-selection session -- never from an id the client supplied -- so an
# unrelated user can neither join, modify nor view another couple's game.
# Reads never create anything; the screen polls the state after every write
# and on an interval while waiting, which is the realtime the reference
# games use (no game owns a WebSocket; only chat streams over Redis).


@router.get("", response_model=TwoKeyEscapeState)
async def current_state(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """This caller's place in the couple's maze.

    Reading this never creates anything: a caller with no couple session
    reads ``no_couple``, a couple that never confirmed a role reads
    ``selecting_roles`` with the shared template, and a playing couple reads
    the same authoritative overlay on every load -- refresh, return or
    reconnect.
    """
    return two_key_escape_gameplay.current_state(db, user)


@router.get("/partners", response_model=List[CouplePartnerCandidate])
async def partner_candidates(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> List[CouplePartnerCandidate]:
    """Every participant the caller may choose, excluding only themselves.

    Declared before ``/{...}``-style routes so ``partners`` is never read as
    an id. The questionnaire's own candidate list: settling the pairing here
    or there chooses from the same people under the same rules.
    """
    return two_key_escape_gameplay.partner_candidates(db, user)


@router.post("/selection", response_model=TwoKeyEscapeState)
async def choose_partner(
    payload: CoupleSelectionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Record this caller's partner choice through the questionnaire's selection.

    A changed mind replaces the previous pick, and a mutual pick creates the
    one session for the pair -- visible in both games at once, because both
    read the same session row. Answers the caller's escape state, so the
    screen advances in one round trip.
    """
    return two_key_escape_gameplay.choose_partner(db, user, payload.selected_user_id)


@router.post("/role-submit", response_model=TwoKeyEscapeState)
async def submit_role(
    payload: TwoKeyEscapeRoleRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Record this caller's confirmed role choice.

    Writes only the caller's own submission -- never the partner's. When both
    submissions are stored and distinct, the assignment finalizes atomically
    in the same transaction; identical submissions finalize nothing and the
    response carries the conflict both sides resolve. Safe to retry: a
    finalized assignment is returned unchanged.
    """
    return two_key_escape_gameplay.submit_role(db, user, payload.role)


@router.post("/role-change", response_model=TwoKeyEscapeState)
async def change_role(
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
async def move(
    payload: TwoKeyEscapeMoveRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Advance the character one cell for the assigned Navigator.

    Validated server-side against the stored board; refusals change nothing
    but the failed-move metric and never satisfy the movement gate.
    """
    return two_key_escape_gameplay.move(db, user, payload.direction)


@router.post("/key", response_model=TwoKeyEscapeState)
async def use_key(
    payload: TwoKeyEscapeKeyRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TwoKeyEscapeState:
    """Spend one key charge for the assigned Key Operator.

    Unlocks the key's designated locked cell and blocks a different eligible
    cell chosen by the game logic, after verifying the maze stays solvable.
    Refusals consume nothing: only a successful activation spends the charge,
    stamps the server-side cooldown and re-arms the movement gate.
    """
    return two_key_escape_gameplay.use_key(db, user, payload.key_index)


@router.get("/leaderboard", response_model=List[TwoKeyEscapeLeaderboardEntry])
async def get_leaderboard(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> List[TwoKeyEscapeLeaderboardEntry]:
    """This period's couples in rank order: completed first, then by play."""
    return two_key_escape_gameplay.leaderboard(db, user)
