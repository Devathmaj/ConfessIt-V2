# app/routers/admin_wordle.py

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from ..dependencies import get_db
from ..accounts import Capability
from ..models import AccountDetails, WordleDraw, WordleGame, WordleGameCreate
from ..postgres import Database
from ..services import wordle_service
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/wordle", tags=["Admin - Wordle"])

# Every route below is gated on the one capability that means "may publish a puzzle". The
# read routes are gated too, and deliberately so: the configured answer is the thing the
# whole feature exists to keep out of the player-facing surface until an administrator
# publishes it, and a list endpoint open to any caller would be that answer, exposed.

@router.get("/preview", response_model=WordleDraw)
async def preview_wordle_answer(
    game_date: Optional[date] = Query(
        default=None,
        description="The day the draw is for. Defaults to today in UTC.",
    ),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleDraw:
    """Draw a word for ``game_date`` from the approved list, signed for that day.

    This is the "show me what will be saved" half of the flow. The word is chosen here, on
    the server, and the token that comes back is an HMAC over the word and the date -- so the
    screen can display the answer before anything is written, and ``POST`` can then store
    exactly that answer without taking the client's word for what it is.

    Declared before ``/{game_id}`` so ``preview`` is never read as an id.
    """
    return wordle_service.draw_answer(game_date)


@router.post("", response_model=WordleGame, status_code=status.HTTP_201_CREATED)
async def create_wordle_game(
    payload: WordleGameCreate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleGame:
    """Configure the answer for one day.

    ``mode="custom"`` stores the supplied word if it is on the approved list;
    ``mode="random"`` stores the word the preview drew, verified against its signature. A day
    that already has a puzzle is a 409 rather than a second row, because ``game_date`` is
    what identifies a puzzle and two answers for one day is a state the player-facing page
    would have no rule for resolving.
    """
    return wordle_service.create_wordle_game(db, payload)


@router.get("", response_model=List[WordleGame])
async def list_wordle_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[WordleGame]:
    """Every configured puzzle, newest day first."""
    return wordle_service.list_wordle_games(db)


@router.get("/{game_id}", response_model=WordleGame)
async def get_wordle_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleGame:
    """One configured puzzle by id."""
    return wordle_service.get_wordle_game(db, game_id)
