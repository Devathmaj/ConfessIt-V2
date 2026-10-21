"""Collect immediate player activity from each game's participation tables.

Player and console views share collectors; these metrics are separate from league awards.
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
    has no single number worth showing. ``user_ids`` names everyone who
    played the round -- one id for solo games, both partners for a couple
    game -- so the console can count distinct players without asking the
    league ledger, which only knows finalized awards.
    """

    game_id: str
    played_at: datetime
    summary: str
    user_ids: tuple[str, ...]
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


def _wordle_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(WordleUserGameRecord).where(
        WordleUserGameRecord.status.in_(("won", "lost")),
    )
    if user_id is not None:
        query = query.where(WordleUserGameRecord.user_id == user_id)
    for row in session.scalars(query).all():
        attempts = int(row.attempt_count)
        yield PlayEvent(
            game_id="wordle",
            played_at=_moment(row.completed_at, row.last_guess_at, row.started_at),
            summary=f"Solved in {attempts}" if row.status == "won" else "Not solved",
            user_ids=(str(row.user_id),),
            score=float(attempts),
            score_label="guesses",
        )


def _anagram_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(AnagramUserGameRecord).where(
        AnagramUserGameRecord.status.in_(("won", "lost")),
    )
    if user_id is not None:
        query = query.where(AnagramUserGameRecord.user_id == user_id)
    for row in session.scalars(query).all():
        attempts = int(row.attempt_count)
        yield PlayEvent(
            game_id="anagram",
            played_at=_moment(row.completed_at, row.last_guess_at, row.started_at),
            summary=f"Solved in {attempts}" if row.status == "won" else "Not solved",
            user_ids=(str(row.user_id),),
            score=float(attempts),
            score_label="guesses",
        )


def _connections_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(ConnectionsSubmissionRecord)
    if user_id is not None:
        query = query.where(ConnectionsSubmissionRecord.user_id == user_id)
    for row in session.scalars(query).all():
        groups = int(row.correct_groups)
        yield PlayEvent(
            game_id="connections",
            played_at=_moment(row.submitted_at),
            summary=f"{groups}/4 groups",
            user_ids=(str(row.user_id),),
            score=float(groups),
            score_label="groups",
        )


def _trivia_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(TriviaDropSubmissionRecord)
    if user_id is not None:
        query = query.where(TriviaDropSubmissionRecord.user_id == user_id)
    for row in session.scalars(query).all():
        yield PlayEvent(
            game_id="trivia-drop",
            played_at=_moment(row.submitted_at),
            summary="Correct answer" if row.is_correct else "Incorrect answer",
            user_ids=(str(row.user_id),),
            score=float(row.response_time_ms),
            score_label="ms",
        )


def _reaction_time_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    """One round per player per session: the best valid measurement, latest
    attempt time.

    A Reaction Time session is a budget of five tries, not five rounds, so
    the session is the unit -- one event per player, even though two players
    share the same session row. Too-early attempts made the session playable
    but are never a score, so only valid rows qualify; a session where every
    click was too early has no result to show and is not history.
    """
    query = select(
        ReactionTimeSubmissionRecord.reaction_time_game_id,
        ReactionTimeSubmissionRecord.user_id,
        ReactionTimeSubmissionRecord.reaction_time_ms,
        ReactionTimeSubmissionRecord.clicked_at,
        ReactionTimeSubmissionRecord.created_at,
    ).where(
        ReactionTimeSubmissionRecord.status == "valid",
        ReactionTimeSubmissionRecord.reaction_time_ms.is_not(None),
    )
    if user_id is not None:
        query = query.where(ReactionTimeSubmissionRecord.user_id == user_id)
    rows = session.execute(query).all()
    by_player: dict[tuple[str, str], dict] = {}
    for game_id, row_user_id, reaction_ms, clicked_at, created_at in rows:
        key = (str(game_id), str(row_user_id))
        moment = _moment(clicked_at, created_at)
        current = by_player.get(key)
        if current is None:
            by_player[key] = {
                "user_id": str(row_user_id),
                "best": int(reaction_ms),
                "tries": 1,
                "moment": moment,
            }
        else:
            current["best"] = min(current["best"], int(reaction_ms))
            current["tries"] += 1
            current["moment"] = max(current["moment"], moment)
    for info in by_player.values():
        yield PlayEvent(
            game_id="reaction-time",
            played_at=info["moment"],
            summary=f"Best of {info['tries']} tries",
            user_ids=(info["user_id"],),
            score=float(info["best"]),
            score_label="ms",
        )


def _couple_questionnaire_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(CoupleGameSessionRecord).where(
        CoupleGameSessionRecord.status == "completed"
    )
    if user_id is not None:
        query = query.where(
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            )
        )
    for row in session.scalars(query).all():
        yield PlayEvent(
            game_id="couple-questionnaire",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Questionnaire complete",
            user_ids=(str(row.user_a_id), str(row.user_b_id)),
        )


def _two_key_escape_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(TwoKeyEscapeSessionRecord).where(
        TwoKeyEscapeSessionRecord.completed.is_(True)
    )
    if user_id is not None:
        query = query.where(
            or_(
                TwoKeyEscapeSessionRecord.user_a_id == user_id,
                TwoKeyEscapeSessionRecord.user_b_id == user_id,
            )
        )
    for row in session.scalars(query).all():
        elapsed = row.elapsed_seconds
        yield PlayEvent(
            game_id="two-key-escape",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Escaped the maze",
            user_ids=(str(row.user_a_id), str(row.user_b_id)),
            score=round(float(elapsed), 1) if elapsed is not None else None,
            score_label="sec" if elapsed is not None else None,
        )


def _plot_twist_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(PlotTwistSessionRecord).where(
        PlotTwistSessionRecord.status == "completed"
    )
    if user_id is not None:
        query = query.where(
            or_(
                PlotTwistSessionRecord.user_a_id == user_id,
                PlotTwistSessionRecord.user_b_id == user_id,
            )
        )
    for row in session.scalars(query).all():
        yield PlayEvent(
            game_id="plot-twist",
            played_at=_moment(row.completed_at, row.created_at),
            summary="Story completed",
            user_ids=(str(row.user_a_id), str(row.user_b_id)),
        )


def _case_closed_plays(session, user_id: Optional[str]) -> Iterable[PlayEvent]:
    query = select(CaseClosedSessionRecord).where(
        CaseClosedSessionRecord.finalized.is_(True)
    )
    if user_id is not None:
        query = query.where(
            or_(
                CaseClosedSessionRecord.user_a_id == user_id,
                CaseClosedSessionRecord.user_b_id == user_id,
            )
        )
    for row in session.scalars(query).all():
        solved = bool(row.is_correct)
        score = float(row.score) if solved and row.score else None
        yield PlayEvent(
            game_id="case-closed",
            played_at=_moment(row.finalized_at, row.accusation_locked_at, row.created_at),
            summary="Case solved" if solved else "Case closed",
            user_ids=(str(row.user_a_id), str(row.user_b_id)),
            score=score,
            score_label="score" if score is not None else None,
        )


#: One collector per game, so a new game joins the history by adding its
#: finished-round query here -- never by teaching the summary, the console
#: overview or the router about a new table.
_COLLECTORS: tuple[Callable[[object, Optional[str]], Iterable[PlayEvent]], ...] = (
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


def collect_play_events(session, user_id: Optional[str] = None) -> list[PlayEvent]:
    """Every finished round in ``session``, all players when ``user_id`` is None.

    The one seam both dashboards read through: a player's summary narrows it
    to their own id, the console overview takes every player. "Finished"
    therefore means exactly the same thing on both screens.
    """
    events: list[PlayEvent] = []
    for collect in _COLLECTORS:
        events.extend(collect(session, user_id))
    return events


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
        events = collect_play_events(session, user_id)
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


def build_games_stats(db: Database) -> dict:
    """Finished-round totals across every player, for the console overview.

    The admin aggregate of :func:`build_player_summary`: same collectors, same
    UTC windows, but no user filter. A couple round counts once as a play and
    both partners count as players, because both of them played it. The
    per-game list only carries games with at least one finished round; a game
    nobody has played has nothing to contribute to the dashboard's play
    panels, and its availability is read from the configuration instead.
    """
    today = datetime.now(timezone.utc).date()
    week_open = week_start(today)
    week_close = week_open + timedelta(days=6)

    with db.sessions() as session:
        events = collect_play_events(session)

    per_game: dict[str, dict[str, int]] = {}
    players_today: set[str] = set()
    players_week: set[str] = set()
    plays_today = 0
    plays_week = 0
    for event in events:
        day = event.played_at.date()
        bucket = per_game.setdefault(
            event.game_id,
            {"plays_today": 0, "plays_this_week": 0, "plays_all_time": 0},
        )
        bucket["plays_all_time"] += 1
        if day == today:
            bucket["plays_today"] += 1
            plays_today += 1
            players_today.update(event.user_ids)
        if week_open <= day <= week_close:
            bucket["plays_this_week"] += 1
            plays_week += 1
            players_week.update(event.user_ids)

    return {
        "server_date": today,
        "plays_today": plays_today,
        "plays_this_week": plays_week,
        "plays_all_time": len(events),
        "players_today": len(players_today),
        "players_this_week": len(players_week),
        "games": [
            {"game_id": game_id, **totals}
            for game_id, totals in sorted(per_game.items())
        ],
    }
