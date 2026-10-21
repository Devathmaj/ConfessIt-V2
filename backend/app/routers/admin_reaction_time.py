# app/routers/admin_reaction_time.py

from typing import List

from fastapi import APIRouter, Depends, status

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    ReactionTimeGame,
    ReactionTimeGameCreate,
)
from ..postgres import Database
from ..services import reaction_time_service
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/reaction-time", tags=["Admin - Reaction Time"])

# Every route below is gated on the one capability that means "may publish a game", and
# the read routes are gated too: the featured schedule is the administrator's own until
# the player-facing surface serves it. The payload is one field -- the day -- because
# there is nothing else to configure: sessions roll on the game's own cadence with a
# fresh five-submission budget per player per session, there is no Update action, and
# timing, attempts, difficulty and thresholds are fixed by the application -- no knob.


@router.post("", response_model=ReactionTimeGame, status_code=status.HTTP_201_CREATED)
def create_reaction_time_game(
    payload: ReactionTimeGameCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ReactionTimeGame:
    """Feature the Reaction Time game for one day.

    Featuring only pins the spotlight: *sessions* are opened by the reset loop under
    the game's own cadence, never by featuring, and a day that already holds a featured
    game (any game) is a 409 -- the spotlight seats one game per date. The caller's
    ``regno`` is recorded as ``created_by``, not taken from the payload: an
    administrator cannot feature a day attributed to somebody else.
    """
    return reaction_time_service.create_reaction_time_game(
        db, payload, created_by=account.regno
    )


@router.get("/session")
def current_reaction_time_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """The current Reaction Time session, if the reset loop opened one.

    Declared before ``/{game_id}`` so ``session`` is never read as an id. Reaction
    Time has no Update: at each boundary the loop ends this session and opens the
    next, and every player gets a fresh five submissions in it.
    """
    from fastapi import HTTPException, status

    current = reaction_time_service.get_current_reaction_time_session(db)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No Reaction Time session yet",
        )
    return current


@router.get("", response_model=List[ReactionTimeGame])
def list_reaction_time_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[ReactionTimeGame]:
    """Every date Reaction Time is featured on, newest day first."""
    return reaction_time_service.list_reaction_time_games(db)


@router.get("/{game_id}", response_model=ReactionTimeGame)
def get_reaction_time_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ReactionTimeGame:
    """One featured date by id."""
    return reaction_time_service.get_reaction_time_game(db, game_id)
