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

# The player-facing surface of the feature. Every route resolves the caller as
# a participant and reads and writes only that caller's own submission: there
# is no leaderboard here, no way to play another player's board, and no way to
# ask what the answer key is -- the tiles are display spellings in the
# caller's own shuffled order, and the submit response is a confirmation that
# carries no score, no correctness and no membership.


@router.get("", response_model=ConnectionsCurrentGame)
def current_connections(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ConnectionsCurrentGame:
    """Today's puzzle and this player's submission state.

    Reading this never creates a submission and never consumes one: a fresh
    player reads their shuffled tiles, a returning one reads the same order,
    and a submitted player reads a confirmation rather than a board.
    """
    return connections_gameplay.current_connections(db, user.id)


@router.post("/submit", response_model=ConnectionsSubmitResponse)
def submit(
    payload: ConnectionsSubmitRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> ConnectionsSubmitResponse:
    """Score one final arrangement against the named puzzle, for this player.

    Exactly one submission per player per session: moving tiles around is not
    a submission, and a second submit -- concurrent or direct -- is refused.
    The response is a confirmation only; scores are persisted server-side for
    a future leaderboard and never returned here.
    """
    return connections_gameplay.submit_arrangement(
        db, user.id, payload.game_id, [list(group) for group in payload.groups]
    )
