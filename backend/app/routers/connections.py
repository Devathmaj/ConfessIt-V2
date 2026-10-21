# app/routers/connections.py

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    ConnectionsCurrentGame,
    ConnectionsSubmitRequest,
    ConnectionsSubmitResponse,
    UserDetails,
)
from ..postgres import Database
from ..services import connections_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/connections", tags=["Connections"])

# Routes access only the caller's submission. The answer key and correctness stay
# hidden until results are available.


@router.get("", response_model=ConnectionsCurrentGame)
def current_connections(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ConnectionsCurrentGame:
    """Today's puzzle and this player's submission state.

    Reading never creates or consumes a submission: a fresh player reads their
    shuffled tiles, a returning one the same order, a submitted one a
    confirmation rather than a board.
    """
    return connections_gameplay.current_connections(db, user.id)


@router.post("/submit", response_model=ConnectionsSubmitResponse)
def submit(
    payload: ConnectionsSubmitRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ConnectionsSubmitResponse:
    """Score one final arrangement against the named puzzle, for this player.

    Exactly one submission per player per session: moving tiles is not a
    submission, and a second submit -- concurrent or direct -- is refused. The
    response is a confirmation only; scores persist server-side for a future
    leaderboard and are never returned here.
    """
    return connections_gameplay.submit_arrangement(
        db, user.id, payload.game_id, [list(group) for group in payload.groups]
    )
