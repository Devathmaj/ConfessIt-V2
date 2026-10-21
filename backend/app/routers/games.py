# app/routers/games.py

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

from ..db_models import (
    AnagramGameRecord,
    CaseClosedGameRecord,
    ConnectionsGameRecord,
    CoupleGameRecord,
    PlotTwistGameRecord,
    ReactionTimeGameRecord,
    TriviaDropQuestionRecord,
    TwoKeyEscapeGameRecord,
    WordleGameRecord,
)
from ..dependencies import get_db
from ..models import (
    Cohort,
    GameSelectEntry,
    GameSelectResponse,
    PlayerGameSummary,
    UserDetails,
)
from ..postgres import Database
from ..services import couple_gameplay, featured_games, player_stats
from ..services.authorization import require_participant
from ..services.game_configs import get_config

router = APIRouter(prefix="/games", tags=["Games"])

# What the selection screen and the featured page read. Disabled games appear as
# temporarily unavailable, and a game whose session is pending (manual word not yet
# provided) as waiting for configuration. Direct game endpoints enforce the same
# rules again, so this list is discovery, never the boundary.

_GAME_NAMES = {
    "wordle": "Wordle",
    "anagram": "Anagram",
    "couple-questionnaire": "Couple Questionnaire",
    "reaction-time": "Reaction Time",
    "connections": "Connections",
    "trivia-drop": "Trivia Drop",
    "two-key-escape": "Two-Key Escape",
    "plot-twist": "Plot Twist",
    "case-closed": "Case Closed",
}

# Former barebones titles now live: kept empty so _entry uses sessions.
_BAREBONES_GAMES: tuple[str, ...] = ()

_SESSION_MODELS = {
    "wordle": WordleGameRecord,
    "anagram": AnagramGameRecord,
    "couple-questionnaire": CoupleGameRecord,
    "reaction-time": ReactionTimeGameRecord,
    "connections": ConnectionsGameRecord,
    "two-key-escape": TwoKeyEscapeGameRecord,
    "plot-twist": PlotTwistGameRecord,
    "case-closed": CaseClosedGameRecord,
}


def _trivia_entry(session, config, featured_id: str | None) -> GameSelectEntry | None:
    """Trivia Drop as the selection screen sees it: live question or waiting.

    Trivia Drop has no periods and no pending state: a live question is playable,
    anything else a wait for the next release. Drafts and scheduled questions are
    never named here.
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


def _barebones_entry(session, game_id: str, featured_id: str | None) -> GameSelectEntry:
    """A session-free placeholder: hidden/disabled behave like every game,
    but an enabled entry is listed as not yet playable until gameplay lands."""
    config = get_config(session, game_id)
    if not config.enabled:
        return GameSelectEntry(
            game_id=game_id,
            name=_GAME_NAMES[game_id],
            enabled=False,
            playable=False,
            featured=featured_id == game_id,
            message="This game is temporarily unavailable. Please try again later.",
        )
    return GameSelectEntry(
        game_id=game_id,
        name=_GAME_NAMES[game_id],
        enabled=True,
        playable=False,
        featured=featured_id == game_id,
        message="Coming soon — gameplay is still being built. Check back later.",
    )


def _entry(
    session, game_id: str, today, featured_id: str | None
) -> GameSelectEntry | None:
    config = get_config(session, game_id)
    if config.is_hidden:
        return None
    if game_id in _BAREBONES_GAMES:
        return _barebones_entry(session, game_id, featured_id)
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


@router.get("/partner-cohorts", response_model=list[Cohort])
def partner_cohorts(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> list[Cohort]:
    """Every ``(year, department)`` pair the partner pickers may filter by.

    One list for all four couple games: they pair from the same participant table
    and filter on the same pair, so they share one palette of cohorts that actually
    exist rather than four derived copies. Declared before any ``/{...}``-style route
    so ``partner-cohorts`` is never read as an id.
    """
    del user  # identity gates the route; the palette is the same for everyone.
    return couple_gameplay.list_partner_cohorts(db)


@router.get("/select", response_model=GameSelectResponse)
def game_selection(
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
                "two-key-escape",
                "plot-twist",
                "case-closed",
            )
            if (entry := _entry(session, game_id, today, featured_id)) is not None
        ]
    return GameSelectResponse(games=games, featured_game_id=featured_id, server_date=today)


@router.get("/featured")
def featured_today(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> dict:
    """Today's featured game (UTC), if the day has a pick.

    The spotlight only: featuring never implies anything about sessions, and a game
    is playable featured or not unless it is disabled or hidden.
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


@router.get("/summary", response_model=PlayerGameSummary)
def player_summary(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> PlayerGameSummary:
    """The caller's own finished rounds, streaks and this week's activity.

    Read from the games' native participation tables so a round played today appears
    immediately, not when its league event finalizes. Strictly personal: every
    collector filters on the caller's id and no other player's activity is materialised.
    """
    return PlayerGameSummary(**player_stats.build_player_summary(db, user_id=user.id))
