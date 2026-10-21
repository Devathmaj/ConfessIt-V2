"""Anagram gameplay: puzzle loading, scrambles, timer tracking, and guess evaluation."""

import random
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db_models import AnagramGameRecord, AnagramUserGameRecord
from ..limits import ANAGRAM_MAX_ATTEMPTS, ANAGRAM_TIMER_STALE_AFTER_SECONDS
from ..models import (
    AnagramCurrentGame,
    AnagramGuessRequest,
    AnagramGuessResponse,
    AnagramParticipation,
)
from ..postgres import Database, is_valid_id


def _today() -> "object":
    """Today's date in UTC, the same clock every other service in the app uses."""
    return datetime.now(timezone.utc).date()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --- the scramble --------------------------------------------------------------


def scramble_answer(answer: str) -> str:
    """Deal a private permutation of ``answer`` that is never the answer itself."""
    letters = list(answer)
    if len(letters) < 2 or len(set(letters)) < 2:
        return answer
    for _ in range(100):
        candidate = letters[:]
        random.shuffle(candidate)
        if candidate != letters:
            return "".join(candidate)
    return answer


# --- the clock -----------------------------------------------------------------


def _segment_is_stale(timer_started_at: datetime, now: datetime) -> bool:
    """Whether an open segment outlived any live game."""
    return (now - timer_started_at).total_seconds() > ANAGRAM_TIMER_STALE_AFTER_SECONDS


def _close_segment(row: AnagramUserGameRecord, now: datetime) -> int:
    """Calculate and return elapsed seconds of row's open segment to bank."""
    if row.timer_started_at is None:
        return 0
    elapsed = (now - row.timer_started_at).total_seconds()
    if elapsed <= 0 or _segment_is_stale(row.timer_started_at, now):
        return 0
    return int(elapsed)


def _snapshot(
    game: AnagramGameRecord, row: AnagramUserGameRecord, now: datetime
) -> AnagramParticipation:
    """Build client-facing participation snapshot, revealing answer upon completion."""
    running = (
        row.status == "in_progress"
        and row.timer_started_at is not None
        and not _segment_is_stale(row.timer_started_at, now)
    )
    return AnagramParticipation(
        anagram_game_id=str(game.id),
        game_date=game.game_date,
        status=row.status,
        success=row.success,
        attempt_count=row.attempt_count,
        guesses=list(row.guesses),
        scrambled_word=row.scrambled_word,
        active_time_seconds=row.active_time_seconds,
        timer_started_at=row.timer_started_at,
        timer_running=running,
        started_at=row.started_at,
        last_guess_at=row.last_guess_at,
        completed_at=row.completed_at,
        solved_at=row.solved_at,
        answer=game.answer if row.status != "in_progress" else None,
    )


# --- reading -------------------------------------------------------------------


def _current_session(session) -> Optional[AnagramGameRecord]:
    """The one active-or-pending session, or None when the game has none."""
    return session.scalar(
        select(AnagramGameRecord)
        .where(AnagramGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def _latest_playable_game(session) -> Optional[AnagramGameRecord]:
    """The current session when it is active and its period has begun."""
    game = _current_session(session)
    if game is None or game.status != "active":
        return None
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        return None
    return game


def _empty_game() -> AnagramCurrentGame:
    return AnagramCurrentGame(
        game_id=None,
        game_date=None,
        participation=None,
        max_attempts=ANAGRAM_MAX_ATTEMPTS,
    )


def current_anagram(db: Database, user_id: str) -> AnagramCurrentGame:
    """Get playable puzzle and user participation without mutating state."""
    from . import game_periods
    from .game_configs import check_playable

    with db.sessions.begin() as session:
        check_playable(session, "anagram")

    with db.sessions() as session:
        game = _current_session(session)
        if game is None:
            return _empty_game()
        if game.status == "pending":
            return AnagramCurrentGame(
                game_id=str(game.id),
                game_date=game.game_date,
                participation=None,
                session_status="pending",
                message="Today's word has not been set yet. Check back soon.",
                max_attempts=ANAGRAM_MAX_ATTEMPTS,
            )
        start = game.period_start_date or game.game_date
        if game.status != "active" or (start is not None and start > _today()):
            return _empty_game()
        if game_periods.is_reset_due(game.period_end_date, _today()):
            return _empty_game()
        row = session.scalar(
            select(AnagramUserGameRecord).where(
                AnagramUserGameRecord.anagram_game_id == game.id,
                AnagramUserGameRecord.user_id == user_id,
            )
        )
        return AnagramCurrentGame(
            game_id=str(game.id),
            game_date=game.game_date,
            participation=_snapshot(game, row, _now()) if row is not None else None,
            session_status="active",
            max_attempts=ANAGRAM_MAX_ATTEMPTS,
        )


# --- starting, pausing, checkpointing ------------------------------------------


def _get_game_for_update(session, game_id: str):
    """The named puzzle, arrived, current and existing, or why it cannot be played."""
    from . import game_periods
    from .game_configs import check_playable, lock_game_for_play

    # Hold lifecycle lock across liveness check and write to serialize rollovers.
    lock_game_for_play(session, "anagram")
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Anagram game id"
        )
    check_playable(session, "anagram")
    game = session.get(AnagramGameRecord, str(game_id))
    if game is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anagram game not found"
        )
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That puzzle has not started yet.",
        )
    if game.status == "pending" or not game.answer:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Today's word has not been set yet. Check back soon.",
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
    return game


def _get_participation_for_update(session, game_id: str, user_id: str):
    """Fetch locked participation row for named puzzle, or raise 404."""
    row = session.scalar(
        select(AnagramUserGameRecord)
        .where(
            AnagramUserGameRecord.anagram_game_id == game_id,
            AnagramUserGameRecord.user_id == user_id,
        )
        .with_for_update()
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No participation yet. Reveal the word to start playing.",
        )
    return row


def start_participation(
    db: Database, user_id: str, game_id: str
) -> AnagramParticipation:
    """Reveal the puzzle: deal the scramble, open the clock, or resume it."""
    with db.sessions.begin() as session:
        game = _get_game_for_update(session, game_id)
        now = _now()

        session.execute(
            pg_insert(AnagramUserGameRecord)
            .values(
                anagram_game_id=str(game.id),
                user_id=user_id,
                scrambled_word=scramble_answer(game.answer),
                attempt_count=0,
                status="in_progress",
                success=False,
                guesses=[],
                active_time_seconds=0,
                timer_started_at=now,
                started_at=now,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    AnagramUserGameRecord.anagram_game_id,
                    AnagramUserGameRecord.user_id,
                ]
            )
        )
        row = session.scalar(
            select(AnagramUserGameRecord)
            .where(
                AnagramUserGameRecord.anagram_game_id == game.id,
                AnagramUserGameRecord.user_id == user_id,
            )
            .with_for_update()
        )
        assert row is not None, "the reveal must leave a participation"
        if row.status != "in_progress":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This puzzle is already finished.",
            )
        # Resume timer: bank paused segment or start fresh segment if stale.
        if row.timer_started_at is None or _segment_is_stale(row.timer_started_at, now):
            row.timer_started_at = now
        return _snapshot(game, row, now)


def pause_timer(db: Database, user_id: str, game_id: str) -> AnagramParticipation:
    """Bank the open segment and stop the clock idempotently."""
    with db.sessions.begin() as session:
        game = _get_game_for_update(session, game_id)
        row = _get_participation_for_update(session, str(game.id), user_id)
        now = _now()
        row.active_time_seconds += _close_segment(row, now)
        row.timer_started_at = None
        return _snapshot(game, row, now)


def heartbeat(db: Database, user_id: str, game_id: str) -> AnagramParticipation:
    """Checkpoint a running clock without stopping it."""
    with db.sessions.begin() as session:
        game = _get_game_for_update(session, game_id)
        row = _get_participation_for_update(session, str(game.id), user_id)
        now = _now()
        if row.status == "in_progress" and row.timer_started_at is not None:
            row.active_time_seconds += _close_segment(row, now)
            row.timer_started_at = now
        return _snapshot(game, row, now)


# --- guessing ------------------------------------------------------------------


def _normalise_guess(raw: str, answer: str) -> str:
    """Normalize and validate guess arrangement against puzzle answer."""
    guess = (raw or "").strip().upper()
    if not guess:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Arrange the tiles, then submit.",
        )
    if not guess.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="A guess is letters only."
        )
    if len(guess) != len(answer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A guess rearranges all {len(answer)} tiles.",
        )
    if sorted(guess) != sorted(answer):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A guess uses exactly the letters you were dealt.",
        )
    return guess


def submit_guess(
    db: Database, user_id: str, payload: AnagramGuessRequest
) -> AnagramGuessResponse:
    """Score one arrangement against the named puzzle for this player."""
    with db.sessions.begin() as session:
        game = _get_game_for_update(session, payload.game_id)
        row = _get_participation_for_update(session, str(game.id), user_id)
        now = _now()

        if row.status != "in_progress":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This puzzle is already finished.",
            )
        if row.attempt_count >= ANAGRAM_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No guesses left for this puzzle.",
            )

        guess = _normalise_guess(payload.guess, game.answer)
        correct = guess == game.answer

        # Checkpoint active play time before updating attempt count.
        row.active_time_seconds += _close_segment(row, now)
        if row.timer_started_at is not None:
            row.timer_started_at = now

        row.guesses = [*row.guesses, guess]
        row.attempt_count += 1
        row.last_guess_at = now
        if correct:
            row.status = "won"
            row.success = True
            row.completed_at = now
            row.solved_at = now
            row.timer_started_at = None
        elif row.attempt_count >= ANAGRAM_MAX_ATTEMPTS:
            row.status = "lost"
            row.success = False
            row.completed_at = now
            row.timer_started_at = None

        snapshot = _snapshot(game, row, now)
        return AnagramGuessResponse(**snapshot.model_dump(), guess=guess, correct=correct)
