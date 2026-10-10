"""Player activity: the caller's own play history across every game.

The League ledger is the cross-game scoring layer, but it only holds
*finalized* events -- a round played today is not awarded until its session
closes. This module answers the dashboard's questions immediately instead,
from each game's own participation tables:

* how many rounds the caller has finished, all time;
* the current and longest daily streaks (consecutive UTC days with at least
  one finished round);
* this week's rounds, and which distinct games they were;
* the newest finished rounds, newest first, each with a short human summary
  and, where the game has one, a native performance number.

Nothing here is a score and nothing here recalculates a league award: native
results stay where they are and the ledger stays the only ranking source.
Only performance numbers travel -- answers, words, scrambles, prompts and
selections never leave this module, mirroring the league adapters' rule.

A round counts once it is settled in game terms: Wordle/Anagram won or lost,
a Connections or Trivia submission, one Reaction Time session with at least
one valid measurement, and a completed (or finalized) couple session. A
round still in progress is not history yet and is deliberately absent.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from sqlalchemy import or_, select

from ..db_models import (
    AnagramUserGameRecord,
    CaseClosedSessionRecord,
    ConnectionsSubmissionRecord,
    CoupleGameSessionRecord,
    PlotTwistSessionRecord,
    ReactionTimeSubmissionRecord,
    TriviaDropSubmissionRecord,
    TwoKeyEscapeSessionRecord,
    WordleUserGameRecord,
)
from ..postgres import Database
from .league_scoring import week_start

#: How many rounds the dashboard's "recently played" list asks for by default.
DEFAULT_RECENT_LIMIT = 5


@dataclass(frozen=True)
class PlayEvent:
    """One finished round, as the player's own history shows it.

    ``summary`` is display text composed here, from performance numbers
    only. ``score``/``score_label`` carry the game's native headline number
    (guesses, milliseconds, seconds) when it has one; null when the round
    has no single number worth showing.
    """

    game_id: str
    played_at: datetime
    summary: str
    score: Optional[float] = None
    score_label: Optional[str] = None


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    """Normalize a stored timestamp to aware UTC; None stays None."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _moment(*candidates: Optional[datetime]) -> datetime:
    """The first present timestamp among ``candidates``, as aware UTC.

    Every settle path stamps its own column; the fallbacks keep a legacy row
    that missed one out of the history rather than letting the row vanish.
    """
    for candidate in candidates:
        normalized = _aware(candidate)
        if normalized is not None:
            return normalized
    raise ValueError("a finished round must carry at least one timestamp")


def _wordle_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(WordleUserGameRecord).where(
            WordleUserGameRecord.user_id == user_id,
            WordleUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    for row in rows:
        attempts = int(row.attempt_count)
        yield PlayEvent(
            game_id="wordle",
            played_at=_moment(row.completed_at, row.last_guess_at, row.started_at),
            summary=f"Solved in {attempts}" if row.status == "won" else "Not solved",
            score=float(attempts),
            score_label="guesses",
        )


def _anagram_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(AnagramUserGameRecord).where(
            AnagramUserGameRecord.user_id == user_id,
            AnagramUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    for row in rows:
        attempts = int(row.attempt_count)
        yield PlayEvent(
            game_id="anagram",
            played_at=_moment(row.completed_at, row.last_guess_at, row.started_at),
            summary=f"Solved in {attempts}" if row.status == "won" else "Not solved",
            score=float(attempts),
            score_label="guesses",
        )


def _connections_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(ConnectionsSubmissionRecord).where(
            ConnectionsSubmissionRecord.user_id == user_id
        )
    ).all()
    for row in rows:
        groups = int(row.correct_groups)
        yield PlayEvent(
            game_id="connections",
            played_at=_moment(row.submitted_at),
            summary=f"{groups}/4 groups",
            score=float(groups),
            score_label="groups",
        )


def _trivia_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(TriviaDropSubmissionRecord).where(
            TriviaDropSubmissionRecord.user_id == user_id
        )
    ).all()
    for row in rows:
        yield PlayEvent(
            game_id="trivia-drop",
            played_at=_moment(row.submitted_at),
            summary="Correct answer" if row.is_correct else "Incorrect answer",
            score=float(row.response_time_ms),
            score_label="ms",
        )


def _reaction_time_plays(session, user_id: str) -> Iterable[PlayEvent]:
    """One round per session: the best valid measurement, latest attempt time.

    A Reaction Time session is a budget of five tries, not five rounds, so
    the session is the unit. Too-early attempts made the session playable
    but are never a score, so only valid rows qualify; a session where every
    click was too early has no result to show and is not history.
    """
    rows = session.execute(
        select(
            ReactionTimeSubmissionRecord.reaction_time_game_id,
            ReactionTimeSubmissionRecord.reaction_time_ms,
            ReactionTimeSubmissionRecord.clicked_at,
            ReactionTimeSubmissionRecord.created_at,
        ).where(
            ReactionTimeSubmissionRecord.user_id == user_id,
            ReactionTimeSubmissionRecord.status == "valid",
            ReactionTimeSubmissionRecord.reaction_time_ms.is_not(None),
        )
    ).all()
    by_session: dict[str, dict] = {}
    for game_id, reaction_ms, clicked_at, created_at in rows:
        key = str(game_id)
        moment = _moment(clicked_at, created_at)
        current = by_session.get(key)
        if current is None:
            by_session[key] = {"best": int(reaction_ms), "tries": 1, "moment": moment}
        else:
            current["best"] = min(current["best"], int(reaction_ms))
            current["tries"] += 1
            current["moment"] = max(current["moment"], moment)
    for info in by_session.values():
        yield PlayEvent(
            game_id="reaction-time",
            played_at=info["moment"],
            summary=f"Best of {info['tries']} tries",
            score=float(info["best"]),
            score_label="ms",
        )


def _couple_questionnaire_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(CoupleGameSessionRecord).where(
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
            CoupleGameSessionRecord.status == "completed",
        )
    ).all()
    for row in rows:
        yield PlayEvent(
            game_id="couple-questionnaire",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Questionnaire complete",
        )


def _two_key_escape_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(TwoKeyEscapeSessionRecord).where(
            or_(
                TwoKeyEscapeSessionRecord.user_a_id == user_id,
                TwoKeyEscapeSessionRecord.user_b_id == user_id,
            ),
            TwoKeyEscapeSessionRecord.completed.is_(True),
        )
    ).all()
    for row in rows:
        elapsed = row.elapsed_seconds
        yield PlayEvent(
            game_id="two-key-escape",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Escaped the maze",
            score=round(float(elapsed), 1) if elapsed is not None else None,
            score_label="sec" if elapsed is not None else None,
        )


def _plot_twist_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(PlotTwistSessionRecord).where(
            or_(
                PlotTwistSessionRecord.user_a_id == user_id,
                PlotTwistSessionRecord.user_b_id == user_id,
            ),
            PlotTwistSessionRecord.status == "completed",
        )
    ).all()
    for row in rows:
        yield PlayEvent(
            game_id="plot-twist",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Story completed",
        )


def _case_closed_plays(session, user_id: str) -> Iterable[PlayEvent]:
    rows = session.scalars(
        select(CaseClosedSessionRecord).where(
            or_(
                CaseClosedSessionRecord.user_a_id == user_id,
                CaseClosedSessionRecord.user_b_id == user_id,
            ),
            CaseClosedSessionRecord.finalized.is_(True),
        )
    ).all()
    for row in rows:
        solved = bool(row.is_correct)
        score = float(row.score) if solved and row.score else None
        yield PlayEvent(
            game_id="case-closed",
            played_at=_moment(row.finalized_at, row.accusation_locked_at, row.created_at),
            summary="Case solved" if solved else "Case closed",
            score=score,
            score_label="score" if score is not None else None,
        )


#: One collector per game, so a new game joins the history by adding its
#: finished-round query here -- never by teaching the summary or the router
#: about a new table.
_COLLECTORS: tuple[Callable[[object, str], Iterable[PlayEvent]], ...] = (
    _wordle_plays,
    _anagram_plays,
    _connections_plays,
    _trivia_plays,
    _reaction_time_plays,
    _couple_questionnaire_plays,
    _two_key_escape_plays,
    _plot_twist_plays,
    _case_closed_plays,
)


def _streaks(days: set[date], today: date) -> tuple[int, int]:
    """``(current, longest)`` consecutive-day streaks over ``days`` (UTC).

    The current streak counts back from today, or from yesterday when today
    has not been played yet: a streak is only broken once a whole day passes
    without a round. The longest streak is the longest unbroken run in the
    whole history.
    """
    if not days:
        return 0, 0
    ordered = sorted(days)
    longest = 1
    run = 1
    for previous, current in zip(ordered, ordered[1:]):
        run = run + 1 if current == previous + timedelta(days=1) else 1
        longest = max(longest, run)
    cursor = today if today in days else today - timedelta(days=1)
    current_streak = 0
    while cursor in days:
        current_streak += 1
        cursor -= timedelta(days=1)
    return current_streak, longest


def build_player_summary(
    db: Database,
    *,
    user_id: str,
    recent_limit: int = DEFAULT_RECENT_LIMIT,
) -> dict:
    """Every figure the player dashboard shows, from finished native rounds.

    Reads the caller's own rows only, so the summary is inherently scoped to
    them; no other player's activity is ever materialized.
    """
    today = datetime.now(timezone.utc).date()
    with db.sessions() as session:
        events: list[PlayEvent] = []
        for collect in _COLLECTORS:
            events.extend(collect(session, user_id))
    events.sort(key=lambda event: event.played_at, reverse=True)

    days = {event.played_at.date() for event in events}
    current_streak, longest_streak = _streaks(days, today)

    week_open = week_start(today)
    week_close = week_open + timedelta(days=6)
    week_events = [
        event for event in events if week_open <= event.played_at.date() <= week_close
    ]

    return {
        "games_played": len(events),
        "current_streak_days": current_streak,
        "longest_streak_days": longest_streak,
        "plays_this_week": len(week_events),
        "games_this_week": sorted({event.game_id for event in week_events}),
        "recent_plays": [
            {
                "game_id": event.game_id,
                "played_at": event.played_at,
                "summary": event.summary,
                "score": event.score,
                "score_label": event.score_label,
            }
            for event in events[:recent_limit]
        ],
    }
