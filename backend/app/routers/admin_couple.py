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

    The payload carries nothing but the date: there is no custom-vs-random choice,
    no word to type, no answer to store and no puzzle to generate, because the
    50-question bank is shared application content rather than per-day input. A
    day that is already featured is a 409 rather than a second row, because
    ``game_date`` is what identifies a featured instance and two rows for one day
    is a state any future player surface would have no rule for resolving.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken from the payload:
    an administrator cannot feature a day attributed to somebody else.
    """
    return couple_service.create_couple_game(db, payload, created_by=account.regno)


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
