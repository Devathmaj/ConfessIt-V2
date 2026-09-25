# app/routers/admin_games.py

from datetime import date
from typing import List

from fastapi import APIRouter, Depends

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    FeaturedGame,
    FeaturedGameCreate,
    GameConfig,
    GameConfigUpdate,
)
from ..postgres import Database
from ..services import featured_games, game_configs
from ..services.authorization import require_capability

router = APIRouter(prefix="/admin", tags=["Admin - Games"])

# Game configuration, the featured spotlight schedule, and the manual reset
# trigger. Every route is gated on GAMES_MANAGE like the per-game admin
# routers: configuration is publish-equivalent, and the featured schedule is
# the administrator's own until the player-facing featured endpoint serves it.


@router.get("/games/config", response_model=List[GameConfig])
async def list_game_configs(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[GameConfig]:
    """Every game's availability, content mode and reset cadence."""
    return [GameConfig(**entry) for entry in game_configs.list_configs(db)]


@router.put("/games/config/{game_id}", response_model=GameConfig)
async def update_game_config(
    game_id: str,
    payload: GameConfigUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> GameConfig:
    """Patch one game's availability, content mode or reset cadence.

    Enabled/hidden, the Wordle/Anagram content mode and each game's own reset
    cadence are set here. Values outside the vocabularies are refused; the
    questionnaire accepts no content mode at all.
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
        )
    )


@router.get("/featured", response_model=List[FeaturedGame])
async def list_featured_games(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> List[FeaturedGame]:
    """Every featured date, newest day first, across all games."""
    return [FeaturedGame(**entry) for entry in featured_games.list_featured(db)]


@router.post("/featured", response_model=FeaturedGame, status_code=201)
async def set_featured_game(
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


@router.delete("/featured/{game_date}", status_code=204)
async def clear_featured_game(
    game_date: date,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> None:
    """Remove the featured pick for one UTC date; a day with none is a 404."""
    featured_games.clear_featured(db, game_date)
    return None


@router.post("/games/run-resets")
async def run_scheduled_resets(
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
