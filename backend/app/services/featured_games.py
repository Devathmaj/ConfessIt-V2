"""The spotlight schedule: at most one featured game per UTC calendar date.

Featured records are independent of sessions by design. Setting Wordle as the
featured game for a date never creates, expires or otherwise touches any
session; it only decides what the featured page highlights that day. The same
game may be featured on many dates, but two games may never share one date --
and that rule is a UNIQUE constraint on ``game_date``, not a UI convention, so
two administrators pinning one day race the database and one of them is refused.

A single game may not hold the spotlight more than seven days in a row: a
write that would stretch its consecutive run past a week is refused, so the
spotlight keeps rotating. Week-per-game blocks are still possible -- one game
for a week, another for a day, then the first game again -- because only the
unbroken run matters, never the total.
"""

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
    """The consecutive-day run of ``game_id`` containing ``day``.

    ``by_date`` maps the dates around ``day`` to the game holding each one,
    already overlaid with whatever the write under validation would store.
    Only the unbroken run through ``day`` counts: a gap or another game on
    either side ends it.
    """
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
    """Pin ``game_id`` as the featured game for ``game_date``.

    A date that already has a featured game is a 409 naming the date -- checked
    here for the message, enforced by the schema for the race. A pin that
    would stretch the game's consecutive run past a week is a 422.
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
        window = _window_map(
            session, game_date - timedelta(days=MAX_FEATURED_CHAIN),
            game_date + timedelta(days=MAX_FEATURED_CHAIN),
        )
        window[game_date] = game_id
        _refuse_overlong_chain(
            game_id, _chain_length(window, game_id, game_date), game_date, game_date
        )
        if existing is not None:
            session.delete(existing)
            session.flush()
        row = FeaturedGameRecord(
            game_date=game_date, game_id=game_id, created_by=created_by
        )
        session.add(row)
        session.flush()
        return _as_dict(row)


def set_featured_range(
    db: Database, start_date: date, end_date: date, game_id: str, created_by: str
) -> list[dict]:
    """Feature ``game_id`` for every date from ``start_date`` to ``end_date``.

    Each date in the span must be free of other games: the first date held by
    another game is a 409 naming it, and nothing is stored. Dates the same
    game already holds are kept as-is. A span whose consecutive run -- merged
    with the same game's picks bordering it -- passes a week is a 422, like
    the single-date pins.
    """
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
    """Feature ``game_id`` for ``[start_date, end_date]``, erasing other picks.

    Unlike :func:`set_featured_range` this never 409s: dates held by other
    games are removed in the same transaction and the new picks stored. Dates
    the same game already holds are kept as-is. A span whose consecutive run
    -- merged with the same game's picks bordering it -- passes a week is a
    422, like every other featuring write. League history is untouched:
    awards snapshot ``is_featured`` at finalization time.
    """
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
