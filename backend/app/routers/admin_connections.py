# app/routers/admin_connections.py

from datetime import date
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Query, status

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    ConnectionsGame,
    ConnectionsPuzzleCreate,
    ConnectionsScheduledPuzzle,
    ConnectionsScheduledPuzzleCreate,
)
from ..postgres import Database
from ..services import connections_service
from ..services.authorization import require_capability
from ..services.connections_service import CONNECTIONS_GAMES_MAX_LIMIT

router = APIRouter(prefix="/admin/connections", tags=["Admin - Connections"])

# Every route below is gated on the one capability that means "may publish a
# puzzle", and the read routes are gated too for the reason the Wordle ones
# are: the configured groups are the thing the whole feature exists to keep out
# of the player-facing surface, and a list endpoint open to any caller would
# be that answer key, exposed.


@router.post("", response_model=ConnectionsGame, status_code=status.HTTP_201_CREATED)
def create_connections_game(
    payload: ConnectionsPuzzleCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ConnectionsGame:
    """Update the current session: end it and open a brand-new one ("Update").

    Stores the sixteen supplied words in their four canonical groups on the
    new session -- or, when no puzzle is supplied, consumes the earliest
    scheduled puzzle inside the new window (pending when the window holds
    none). The old session is stamped ended and kept as history with its
    words and submissions; the new session starts from scratch for today's UTC
    date under the game's own reset cadence. Future puzzles belong on the
    schedule endpoint below.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken from the payload:
    an administrator cannot publish a puzzle attributed to somebody else.
    """
    return connections_service.create_connections_game(
        db,
        list(payload.groups) if payload.groups is not None else None,
        created_by=account.regno,
    )


@router.get("/current", response_model=ConnectionsGame)
def current_connections_session(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ConnectionsGame:
    """The current active-or-pending session, or 404 when the game has none.

    Declared before ``/{game_id}`` so ``current`` is never read as an id.
    """
    from fastapi import HTTPException

    current = connections_service.get_current_connections_session(db)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Connections session yet")
    return current


@router.post("/schedule", response_model=ConnectionsScheduledPuzzle, status_code=status.HTTP_201_CREATED)
def schedule_connections_puzzle(
    payload: ConnectionsScheduledPuzzleCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ConnectionsScheduledPuzzle:
    """Schedule a manual puzzle for a future session period.

    Bounded by ``MAX_FUTURE_SCHEDULE_DAYS`` ahead of today (UTC), enforced
    server-side. The puzzle waits until its period opens and never activates a
    session early.
    """
    return ConnectionsScheduledPuzzle(
        **connections_service.schedule_puzzle(
            db,
            payload.target_date,
            list(payload.groups),
            created_by=account.regno,
        )
    )


@router.get("/scheduled", response_model=list[ConnectionsScheduledPuzzle])
def list_connections_scheduled(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> list[ConnectionsScheduledPuzzle]:
    """Every scheduled Connections puzzle, earliest date first."""
    return [ConnectionsScheduledPuzzle(**entry) for entry in connections_service.list_scheduled(db)]


@router.delete("/scheduled/{target_date}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connections_scheduled(
    target_date: date,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> None:
    """Withdraw the scheduled Connections puzzle for one date; a date with none is a 404."""
    connections_service.delete_scheduled(db, target_date)
    return None


@router.get("", response_model=Dict[str, Any])
def list_connections_games(
    skip: int = Query(0, ge=0, description="How many sessions to skip."),
    limit: int = Query(50, ge=1, le=CONNECTIONS_GAMES_MAX_LIMIT, description="How many sessions to return. Clamped so one request can never materialise a whole season at once."),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> Dict[str, Any]:
    """One page of every configured puzzle, newest day first, with the total beside it."""
    return connections_service.list_connections_games(db, skip=skip, limit=limit)


@router.get("/{game_id}", response_model=ConnectionsGame)
def get_connections_game(
    game_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> ConnectionsGame:
    """One configured puzzle by id."""
    return connections_service.get_connections_game(db, game_id)
