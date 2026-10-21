"""Spotlight schedule: at most one featured game per UTC date with max 7 consecutive days."""

from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db_models import FeaturedGameRecord
from ..postgres import Database
from .game_configs import validate_game_id

#: The longest unbroken run one game may hold the spotlight.
MAX_FEATURED_CHAIN = 7


def _as_dict(row: FeaturedGameRecord) -> dict:
    return {
        "id": str(row.id),
        "game_date": row.game_date.isoformat(),
        "game_id": row.game_id,
        "created_by": row.created_by,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _chain_length(
    by_date: dict[date, str], game_id: str, day: date
) -> int:
    """Calculate unbroken consecutive days game_id holds spotlight around day."""
    run = 1
    cursor = day - timedelta(days=1)
    while by_date.get(cursor) == game_id:
        run += 1
        cursor -= timedelta(days=1)
    cursor = day + timedelta(days=1)
    while by_date.get(cursor) == game_id:
        run += 1
        cursor += timedelta(days=1)
    return run


def _refuse_overlong_chain(game_id: str, run: int, start: date, end: date) -> None:
    """Refuse a write whose consecutive run passes the one-week cap."""
    if run > MAX_FEATURED_CHAIN:
        span = start.isoformat() if start == end else f"{start.isoformat()} to {end.isoformat()}"
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Featuring {game_id} for {span} would run it {run} days in a row: "
                f"one game may hold the spotlight at most {MAX_FEATURED_CHAIN} days "
                "in a row. Feature another game in between instead."
            ),
        )


def _window_map(session, start: date, end: date) -> dict[date, str]:
    """The spotlight holder per date over ``[start, end]``, as stored now."""
    rows = session.scalars(
        select(FeaturedGameRecord).where(
            FeaturedGameRecord.game_date >= start,
            FeaturedGameRecord.game_date <= end,
        )
    ).all()
    return {row.game_date: row.game_id for row in rows}


def set_featured(
    db: Database, game_date: date, game_id: str, created_by: str
) -> dict:
    """Pin game_id as featured game for date, enforcing uniqueness and run limits."""
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
        window = _window_map(
            session, game_date - timedelta(days=MAX_FEATURED_CHAIN),
            game_date + timedelta(days=MAX_FEATURED_CHAIN),
        )
        window[game_date] = game_id
        _refuse_overlong_chain(
            game_id, _chain_length(window, game_id, game_date), game_date, game_date
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
    """Set game_id as featured game for date, replacing existing pick if present."""
    validate_game_id(game_id)
    with db.sessions.begin() as session:
        existing = session.scalar(
            select(FeaturedGameRecord).where(
                FeaturedGameRecord.game_date == game_date
            )
        )
        if existing is not None and existing.game_id == game_id:
            return _as_dict(existing)
        window = _window_map(
            session, game_date - timedelta(days=MAX_FEATURED_CHAIN),
            game_date + timedelta(days=MAX_FEATURED_CHAIN),
        )
        window[game_date] = game_id
        _refuse_overlong_chain(
            game_id, _chain_length(window, game_id, game_date), game_date, game_date
        )
        try:
            with session.begin_nested():
                if existing is not None:
                    session.delete(existing)
                    session.flush()
                row = FeaturedGameRecord(
                    game_date=game_date, game_id=game_id, created_by=created_by
                )
                session.add(row)
                session.flush()
        except IntegrityError:
            # Retry replacement on concurrent race conflict.
            winner = session.scalar(
                select(FeaturedGameRecord).where(
                    FeaturedGameRecord.game_date == game_date
                )
            )
            if winner is not None and winner.game_id == game_id:
                return _as_dict(winner)
            try:
                with session.begin_nested():
                    if winner is not None:
                        session.delete(winner)
                        session.flush()
                    row = FeaturedGameRecord(
                        game_date=game_date, game_id=game_id, created_by=created_by
                    )
                    session.add(row)
                    session.flush()
            except IntegrityError as exc:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A featured game is already set for {game_date.isoformat()}.",
                ) from exc
            return _as_dict(row)
        return _as_dict(row)


def set_featured_range(
    db: Database, start_date: date, end_date: date, game_id: str, created_by: str
) -> list[dict]:
    """Feature game_id across date range, failing if any date is already taken."""
    validate_game_id(game_id)
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The range start must not be after the range end.",
        )
    days = [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]
    with db.sessions.begin() as session:
        window = _window_map(
            session, start_date - timedelta(days=MAX_FEATURED_CHAIN),
            end_date + timedelta(days=MAX_FEATURED_CHAIN),
        )
        for day in days:
            holder = window.get(day)
            if holder is not None and holder != game_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A featured game is already set for {day.isoformat()}.",
                )
        for day in days:
            window[day] = game_id
        longest = max(_chain_length(window, game_id, day) for day in days)
        _refuse_overlong_chain(game_id, longest, start_date, end_date)
        try:
            created: list[dict] = []
            for day in days:
                row = session.scalar(
                    select(FeaturedGameRecord).where(
                        FeaturedGameRecord.game_date == day
                    )
                )
                if row is None:
                    row = FeaturedGameRecord(
                        game_date=day, game_id=game_id, created_by=created_by
                    )
                    session.add(row)
                    session.flush()
                created.append(_as_dict(row))
            return created
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A featured game is already set for one of those dates.",
            ) from exc


def replace_featured_range(
    db: Database, start_date: date, end_date: date, game_id: str, created_by: str
) -> list[dict]:
    """Feature game_id across date range, overwriting conflicting picks."""
    validate_game_id(game_id)
    if start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The range start must not be after the range end.",
        )
    days = [
        start_date + timedelta(days=offset)
        for offset in range((end_date - start_date).days + 1)
    ]
    with db.sessions.begin() as session:
        window = _window_map(
            session, start_date - timedelta(days=MAX_FEATURED_CHAIN),
            end_date + timedelta(days=MAX_FEATURED_CHAIN),
        )
        for day in days:
            window[day] = game_id
        longest = max(_chain_length(window, game_id, day) for day in days)
        _refuse_overlong_chain(game_id, longest, start_date, end_date)
        try:
            replaced: list[dict] = []
            for day in days:
                row = session.scalar(
                    select(FeaturedGameRecord).where(
                        FeaturedGameRecord.game_date == day
                    )
                )
                if row is None:
                    row = FeaturedGameRecord(
                        game_date=day, game_id=game_id, created_by=created_by
                    )
                    session.add(row)
                    session.flush()
                elif row.game_id != game_id:
                    session.delete(row)
                    session.flush()
                    row = FeaturedGameRecord(
                        game_date=day, game_id=game_id, created_by=created_by
                    )
                    session.add(row)
                    session.flush()
                replaced.append(_as_dict(row))
            return replaced
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A featured game is already set for one of those dates.",
            ) from exc


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
