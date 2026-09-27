"""Configuring the day's Anagram puzzle: which word the day runs on, and where it came from.

The table this writes is the *word* and nothing else -- a day, a word, how the word was
chosen, and who chose it. There is no scramble, no attempt, no guess and no result here,
because none of those exist yet and the separation is worth stating rather than assuming:
every write below is an administrator publishing something, and none of it is ever triggered
by a participant.

**The stored answer is canonical and uppercase, and it is never scrambled here.** Each
player will later receive their own permutation of this same word, so a scrambled copy in
``anagram_games`` would be a second, conflicting spelling of one puzzle -- and the whole
point of the split is that the shared word lives in exactly one place. Normalisation to
uppercase happens once, on the way in, so ``elephant``, ``ElePhAnT`` and `` ELEPHANT `` are
one answer rather than three.

**The word list is data, not a dependency.** ``backend/assets/anagram_words.txt`` is
committed and read from disk, so the running application needs no package and no network.
It is application-owned static data: this module reads it and nothing else -- never the
Wordle vocabulary, and never a word API. The file is loaded once and reused for the
process's life, and it is cleaned rather than trusted on the way in: entries are stripped
and uppercased, empty lines are dropped, and entries that are not letters at all (the list
contains contractions such as ``I'd``) are excluded from the draw because an anagram answer
is a bag of letters. A missing or empty file is a loud error on first use rather than a
silent fall back to some other dictionary.

**The draw is previewed, not saved sight unseen.** ``GET /admin/anagram/preview`` picks a
word and signs it together with the date; ``POST /admin/anagram`` re-derives the signature
before storing. That is what makes the preview honest -- the admin sees the answer *before*
saving, can draw again as often as they like, and saving cannot produce a different word --
while keeping the choice on the server: the client echoes a word back only alongside a
signature it cannot forge, so no client can supply an answer of its own making under
``mode="random"``. The minimum applies only to the draw -- a custom answer of any length is
legal, which is why the filter lives here rather than on the column.
"""

import hashlib
import hmac
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db_models import AnagramGameRecord
from ..limits import ANAGRAM_ANSWER_MAX, ANAGRAM_RANDOM_MIN_LENGTH
from ..models import AnagramDraw, AnagramGame, AnagramGameCreate
from ..postgres import Database, is_valid_id

# Three levels up -- out of ``app/services`` and out of ``app`` itself -- because this is
# data at the top of the backend rather than inside the package: ``backend/assets/`` sits
# beside ``backend/app``, so one fewer ``parent`` would look for the file inside the
# package and find nothing.
_WORDS_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "anagram_words.txt"

# Loaded once and reused for the process's life. The list is read on the first random draw,
# not at import: a module that fails to import would take the application down before it
# could log why, where a failure here is a request that answers 500 with the reason attached.
_anagram_words: Optional[tuple[str, ...]] = None


def anagram_words() -> tuple[str, ...]:
    """Every word the draw may choose from, loaded once and cleaned on the way in.

    Cleaning rather than validating: unlike the Wordle list -- where one malformed entry
    means the committed artifact drifted and the game cannot run -- this file is a general
    word list that legitimately holds short words, duplicates and contractions. Those are
    not errors, they are entries the anagram draw simply cannot use, so they are dropped
    here instead of failing the load.
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

    The filter is a list comprehension over the loaded entries rather than a change to the
    file itself: the committed data stays exactly as it is, and the minimum is a rule about
    what the *draw* may return.

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
    differently. There is deliberately *no* minimum length here: ``cat`` is a legal custom
    answer, and the five-letter floor belongs to the draw alone.

    The answer to a wrong word is a 400 rather than a 422, matching ``wordle_service``: the
    request was well-formed, it asked for something this server will not do.
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
    mode: str,
    word: Optional[str],
    token: Optional[str],
    updated_by: str,
    now: Optional[datetime] = None,
) -> AnagramGame:
    """End the current session and open a brand-new one starting now ("Update").

    Same contract as the Wordle Update: the old row is stamped ended and kept
    as history, the new session starts from scratch for today's UTC date under
    the game's own reset cadence, and the game's content mode follows the
    update. Atomic under the game's advisory lock.
    """
    from sqlalchemy import text as _text

    from . import game_periods
    from .game_configs import game_lock_key, get_config

    moment = now or datetime.now(timezone.utc)
    today = moment.date()
    answer = _resolve_answer(
        AnagramGameCreate(mode=mode, word=word, token=token), today  # type: ignore[arg-type]
    )

    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("anagram")},
            )
            config = get_config(session, "anagram")
            # See ``wordle_service.update_wordle_session``: provenance is not
            # behaviour, so the content mode follows the update's meaning.
            config.content_mode = "manual" if mode == "custom" else "random"
            cadence = config.reset_cadence
            current = _current_session_row(session)
            if current is not None:
                current.status = "ended"
                current.ended_at = moment
            start, end = game_periods.period_for_start(today, cadence)
            row = AnagramGameRecord(
                game_date=today,
                mode=mode,
                answer=answer,
                status="active",
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


def list_anagram_games(db: Database) -> List[AnagramGame]:
    """Every session, newest first -- active, pending and ended history alike."""
    with db.sessions() as session:
        rows = session.scalars(
            select(AnagramGameRecord).order_by(AnagramGameRecord.created_at.desc())
        ).all()
        return [_as_model(row) for row in rows]


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
