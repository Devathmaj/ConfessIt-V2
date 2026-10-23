"""Manual word scheduling for Wordle and Anagram within bounded future horizons."""

from datetime import date, timedelta
from typing import Optional

from fastapi import HTTPException, status

from ..db_models import ScheduledGameContentRecord
from ..limits import MAX_FUTURE_SCHEDULE_DAYS
from ..postgres import Database
from . import game_periods
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError


def _manual_games() -> tuple[str, ...]:
    return ("wordle", "anagram")


def validate_schedule_target(target: date, today: Optional[date] = None) -> date:
    """Refuse targets outside ``[today, today + MAX_FUTURE_SCHEDULE_DAYS]``."""
    day = today if today is not None else game_periods.utc_today()
    horizon = day + timedelta(days=MAX_FUTURE_SCHEDULE_DAYS)
    if target < day:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Scheduled content must be for today or a future date.",
        )
    if target > horizon:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Scheduled content is limited to {MAX_FUTURE_SCHEDULE_DAYS} days "
                f"ahead (up to {horizon.isoformat()})."
            ),
        )
    return target


def _validate_word(game_id: str, word: Optional[str]) -> str:
    """Apply the game's own word rules to a scheduled word."""
    if game_id == "wordle":
        from .wordle_service import _require_approved

        return _require_approved(word, "A scheduled Wordle needs a word.")
    from .anagram_service import _require_answer

    return _require_answer(word, "A scheduled Anagram needs a word.")


def schedule_word(
    db: Database,
    game_id: str,
    target_date: date,
    word: Optional[str],
    created_by: str,
    today: Optional[date] = None,
) -> dict:
    """Store (or replace) the manual word for one future period-start date."""
    if game_id not in _manual_games():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only Wordle and Anagram accept scheduled words.",
        )
    validate_schedule_target(target_date, today)
    normalized = _validate_word(game_id, word)
    with db.sessions.begin() as session:
        existing = session.scalar(
            select(ScheduledGameContentRecord).where(
                ScheduledGameContentRecord.game_id == game_id,
                ScheduledGameContentRecord.target_date == target_date,
            )
        )
        if existing is not None:
            existing.word = normalized
            existing.created_by = created_by
            session.flush()
            row = existing
        else:
            row = ScheduledGameContentRecord(
                game_id=game_id,
                target_date=target_date,
                word=normalized,
                created_by=created_by,
            )
            try:
                with session.begin_nested():
                    # Add inside the savepoint: begin_nested() flushes pending
                    # state first, so adding before it would put the INSERT in
                    # the outer transaction and its unique violation would
                    # abort the transaction the retry below needs.
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # Retry update on concurrent insert conflict for same (game, date).
                existing = session.scalar(
                    select(ScheduledGameContentRecord).where(
                        ScheduledGameContentRecord.game_id == game_id,
                        ScheduledGameContentRecord.target_date == target_date,
                    )
                )
                assert existing is not None, "the winning schedule must exist"
                existing.word = normalized
                existing.created_by = created_by
                session.flush()
                row = existing
        return {
            "id": str(row.id),
            "game_id": row.game_id,
            "target_date": row.target_date.isoformat(),
            "word": row.word,
            "created_by": row.created_by,
        }


def list_scheduled(db: Database, game_id: str) -> list[dict]:
    """Every scheduled word for ``game_id``, earliest date first."""
    with db.sessions() as session:
        rows = session.scalars(
            select(ScheduledGameContentRecord)
            .where(ScheduledGameContentRecord.game_id == game_id)
            .order_by(ScheduledGameContentRecord.target_date.asc())
        ).all()
        return [
            {
                "id": str(row.id),
                "game_id": row.game_id,
                "target_date": row.target_date.isoformat(),
                "word": row.word,
                "created_by": row.created_by,
            }
            for row in rows
        ]


def delete_scheduled(db: Database, game_id: str, target_date: date) -> None:
    """Withdraw the scheduled word for one date; a date with none is a 404."""
    with db.sessions.begin() as session:
        row = session.scalar(
            select(ScheduledGameContentRecord).where(
                ScheduledGameContentRecord.game_id == game_id,
                ScheduledGameContentRecord.target_date == target_date,
            )
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No scheduled word for {target_date.isoformat()}.",
            )
        session.delete(row)
