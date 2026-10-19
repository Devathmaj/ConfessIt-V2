# app/routers/admin_games.py

from datetime import date
from typing import List, Optional, Union

from fastapi import APIRouter, Depends, Query

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    FeaturedGame,
    FeaturedGameCreate,
    FeaturedGameRangeCreate,
    GameCategoryBoard,
    GameCategoryInfo,
    GameConfig,
    GameConfigUpdate,
    GameRecordHighlights,
    GameStatsSummary,
    LeagueCoupleBoard,
    LeagueFinalizeRequest,
    LeagueIndividualBoard,
    LeagueSummary,
)
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX
from ..services import (
    featured_games,
    game_configs,
    league_boards,
    league_finalization,
    league_game_boards,
    player_stats,
)
from ..services.authorization import require_capability
from ..services.league_scoring import COMPETITION_COUPLE, COMPETITION_INDIVIDUAL

router = APIRouter(prefix="/admin", tags=["Admin - Games"])

# Game configuration, the featured spotlight schedule, and the manual reset
# trigger. Every route is gated on GAMES_MANAGE like the per-game admin
# routers: configuration is publish-equivalent, and the featured schedule is
# the administrator's own until the player-facing featured endpoint serves it.


@router.get("/games/config", response_model=List[GameConfig])
def list_game_configs(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[GameConfig]:
    """Every game's availability, content mode and reset cadence."""
    return [GameConfig(**entry) for entry in game_configs.list_configs(db)]


@router.put("/games/config/{game_id}", response_model=GameConfig)
def update_game_config(
    game_id: str,
    payload: GameConfigUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> GameConfig:
    """Patch one game's availability, content mode or reset cadence.

    Enabled/hidden, the Wordle/Anagram content mode, each game's own reset
    cadence and the two AI games' difficulty are set here. Values outside the
    vocabularies are refused; the questionnaire accepts no content mode at all,
    and only Plot Twist and Case Closed accept a difficulty.
    """
    return GameConfig(
        **game_configs.update_config(
            db,
            game_id,
            enabled=payload.enabled,
            is_hidden=payload.is_hidden,
            content_mode=payload.content_mode,
            reset_cadence=payload.reset_cadence,
            grid_size=payload.grid_size,
            difficulty=payload.difficulty,
        )
    )


@router.get("/featured", response_model=List[FeaturedGame])
def list_featured_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[FeaturedGame]:
    """Every featured date, newest day first, across all games."""
    return [FeaturedGame(**entry) for entry in featured_games.list_featured(db)]


@router.post("/featured", response_model=FeaturedGame, status_code=201)
def set_featured_game(
    payload: FeaturedGameCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> FeaturedGame:
    """Pin one game as featured for one UTC date.

    A date that already holds a featured game is a 409: the spotlight seats
    one game per day, enforced by the schema as well as this check. Featuring
    never creates or touches any session.
    """
    created = featured_games.set_featured(
        db, payload.game_date, payload.game_id, created_by=account.regno
    )
    return FeaturedGame(**created)


@router.put("/featured", response_model=FeaturedGame)
def replace_featured_game(
    payload: FeaturedGameCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> FeaturedGame:
    """Set one game as featured for one UTC date, replacing any current pick.

    The atomic path for the per-game "Set as Featured" action: when another
    game holds the date it is removed in the same transaction and the new pick
    is stored, so the admin confirms once and the schedule never sits empty
    between a delete and a create. Replacing the spotlight never rewrites
    league history -- awards snapshot ``is_featured`` at finalization time, so
    points already earned keep their 2x while only future plays earn 2x on the
    new game. Featuring never creates or touches any session.
    """
    replaced = featured_games.replace_featured(
        db, payload.game_date, payload.game_id, created_by=account.regno
    )
    return FeaturedGame(**replaced)


@router.post("/featured/range", response_model=List[FeaturedGame], status_code=201)
def set_featured_range(
    payload: FeaturedGameRangeCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[FeaturedGame]:
    """Feature one game for a whole span, start to end inclusive.

    Every date in the span must be free of other games: the first date held
    by another game is a 409 naming it, and nothing is stored. A span whose
    consecutive run -- merged with the same game's picks bordering it -- would
    pass a week is a 422. Featuring never creates or touches any session.
    """
    created = featured_games.set_featured_range(
        db, payload.start_date, payload.end_date, payload.game_id,
        created_by=account.regno,
    )
    return [FeaturedGame(**entry) for entry in created]


@router.put("/featured/range", response_model=List[FeaturedGame])
def replace_featured_range(
    payload: FeaturedGameRangeCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[FeaturedGame]:
    """Feature one game for a whole span, erasing any other picks it covers.

    The override path for the schedule page: dates held by other games are
    removed in the same transaction and the new picks stored, so the admin
    confirms once and the schedule never sits half-written. A span whose
    consecutive run would pass a week is still a 422. League history is
    untouched -- awards snapshot ``is_featured`` at finalization time.
    """
    replaced = featured_games.replace_featured_range(
        db, payload.start_date, payload.end_date, payload.game_id,
        created_by=account.regno,
    )
    return [FeaturedGame(**entry) for entry in replaced]


@router.delete("/featured/{game_date}", status_code=204)
def clear_featured_game(
    game_date: date,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> None:
    """Remove the featured pick for one UTC date; a day with none is a 404."""
    featured_games.clear_featured(db, game_date)
    return None


@router.get("/games/stats", response_model=GameStatsSummary)
def game_stats(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> GameStatsSummary:
    """Live play telemetry across every game, for the console overview.

    Read from the games' native participation tables, not the league ledger,
    so a round played today appears immediately instead of waiting for its
    event to finalize. The same collectors answer ``GET /games/summary`` for
    a player, so both screens agree on what counts as a finished round.
    """
    return GameStatsSummary(**player_stats.build_games_stats(db))


@router.post("/games/run-resets")
def run_scheduled_resets(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """Evaluate every game's session boundary now (the manual 00:01 UTC trigger).

    The same function the automatic loop runs: idempotent per game, safe to
    retry, and a no-op for games still inside their period. Hidden games are
    skipped -- no new sessions while hidden. Returns the per-game summary plus
    the active-session counts, so an operator can see what the run did.
    """
    from ..services import game_scheduler

    summary = game_scheduler.ensure_game_sessions(db)
    return {"resets": summary, "active": game_scheduler.active_session_counts(db)}


@router.post("/leaderboards/finalize")
def finalize_league_opportunity(
    payload: LeagueFinalizeRequest,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """Finalize one closed event's league awards (idempotent repair path).

    The scheduler finalizes ended events on its own runs; this endpoint is
    the explicit path for backfilling history or retrying an event the loop
    has not reached. Ranking an event that is still open is refused, and a
    repeated call settles on the same rows.
    """
    return league_finalization.finalize_opportunity(
        db, payload.game_id, payload.opportunity_id
    )


@router.post("/leaderboards/reprocess")
def reprocess_league_opportunity(
    payload: LeagueFinalizeRequest,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> dict:
    """Supersede one event's live awards and issue auditable replacements.

    The deliberate correction path for a wrong snapshot or result: original
    rows stay in the ledger marked superseded, replacements carry a fresh
    finalization instant, and boards read live rows only. Changing the
    featured schedule never triggers this silently.
    """
    return league_finalization.reprocess_opportunity(
        db, payload.game_id, payload.opportunity_id
    )


@router.get("/leaderboards/summary", response_model=LeagueSummary)
def league_summary(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> LeagueSummary:
    """Finalized opportunities and live awards per game, for operators."""
    return LeagueSummary(games=league_boards.leaderboard_summary(db))


_DEFAULT_BOARD_LIMIT = 50
_MAX_BOARD_LIMIT = 200


@router.get("/leaderboards/individual", response_model=LeagueIndividualBoard)
def admin_individual_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_BOARD_LIMIT, ge=1, le=_MAX_BOARD_LIMIT),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> LeagueIndividualBoard:
    """Ranked individual standings for the games console.

    The admin mirror of the player-facing board: staff hold no participant
    row, so they read standings here rather than through ``/leaderboards``.
    ``game_id`` narrows an individual game to its own board.
    """
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


@router.get("/leaderboards/couples", response_model=LeagueCoupleBoard)
def admin_couples_board(
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    game_id: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_BOARD_LIMIT, ge=1, le=_MAX_BOARD_LIMIT),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> LeagueCoupleBoard:
    """Ranked couple standings for the games console.

    Each canonical pair is one entry; member order never duplicates a
    couple. ``game_id`` narrows a couple game to its own board.
    """
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
    "/leaderboards/games/{game_id}",
    response_model=Union[LeagueIndividualBoard, LeagueCoupleBoard],
)
def admin_game_board(
    game_id: str,
    competition_type: str = Query(default=COMPETITION_INDIVIDUAL),
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_BOARD_LIMIT, ge=1, le=_MAX_BOARD_LIMIT),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
):
    """One game's normalized leaderboard for the games console.

    Unavailable combinations are refused: individual games have no couple
    board and couple games have no individual board.
    """
    board = league_boards.game_board(
        db, game_id=game_id, competition_type=competition_type,
        period=period, anchor=anchor, skip=skip, limit=limit,
    )
    if competition_type == COMPETITION_COUPLE:
        return LeagueCoupleBoard(**board)
    return LeagueIndividualBoard(**board)


@router.get(
    "/leaderboards/games/{game_id}/categories",
    response_model=List[GameCategoryInfo],
)
def admin_game_categories(
    game_id: str,
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[GameCategoryInfo]:
    """The selectable metric views for one game, the console mirror.

    The same descriptors the player-facing board offers -- Performance,
    Fastest Solve, All-Time Fastest and the rest -- so an operator reads the
    expanded rankings exactly as players do.
    """
    return [
        GameCategoryInfo(**info) for info in league_game_boards.list_categories(game_id)
    ]


@router.get(
    "/leaderboards/games/{game_id}/boards/{category}",
    response_model=GameCategoryBoard,
)
def admin_game_category_board(
    game_id: str,
    category: str,
    period: str = Query(default="weekly"),
    anchor: Optional[date] = Query(default=None),
    skip: int = Query(default=0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(default=_DEFAULT_BOARD_LIMIT, ge=1, le=_MAX_BOARD_LIMIT),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> GameCategoryBoard:
    """One game's category board: metric rankings over finalized results.

    Record-scope categories always answer all-time history; the response says
    so (``period: "all_time"``) rather than echoing an ignored window. There
    is deliberately no admin ``/me``: staff hold no participant row to rank.
    """
    board = league_game_boards.category_board(
        db, game_id=game_id, category=category, period=period,
        anchor=anchor, skip=skip, limit=limit,
    )
    return GameCategoryBoard(**board)


@router.get("/leaderboards/records", response_model=GameRecordHighlights)
def admin_game_records(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> GameRecordHighlights:
    """Every game's headline record, all-time, for the console overview.

    One leader per game (fastest time, fewest guesses, fastest escape, ...)
    read through the same category builders the expanded rankings use, so the
    overview can never disagree with the board behind it.
    """
    return GameRecordHighlights(
        highlights=league_game_boards.record_highlights(db)
    )
