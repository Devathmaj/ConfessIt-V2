"""Playing a day's Anagram: the participant-facing half of the feature.

``anagram_service`` configures a puzzle -- a day and a word. This module is everything that
happens *afterwards*: reading the day's puzzle without revealing its answer, dealing each
player their own scramble, settling one guess at a time against it, and keeping the
active-play clock.

Four rules hold at the bottom of everything here.

**One participation per player per puzzle.** An ``anagram_user_games`` row pairs a user
with an anagram game, and the unique pair is the schema's rule, not a service check. A
reveal against a puzzle a player has no row for creates the row; two concurrent reveals
race that insert: each reads "no participation yet", each tries to write, and the unique
index lets exactly one of them own the row. The loser re-reads the winner's row under
``FOR UPDATE`` and plays against *that* -- so a day's puzzle cannot be played twice for an
extra attempt budget or a second scramble.

**The answer is stored once and revealed once.** ``anagram_games`` owns the word; a
participation row holds guesses and exactly one derived copy of it -- the player's own
``scrambled_word``, dealt at reveal and never re-dealt. A read emits the answer at exactly
one moment -- when the player's game is complete -- and no write path ever needs to trust
a client-supplied answer, because the comparison is always against the stored word.

**The clock counts play, not wall time.** ``active_time_seconds`` banks finished timing
segments; ``timer_started_at`` opens the current one. A segment closes on pause, on
heartbeat, on a guess that settles the game, and whenever a new segment would open over a
dead one -- and a segment older than ``ANAGRAM_TIMER_STALE_AFTER_SECONDS`` is discarded
rather than counted, because a browser that died mid-game never sends the close and the
dead hours are not play. The client owns the visible ticking and the decision to pause;
the server owns every number that persists, so a forged clock buys nothing.

**A guess is a rearrangement.** The screen submits the tile order, and the service refuses
anything that is not exactly the dealt letters rearranged -- wrong length, non-letters,
letters the player was never dealt -- with a 400 that consumes no attempt. Only a
well-formed arrangement reaches the comparison and the attempt budget.
"""

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
    """Deal a private permutation of ``answer`` that is never the answer itself.

    The comparison is of ordered sequences, not of letter sets: ``APPLE`` dealt as
    ``APPLE`` is refused and reshuffled even though every letter is trivially "present",
    while ``PLAPE`` is accepted. Duplicates need no special handling for exactly this
    reason -- positions are compared, not counts.

    A word with fewer than two distinct letters (``A``, ``AA``) has no other permutation,
    and a custom answer may be exactly that; rather than shuffle forever, the answer is
    returned as dealt. Random draws are always at least five letters, so this fallback is
    a custom-word grace rather than a draw outcome. The bounded retries below are the same
    grace for the same reason: a two-letter word lands back where it started half the
    time, and the loop must end regardless.
    """
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
    """Whether an open segment outlived any live game.

    The client heartbeats while the page runs visibly, so a live segment is never older
    than about a heartbeat interval; anything past the stale horizon means the heartbeats
    stopped, which means the page is gone, hidden without pausing, or dead.
    """
    return (now - timer_started_at).total_seconds() > ANAGRAM_TIMER_STALE_AFTER_SECONDS


def _close_segment(row: AnagramUserGameRecord, now: datetime) -> int:
    """Seconds of ``row``'s open segment to bank, closing nothing itself.

    One place for the rule every transition shares: no open segment banks nothing, a fresh
    segment banks its elapsed seconds, and a stale segment banks nothing -- the dead tail
    is discarded rather than counted. Callers add the return to ``active_time_seconds``
    and then move ``timer_started_at`` to NULL (pause, completion) or to ``now``
    (heartbeat, non-settling guess, resume).
    """
    if row.timer_started_at is None:
        return 0
    elapsed = (now - row.timer_started_at).total_seconds()
    if elapsed <= 0 or _segment_is_stale(row.timer_started_at, now):
        return 0
    return int(elapsed)


def _snapshot(
    game: AnagramGameRecord, row: AnagramUserGameRecord, now: datetime
) -> AnagramParticipation:
    """A participation answer as a client reads it.

    ``answer`` is the reveal and is the one field a read must decide about. It is emitted
    only once the game is over -- the completed puzzle has nothing left to hide, and on a
    loss it is the one field the player is owed. ``timer_running`` restates "an open,
    non-stale segment on a game still in progress" as one boolean so the screen knows
    whether to tick or to offer RESUME; a stale segment reads as paused, because that is
    what it is -- the page behind it is gone.
    """
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
    """The playable puzzle and this player's participation, without creating a row.

    Reading the screen must leave no trace: opening it twice costs nothing, and a player
    who only looks at today's puzzle has not started playing it. A participation exists
    exactly once the player presses REVEAL WORD, so a fresh player reads
    ``participation=None`` and a returning one reads the same scramble, guesses, attempts
    and banked time they left.

    Hidden and disabled games refuse with a coded 403; a pending session answers
    200 with ``session_status="pending"`` and a sentence instead of a board. A
    session past its calendar boundary reads as absent until the reset loop
    opens the next period -- reads never create sessions.
    """
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

    # ENG-001: hold the lifecycle lock across liveness-check + write so a
    # scheduler rollover cannot slip between them. All callers write.
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
    """This player's locked row for the named puzzle, or the reason there is none.

    A guess, a pause and a heartbeat all act on a participation the reveal created; none
    of them creates one, because creating is the reveal's job and a row conjured by a
    pause would be a timer with no scramble behind it.
    """
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
    """Reveal the puzzle: deal the scramble, open the clock, or resume it.

    The first call creates the row -- the scramble is dealt once, the timer opens, and a
    player who refreshes or returns finds the same tiles. A later call on a paused game
    re-opens the clock, which is the RESUME half of the screen's single control: returning
    to the page never restarts the timer by itself, only an explicit press does. A call on
    a game already running is a no-op read, and a call on a finished game is a 409 -- a
    completed puzzle is read-only and there is no second row to make.

    Two concurrent first reveals race the insert the way concurrent first Wordle guesses
    do: ON CONFLICT DO NOTHING lets exactly one own the row, and every caller re-reads
    the row under FOR UPDATE and resumes *that* -- so the race cannot mint two scrambles
    or two attempt budgets. (Whether the row was just created or already existed needs no
    branch: a fresh row is open and in progress, so the resume and finished checks below
    are no-ops for it.)
    """
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
        # Resume: the paused segment was banked when it closed, and a stale open one
        # is discarded rather than counted, so opening a fresh segment now is exact
        # either way. An already-running game needs nothing.
        if row.timer_started_at is None or _segment_is_stale(row.timer_started_at, now):
            row.timer_started_at = now
        return _snapshot(game, row, now)


def pause_timer(db: Database, user_id: str, game_id: str) -> AnagramParticipation:
    """Bank the open segment and stop the clock. Idempotent by construction.

    The screen calls this when the page hides, the route changes, or the component
    unmounts -- every transition where counting on would credit time the player is not
    spending. Pausing a paused game, or a finished one, changes nothing and answers the
    current row, so a pause racing a completion lands softly either way.
    """
    with db.sessions.begin() as session:
        game = _get_game_for_update(session, game_id)
        row = _get_participation_for_update(session, str(game.id), user_id)
        now = _now()
        row.active_time_seconds += _close_segment(row, now)
        row.timer_started_at = None
        return _snapshot(game, row, now)


def heartbeat(db: Database, user_id: str, game_id: str) -> AnagramParticipation:
    """Checkpoint a running clock without stopping it.

    The screen calls this on an interval while the game runs visibly. Banking the segment
    so far and opening a fresh one keeps ``active_time_seconds`` within one interval of
    the truth, which is what survives an abrupt browser death: the next resume discards
    only the tail past the last checkpoint instead of the whole session. A heartbeat on a
    paused or finished game is a no-op read rather than an error, because a pause and a
    heartbeat in flight cannot be ordered and neither may fail the other.
    """
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
    """The one place a guess is turned into the spelling the answer compares.

    A submitted guess is raw on arrival, and every refusal here is a 400 with a sentence
    a player can act on rather than a 422 about a field shape. Crucially, a refusal
    consumes no attempt: the budget counts arrangements actually played, not malformed
    submissions, so the length, alphabet and rearrangement checks all run before anything
    is appended or incremented.
    """
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
    """Score one arrangement against the named puzzle, for this player.

    The whole exchange runs in one transaction. The participation row is read for update,
    which serialises concurrent guesses on the same puzzle, and the reveal is what created
    it: a guess with no participation is a 404 naming the reveal, not a lazy row, because
    a row conjured here would be a scramble the player never saw. The timer checkpoints
    across the guess -- play continues while the player rearranges -- and is banked and
    closed for good by the guess that settles the game, so the final total is persisted
    atomically with the verdict rather than in a call that might never come.

    ``correct`` is computed against the stored answer inside the transaction, so the
    answer and the verdict a client receives cannot disagree, and the response is built
    while the row is still locked so the ``attempt_count`` it reports is the one that was
    committed.
    """
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

        # Checkpoint first: the seconds played thinking about this guess belong to the
        # total whether the guess settles the game or not, and the reopen below keeps a
        # continuing game ticking without a second write.
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
