"""Playing a day's Wordle: the participant-facing half of the feature.

``wordle_service`` configures a puzzle -- a day and a word. This module is everything that
happens *afterwards*: reading the day's puzzle without revealing its answer, and settling
one guess at a time against it.

Two rules hold at the bottom of everything here.

**One participation per player per puzzle.** A ``wordle_user_games`` row pairs a user with
a wordle game, and the unique pair is the schema's rule, not a service check. A guess
against a puzzle a player has no row for creates the row lazily, and two concurrent first
guesses race that insert: each reads "no participation yet", each tries to write, and the
unique index lets exactly one of them own the row. The loser re-reads the winner's row
under ``FOR UPDATE`` and plays the guess against *that* -- so a day's puzzle cannot be
played twice for an extra attempt.

**The answer is never stored here.** ``wordle_games`` is the only place the word exists;
a participation row stores guesses only, and a read re-derives each guess's verdict with
the same deterministic evaluation the guess was scored with. ``current_wordle`` therefore
reveals the answer at exactly one moment -- when the player's game is complete -- and no
write path ever needs to trust a client-supplied answer, because there is no answer
anywhere in this table to send.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db_models import WordleGameRecord, WordleUserGameRecord
from ..limits import WORDLE_ANSWER_LENGTH, WORDLE_MAX_ATTEMPTS
from ..models import (
    WordleCurrentGame,
    WordleGuessRequest,
    WordleGuessResponse,
    WordleParticipation,
)
from ..postgres import Database, is_valid_id
from .wordle_service import guess_words


def _today() -> "object":
    """Today's date in UTC, the same clock every other service in the app uses."""
    return datetime.now(timezone.utc).date()


def _reveal_result(guess: str, actual: str) -> list[str]:
    """The verdict for one guess letter by letter: correct > present > absent, in order.

    The duplicates rule is scorable in the order letters are evaluated: a position that
    matched the answer consumes one instance of that letter, and a remaining copy may still
    be marked present elsewhere. Evaluated twice -- once at submit, once per GUESS on a
    read -- so the verdict a player saw is always the verdict a refreshed board shows.
    """
    result = ["absent"] * WORDLE_ANSWER_LENGTH
    remaining: dict[str, int] = {}
    for position, (answer_letter, guess_letter) in enumerate(zip(actual, guess)):
        if answer_letter == guess_letter:
            result[position] = "correct"
        else:
            remaining[answer_letter] = remaining.get(answer_letter, 0) + 1
    for position, guess_letter in enumerate(guess):
        if result[position] == "correct":
            continue
        if remaining.get(guess_letter, 0) > 0:
            result[position] = "present"
            remaining[guess_letter] -= 1
    return result


def evaluate_guess(answer: str, guess: str) -> list[str]:
    """The colour verdict a submitted ``guess`` earns against ``answer``.

    Kept separate from ``_reveal_result``'s internal use so a test (or a future streak
    line) can feed it any five-letter pair. The two accept the same shape and return the
    same vocabulary of tiles as the response models.
    """
    return _reveal_result(guess, answer)


def _participation_snapshot(
    game: WordleGameRecord, row: WordleUserGameRecord
) -> WordleParticipation:
    """A participation answer as a client reads it: guesses plus re-derived verdicts.

    ``answer`` is the reveal and is the one field a read must decide about. It is emitted
    only once the game is over -- the completed puzzle has nothing left to hide, and the
    answer is the one field a player who lost is owed. Values that are NULL-like are
    passed through so the JSON carries ``null`` rather than eliding the field.
    """
    return WordleParticipation(
        wordle_game_id=str(game.id),
        game_date=game.game_date,
        status=row.status,
        success=row.success,
        attempt_count=row.attempt_count,
        max_attempts=WORDLE_MAX_ATTEMPTS,
        guesses=list(row.guesses),
        results=[_reveal_result(guess, game.answer) for guess in row.guesses],
        started_at=row.started_at,
        last_guess_at=row.last_guess_at,
        completed_at=row.completed_at,
        solved_at=row.solved_at,
        answer=game.answer if row.status != "in_progress" else None,
    )


def _current_session(session) -> Optional[WordleGameRecord]:
    """The one active-or-pending session, or None when the game has none."""
    return session.scalar(
        select(WordleGameRecord)
        .where(WordleGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def _latest_playable_game(session) -> Optional[WordleGameRecord]:
    """The current session when it is active and its period has begun."""
    game = _current_session(session)
    if game is None or game.status != "active":
        return None
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        return None
    return game


def _normalise_guess(raw: str) -> str:
    """The one place a guess is turned into the spelling the board compares.

    A submitted guess is raw on arrival -- a player may type spaces, capitals or a stray
    hyphen -- and every refusal here is a 400 with a sentence a player can act on rather
    than a 422 about a field shape: the word was understood, it is just not acceptable.
    """
    guess = (raw or "").strip().lower()
    if not guess:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Type a word to guess."
        )
    if len(guess) != WORDLE_ANSWER_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A guess is exactly {WORDLE_ANSWER_LENGTH} letters.",
        )
    if not guess.isalpha():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A guess is letters only.",
        )
    if guess not in guess_words():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not a word on our list.",
        )
    return guess


def _empty_game() -> WordleCurrentGame:
    return WordleCurrentGame(
        game_id=None,
        game_date=None,
        participation=None,
        max_attempts=WORDLE_MAX_ATTEMPTS,
        word_length=WORDLE_ANSWER_LENGTH,
    )


def current_wordle(db: Database, user_id: str) -> WordleCurrentGame:
    """The playable puzzle and this player's participation, without creating a row.

    Reading the board must leave no trace: opening the screen twice at six guesses costs
    nothing, and a fresh player who only looks at today's puzzle has not started playing it.
    A player's participation exists exactly once they commit a guess, so a fresh player
    reads ``participation=None`` and a returning one reads the same board they left.

    Availability is enforced before anything else: hidden and disabled games
    refuse with a coded 403 rather than a board. A pending session (manual mode,
    word not yet provided) answers 200 with ``session_status="pending"`` and a
    sentence the screen shows instead of a playable board. A session past its
    calendar boundary reads as absent until the reset loop opens the next
    period -- reads never create sessions.
    """
    from . import game_periods
    from .game_configs import check_playable

    # Availability first, in its own committed transaction: hidden and disabled
    # games refuse here, and the config row is persisted exactly once.
    with db.sessions.begin() as session:
        check_playable(session, "wordle")

    with db.sessions() as session:
        game = _current_session(session)
        if game is None:
            return _empty_game()
        if game.status == "pending":
            return WordleCurrentGame(
                game_id=str(game.id),
                game_date=game.game_date,
                participation=None,
                session_status="pending",
                message="Today's word has not been set yet. Check back soon.",
                max_attempts=WORDLE_MAX_ATTEMPTS,
                word_length=WORDLE_ANSWER_LENGTH,
            )
        if game.status != "active":
            return _empty_game()
        start = game.period_start_date or game.game_date
        if start is not None and start > _today():
            return _empty_game()
        if game_periods.is_reset_due(game.period_end_date, _today()):
            return _empty_game()
        return _read_board(session, game, user_id)


def _read_board(session, game: WordleGameRecord, user_id: str) -> WordleCurrentGame:
    row = session.scalar(
        select(WordleUserGameRecord).where(
            WordleUserGameRecord.wordle_game_id == game.id,
            WordleUserGameRecord.user_id == user_id,
        )
    )
    return WordleCurrentGame(
        game_id=str(game.id),
        game_date=game.game_date,
        participation=_participation_snapshot(game, row) if row is not None else None,
        session_status="active",
        max_attempts=WORDLE_MAX_ATTEMPTS,
        word_length=WORDLE_ANSWER_LENGTH,
    )


def submit_guess(db: Database, user_id: str, payload: WordleGuessRequest) -> WordleGuessResponse:
    """Score one guess against ``payload.game_id`` and record it for ``user_id``.

    The whole exchange runs in one transaction. The participation row is read for update,
    which serialises concurrent guesses on the same puzzle: a first guess creates the row
    (two creations racing on the unique pair settle to the one that inserted first), and
    every subsequent guess appends to whatever row that was. A guess against a finished
    puzzle, or a seventh guess against an in-progress one, is refused rather than stored.

    ``result`` is computed against the stored answer inside the transaction, so the answer
    and the verdict a client receives cannot disagree, and the response is built while the
    row is still locked so the ``attempt_count`` it reports is the one that was committed.
    """
    if not is_valid_id(payload.game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Wordle game id"
        )

    guess = _normalise_guess(payload.guess)

    with db.sessions.begin() as session:
        from . import game_periods
        from .game_configs import check_playable, lock_game_for_play

        # ENG-001: hold the lifecycle lock across the liveness checks below and
        # the participation write, so a scheduler rollover cannot slip between
        # them and land this guess in the session being ended.
        lock_game_for_play(session, "wordle")
        check_playable(session, "wordle")
        game = session.get(WordleGameRecord, payload.game_id)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Wordle game not found"
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

        participation = session.scalar(
            select(WordleUserGameRecord)
            .where(
                WordleUserGameRecord.wordle_game_id == game.id,
                WordleUserGameRecord.user_id == user_id,
            )
            .with_for_update()
        )

        now = datetime.now(timezone.utc)
        if participation is None:
            # First guess for this player on this puzzle. Two requests can both read "no
            # participation yet" and both try to write; the unique (game, user) pair is the
            # rule, so the insert races them. ON CONFLICT DO NOTHING makes the losing
            # insert a non-event rather than an IntegrityError: exactly one of the two
            # requests inserts, and the loser's rowcount is zero. Either way a fresh
            # FOR UPDATE read follows -- the loser waits for the winner's transaction to
            # commit, then plays against the row that won -- so the day's puzzle is not
            # two puzzles with two attempt budgets.
            inserted = session.execute(
                pg_insert(WordleUserGameRecord)
                .values(
                    wordle_game_id=str(game.id),
                    user_id=user_id,
                    attempt_count=0,
                    status="in_progress",
                    success=False,
                    guesses=[],
                    started_at=now,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        WordleUserGameRecord.wordle_game_id,
                        WordleUserGameRecord.user_id,
                    ]
                )
            )
            participation = session.scalar(
                select(WordleUserGameRecord)
                .where(
                    WordleUserGameRecord.wordle_game_id == game.id,
                    WordleUserGameRecord.user_id == user_id,
                )
                .with_for_update()
            )
            if inserted.rowcount == 0:
                assert participation is not None, "the winning participation must exist"

        if participation.status != "in_progress":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This puzzle is already finished.",
            )
        if participation.attempt_count >= WORDLE_MAX_ATTEMPTS:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No guesses left for today's puzzle.",
            )

        result = _reveal_result(guess, game.answer)
        participation.guesses = [*participation.guesses, guess]
        participation.attempt_count += 1
        participation.last_guess_at = now
        if result == ["correct"] * WORDLE_ANSWER_LENGTH:
            participation.status = "won"
            participation.success = True
            participation.completed_at = now
            participation.solved_at = now
        elif participation.attempt_count >= WORDLE_MAX_ATTEMPTS:
            participation.status = "lost"
            participation.success = False
            participation.completed_at = now

        snapshot = _participation_snapshot(game, participation)
        return WordleGuessResponse(**snapshot.model_dump(), guess=guess, result=result)