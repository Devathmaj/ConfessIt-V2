"""Configure the daily Anagram answer and its source.

Answers are stored uppercase; player scrambles and attempts live elsewhere. Draws use the
committed word list and are signed for preview before an administrator saves them.
"""

import hashlib
import hmac
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db_models import AnagramGameRecord
from ..limits import ANAGRAM_ANSWER_MAX, ANAGRAM_RANDOM_MIN_LENGTH
from ..models import AnagramDraw, AnagramGame, AnagramGameCreate
from ..postgres import Database, is_valid_id

# Three levels up -- out of ``app`` itself -- because ``backend/assets/`` sits beside
# ``backend/app``: one fewer ``parent`` would look for the file inside the package.
_WORDS_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "anagram_words.txt"

# Loaded once and reused for the process's life, on the first random draw rather than at
# import: an import failure takes the application down before it could log why, where a
# failure here is a request that answers 500 with the reason attached.
_anagram_words: Optional[tuple[str, ...]] = None


def anagram_words() -> tuple[str, ...]:
    """Every word the draw may choose from, loaded once and cleaned on the way in.

    Cleaning rather than validating: unlike the Wordle list, this general word list
    legitimately holds short words, duplicates and contractions. Those are entries the
    anagram draw cannot use, dropped here instead of failing the load.
    """
    global _anagram_words
    if _anagram_words is None:
        try:
            lines = _WORDS_PATH.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            # Loud rather than a fall back: silently drawing from another dictionary would
            # publish a word this deployment never committed to.
            raise RuntimeError(
                f"Cannot read the Anagram word list at {_WORDS_PATH}: {exc}"
            ) from exc
        cleaned: dict[str, None] = {}
        for line in lines:
            word = line.strip().upper()
            if not word or not word.isalpha():
                continue
            # A dict rather than a set, so duplicates collapse while file order -- and
            # therefore a predictable draw -- is preserved.
            cleaned[word] = None
        if not cleaned:
            raise RuntimeError(f"{_WORDS_PATH} holds no usable words")
        _anagram_words = tuple(cleaned)
    return _anagram_words


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- the draw ------------------------------------------------------------------


def _signature(word: str, game_date: date) -> str:
    message = f"{word}|{game_date.isoformat()}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def draw_answer(game_date: Optional[date] = None) -> AnagramDraw:
    """Pick a word of at least ``ANAGRAM_RANDOM_MIN_LENGTH`` letters and sign the pair.

    The filter is over the loaded entries, not a change to the file: the minimum is a
    rule about what the *draw* may return, and the committed data stays as it is.

    The date is part of the signature rather than a comment on it: a draw made for Monday
    cannot be submitted as Tuesday's puzzle, so an admin who draws, changes the date field
    and saves is refused and asked to draw again -- rather than silently publishing a word
    for a day it was never chosen for.
    """
    day = game_date or _today()
    eligible = [
        word for word in anagram_words() if len(word) >= ANAGRAM_RANDOM_MIN_LENGTH
    ]
    if not eligible:
        raise RuntimeError(
            f"{_WORDS_PATH} has no word of at least {ANAGRAM_RANDOM_MIN_LENGTH} letters"
        )
    word = random.choice(eligible)
    return AnagramDraw(word=word, token=_signature(word, day))


# --- the answer ----------------------------------------------------------------


def _require_answer(word: Optional[str], missing_detail: str) -> str:
    """Normalize a supplied word and refuse anything that cannot be an answer.

    One place for the three rules -- present, letters only, short enough -- so a custom
    answer and the stored form of any future input cannot drift into answering them
    differently. Deliberately *no* minimum length here: ``cat`` is a legal custom answer.

    A wrong word is a 400 rather than a 422, matching ``wordle_service``: the request was
    well-formed, it asked for something this server will not do.
    """
    if word is None or not word.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=missing_detail)
    normalized = word.strip().upper()
    if not normalized.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An answer is letters only -- no spaces, digits or punctuation.",
        )
    if len(normalized) > ANAGRAM_ANSWER_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"An answer is at most {ANAGRAM_ANSWER_MAX} characters.",
        )
    return normalized


def _resolve_answer(payload: AnagramGameCreate, game_date: date) -> str:
    """The word to store for this payload, for either mode."""
    if payload.mode == "custom":
        return _require_answer(payload.word, "A custom game needs a word.")

    # Random: the word must be one this server drew for this exact day. The membership
    # check runs again even though a signature could only have been minted over a word from
    # the pool -- the file can be regenerated between a draw and a save, and the row must
    # never hold a word the current list would refuse.
    answer = _require_answer(payload.word, "Draw a word before saving a random game.")
    signed = _signature(answer, game_date)
    if payload.token is None or not hmac.compare_digest(signed, payload.token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That draw does not match this date. Draw a new word and save again.",
        )
    if answer not in set(anagram_words()) or len(answer) < ANAGRAM_RANDOM_MIN_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That word is no longer in the draw pool. Draw a new word and save again.",
        )
    return answer


# --- persistence ---------------------------------------------------------------


def _as_model(row: AnagramGameRecord) -> AnagramGame:
    return AnagramGame(
        id=str(row.id),
        game_date=row.game_date,
        mode=row.mode,
        answer=row.answer,
        status=row.status or "active",
        period_start_date=row.period_start_date,
        period_end_date=row.period_end_date,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _current_session_row(session):
    """The one active-or-pending Anagram session, or None."""
    return session.scalar(
        select(AnagramGameRecord)
        .where(AnagramGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def update_anagram_session(
    db: Database,
    *,
    mode: Optional[str] = None,
    word: Optional[str] = None,
    token: Optional[str] = None,
    updated_by: str = "admin",
    now: Optional[datetime] = None,
    discard_future_schedules: bool = False,
) -> AnagramGame:
    """End the current session and open a brand-new one starting now ("Update").

    Same contract as the Wordle Update: the old row is stamped ended and kept
    as history, the new session starts from scratch for today's UTC date under
    the game's own reset cadence, and -- when ``mode`` is given -- the game's
    content mode follows the update. Atomic under the game's advisory lock.

    Without a supplied word in manual mode, the earliest scheduled entry
    inside the new window is consumed, or the session opens pending. Other
    pending entries dated before the window end are discarded (all of them,
    including future ones, with ``discard_future_schedules`` for cadence-change
    re-anchoring). Random mode never touches schedules; a random update
    without a draw is refused.
    """
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
        resolve_mode = mode if mode is not None else "custom"
        answer: Optional[str] = _resolve_answer(
            AnagramGameCreate(mode=resolve_mode, word=word, token=token),  # type: ignore[arg-type]
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
                {"key": game_lock_key("anagram")},
            )
            config = get_config(session, "anagram")
            if mode is not None:
                # See ``wordle_service.update_wordle_session``: provenance is
                # not behaviour, so the content mode follows the update's
                # meaning.
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
            if effective_mode == "manual":
                if not supplied:
                    answer = _consume_earliest_word(session, "anagram", start, end)
                if discard_future_schedules:
                    _discard_all_scheduled_words(session, "anagram")
                else:
                    _discard_scheduled_words_before(session, "anagram", end)
            row = AnagramGameRecord(
                game_date=today,
                mode=session_mode if mode is None else mode,
                answer=answer,
                status="active" if answer else "pending",
                period_start_date=start,
                period_end_date=end,
                started_at=moment,
                created_by=updated_by,
            )
            session.add(row)
            session.flush()
            from . import league_finalization

            league_finalization.snapshot_opened_safely(
                session, game_id="anagram", opportunity_id=str(row.id)
            )
            result = _as_model(row)
        # The Update deliberately closes the previous session, so its awards
        # finalize now rather than waiting for the next reset run.
        league_finalization.finalize_game_safely(db, "anagram")
        return result
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An Anagram session update collided with a concurrent reset. Try again.",
        ) from exc


def create_anagram_game(
    db: Database, payload: AnagramGameCreate, created_by: str
) -> AnagramGame:
    """Publish the current Anagram session ("Update").

    ``POST /admin/anagram`` is the Update action: it ends the currently
    active/pending session and opens a new one for today. An explicit
    ``game_date`` other than today is refused -- future words belong on the
    schedule endpoint, and history is read-only.
    """
    game_date = payload.game_date or _today()
    if game_date != _today():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Updates apply to the current session. "
                "Schedule future words with POST /admin/anagram/schedule."
            ),
        )
    return update_anagram_session(
        db, mode=payload.mode, word=payload.word, token=payload.token,
        updated_by=created_by,
    )


# Hard ceiling for one page of the session-history list. The endpoint clamps anything
# larger, so no client can ask for every session at once and make the backend
# materialise a whole season in one response.
ANAGRAM_GAMES_MAX_LIMIT = 100


def list_anagram_games(
    db: Database, *, skip: int = 0, limit: int = 50
) -> Dict[str, Any]:
    """One page of every session, newest first -- active, pending and ended history alike.

    The page is a SQL LIMIT/OFFSET and ``total`` a COUNT, so the screen loads history
    downwards without ever holding the sessions it is not showing. ``id`` seconds the
    order so two sessions stamped in the same instant still page deterministically.
    """
    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or 50), ANAGRAM_GAMES_MAX_LIMIT))
    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(AnagramGameRecord)) or 0
        rows = session.scalars(
            select(AnagramGameRecord)
            .order_by(AnagramGameRecord.created_at.desc(), AnagramGameRecord.id.asc())
            .offset(skip).limit(limit)
        ).all()
        games = [_as_model(row) for row in rows]
    return {"games": games, "total": total, "skip": skip, "limit": limit}


def get_current_anagram_session(db: Database) -> Optional[AnagramGame]:
    """The current active-or-pending session for the admin screen, if any."""
    with db.sessions() as session:
        row = _current_session_row(session)
        return _as_model(row) if row is not None else None


def get_anagram_game(db: Database, game_id: str) -> AnagramGame:
    """One configured puzzle by id.

    A malformed id is a 400 and a well-formed one that matches nothing is a 404, so a typo
    in a URL and a puzzle that has been superseded are told apart rather than both reported
    as "not found".
    """
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Anagram game id"
        )
    with db.sessions() as session:
        row = session.get(AnagramGameRecord, str(game_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Anagram game not found"
        )
    return _as_model(row)
