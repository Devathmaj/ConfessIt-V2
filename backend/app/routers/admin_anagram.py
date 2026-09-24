# app/routers/admin_anagram.py

from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from ..dependencies import get_db
from ..accounts import Capability
from ..models import (
    AccountDetails,
    AnagramDraw,
    AnagramGame,
    AnagramGameCreate,
    ScheduledWord,
    ScheduledWordCreate,
)
from ..postgres import Database
from ..services import anagram_service
from ..services.authorization import require_capability
from ..services.scheduled_contents import (
    delete_scheduled,
    list_scheduled,
    schedule_word,
)
from datetime import date as _date

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
    """Update the current session: end it and open a brand-new one ("Update").

    ``mode="custom"`` stores the supplied word, normalized to uppercase; ``mode="random"``
    stores the word the preview drew, verified against its signature. The old session is
    stamped ended and kept as history; the new session starts from scratch for today's
    UTC date under the game's own reset cadence. An explicit ``game_date`` other than
    today is refused -- future words belong on the schedule endpoint below.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken from the payload:
    an administrator cannot publish a puzzle attributed to somebody else.
    """
    return anagram_service.create_anagram_game(db, payload, created_by=account.regno)


@router.get("/current", response_model=AnagramGame)
async def current_anagram_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> AnagramGame:
    """The current active-or-pending session, or 404 when the game has none.

    Declared before ``/{game_id}`` so ``current`` is never read as an id.
    """
    from fastapi import HTTPException

    current = anagram_service.get_current_anagram_session(db)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Anagram session yet")
    return current


@router.post("/schedule", response_model=ScheduledWord, status_code=status.HTTP_201_CREATED)
async def schedule_anagram_word(
    payload: ScheduledWordCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ScheduledWord:
    """Schedule a manual word for a future session period.

    Bounded by ``MAX_FUTURE_SCHEDULE_DAYS`` ahead of today (UTC), enforced
    server-side. The word waits until its period opens and never activates a
    session early.
    """
    return ScheduledWord(
        **schedule_word(
            db, "anagram", payload.target_date, payload.word, created_by=account.regno
        )
    )


@router.get("/scheduled", response_model=list[ScheduledWord])
async def list_anagram_scheduled(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> list[ScheduledWord]:
    """Every scheduled Anagram word, earliest date first."""
    return [ScheduledWord(**entry) for entry in list_scheduled(db, "anagram")]


@router.delete("/scheduled/{target_date}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_anagram_scheduled(
    target_date: _date,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> None:
    """Withdraw the scheduled Anagram word for one date; a date with none is a 404."""
    delete_scheduled(db, "anagram", target_date)
    return None


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
