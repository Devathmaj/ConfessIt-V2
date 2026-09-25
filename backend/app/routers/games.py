# app/routers/games.py

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..db_models import (
    AnagramGameRecord,
    ConnectionsGameRecord,
    CoupleGameRecord,
    ReactionTimeGameRecord,
    TriviaDropQuestionRecord,
    WordleGameRecord,
)
from ..dependencies import get_db
from ..models import GameSelectEntry, GameSelectResponse, UserDetails
from ..postgres import Database
from ..services import featured_games
from ..services.authorization import require_participant
from ..services.game_configs import get_config

router = APIRouter(prefix="/games", tags=["Games"])

# What the selection screen and the featured page read. Hidden games never
# appear here; disabled games appear as temporarily unavailable; a game whose
# session is pending (manual word not yet provided) appears as waiting for
# configuration. Direct game endpoints enforce the same rules again, so this
# list is discovery, never the boundary.

_GAME_NAMES = {
    "wordle": "Wordle",
    "anagram": "Anagram",
    "couple-questionnaire": "Couple Questionnaire",
    "reaction-time": "Reaction Time",
    "connections": "Connections",
    "trivia-drop": "Trivia Drop",
}

_SESSION_MODELS = {
    "wordle": WordleGameRecord,
    "anagram": AnagramGameRecord,
    "couple-questionnaire": CoupleGameRecord,
    "reaction-time": ReactionTimeGameRecord,
    "connections": ConnectionsGameRecord,
}


def _trivia_entry(session, config, featured_id: str | None) -> GameSelectEntry | None:
    """Trivia Drop as the selection screen sees it: live question or waiting.

    Trivia Drop has no periods and no pending state: a live question is
    playable, anything else is a wait for the next release. Drafts and
    scheduled questions are never named here.
    """
    live = session.scalar(
        select(TriviaDropQuestionRecord)
        .where(TriviaDropQuestionRecord.status == "active")
        .limit(1)
    )
    if not config.enabled:
        return GameSelectEntry(
            game_id="trivia-drop",
            name=_GAME_NAMES["trivia-drop"],
            enabled=False,
            playable=False,
            session_status=live.status if live is not None else None,
            featured=featured_id == "trivia-drop",
            message="This game is temporarily unavailable. Please try again later.",
        )
    if live is None:
        return GameSelectEntry(
            game_id="trivia-drop",
            name=_GAME_NAMES["trivia-drop"],
            enabled=True,
            playable=False,
            featured=featured_id == "trivia-drop",
            message="No question is live right now. Check back soon.",
        )
    return GameSelectEntry(
        game_id="trivia-drop",
        name=_GAME_NAMES["trivia-drop"],
        enabled=True,
        playable=True,
        session_status="active",
        featured=featured_id == "trivia-drop",
    )


def _entry(
    session, game_id: str, today, featured_id: str | None
) -> GameSelectEntry | None:
    config = get_config(session, game_id)
    if config.is_hidden:
        return None
    if game_id == "trivia-drop":
        return _trivia_entry(session, config, featured_id)
    model = _SESSION_MODELS[game_id]
    current = session.scalar(
        select(model).where(model.status.in_(("active", "pending"))).limit(1)
    )
    if not config.enabled:
        return GameSelectEntry(
            game_id=game_id,
            name=_GAME_NAMES[game_id],
            enabled=False,
            playable=False,
            session_status=current.status if current is not None else None,
            featured=featured_id == game_id,
            message="This game is temporarily unavailable. Please try again later.",
        )
    if current is None:
        return GameSelectEntry(
            game_id=game_id,
            name=_GAME_NAMES[game_id],
            enabled=True,
            playable=False,
            featured=featured_id == game_id,
            message="This game is not available right now. Check back soon.",
        )
    if current.status == "pending":
        return GameSelectEntry(
            game_id=game_id,
            name=_GAME_NAMES[game_id],
            enabled=True,
            playable=False,
            session_status="pending",
            featured=featured_id == game_id,
            message="Today's content has not been set yet. Check back soon.",
        )
    return GameSelectEntry(
        game_id=game_id,
        name=_GAME_NAMES[game_id],
        enabled=True,
        playable=True,
        session_status="active",
        featured=featured_id == game_id,
    )


@router.get("/select", response_model=GameSelectResponse)
async def game_selection(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> GameSelectResponse:
    """Every discoverable game with its playability, plus today's featured pick."""
    del user  # identity gates the route; the list itself is per-game, not per-user.
    today = datetime.now(timezone.utc).date()
    with db.sessions.begin() as session:
        featured = featured_games.get_featured_for_date(session, today)
        featured_id = featured.game_id if featured is not None else None
        games = [
            entry
            for game_id in (
                "wordle",
                "anagram",
                "couple-questionnaire",
                "reaction-time",
                "connections",
                "trivia-drop",
            )
            if (entry := _entry(session, game_id, today, featured_id)) is not None
        ]
    return GameSelectResponse(games=games, featured_game_id=featured_id, server_date=today)


@router.get("/featured")
async def featured_today(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> dict:
    """Today's featured game (UTC), if the day has a pick.

    The spotlight only: featuring never implies anything about sessions, and a
    game is playable featured or not unless it is disabled or hidden.
    """
    del user
    today = datetime.now(timezone.utc).date()
    with db.sessions() as session:
        featured = featured_games.get_featured_for_date(session, today)
        if featured is None:
            return {"game_id": None, "game_date": None, "server_date": today.isoformat()}
        return {
            "game_id": featured.game_id,
            "game_date": featured.game_date.isoformat(),
            "server_date": today.isoformat(),
        }
