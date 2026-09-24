"""Featuring the Reaction Time game for a day: the administrator's half.

Featuring only pins the spotlight: Reaction Time *sessions* are opened by the
reset loop under the game's own cadence, never by featuring, and there is no
Update action for this game -- the session carries no content to configure
(no word, no timing range, no difficulty), so a new period is playable the
moment it opens. This endpoint keeps its request/response shape and delegates
to the featured store, so the admin screen pins the spotlight without
touching any session. The same shape the Couple Questionnaire keeps, for the
same reason: one featured-date surface across games that need nothing else.
"""

from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select

from ..db_models import ReactionTimeGameRecord
from ..models import ReactionTimeGame, ReactionTimeGameCreate
from ..postgres import Database, is_valid_id


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _as_model(row) -> ReactionTimeGame:
    return ReactionTimeGame(
        id=str(row.id),
        game_date=row.game_date,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_featured_model(row) -> ReactionTimeGame:
    """A featured-games row for Reaction Time, in the admin response shape."""
    return ReactionTimeGame(
        id=str(row.id),
        game_date=row.game_date,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create_reaction_time_game(
    db: Database, payload: ReactionTimeGameCreate, created_by: str
) -> ReactionTimeGame:
    """Feature the Reaction Time game for one day (spotlight pin, new table).

    Featured dates live in ``featured_games`` -- one game per date across all
    games -- while Reaction Time *sessions* live in ``reaction_time_games``
    and are opened by the reset loop, never by featuring. This endpoint keeps
    its request/response shape and delegates to the featured store, so the
    admin screen pins the spotlight without touching any session.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken
    from the payload: an administrator cannot feature a day attributed to
    somebody else. A day that already holds a featured game (any game) is a
    409, because the spotlight seats one game per date.
    """
    from .featured_games import set_featured as _set_featured

    game_date = payload.game_date or _today()
    created = _set_featured(db, game_date, "reaction-time", created_by)
    with db.sessions() as session:
        from ..db_models import FeaturedGameRecord

        row = session.get(FeaturedGameRecord, created["id"])
        assert row is not None
        return _as_featured_model(row)


def list_reaction_time_games(db: Database) -> List[ReactionTimeGame]:
    """Every date Reaction Time is featured on, newest day first."""
    from ..db_models import FeaturedGameRecord

    with db.sessions() as session:
        rows = session.scalars(
            select(FeaturedGameRecord)
            .where(FeaturedGameRecord.game_id == "reaction-time")
            .order_by(FeaturedGameRecord.game_date.desc())
        ).all()
        return [_as_featured_model(row) for row in rows]


def get_reaction_time_game(db: Database, game_id: str) -> ReactionTimeGame:
    """One featured date by id.

    A malformed id is a 400 and a well-formed one that matches nothing is a
    404, so a typo in a URL and a date that was never featured are told apart
    rather than both reported as "not found".
    """
    from ..db_models import FeaturedGameRecord

    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Reaction Time game id",
        )
    with db.sessions() as session:
        row = session.get(FeaturedGameRecord, str(game_id))
    if row is None or row.game_id != "reaction-time":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Reaction Time game not found",
        )
    return _as_featured_model(row)


def get_current_reaction_time_session(db: Database) -> Optional[dict]:
    """The current Reaction Time session for the admin screen, if any."""
    with db.sessions() as session:
        row = session.scalar(
            select(ReactionTimeGameRecord)
            .where(ReactionTimeGameRecord.status.in_(("active", "pending")))
            .limit(1)
        )
        if row is None:
            return None
        return {
            "id": str(row.id),
            "game_date": row.game_date.isoformat(),
            "status": row.status,
            "period_start_date": row.period_start_date.isoformat()
            if row.period_start_date
            else None,
            "period_end_date": row.period_end_date.isoformat()
            if row.period_end_date
            else None,
        }
