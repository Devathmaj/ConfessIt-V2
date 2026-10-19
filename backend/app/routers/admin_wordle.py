# app/routers/admin_wordle.py

from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, status

from ..dependencies import get_db
from ..accounts import Capability
from ..models import (
    AccountDetails,
    ScheduledWord,
    ScheduledWordCreate,
    WordleDraw,
    WordleGame,
    WordleGameCreate,
)
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX
from ..services import wordle_service
from ..services.wordle_service import WORDLE_GAMES_MAX_LIMIT
from ..services.authorization import require_capability
from ..services.scheduled_contents import (
    delete_scheduled,
    list_scheduled,
    schedule_word,
)

router = APIRouter(prefix="/admin/wordle", tags=["Admin - Wordle"])

# Every route below is gated on the one capability that means "may publish a puzzle". The
# read routes are gated too, and deliberately so: the configured answer is the thing the
# whole feature exists to keep out of the player-facing surface until an administrator
# publishes it, and a list endpoint open to any caller would be that answer, exposed.

@router.get("/preview", response_model=WordleDraw)
def preview_wordle_answer(
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
def create_wordle_game(
    payload: WordleGameCreate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleGame:
    """Update the current session: end it and open a brand-new one ("Update").

    ``mode="custom"`` stores the supplied word if it is on the approved list;
    ``mode="random"`` stores the word the preview drew, verified against its signature.
    The old session is stamped ended and kept as history with its user progress;
    the new session starts from scratch for today's UTC date under the game's own
    reset cadence. An explicit ``game_date`` other than today is refused --
    future words belong on the schedule endpoint below.
    """
    return wordle_service.create_wordle_game(db, payload)


@router.get("/current", response_model=WordleGame)
def current_wordle_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleGame:
    """The current active-or-pending session, or 404 when the game has none.

    Declared before ``/{game_id}`` so ``current`` is never read as an id. This
    is what the admin screen renders as "the session now": its word (or its
    pending state), its period, and whether it is waiting for configuration.
    """
    from fastapi import HTTPException

    current = wordle_service.get_current_wordle_session(db)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Wordle session yet")
    return current


@router.post("/schedule", response_model=ScheduledWord, status_code=status.HTTP_201_CREATED)
def schedule_wordle_word(
    payload: ScheduledWordCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ScheduledWord:
    """Schedule a manual word for a future session period.

    Bounded by ``MAX_FUTURE_SCHEDULE_DAYS`` ahead of today (UTC), enforced
    server-side: farther dates are refused with the window named. The word waits
    until its period opens and never activates a session early.
    """
    return ScheduledWord(
        **schedule_word(
            db, "wordle", payload.target_date, payload.word, created_by=account.regno
        )
    )


@router.get("/scheduled", response_model=list[ScheduledWord])
def list_wordle_scheduled(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> list[ScheduledWord]:
    """Every scheduled Wordle word, earliest date first."""
    return [ScheduledWord(**entry) for entry in list_scheduled(db, "wordle")]


@router.delete("/scheduled/{target_date}", status_code=status.HTTP_204_NO_CONTENT)
def delete_wordle_scheduled(
    target_date: date,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> None:
    """Withdraw the scheduled Wordle word for one date; a date with none is a 404."""
    delete_scheduled(db, "wordle", target_date)
    return None


@router.get("", response_model=Dict[str, Any])
def list_wordle_games(
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many sessions to skip."),
    limit: int = Query(50, ge=1, le=WORDLE_GAMES_MAX_LIMIT, description="How many sessions to return. Clamped so one request can never materialise a whole season at once."),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> Dict[str, Any]:
    """One page of every configured puzzle, newest day first, with the total beside it."""
    return wordle_service.list_wordle_games(db, skip=skip, limit=limit)


@router.get("/{game_id}", response_model=WordleGame)
def get_wordle_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> WordleGame:
    """One configured puzzle by id."""
    return wordle_service.get_wordle_game(db, game_id)
