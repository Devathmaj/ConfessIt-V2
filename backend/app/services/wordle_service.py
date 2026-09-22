"""Configuring the day's Wordle puzzle: which word the day runs on, and where it came from.

The table this writes is the *configuration* of a puzzle and nothing else -- a day, a word,
and whether the word was typed or drawn. There is no attempt, no guess and no result here,
because there is no gameplay yet, and the separation is worth stating rather than assuming:
every write below is an administrator publishing something, and none of it is ever triggered
by a participant.

**The word list is data, not a dependency.** ``backend/app/data/wordle_solutions.json`` is
committed and read from disk, so the running application needs no npm package and no network.
The list is regenerated only when the vocabulary itself changes, by
``scripts/generate-wordle-words.mjs``; nothing at runtime reaches for anything but the file.
The file is validated on first read rather than trusted: a committed artifact is exactly the
kind of thing that gets edited by hand one day, and a short word silently joining the list
would be accepted by this service and then rejected by the column it is written to.

**A drawn word is bound to the day it was drawn for.** ``GET /admin/wordle/preview`` picks a
word and signs it together with the date; ``POST /admin/wordle`` re-derives the signature
before storing. That is what makes the preview honest -- the admin sees the answer *before*
saving, and saving cannot produce a different one -- while keeping the choice on the server:
the client echoes a word back only alongside a signature it cannot forge, so no client can
supply an answer of its own making under ``mode="random"``.
"""

import hashlib
import hmac
import json
import random
from datetime import date, datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..db_models import WordleGameRecord
from ..limits import WORDLE_ANSWER_LENGTH
from ..models import WordleDraw, WordleGame, WordleGameCreate
from ..postgres import Database, is_valid_id

# Next to this package rather than inside it: ``app.data`` would be an importable package only
# with an ``__init__.py`` it does not need, and the file is data rather than code.
_SOLUTIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "wordle_solutions.json"

# Loaded once and reused for the process's life. The list is read on the first draw or the
# first custom word, not at import: a module that fails to import would take the application
# down before it could log why, where a failure here is a request that answers 500 with the
# reason attached.
_solutions: Optional[tuple[str, ...]] = None


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


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- the draw ------------------------------------------------------------------


def _signature(word: str, game_date: date) -> str:
    message = f"{word}|{game_date.isoformat()}".encode("utf-8")
    return hmac.new(settings.SECRET_KEY.encode("utf-8"), message, hashlib.sha256).hexdigest()


def draw_answer(game_date: Optional[date] = None) -> WordleDraw:
    """Pick a word from the approved list for ``game_date`` and sign the pair.

    The date is part of the signature rather than a comment on it: a draw made for Monday
    cannot be submitted as Tuesday's puzzle, so an admin who draws, changes the date field
    and saves is refused and asked to draw again -- rather than silently publishing a word
    for a day it was never chosen for.
    """
    day = game_date or _today()
    word = random.choice(solution_words())
    return WordleDraw(word=word, token=_signature(word, day))


def _require_approved(word: Optional[str], missing_detail: str) -> str:
    """Normalize a supplied word and refuse anything that is not on the approved list.

    One place for the three rules -- present, five letters, in the list -- so custom and
    random modes cannot drift into answering them differently. The answer to a wrong word is
    a 400 rather than a 422: the request was well-formed, it asked for something this
    server will not do.
    """
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

    # Random: the word must be one this server signed for this exact day. The membership
    # check runs again even though a signature could only have been minted over a word from
    # the list -- the file can be regenerated between a draw and a save, and the row must
    # never hold a word the current list would refuse.
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
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create_wordle_game(db: Database, payload: WordleGameCreate) -> WordleGame:
    """Store one day's puzzle, refusing a second row for a day that already has one.

    Uniqueness is checked here *and* enforced by the schema, and both matter: the check gives
    a 409 with a message an operator can act on, while the constraint is what holds when two
    saves race past that check. The ``IntegrityError`` path is the same 409, so the race reads
    identically to the ordinary conflict rather than as a 500.
    """
    game_date = payload.game_date or _today()
    mode = payload.mode
    answer = _resolve_answer(payload, game_date)

    with db.sessions() as session:
        existing = session.scalar(
            select(WordleGameRecord.id)
            .where(WordleGameRecord.game_date == game_date)
            .limit(1)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A Wordle game is already configured for {game_date.isoformat()}.",
            )

    try:
        with db.sessions.begin() as session:
            row = WordleGameRecord(game_date=game_date, mode=mode, answer=answer)
            session.add(row)
            session.flush()
            return _as_model(row)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A Wordle game is already configured for {game_date.isoformat()}.",
        ) from exc


def list_wordle_games(db: Database) -> List[WordleGame]:
    """Every configured puzzle, newest day first."""
    with db.sessions() as session:
        rows = session.scalars(
            select(WordleGameRecord).order_by(WordleGameRecord.game_date.desc())
        ).all()
        return [_as_model(row) for row in rows]


def get_wordle_game(db: Database, game_id: str) -> WordleGame:
    """One configured puzzle by id.

    A malformed id is a 400 and a well-formed one that matches nothing is a 404, so a typo in
    a URL and a puzzle that has been superseded are told apart rather than both reported as
    "not found".
    """
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
