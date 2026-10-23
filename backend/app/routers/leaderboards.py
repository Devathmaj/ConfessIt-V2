# app/routers/leaderboards.py

from datetime import date
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Query

from ..dependencies import get_db
from ..models import (
    GameCategoryBoard,
    GameCategoryInfo,
    GameCategoryMe,
    LeagueCoupleBoard,
    LeagueIndividualBoard,
    LeagueMyStanding,
    UserDetails,
)
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX
from ..services import leaderboard_cache, league_boards, league_game_boards
from ..services.authorization import require_participant
from ..services.league_scoring import COMPETITION_COUPLE, COMPETITION_INDIVIDUAL

router = APIRouter(prefix="/leaderboards", tags=["Leaderboards"])

# Server-calculated league standings read from the persisted award ledger.

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _board_response(board: dict, competition_type: str):
    if competition_type == COMPETITION_COUPLE:
        return LeagueCoupleBoard(**board)
    return LeagueIndividualBoard(**board)


@router.get("/individual", response_model=LeagueIndividualBoard)
def individual_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueIndividualBoard:
    """Ranked individual standings: global or a single game's board."""
    del user  # identity gates the route; standings are not per-user.
    window = leaderboard_cache.window_key(period, anchor)
    if game_id is not None:
        board = leaderboard_cache.cached_board(
            ("individual", game_id, period, window, skip, limit),
            lambda: league_boards.game_board(
                db, game_id=game_id, competition_type=COMPETITION_INDIVIDUAL,
                period=period, anchor=anchor, skip=skip, limit=limit,
            ),
        )
        return LeagueIndividualBoard(**board)
    board = leaderboard_cache.cached_board(
        ("individual", "-", period, window, skip, limit),
        lambda: league_boards.global_board(
            db, competition_type=COMPETITION_INDIVIDUAL,
            period=period, anchor=anchor, skip=skip, limit=limit,
        ),
    )
    return LeagueIndividualBoard(**board)


@router.get("/couples", response_model=LeagueCoupleBoard)
def couples_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueCoupleBoard:
    """Ranked couple standings: global or a single couple game's board."""
    del user
    window = leaderboard_cache.window_key(period, anchor)
    if game_id is not None:
        board = leaderboard_cache.cached_board(
            ("couples", game_id, period, window, skip, limit),
            lambda: league_boards.game_board(
                db, game_id=game_id, competition_type=COMPETITION_COUPLE,
                period=period, anchor=anchor, skip=skip, limit=limit,
            ),
        )
        return LeagueCoupleBoard(**board)
    board = leaderboard_cache.cached_board(
        ("couples", "-", period, window, skip, limit),
        lambda: league_boards.global_board(
            db, competition_type=COMPETITION_COUPLE,
            period=period, anchor=anchor, skip=skip, limit=limit,
        ),
    )
    return LeagueCoupleBoard(**board)


@router.get(
    "/games/{game_id}",
    response_model=Union[LeagueIndividualBoard, LeagueCoupleBoard],
)
def game_board(
    game_id: str,
    competition_type: str = Query(default=COMPETITION_INDIVIDUAL),
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
):
    """One game's normalized leaderboard for a period and competition type."""
    del user
    window = leaderboard_cache.window_key(period, anchor)
    board = leaderboard_cache.cached_board(
        ("game", game_id, competition_type, period, window, skip, limit),
        lambda: league_boards.game_board(
            db, game_id=game_id, competition_type=competition_type,
            period=period, anchor=anchor, skip=skip, limit=limit,
        ),
    )
    return _board_response(board, competition_type)


@router.get("/me", response_model=LeagueMyStanding)
def my_standing(
    competition_type: str = Query(default=COMPETITION_INDIVIDUAL),
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> LeagueMyStanding:
    """The caller's own entries and ranks on one board."""
    board = league_boards.my_standing(
        db, user_id=user.id, competition_type=competition_type,
        period=period, anchor=anchor,
    )
    return LeagueMyStanding(**board)


@router.get(
    "/games/{game_id}/categories",
    response_model=List[GameCategoryInfo],
)
def game_categories(
    game_id: str,
    user: UserDetails = Depends(require_participant),
) -> List[GameCategoryInfo]:
    """Selectable leaderboard category views for one game."""
    del user
    return [
        GameCategoryInfo(**info)
        for info in league_game_boards.list_categories(game_id)
    ]


@router.get(
    "/games/{game_id}/boards/{category}",
    response_model=GameCategoryBoard,
)
def game_category_board(
    game_id: str,
    category: str,
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> GameCategoryBoard:
    """One game's category leaderboard ranked by native metrics."""
    del user
    window = leaderboard_cache.window_key(period, anchor)
    board = leaderboard_cache.cached_board(
        ("category", game_id, category, period, window, skip, limit),
        lambda: league_game_boards.category_board(
            db, game_id=game_id, category=category,
            period=period, anchor=anchor, skip=skip, limit=limit,
        ),
    )
    return GameCategoryBoard(**board)


@router.get(
    "/games/{game_id}/boards/{category}/me",
    response_model=GameCategoryMe,
)
def game_category_me(
    game_id: str,
    category: str,
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> GameCategoryMe:
    """The caller's own entries and ranks on one category board."""
    board = league_game_boards.category_me(
        db, game_id=game_id, category=category, user_id=user.id,
        period=period, anchor=anchor,
    )
    return GameCategoryMe(**board)
