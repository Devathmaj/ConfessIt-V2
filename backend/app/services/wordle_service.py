"""Wordle configuration, word validation, and session lifecycle management."""

import hashlib
import hmac
import json
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db_models import WordleGameRecord
from ..limits import WORDLE_ANSWER_LENGTH
from ..models import WordleDraw, WordleGame, WordleGameCreate
from ..postgres import Database, is_valid_id

# Committed word lists live with the backend assets.
_ASSETS_PATH = Path(__file__).resolve().parents[2] / "assets"
_SOLUTIONS_PATH = _ASSETS_PATH / "wordle_solutions.json"
_GUESSES_PATH = _ASSETS_PATH / "wordle_guesses.json"

# Cached word lists loaded lazily on first access.
_solutions: Optional[tuple[str, ...]] = None
_guesses: Optional[tuple[str, ...]] = None


def solution_words() -> tuple[str, ...]:
    """Every approved solution word, loaded once and validated on the way in."""
    global _solutions
    if _solutions is None:
        words = json.loads(_SOLUTIONS_PATH.read_text(encoding="utf-8"))
        malformed = [word for word in words if not _is_well_formed(word)]
        if malformed:
            raise RuntimeError(
                f"{_SOLUTIONS_PATH} holds {len(malformed)} entries that are not exactly "
                f"{WORDLE_ANSWER_LENGTH} lowercase letters, first: {malformed[0]!r}"
            )
        _solutions = tuple(words)
    return _solutions


def _is_well_formed(word: object) -> bool:
    return isinstance(word, str) and len(word) == WORDLE_ANSWER_LENGTH and word.islower() and word.isalpha()


def guess_words() -> tuple[str, ...]:
    """Load and return validated set of allowable guess words."""
    global _guesses
    if _guesses is None:
        words = json.loads(_GUESSES_PATH.read_text(encoding="utf-8"))
        malformed = [word for word in words if not _is_well_formed(word)]
        if malformed:
            raise RuntimeError(
                f"{_GUESSES_PATH} holds {len(malformed)} entries that are not exactly "
                f"{WORDLE_ANSWER_LENGTH} lowercase letters, first: {malformed[0]!r}"
            )
        _guesses = tuple(words)
    return _guesses


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- the draw ------------------------------------------------------------------


def _signature(word: str, game_date: date) -> str:
    message = f"{word}|{game_date.isoformat()}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def draw_answer(game_date: Optional[date] = None) -> WordleDraw:
    """Pick a random solution word for the given date and return it with HMAC token."""
    day = game_date or _today()
    word = random.choice(solution_words())
    return WordleDraw(word=word, token=_signature(word, day))


def _require_approved(word: Optional[str], missing_detail: str) -> str:
    """Validate that the word is five letters and present in the solution dictionary."""
    if word is None or not word.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=missing_detail)
    normalized = word.strip().lower()
    if len(normalized) != WORDLE_ANSWER_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An answer is exactly {WORDLE_ANSWER_LENGTH} letters.",
        )
    if normalized not in solution_words():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That word is not on the approved list.",
        )
    return normalized


def _resolve_answer(payload: WordleGameCreate, game_date: date) -> str:
    """The word to store for this payload, for either mode."""
    if payload.mode == "custom":
        return _require_approved(payload.word, "A custom game needs a word.")

    # Re-verify that the drawn word and HMAC token match the target date.
    answer = _require_approved(payload.word, "Draw a word before saving a random game.")
    signed = _signature(answer, game_date)
    if payload.token is None or not hmac.compare_digest(signed, payload.token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That draw does not match this date. Draw a new word and save again.",
        )
    return answer


# --- persistence ---------------------------------------------------------------


def _as_model(row: WordleGameRecord) -> WordleGame:
    return WordleGame(
        id=str(row.id),
        game_date=row.game_date,
        mode=row.mode,
        answer=row.answer,
        status=row.status or "active",
        period_start_date=row.period_start_date,
        period_end_date=row.period_end_date,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _current_session_row(session):
    """The one active-or-pending Wordle session, or None."""
    return session.scalar(
        select(WordleGameRecord)
        .where(WordleGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def update_wordle_session(
    db: Database,
    *,
    mode: Optional[str] = None,
    word: Optional[str] = None,
    token: Optional[str] = None,
    updated_by: str = "admin",
    now: Optional[datetime] = None,
    discard_future_schedules: bool = False,
) -> WordleGame:
    """End the active session and start a new Wordle session for today."""
    from sqlalchemy import text as _text

    from . import game_periods
    from .game_configs import game_lock_key, get_config
    from .game_scheduler import (
        _consume_earliest_word,
        _discard_all_scheduled_words,
        _discard_scheduled_words_before,
    )

    moment = now or datetime.now(timezone.utc)
    today = moment.date()
    if word is not None:
        # Verify draw signature against today's date for session updates.
        resolve_mode = mode if mode is not None else "custom"
        answer: Optional[str] = _resolve_answer(
            WordleGameCreate(mode=resolve_mode, word=word, token=token),  # type: ignore[arg-type]
            today,
        )
        supplied = True
    elif mode == "random":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Draw a word before saving a random game.",
        )
    else:
        answer = None
        supplied = False

    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("wordle")},
            )
            config = get_config(session, "wordle")
            if mode is not None:
                # Set content mode based on update mode provenance.
                config.content_mode = "manual" if mode == "custom" else "random"
            effective_mode = config.content_mode or "manual"
            if not supplied and effective_mode == "random":
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Draw a word before saving a random game.",
                )
            session_mode = "random" if effective_mode == "random" else "custom"
            cadence = config.reset_cadence
            current = _current_session_row(session)
            if current is not None:
                current.status = "ended"
                current.ended_at = moment
            start, end = game_periods.period_for_start(today, cadence)
            if not supplied and effective_mode == "manual":
                answer = _consume_earliest_word(session, "wordle", start, end)
                if discard_future_schedules:
                    _discard_all_scheduled_words(session, "wordle")
                else:
                    _discard_scheduled_words_before(session, "wordle", end)
            elif supplied and effective_mode == "manual":
                if discard_future_schedules:
                    _discard_all_scheduled_words(session, "wordle")
                else:
                    _discard_scheduled_words_before(session, "wordle", end)
            row = WordleGameRecord(
                game_date=today,
                mode=session_mode if mode is None else mode,
                answer=answer,
                status="active" if answer else "pending",
                period_start_date=start,
                period_end_date=end,
                started_at=moment,
            )
            session.add(row)
            session.flush()
            from . import league_finalization

            league_finalization.snapshot_opened_safely(
                session, game_id="wordle", opportunity_id=str(row.id)
            )
            result = _as_model(row)
        # Finalize previous session awards immediately on update.
        league_finalization.finalize_game_safely(db, "wordle")
        return result
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Wordle session update collided with a concurrent reset. Try again.",
        ) from exc


def create_wordle_game(db: Database, payload: WordleGameCreate) -> WordleGame:
    """Publish the current Wordle session for today."""
    game_date = payload.game_date or _today()
    if game_date != _today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Updates apply to the current session. "
                "Schedule future words with POST /admin/wordle/schedule."
            ),
        )
    return update_wordle_session(
        db, mode=payload.mode, word=payload.word, token=payload.token
    )


# Maximum page size limit for Wordle session history.
WORDLE_GAMES_MAX_LIMIT = 100


def list_wordle_games(
    db: Database, *, skip: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """Return a paginated list of Wordle sessions, newest first."""
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), WORDLE_GAMES_MAX_LIMIT))
    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(WordleGameRecord)) or 0
        rows = session.scalars(
            select(WordleGameRecord)
            .order_by(WordleGameRecord.created_at.desc(), WordleGameRecord.id.asc())
            .offset(skip).limit(limit)
        ).all()
        games = [_as_model(row) for row in rows]
    return {"games": games, "total": total, "skip": skip, "limit": limit}


def get_current_wordle_session(db: Database) -> Optional[WordleGame]:
    """The current active-or-pending session for the admin screen, if any."""
    with db.sessions() as session:
        row = _current_session_row(session)
        return _as_model(row) if row is not None else None


def get_wordle_game(db: Database, game_id: str) -> WordleGame:
    """Fetch a Wordle session record by ID or raise HTTPException."""
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Wordle game id"
        )
    with db.sessions() as session:
        row = session.get(WordleGameRecord, str(game_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Wordle game not found"
        )
    return _as_model(row)
