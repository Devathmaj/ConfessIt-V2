"""Connections gameplay: tile presentation, deterministic shuffle, and arrangement scoring."""

import hashlib
import random
from datetime import datetime, timezone
from itertools import permutations
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    ConnectionsGameRecord,
    ConnectionsSubmissionRecord,
    ConnectionsWordRecord,
)
from ..limits import (
    CONNECTIONS_GROUPS_COUNT,
    CONNECTIONS_WORDS_PER_GROUP,
    CONNECTIONS_WORDS_TOTAL,
)
from ..models import ConnectionsCurrentGame, ConnectionsSubmitResponse
from ..postgres import Database, is_valid_id


def _today() -> "object":
    """Today's date in UTC, the same clock every other service in the app uses."""
    return datetime.now(timezone.utc).date()


def shuffle_words_for_player(words: list[str], user_id: str, game_id: str) -> list[str]:
    """Derive stable deterministic tile ordering for player and session."""
    digest = hashlib.sha256(f"{user_id}|{game_id}".encode("utf-8")).hexdigest()
    rng = random.Random(int(digest, 16))
    ordered = list(words)
    rng.shuffle(ordered)
    return ordered


def score_arrangement(
    submitted: list[list[str]], canonical: list[list[str]]
) -> tuple[int, int]:
    """Calculate matching groups and best-fit word overlaps against canonical groups."""
    canonical_sets = [frozenset(group) for group in canonical]
    submitted_sets = [frozenset(group) for group in submitted]
    remaining = list(canonical_sets)
    correct_groups = 0
    for group in submitted_sets:
        if group in remaining:
            correct_groups += 1
            remaining.remove(group)
    best = 0
    for mapping in permutations(range(CONNECTIONS_GROUPS_COUNT)):
        total = sum(
            len(submitted_sets[index] & canonical_sets[mapping[index]])
            for index in range(CONNECTIONS_GROUPS_COUNT)
        )
        if total > best:
            best = total
    return correct_groups, best


def _current_session(session) -> Optional[ConnectionsGameRecord]:
    """The one active-or-pending session, or None when the game has none."""
    return session.scalar(
        select(ConnectionsGameRecord)
        .where(ConnectionsGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def _words_for(session, game_id: str) -> list[ConnectionsWordRecord]:
    return list(
        session.scalars(
            select(ConnectionsWordRecord).where(
                ConnectionsWordRecord.connections_game_id == game_id
            )
        ).all()
    )


def _canonical_groups(word_rows: list[ConnectionsWordRecord]) -> list[list[str]]:
    """The four canonical groups as normalized spellings, ordered by group."""
    groups: list[list[str]] = [[] for _ in range(CONNECTIONS_GROUPS_COUNT)]
    for row in word_rows:
        groups[row.group_index].append(row.word_normalized)
    return groups


def _empty_game() -> ConnectionsCurrentGame:
    return ConnectionsCurrentGame(
        game_id=None,
        game_date=None,
        words=[],
        submitted=False,
    )


def _pending_game(game: ConnectionsGameRecord, message: str) -> ConnectionsCurrentGame:
    return ConnectionsCurrentGame(
        game_id=str(game.id),
        game_date=game.game_date,
        words=[],
        submitted=False,
        session_status="pending",
        message=message,
    )


def current_connections(db: Database, user_id: str) -> ConnectionsCurrentGame:
    """Fetch playable puzzle state and user submission status without mutating state."""
    from . import game_periods
    from .game_configs import check_playable

    with db.sessions.begin() as session:
        check_playable(session, "connections")

    with db.sessions() as session:
        game = _current_session(session)
        if game is None:
            return _empty_game()
        if game.status == "pending":
            return _pending_game(
                game, "Today's puzzle has not been set yet. Check back soon."
            )
        if game.status != "active":
            return _empty_game()
        start = game.period_start_date or game.game_date
        if start is not None and start > _today():
            return _empty_game()
        if game_periods.is_reset_due(game.period_end_date, _today()):
            return _empty_game()
        word_rows = _words_for(session, str(game.id))
        if len(word_rows) != CONNECTIONS_WORDS_TOTAL:
            # Treat active sessions without valid puzzles as pending.
            return _pending_game(
                game, "Today's puzzle has not been set yet. Check back soon."
            )
        submission = session.scalar(
            select(ConnectionsSubmissionRecord).where(
                ConnectionsSubmissionRecord.connections_game_id == game.id,
                ConnectionsSubmissionRecord.user_id == user_id,
            )
        )
        if submission is not None:
            return ConnectionsCurrentGame(
                game_id=str(game.id),
                game_date=game.game_date,
                words=[],
                submitted=True,
                session_status="submitted",
            )
        displays = [row.word_display for row in word_rows]
        return ConnectionsCurrentGame(
            game_id=str(game.id),
            game_date=game.game_date,
            words=shuffle_words_for_player(displays, user_id, str(game.id)),
            submitted=False,
            session_status="active",
        )


def _get_game_for_update(session, game_id: str) -> tuple[ConnectionsGameRecord, list[ConnectionsWordRecord]]:
    """The named puzzle, arrived, current, configured -- or why it cannot be played."""
    from . import game_periods
    from .game_configs import check_playable, lock_game_for_play

    # Hold lifecycle lock across liveness check and write.
    lock_game_for_play(session, "connections")
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Connections game id"
        )
    check_playable(session, "connections")
    game = session.get(ConnectionsGameRecord, str(game_id))
    if game is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Connections game not found"
        )
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That puzzle has not started yet.",
        )
    if game.status == "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Today's puzzle has not been set yet. Check back soon.",
        )
    if game.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That session has ended. Open the current puzzle to keep playing.",
        )
    if game_periods.is_reset_due(game.period_end_date, _today()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That session has ended. Open the current puzzle to keep playing.",
        )
    current = _current_session(session)
    if current is None or str(current.id) != str(game.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That session has ended. Open the current puzzle to keep playing.",
        )
    word_rows = _words_for(session, str(game.id))
    if len(word_rows) != CONNECTIONS_WORDS_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Today's puzzle has not been set yet. Check back soon.",
        )
    return game, word_rows


def _normalize_submission(
    raw_groups: object, word_rows: list[ConnectionsWordRecord]
) -> list[list[str]]:
    """Validate and normalize 4 groups of 4 distinct puzzle words."""
    if not isinstance(raw_groups, list) or len(raw_groups) != CONNECTIONS_GROUPS_COUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Submit four groups of four words.",
        )
    known = {row.word_normalized: row.word_display for row in word_rows}
    normalized_groups: list[list[str]] = []
    seen: set[str] = set()
    for group in raw_groups:
        if not isinstance(group, list) or len(group) != CONNECTIONS_WORDS_PER_GROUP:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Submit four groups of four words.",
            )
        normalized_group: list[str] = []
        for raw in group:
            if not isinstance(raw, str) or not raw.strip():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Each submitted word must be one of the puzzle words.",
                )
            normalized = raw.strip().lower()
            if normalized not in known:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{raw.strip()!r} is not one of this puzzle's words.",
                )
            if normalized in seen:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Each puzzle word may appear only once.",
                )
            seen.add(normalized)
            normalized_group.append(normalized)
        normalized_groups.append(normalized_group)
    if len(seen) != CONNECTIONS_WORDS_TOTAL:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Assign all 16 words before submitting.",
        )
    return normalized_groups


def submit_arrangement(
    db: Database, user_id: str, game_id: str, raw_groups: list[list[str]]
) -> ConnectionsSubmitResponse:
    """Persist submission and compute scores against canonical groups atomically."""
    with db.sessions.begin() as session:
        game, word_rows = _get_game_for_update(session, game_id)
        normalized = _normalize_submission(raw_groups, word_rows)

        existing = session.scalar(
            select(ConnectionsSubmissionRecord).where(
                ConnectionsSubmissionRecord.connections_game_id == game.id,
                ConnectionsSubmissionRecord.user_id == user_id,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This puzzle has already been submitted.",
            )

        correct_groups, correct_words = score_arrangement(
            normalized, _canonical_groups(word_rows)
        )
        displays = {row.word_normalized: row.word_display for row in word_rows}
        stored = [
            [displays[word] for word in group] for group in normalized
        ]
        now = datetime.now(timezone.utc)
        try:
            row = ConnectionsSubmissionRecord(
                connections_game_id=str(game.id),
                user_id=user_id,
                groups=stored,
                correct_groups=correct_groups,
                correct_words=correct_words,
                submitted_at=now,
            )
            session.add(row)
            session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This puzzle has already been submitted.",
            ) from exc
        return ConnectionsSubmitResponse(
            game_id=str(game.id),
            submitted=True,
            submitted_at=now,
        )
