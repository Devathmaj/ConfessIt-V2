"""The spotlight schedule: at most one featured game per UTC calendar date.

Featured records are independent of sessions by design. Setting Wordle as the
featured game for a date never creates, expires or otherwise touches any
session; it only decides what the featured page highlights that day. The same
game may be featured on many dates, but two games may never share one date --
and that rule is a UNIQUE constraint on ``game_date``, not a UI convention, so
two administrators pinning one day race the database and one of them is refused.
"""

from datetime import date
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db_models import FeaturedGameRecord
from ..postgres import Database
from .game_configs import validate_game_id


def _as_dict(row: FeaturedGameRecord) -> dict:
    return {
        "id": str(row.id),
        "game_date": row.game_date.isoformat(),
        "game_id": row.game_id,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def set_featured(
    db: Database, game_date: date, game_id: str, created_by: str
) -> dict:
    """Pin ``game_id`` as the featured game for ``game_date``.

    A date that already has a featured game is a 409 naming the date -- checked
    here for the message, enforced by the schema for the race.
    """
    validate_game_id(game_id)
    with db.sessions() as session:
        existing = session.scalar(
            select(FeaturedGameRecord.id)
            .where(FeaturedGameRecord.game_date == game_date)
            .limit(1)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A featured game is already set for {game_date.isoformat()}.",
            )
    try:
        with db.sessions.begin() as session:
            row = FeaturedGameRecord(
                game_date=game_date, game_id=game_id, created_by=created_by
            )
            session.add(row)
            session.flush()
            return _as_dict(row)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A featured game is already set for {game_date.isoformat()}.",
        ) from exc


def replace_featured(
    db: Database, game_date: date, game_id: str, created_by: str
) -> dict:
    """Set ``game_id`` as the featured game for ``game_date``, replacing any pick.

    Unlike :func:`set_featured` this never 409s: when another game holds the
    date it is removed in the same transaction and the new pick is stored.
    When the same game already holds the date the existing row is returned
    unchanged (idempotent). League awards already finalized keep the snapshot
    they were scored with -- finalization reads the schedule once at scoring
    time and never re-derives it -- so replacing the spotlight only changes
    which game earns 2x from now on.
    """
    validate_game_id(game_id)
    with db.sessions.begin() as session:
        existing = session.scalar(
            select(FeaturedGameRecord).where(
                FeaturedGameRecord.game_date == game_date
            )
        )
        if existing is not None and existing.game_id == game_id:
            return _as_dict(existing)
        if existing is not None:
            session.delete(existing)
            session.flush()
        row = FeaturedGameRecord(
            game_date=game_date, game_id=game_id, created_by=created_by
        )
        session.add(row)
        session.flush()
        return _as_dict(row)


def clear_featured(db: Database, game_date: date) -> None:
    """Remove the featured pick for ``game_date``; a day with none is a 404."""
    with db.sessions.begin() as session:
        row = session.scalar(
            select(FeaturedGameRecord).where(
                FeaturedGameRecord.game_date == game_date
            )
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No featured game is set for {game_date.isoformat()}.",
            )
        session.delete(row)


def list_featured(db: Database) -> list[dict]:
    """Every featured date, newest day first."""
    with db.sessions() as session:
        rows = session.scalars(
            select(FeaturedGameRecord).order_by(FeaturedGameRecord.game_date.desc())
        ).all()
        return [_as_dict(row) for row in rows]


def get_featured_for_date(session, game_date: date) -> Optional[FeaturedGameRecord]:
    """The featured row for ``game_date``, or None when the day has no pick."""
    return session.scalar(
        select(FeaturedGameRecord).where(FeaturedGameRecord.game_date == game_date)
    )
