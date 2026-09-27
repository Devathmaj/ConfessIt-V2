# app/routers/leaderboards.py

from datetime import date
from typing import Optional, Union

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_db
from ..models import (
    LeagueCoupleBoard,
    LeagueIndividualBoard,
    LeagueMyStanding,
    UserDetails,
)
from ..postgres import Database
from ..services import league_boards
from ..services.authorization import require_participant
from ..services.league_scoring import COMPETITION_COUPLE, COMPETITION_INDIVIDUAL

router = APIRouter(prefix="/leaderboards", tags=["Leaderboards"])

# Server-calculated league standings, read from the persisted award ledger.
# Every route resolves the caller as a participant like the rest of the
# player-facing games surface. Scores are never computed from raw
# browser-side results: finalization writes awards first, these endpoints
# only query them. Native answers, words, prompts and selections never
# travel through these responses -- only performance numbers.

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _board_response(board: dict, competition_type: str):
    if competition_type == COMPETITION_COUPLE:
        return LeagueCoupleBoard(**board)
    return LeagueIndividualBoard(**board)


@router.get("/individual", response_model=LeagueIndividualBoard)
async def individual_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueIndividualBoard:
    """Ranked individual standings: global, or one game's board.

    ``period`` is daily, weekly, monthly or all-time in UTC; ``anchor``
    selects the day/week/month (default: today UTC). ``game_id`` narrows an
    individual game to its own board -- a couple game is refused here.
    """
    del user  # identity gates the route; standings are not per-user.
    if game_id is not None:
        board = league_boards.game_board(
            db, game_id=game_id, competition_type=COMPETITION_INDIVIDUAL,
            period=period, anchor=anchor, skip=skip, limit=limit,
        )
        return LeagueIndividualBoard(**board)
    board = league_boards.global_board(
        db, competition_type=COMPETITION_INDIVIDUAL,
        period=period, anchor=anchor, skip=skip, limit=limit,
    )
    return LeagueIndividualBoard(**board)


@router.get("/couples", response_model=LeagueCoupleBoard)
async def couples_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueCoupleBoard:
    """Ranked couple standings: global, or one couple game's board.

    Each canonical pair is one entry; member order never duplicates a
    couple. ``game_id`` narrows a couple game to its own board -- an
    individual game is refused here.
    """
    del user
    if game_id is not None:
        board = league_boards.game_board(
            db, game_id=game_id, competition_type=COMPETITION_COUPLE,
            period=period, anchor=anchor, skip=skip, limit=limit,
        )
        return LeagueCoupleBoard(**board)
    board = league_boards.global_board(
        db, competition_type=COMPETITION_COUPLE,
        period=period, anchor=anchor, skip=skip, limit=limit,
    )
    return LeagueCoupleBoard(**board)


@router.get(
    "/games/{game_id}",
    response_model=Union[LeagueIndividualBoard, LeagueCoupleBoard],
)
async def game_board(
    game_id: str,
    competition_type: str = Query(default=COMPETITION_INDIVIDUAL),
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
):
    """One game's normalized leaderboard for one period and subject type.

    Unavailable combinations are refused: individual games have no couple
    board and couple games have no individual board.
    """
    del user
    board = league_boards.game_board(
        db, game_id=game_id, competition_type=competition_type,
        period=period, anchor=anchor, skip=skip, limit=limit,
    )
    return _board_response(board, competition_type)


@router.get("/me", response_model=LeagueMyStanding)
async def my_standing(
    competition_type: str = Query(default=COMPETITION_INDIVIDUAL),
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueMyStanding:
    """The caller's own entries on one board, with ranks.

    For couples, every canonical pair the caller belongs to is returned.
    """
    board = league_boards.my_standing(
        db, user_id=user.id, competition_type=competition_type,
        period=period, anchor=anchor,
    )
    return LeagueMyStanding(**board)
