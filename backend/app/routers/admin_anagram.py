# app/routers/admin_anagram.py

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from ..dependencies import get_db
from ..accounts import Capability
from ..models import AccountDetails, AnagramDraw, AnagramGame, AnagramGameCreate
from ..postgres import Database
from ..services import anagram_service
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin/anagram", tags=["Admin - Anagram"])

# Every route below is gated on the one capability that means "may publish a puzzle", and
# the read routes are gated too for the reason the Wordle ones are: the configured answer is
# the thing the whole feature exists to keep out of the player-facing surface until an
# administrator publishes it, and a list endpoint open to any caller would be that answer,
# exposed.


@router.get("/preview", response_model=AnagramDraw)
async def preview_anagram_answer(
    game_date: Optional[date] = Query(
        default=None,
        description="The day the draw is for. Defaults to today in UTC.",
    ),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> AnagramDraw:
    """Draw a word for ``game_date`` from the word list, signed for that day.

    This is the "show me what will be saved" half of the flow. The word is chosen here, on
    the server, and the token that comes back is an HMAC over the word and the date -- so the
    screen can display the answer before anything is written, and ``POST`` can then store
    exactly that answer without taking the client's word for what it is.

    Declared before ``/{game_id}`` so ``preview`` is never read as an id.
    """
    return anagram_service.draw_answer(game_date)


@router.post("", response_model=AnagramGame, status_code=status.HTTP_201_CREATED)
async def create_anagram_game(
    payload: AnagramGameCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> AnagramGame:
    """Configure the answer for one day.

    ``mode="custom"`` stores the supplied word, normalized to uppercase; ``mode="random"``
    stores the word the preview drew, verified against its signature. A day that already has
    a puzzle is a 409 rather than a second row, because ``game_date`` is what identifies a
    puzzle and two answers for one day is a state the player-facing page would have no rule
    for resolving.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken from the payload:
    an administrator cannot publish a puzzle attributed to somebody else.
    """
    return anagram_service.create_anagram_game(db, payload, created_by=account.regno)


@router.get("", response_model=List[AnagramGame])
async def list_anagram_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[AnagramGame]:
    """Every configured puzzle, newest day first."""
    return anagram_service.list_anagram_games(db)


@router.get("/{game_id}", response_model=AnagramGame)
async def get_anagram_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> AnagramGame:
    """One configured puzzle by id."""
    return anagram_service.get_anagram_game(db, game_id)
