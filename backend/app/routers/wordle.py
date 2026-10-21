# app/routers/wordle.py

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    UserDetails,
    WordleCurrentGame,
    WordleGuessRequest,
    WordleGuessResponse,
)
from ..postgres import Database
from ..services import wordle_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/wordle", tags=["Wordle"])

# The player-facing surface of the feature. Every route resolves the caller as a participant
# and reads and writes only that caller's own participation: no leaderboard, no way to
# play another player's board, and no way to ask what the answer is -- it is carried by
# the puzzle the caller was shown and revealed only by a completed participation.


@router.get("", response_model=WordleCurrentGame)
def current_wordle(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> WordleCurrentGame:
    """Today's puzzle and this player's place in it.

    Reading this never creates a participation: a fresh player simply has none yet, and a
    returning one sees the board they left.
    """
    return wordle_gameplay.current_wordle(db, user.id)


@router.post("/guess", response_model=WordleGuessResponse)
def guess(
    payload: WordleGuessRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> WordleGuessResponse:
    """Score one guess against the named puzzle, for this player.

    The guess is recorded against this player's participation (created on the first
    guess); the response carries the new board row plus the ``guess`` and ``result``
    that just happened, so the screen flips tiles and colours the keyboard in one
    round trip.
    """
    return wordle_gameplay.submit_guess(db, user.id, payload)