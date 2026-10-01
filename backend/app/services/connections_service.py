"""Configuring the day's Connections puzzle: sixteen words in four groups.

The tables this writes are the *configuration* of a puzzle and nothing else --
a session plus its sixteen words and their four canonical answer groups. There
is no arrangement, no guess and no score here, because none of those exist yet:
every write below is an administrator publishing something, and none of it is
ever triggered by a participant.

**There is no random-content mode.** Every session needs an administrator to
supply its own sixteen words. A session with no valid puzzle stays pending and
unplayable -- the scheduler never draws, reuses or invents words, and the
player surface never serves a board for one.

**An Update never mutates history.** ``POST /admin/connections`` ends the
current session and opens a brand-new one starting now, attaching the fresh
puzzle to the new session. The old session keeps its word rows and its
submissions, so earlier scores stay attached to the answer key they were
computed against.
"""

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
    """The display and comparison spellings of one submitted word.

    Leading and trailing whitespace is trimmed; the display spelling keeps its
    capitalization, and the normalized spelling lowercases it. An empty,
    non-string or over-long word is refused with a 400 naming the rule.
    """
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
    """Normalize and validate a full 4x4 puzzle, returning display spellings.

    Exactly four groups of exactly four non-empty words each, with no duplicate
    words -- including duplicates that differ only by capitalization or
    surrounding whitespace. Every refusal is a 400 with a sentence the admin
    screen can show without losing the entered puzzle; shape violations are
    deliberately not 422s because the request was well-formed, it asked for
    something this server will not store.
    """
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
    """End the current session and open a brand-new one starting now ("Update").

    Never mutates the old row: it is stamped ended and kept as history with its
    word rows and submissions attached, while the new session starts from
    scratch for today's UTC date under the game's own reset cadence. The game's
    content mode stays ``manual`` -- Connections has no random mode. Atomic
    under the game's advisory lock, so an Update racing the 00:01 reset cannot
    leave two active sessions.

    Without supplied groups, the earliest scheduled puzzle inside the new
    window is consumed, or the session opens pending when the window holds
    none. Other pending scheduled puzzles dated before the window end are
    discarded (all of them, including future ones, with
    ``discard_future_schedules`` for cadence-change re-anchoring).
    """
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
        # The Update deliberately closes the previous session, so its awards
        # finalize now rather than waiting for the next reset run.
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
    """Publish the current Connections session ("Update").

    ``POST /admin/connections`` is the Update action: it ends the currently
    active/pending session and opens a new one for today with the supplied
    puzzle -- or, when no puzzle is supplied, with the earliest scheduled
    puzzle inside the new window (pending when the window holds none). Future
    puzzles belong on the schedule endpoint below, and history is read-only.
    """
    return update_connections_session(db, groups=groups, updated_by=created_by)


# Hard ceiling for one page of the session-history list. The endpoint clamps anything
# larger, so no client can ask for every session at once and make the backend
# materialise a whole season in one response.
CONNECTIONS_GAMES_MAX_LIMIT = 100


def _words_for_many(session, game_ids: List[str]) -> Dict[str, List]:
    """Every word row for a page of sessions, grouped by session, in one read.

    The single-row ``_words_for`` stays for the single-row readers; fanning it out per
    row of a page would turn one bounded read into a query per session.
    """
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
    """One page of every session, newest first -- active, pending and ended history alike.

    The page is a SQL LIMIT/OFFSET and ``total`` a COUNT, so the screen loads history
    downwards without ever holding the sessions it is not showing. ``id`` seconds the
    order so two sessions stamped in the same instant still page deterministically.
    """
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
    """One configured puzzle by id.

    A malformed id is a 400 and a well-formed one that matches nothing is a
    404, so a typo in a URL and a puzzle that has been superseded are told
    apart rather than both reported as "not found".
    """
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
#
# Future manual puzzles, bounded by the same finite horizon as the scheduled
# words. ``schedule_puzzle`` stores one 4x4 puzzle for one future period-start
# date; the reset loop attaches it when that period opens instead of leaving
# the session pending. A scheduled puzzle never activates early.


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
            session.add(row)
            try:
                with session.begin_nested():
                    session.flush()
            except IntegrityError:
                # Lost an insert race with a concurrent schedule for the same
                # date: the unique index refused the duplicate. Re-read the
                # winner (committed before the violation surfaced) and apply
                # the replace the caller asked for, so concurrent identical
                # writes converge instead of answering 500.
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
