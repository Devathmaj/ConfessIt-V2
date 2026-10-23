"""Configuring Connections puzzles: session management, word groups, and scheduling."""

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..db_models import ConnectionsGameRecord, ConnectionsWordRecord
from ..limits import (
    CONNECTIONS_GROUPS_COUNT,
    CONNECTIONS_WORD_MAX,
    CONNECTIONS_WORDS_PER_GROUP,
    CONNECTIONS_WORDS_TOTAL,
)
from ..models import ConnectionsGame
from ..postgres import Database, is_valid_id


def _today() -> date:
    return datetime.now(timezone.utc).date()


def normalize_word(raw: object) -> tuple[str, str]:
    """Validate and return display and lowercase comparison spellings of one word."""
    if not isinstance(raw, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Each word must be text.",
        )
    display = raw.strip()
    if not display:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Words must not be empty.",
        )
    if len(display) > CONNECTIONS_WORD_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Each word is at most {CONNECTIONS_WORD_MAX} characters.",
        )
    return display, display.lower()


def validate_puzzle_groups(groups: object) -> List[List[str]]:
    """Validate a 4x4 puzzle structure and return display spellings."""
    if not isinstance(groups, list) or len(groups) != CONNECTIONS_GROUPS_COUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A puzzle has exactly {CONNECTIONS_GROUPS_COUNT} groups.",
        )
    normalized_groups: List[List[str]] = []
    seen: dict[str, str] = {}
    for position, group in enumerate(groups):
        if not isinstance(group, list) or len(group) != CONNECTIONS_WORDS_PER_GROUP:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Group {position + 1} must hold exactly "
                    f"{CONNECTIONS_WORDS_PER_GROUP} words."
                ),
            )
        normalized_group: List[str] = []
        for raw in group:
            display, normalized = normalize_word(raw)
            if normalized in seen:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Duplicate word {display!r}: each word may appear only once.",
                )
            seen[normalized] = display
            normalized_group.append(display)
        normalized_groups.append(normalized_group)
    if len(seen) != CONNECTIONS_WORDS_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A puzzle has exactly {CONNECTIONS_WORDS_TOTAL} distinct words.",
        )
    return normalized_groups


def _as_model(row: ConnectionsGameRecord, word_rows: List[ConnectionsWordRecord]) -> ConnectionsGame:
    """A session as the admin screen reads it, with its canonical groups."""
    ordered = sorted(word_rows, key=lambda word: (word.group_index, word.position_in_group))
    groups: List[List[str]] = [[] for _ in range(CONNECTIONS_GROUPS_COUNT)]
    for word in ordered:
        if 0 <= word.group_index < CONNECTIONS_GROUPS_COUNT:
            groups[word.group_index].append(word.word_display)
    return ConnectionsGame(
        id=str(row.id),
        game_date=row.game_date,
        groups=groups if word_rows else [],
        status=row.status or "active",
        period_start_date=row.period_start_date,
        period_end_date=row.period_end_date,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _current_session_row(session):
    """The one active-or-pending Connections session, or None."""
    return session.scalar(
        select(ConnectionsGameRecord)
        .where(ConnectionsGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def _words_for(session, game_id: str) -> List[ConnectionsWordRecord]:
    return list(
        session.scalars(
            select(ConnectionsWordRecord).where(
                ConnectionsWordRecord.connections_game_id == game_id
            )
        ).all()
    )


def _attach_words(session, game_id: str, groups: List[List[str]]) -> None:
    """Store the sixteen validated display spellings for one session."""
    for group_index, group in enumerate(groups):
        for position, display in enumerate(group):
            session.add(
                ConnectionsWordRecord(
                    connections_game_id=str(game_id),
                    word_display=display,
                    word_normalized=display.strip().lower(),
                    group_index=group_index,
                    position_in_group=position,
                )
            )
    session.flush()


def update_connections_session(
    db: Database,
    *,
    groups: Optional[List[List[str]]] = None,
    updated_by: str = "admin",
    now: Optional[datetime] = None,
    discard_future_schedules: bool = False,
) -> ConnectionsGame:
    """End current session and open a new session with supplied or scheduled puzzle."""
    from sqlalchemy import text as _text

    from . import game_periods
    from .game_configs import game_lock_key, get_config
    from .game_scheduler import (
        _consume_earliest_puzzle,
        _discard_all_scheduled_puzzles,
        _discard_scheduled_puzzles_before,
    )

    normalized = validate_puzzle_groups(groups) if groups is not None else None
    supplied = normalized is not None
    moment = now or datetime.now(timezone.utc)
    today = moment.date()

    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("connections")},
            )
            config = get_config(session, "connections")
            config.content_mode = "manual"
            cadence = config.reset_cadence
            current = _current_session_row(session)
            if current is not None:
                current.status = "ended"
                current.ended_at = moment
            start, end = game_periods.period_for_start(today, cadence)
            if not supplied:
                normalized = _consume_earliest_puzzle(session, start, end)
            if discard_future_schedules:
                _discard_all_scheduled_puzzles(session)
            else:
                _discard_scheduled_puzzles_before(session, end)
            row = ConnectionsGameRecord(
                game_date=today,
                status="active" if normalized else "pending",
                period_start_date=start,
                period_end_date=end,
                started_at=moment,
                created_by=updated_by,
            )
            session.add(row)
            session.flush()
            if normalized:
                _attach_words(session, str(row.id), normalized)
            from . import league_finalization

            league_finalization.snapshot_opened_safely(
                session, game_id="connections", opportunity_id=str(row.id)
            )
            result = _as_model(row, _words_for(session, str(row.id)))
        # Finalize awards for the closed session immediately.
        league_finalization.finalize_game_safely(db, "connections")
        return result
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Connections session update collided with a concurrent reset. Try again.",
        ) from exc


def create_connections_game(
    db: Database, groups: Optional[List[List[str]]], created_by: str
) -> ConnectionsGame:
    """Publish or roll over Connections session from admin request."""
    return update_connections_session(db, groups=groups, updated_by=created_by)


# Max page limit for session history.
CONNECTIONS_GAMES_MAX_LIMIT = 100


def _words_for_many(session, game_ids: List[str]) -> Dict[str, List]:
    """Fetch word rows for multiple sessions in a single query."""
    by_game: Dict[str, List] = {}
    if not game_ids:
        return by_game
    for word in session.scalars(
        select(ConnectionsWordRecord).where(
            ConnectionsWordRecord.connections_game_id.in_(game_ids)
        )
    ).all():
        by_game.setdefault(str(word.connections_game_id), []).append(word)
    return by_game


def list_connections_games(
    db: Database, *, skip: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """Paginated list of Connections sessions, newest first."""
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), CONNECTIONS_GAMES_MAX_LIMIT))
    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(ConnectionsGameRecord)) or 0
        rows = session.scalars(
            select(ConnectionsGameRecord)
            .order_by(ConnectionsGameRecord.created_at.desc(), ConnectionsGameRecord.id.asc())
            .offset(skip).limit(limit)
        ).all()
        words = _words_for_many(session, [str(row.id) for row in rows])
        games = [_as_model(row, words.get(str(row.id), [])) for row in rows]
    return {"games": games, "total": total, "skip": skip, "limit": limit}


def get_current_connections_session(db: Database) -> Optional[ConnectionsGame]:
    """The current active-or-pending session for the admin screen, if any."""
    with db.sessions() as session:
        row = _current_session_row(session)
        if row is None:
            return None
        return _as_model(row, _words_for(session, str(row.id)))


def get_connections_game(db: Database, game_id: str) -> ConnectionsGame:
    """Fetch configured puzzle by session ID."""
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Connections game id"
        )
    with db.sessions() as session:
        row = session.get(ConnectionsGameRecord, str(game_id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Connections game not found"
            )
        return _as_model(row, _words_for(session, str(row.id)))


# --- scheduled puzzles --------------------------------------------------------
# Scheduled future manual puzzles bounded by finite horizon.


def schedule_puzzle(
    db: Database,
    target_date: date,
    groups: List[List[str]],
    created_by: str,
    today: Optional[date] = None,
) -> dict:
    """Store (or replace) the manual puzzle for one future period-start date."""
    from ..db_models import ConnectionsScheduledPuzzleRecord
    from .scheduled_contents import validate_schedule_target

    normalized = validate_puzzle_groups(groups)
    validate_schedule_target(target_date, today)
    with db.sessions.begin() as session:
        existing = session.scalar(
            select(ConnectionsScheduledPuzzleRecord).where(
                ConnectionsScheduledPuzzleRecord.target_date == target_date,
            )
        )
        if existing is not None:
            existing.groups = normalized
            existing.created_by = created_by
            session.flush()
            row = existing
        else:
            row = ConnectionsScheduledPuzzleRecord(
                target_date=target_date,
                groups=normalized,
                created_by=created_by,
            )
            try:
                with session.begin_nested():
                    # The insert belongs inside the savepoint: begin_nested()
                    # flushes pending state before it opens one, so adding the
                    # row earlier put the INSERT in the outer transaction and
                    # its unique violation rolled the whole transaction back,
                    # leaving the retry below unable to query.
                    session.add(row)
                    session.flush()
            except IntegrityError:
                # Retry update on concurrent insert conflict for same date.
                existing = session.scalar(
                    select(ConnectionsScheduledPuzzleRecord).where(
                        ConnectionsScheduledPuzzleRecord.target_date == target_date,
                    )
                )
                assert existing is not None, "the winning schedule must exist"
                existing.groups = normalized
                existing.created_by = created_by
                session.flush()
                row = existing
        return {
            "id": str(row.id),
            "target_date": row.target_date.isoformat(),
            "groups": [list(group) for group in row.groups],
            "created_by": row.created_by,
        }


def list_scheduled(db: Database) -> list[dict]:
    """Every scheduled Connections puzzle, earliest date first."""
    from ..db_models import ConnectionsScheduledPuzzleRecord

    with db.sessions() as session:
        rows = session.scalars(
            select(ConnectionsScheduledPuzzleRecord).order_by(
                ConnectionsScheduledPuzzleRecord.target_date.asc()
            )
        ).all()
        return [
            {
                "id": str(row.id),
                "target_date": row.target_date.isoformat(),
                "groups": [list(group) for group in row.groups],
                "created_by": row.created_by,
            }
            for row in rows
        ]


def delete_scheduled(db: Database, target_date: date) -> None:
    """Withdraw the scheduled puzzle for one date; a date with none is a 404."""
    from ..db_models import ConnectionsScheduledPuzzleRecord

    with db.sessions.begin() as session:
        row = session.scalar(
            select(ConnectionsScheduledPuzzleRecord).where(
                ConnectionsScheduledPuzzleRecord.target_date == target_date,
            )
        )
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No scheduled puzzle for {target_date.isoformat()}.",
            )
        session.delete(row)
