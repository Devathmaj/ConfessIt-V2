# app/routers/admin_couple.py

from typing import List

from fastapi import APIRouter, Depends, status

from ..dependencies import get_db
from ..accounts import Capability
from ..models import AccountDetails, CoupleGame, CoupleGameCreate
from ..postgres import Database
from ..services import couple_service
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/couple", tags=["Admin - Couple"])

# Every route below is gated on the one capability that means "may publish a game", and
# the read routes are gated too for the reason the Wordle ones are: the featured
# schedule is the administrator's own configuration, and a list endpoint open to any
# caller would publish it to a player surface that does not exist yet.


@router.post("", response_model=CoupleGame, status_code=status.HTTP_201_CREATED)
async def create_couple_game(
    payload: CoupleGameCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CoupleGame:
    """Mark the Couple Prediction game featured for one day.

    Featuring only pins the spotlight: questionnaire *sessions* are opened by the
    reset loop under the game's own cadence, never by featuring, and there is no
    Update action for this game. A day that already holds a featured game (any
    game) is a 409, because the spotlight seats one game per date.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken from the payload:
    an administrator cannot feature a day attributed to somebody else.
    """
    return couple_service.create_couple_game(db, payload, created_by=account.regno)


@router.get("/session")
async def current_couple_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """The current global questionnaire session, if the reset loop opened one.

    Declared before ``/{game_id}`` so ``session`` is never read as an id. The
    questionnaire has no Update: at each boundary the loop ends this session
    and opens the next with a fresh question set per couple.
    """
    from fastapi import HTTPException, status

    current = couple_service.get_current_couple_session(db)
    if current is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No Couple session yet"
        )
    return current


@router.get("", response_model=List[CoupleGame])
async def list_couple_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[CoupleGame]:
    """Every featured date, newest day first."""
    return couple_service.list_couple_games(db)


@router.get("/{game_id}", response_model=CoupleGame)
async def get_couple_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> CoupleGame:
    """One featured date by id."""
    return couple_service.get_couple_game(db, game_id)
