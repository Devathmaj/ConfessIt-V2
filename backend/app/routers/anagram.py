# app/routers/anagram.py

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    AnagramActionRequest,
    AnagramCurrentGame,
    AnagramGuessRequest,
    AnagramGuessResponse,
    AnagramParticipation,
    UserDetails,
)
from ..postgres import Database
from ..services import anagram_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/anagram", tags=["Anagram"])

# Routes expose only the caller's participation. The answer appears on completion, and
# the server tracks elapsed time across each transition.


@router.get("", response_model=AnagramCurrentGame)
def current_anagram(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> AnagramCurrentGame:
    """Today's puzzle and this player's place in it.

    Reading this never creates a participation: a fresh player simply has none yet, and a
    returning one sees the scramble, guesses, attempts and banked time they left.
    """
    return anagram_gameplay.current_anagram(db, user.id)


@router.post("/start", response_model=AnagramParticipation)
def start(
    payload: AnagramActionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> AnagramParticipation:
    """Reveal the puzzle: deal this player's scramble and start -- or resume -- the clock.

    The first call creates the participation; a later call on a paused game re-opens the
    clock (the RESUME half of the screen's single control). Returning to the page never
    restarts the timer by itself -- only this explicit press does.
    """
    return anagram_gameplay.start_participation(db, user.id, payload.game_id)


@router.post("/pause", response_model=AnagramParticipation)
def pause(
    payload: AnagramActionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> AnagramParticipation:
    """Bank the open timing segment and stop the clock. Idempotent.

    The screen calls this when the page hides, the route changes, or the component
    unmounts, so no time is credited while the player is elsewhere.
    """
    return anagram_gameplay.pause_timer(db, user.id, payload.game_id)


@router.post("/heartbeat", response_model=AnagramParticipation)
def heartbeat(
    payload: AnagramActionRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> AnagramParticipation:
    """Checkpoint a running clock without stopping it.

    Called on an interval while the game runs visibly, so an abrupt browser death loses
    at most one interval of banked time rather than the whole session.
    """
    return anagram_gameplay.heartbeat(db, user.id, payload.game_id)


@router.post("/guess", response_model=AnagramGuessResponse)
def guess(
    payload: AnagramGuessRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> AnagramGuessResponse:
    """Score one tile arrangement against the named puzzle, for this player.

    Recorded against this player's participation -- created by the reveal, never here --
    and the response carries the new attempt count plus whether it matched. The fifth
    wrong guess ends the game and reveals the answer; anything earlier leaves the
    scramble, attempts and running clock exactly where they were.
    """
    return anagram_gameplay.submit_guess(db, user.id, payload)
