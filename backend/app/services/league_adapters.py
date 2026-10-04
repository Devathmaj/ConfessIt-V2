"""Per-game adapters: finalized native results in, rankable league entries out.

Each adapter exposes, for one game, the information the central scoring
service needs:

* the official scoring opportunity identifier (a session id, or a question
  id for Trivia Drop);
* the finalized result records for one opportunity;
* the game-native comparison that orders them;
* the ranking subject (one user, or one canonical pair);
* the canonical opportunity timestamp for period attribution;
* whether the opportunity was featured (read from the snapshot row).

Adapters never change native results and never expose secrets: answers,
words, scrambles, guesses, question text, options, maze layouts and
selections stay out of ``raw_metrics``. Only performance numbers travel.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select

from ..db_models import (
    AnagramGameRecord,
    AnagramUserGameRecord,
    ConnectionsGameRecord,
    ConnectionsSubmissionRecord,
    CoupleGameRecord,
    CoupleGameSessionPredictionRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    FeaturedGameRecord,
    PlotTwistDecisionRecord,
    PlotTwistGameRecord,
    PlotTwistPackageRecord,
    PlotTwistSessionRecord,
    ReactionTimeGameRecord,
    ReactionTimeSubmissionRecord,
    TriviaDropOptionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
    WordleGameRecord,
    WordleUserGameRecord,
)
from .league_scoring import (
    COMPETITION_COUPLE,
    COMPETITION_INDIVIDUAL,
    GAME_COMPETITION_TYPES,
    canonical_couple,
)


@dataclass
class RankedSubject:
    """One eligible finalized result inside an opportunity.

    ``tie_key`` groups equal native performances (they share averaged
    points); ``order_key`` additionally breaks the group deterministically
    for stable presentation. ``subject`` is ``(user_id,)`` for individuals
    or ``(member_a_id, member_b_id)`` canonically ordered for couples.
    """

    subject: tuple[str, ...]
    tie_key: tuple
    order_key: tuple
    raw_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpportunityInfo:
    """The canonical identity of one official scoring opportunity."""

    game_id: str
    opportunity_id: str
    competition_type: str
    # UTC day the featured lookup runs against (period start / release day).
    activation_date: date
    # Canonical UTC instant period boundaries are attributed to.
    competition_at: datetime


def _utc_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _as_utc(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _period_start(row) -> Optional[date]:
    return row.period_start_date or row.game_date


def competition_type_for(game_id: str) -> str:
    """The leaderboard subject type a game feeds; unknown games are refused."""
    try:
        return GAME_COMPETITION_TYPES[game_id]
    except KeyError:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown game {game_id!r}.",
        )


# ---------------------------------------------------------------------------
# Opportunity enumeration: ended events still missing league treatment
# ---------------------------------------------------------------------------

def _ended_session_ids(session, model, game_id: str, known: set[str]) -> list[str]:
    rows = session.scalars(
        select(model.id).where(model.status == "ended")
    ).all()
    return [str(row_id) for row_id in rows if str(row_id) not in known]


def pending_opportunities(session, game_id: str, known: set[str]) -> list[str]:
    """Ended native events of ``game_id`` absent from ``known`` snapshots.

    Only closed events qualify: an event others can still submit to must
    never be ranked early. Cancelled Trivia Drop questions are not scoring
    opportunities at all.
    """
    if game_id == "wordle":
        return _ended_session_ids(session, WordleGameRecord, game_id, known)
    if game_id == "anagram":
        return _ended_session_ids(session, AnagramGameRecord, game_id, known)
    if game_id == "connections":
        return _ended_session_ids(session, ConnectionsGameRecord, game_id, known)
    if game_id == "reaction-time":
        return _ended_session_ids(session, ReactionTimeGameRecord, game_id, known)
    if game_id == "couple-questionnaire":
        return _ended_session_ids(session, CoupleGameRecord, game_id, known)
    if game_id == "two-key-escape":
        return _ended_session_ids(session, TwoKeyEscapeGameRecord, game_id, known)
    if game_id == "plot-twist":
        return _ended_session_ids(session, PlotTwistGameRecord, game_id, known)
    if game_id == "trivia-drop":
        rows = session.scalars(
            select(TriviaDropQuestionRecord.id).where(
                TriviaDropQuestionRecord.status == "ended"
            )
        ).all()
        return [str(row_id) for row_id in rows if str(row_id) not in known]
    competition_type_for(game_id)
    return []


def opportunity_info(session, game_id: str, opportunity_id: str) -> Optional[OpportunityInfo]:
    """Canonical identity for one event, or None when it does not exist."""
    competition_type = competition_type_for(game_id)
    if game_id == "wordle":
        row = session.get(WordleGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "anagram":
        row = session.get(AnagramGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "connections":
        row = session.get(ConnectionsGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "reaction-time":
        row = session.get(ReactionTimeGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "couple-questionnaire":
        row = session.get(CoupleGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "two-key-escape":
        row = session.get(TwoKeyEscapeGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "plot-twist":
        row = session.get(PlotTwistGameRecord, opportunity_id)
        start = _period_start(row) if row is not None else None
    elif game_id == "trivia-drop":
        row = session.get(TriviaDropQuestionRecord, opportunity_id)
        if row is None:
            return None
        released = _as_utc(row.released_at)
        if released is None:
            return None
        return OpportunityInfo(
            game_id=game_id,
            opportunity_id=str(row.id),
            competition_type=competition_type,
            activation_date=released.date(),
            competition_at=released,
        )
    else:
        return None
    if row is None or start is None:
        return None
    return OpportunityInfo(
        game_id=game_id,
        opportunity_id=str(row.id),
        competition_type=competition_type,
        activation_date=start,
        competition_at=_utc_midnight(start),
    )


def is_event_closed(session, game_id: str, opportunity_id: str) -> bool:
    """Whether the event is finalized in game terms (ended session/question)."""
    if game_id == "trivia-drop":
        row = session.get(TriviaDropQuestionRecord, opportunity_id)
        return row is not None and row.status == "ended"
    info = opportunity_info(session, game_id, opportunity_id)
    if info is None:
        return False
    model = {
        "wordle": WordleGameRecord,
        "anagram": AnagramGameRecord,
        "connections": ConnectionsGameRecord,
        "reaction-time": ReactionTimeGameRecord,
        "couple-questionnaire": CoupleGameRecord,
        "two-key-escape": TwoKeyEscapeGameRecord,
        "plot-twist": PlotTwistGameRecord,
    }[game_id]
    row = session.get(model, opportunity_id)
    return row is not None and row.status == "ended"


def featured_on(session, game_id: str, activation_day: date) -> bool:
    """Whether ``game_id`` held the spotlight on ``activation_day`` (UTC).

    Exactly one lookup against the featured schedule: the multiplier can
    therefore never stack from two markings.
    """
    return (
        session.scalar(
            select(FeaturedGameRecord.id)
            .where(
                FeaturedGameRecord.game_date == activation_day,
                FeaturedGameRecord.game_id == game_id,
            )
            .limit(1)
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Finalized results per opportunity (eligible rows only)
# ---------------------------------------------------------------------------

def _completed_at_iso(value: Optional[datetime]) -> str:
    moment = _as_utc(value)
    return moment.isoformat() if moment is not None else ""


def wordle_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Solved ranks ahead of unsolved; fewer guesses is better."""
    rows = session.scalars(
        select(WordleUserGameRecord).where(
            WordleUserGameRecord.wordle_game_id == opportunity_id,
            WordleUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    subjects = []
    for row in rows:
        user_id = str(row.user_id)
        solved = 1 if row.status == "won" else 0
        attempts = int(row.attempt_count)
        subjects.append(
            RankedSubject(
                subject=(user_id,),
                tie_key=(solved, -attempts),
                order_key=(solved, -attempts, _completed_at_iso(row.completed_at), user_id),
                raw_metrics={"solved": bool(solved), "attempts": attempts},
            )
        )
    return subjects


def anagram_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Solved first, then fewer attempts, then less banked active time."""
    rows = session.scalars(
        select(AnagramUserGameRecord).where(
            AnagramUserGameRecord.anagram_game_id == opportunity_id,
            AnagramUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    subjects = []
    for row in rows:
        user_id = str(row.user_id)
        solved = 1 if row.status == "won" else 0
        attempts = int(row.attempt_count)
        active = row.active_time_seconds if row.active_time_seconds is not None else 10**9
        subjects.append(
            RankedSubject(
                subject=(user_id,),
                tie_key=(solved, -attempts, -float(active)),
                order_key=(
                    solved, -attempts, -float(active),
                    _completed_at_iso(row.completed_at), user_id,
                ),
                raw_metrics={
                    "solved": bool(solved),
                    "attempts": attempts,
                    "active_time_seconds": row.active_time_seconds,
                },
            )
        )
    return subjects


def connections_results(session, opportunity_id: str) -> list[RankedSubject]:
    """More correct groups first, then more correct words."""
    rows = session.scalars(
        select(ConnectionsSubmissionRecord).where(
            ConnectionsSubmissionRecord.connections_game_id == opportunity_id
        )
    ).all()
    subjects = []
    for row in rows:
        user_id = str(row.user_id)
        groups = int(row.correct_groups)
        words = int(row.correct_words)
        subjects.append(
            RankedSubject(
                subject=(user_id,),
                tie_key=(groups, words),
                order_key=(groups, words, _completed_at_iso(row.submitted_at), user_id),
                raw_metrics={"correct_groups": groups, "correct_words": words},
            )
        )
    return subjects


def trivia_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Correct first, then faster -- mirroring the native results order.

    ``raw_metrics`` additionally carries the inputs the absolute global
    profile needs: the submitted selection, the key size, the option count,
    and the resulting ``accuracy`` in 0..1 (via the shared
    ``trivia_accuracy`` helper, including its anti-select-all rule). The
    key itself (which options are correct) is read from the persisted
    options at finalization and never stored on the award, so native
    answer content stays out of the ledger.
    """
    rows = session.scalars(
        select(TriviaDropSubmissionRecord).where(
            TriviaDropSubmissionRecord.question_id == opportunity_id
        )
    ).all()
    option_rows = session.execute(
        select(
            TriviaDropOptionRecord.option_index,
            TriviaDropOptionRecord.is_correct,
        ).where(TriviaDropOptionRecord.question_id == opportunity_id)
    ).all()
    universe = {int(option) for option, _ in option_rows}
    key = {int(option) for option, correct in option_rows if correct}
    # Imported lazily: league_global_scoring imports this module's
    # competition_type_for lazily in turn, so a top-level import would cycle.
    from .league_global_scoring import trivia_accuracy

    subjects = []
    for row in rows:
        user_id = str(row.user_id)
        correct = 1 if row.is_correct else 0
        elapsed = int(row.response_time_ms)
        if not universe and not key:
            # Degraded data path: real questions always carry their four
            # options, but legacy rows may predate them. The stored
            # is_correct verdict was computed server-side at submit time
            # under exact-match semantics, so it doubles as single-answer
            # accuracy rather than zeroing a correct answer for missing key
            # data no scoring function may invent.
            accuracy = 1.0 if row.is_correct else 0.0
        else:
            accuracy = trivia_accuracy(row.selected_options, key, universe)
        try:
            picked = sorted({int(option) for option in (row.selected_options or [])})
        except (TypeError, ValueError):
            picked = []
        subjects.append(
            RankedSubject(
                subject=(user_id,),
                tie_key=(correct, -elapsed),
                order_key=(correct, -elapsed, _completed_at_iso(row.submitted_at), user_id),
                raw_metrics={
                    "is_correct": bool(row.is_correct),
                    "response_time_ms": elapsed,
                    "selected_options": picked,
                    "correct_count": len(key),
                    "total_options": len(universe),
                    "accuracy": accuracy,
                },
            )
        )
    return subjects


def reaction_time_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Each user's best valid reaction ranks; too-early-only players are out.

    The authoritative result per participant is the minimum valid
    measurement, matching how the game reads its own state.
    """
    rows = session.scalars(
        select(ReactionTimeSubmissionRecord).where(
            ReactionTimeSubmissionRecord.reaction_time_game_id == opportunity_id,
            ReactionTimeSubmissionRecord.status == "valid",
            ReactionTimeSubmissionRecord.reaction_time_ms.is_not(None),
        )
    ).all()
    by_user: dict[str, list[int]] = {}
    for row in rows:
        by_user.setdefault(str(row.user_id), []).append(int(row.reaction_time_ms))
    subjects = []
    for user_id, measurements in by_user.items():
        best = min(measurements)
        average = sum(measurements) / len(measurements)
        subjects.append(
            RankedSubject(
                subject=(user_id,),
                tie_key=(float(-best),),
                order_key=(-best, -average, user_id),
                raw_metrics={
                    "best_ms": best,
                    "valid_count": len(measurements),
                    "average_ms": round(average, 2),
                },
            )
        )
    return subjects


def couple_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Completed pair sessions ranked by joint prediction accuracy.

    Only ``completed`` sessions qualify; the couple score is the total
    correct predictions across both partners. No question text, options or
    selections travel into the metrics.

    ``raw_metrics`` additionally carries ``total_questions``: the session's
    eligible question set, i.e. every prediction opportunity the couple had
    -- two per shared question (one original per partner) plus one per
    exclusive question -- read from the session's drawn question rows, not
    from submissions. The absolute profiles divide correct predictions by
    this total, so unanswered or never-submitted questions count as zero
    without shrinking the denominator to "only what was answered". The
    model has no invalidation flag, so every drawn session question is
    scoreable; a session with no question rows scores zero.
    """
    pair_rows = session.scalars(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == opportunity_id,
            CoupleGameSessionRecord.status == "completed",
        )
    ).all()
    if not pair_rows:
        return []
    session_ids = [str(row.id) for row in pair_rows]
    correct_counts = dict(
        session.execute(
            select(
                CoupleGameSessionPredictionRecord.session_id,
                func.count().filter(CoupleGameSessionPredictionRecord.is_correct.is_(True)),
            )
            .where(
                CoupleGameSessionPredictionRecord.session_id.in_(session_ids)
            )
            .group_by(CoupleGameSessionPredictionRecord.session_id)
        ).all()
    )
    # Scoreable opportunities from the drawn question set: shared questions
    # yield one prediction per partner (two), exclusive questions one.
    # Questions the couple never answered stay in the denominator.
    question_counts: dict[str, dict[str, int]] = {}
    for session_id, assignment, count in session.execute(
        select(
            CoupleGameSessionQuestionRecord.session_id,
            CoupleGameSessionQuestionRecord.assignment,
            func.count(),
        )
        .where(CoupleGameSessionQuestionRecord.session_id.in_(session_ids))
        .group_by(
            CoupleGameSessionQuestionRecord.session_id,
            CoupleGameSessionQuestionRecord.assignment,
        )
    ).all():
        question_counts.setdefault(str(session_id), {})[str(assignment)] = int(count)
    subjects = []
    for row in pair_rows:
        member_a, member_b = canonical_couple(str(row.user_a_id), str(row.user_b_id))
        correct = int(correct_counts.get(str(row.id), 0))
        assignments = question_counts.get(str(row.id), {})
        total = (
            2 * assignments.get("shared", 0)
            + assignments.get("user_a", 0)
            + assignments.get("user_b", 0)
        )
        subjects.append(
            RankedSubject(
                subject=(member_a, member_b),
                tie_key=(correct,),
                order_key=(correct, _completed_at_iso(row.completed_at), member_a, member_b),
                raw_metrics={"correct_predictions": correct, "total_questions": total},
            )
        )
    return subjects


def two_key_escape_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Completed escapes ranked by elapsed time; unfinished overlays are out.

    Mirrors the game's own leaderboard order (completed first, then time).
    Unfinished couples at finalization are missed opportunities worth zero,
    not ranked participants.
    """
    rows = session.scalars(
        select(TwoKeyEscapeSessionRecord).where(
            TwoKeyEscapeSessionRecord.two_key_escape_game_id == opportunity_id,
            TwoKeyEscapeSessionRecord.completed.is_(True),
        )
    ).all()
    subjects = []
    for row in rows:
        if row.elapsed_seconds is None:
            continue
        member_a, member_b = canonical_couple(str(row.user_a_id), str(row.user_b_id))
        elapsed = float(row.elapsed_seconds)
        subjects.append(
            RankedSubject(
                subject=(member_a, member_b),
                tie_key=(-elapsed,),
                order_key=(-elapsed, _completed_at_iso(row.completed_at), member_a, member_b),
                raw_metrics={
                    "elapsed_seconds": elapsed,
                    "total_moves": int(row.total_moves or 0),
                    "total_keys": int(row.total_keys or 0),
                },
            )
        )
    return subjects


def plot_twist_results(session, opportunity_id: str) -> list[RankedSubject]:
    """Completed story sessions ranked by outcome, then treasure, then pace.

    Only ``completed`` sessions qualify; unfinished stories at finalization
    are missed opportunities worth zero, not ranked participants. The
    deterministic base comes from the pinned package's ending tier plus
    bounded treasure bonuses (plot_twist_gameplay.score_for_outcome), so
    the adapter and the result screen agree on the number.
    """
    from .plot_twist_gameplay import score_for_outcome

    rows = session.scalars(
        select(PlotTwistSessionRecord).where(
            PlotTwistSessionRecord.plot_twist_game_id == opportunity_id,
            PlotTwistSessionRecord.status == "completed",
        )
    ).all()
    if not rows:
        return []
    package_ids = {str(row.story_package_id) for row in rows}
    packages = {
        str(pkg.id): dict(pkg.package or {})
        for pkg in session.scalars(
            select(PlotTwistPackageRecord).where(
                PlotTwistPackageRecord.id.in_(package_ids)
            )
        ).all()
    }
    subjects = []
    for row in rows:
        member_a, member_b = canonical_couple(str(row.user_a_id), str(row.user_b_id))
        package = packages.get(str(row.story_package_id), {})
        endings = {e.get("id"): e for e in (package.get("endings") or [])}
        ending = endings.get(row.ending_id or "")
        decisions = session.scalar(
            select(func.count())
            .select_from(PlotTwistDecisionRecord)
            .where(PlotTwistDecisionRecord.session_id == row.id)
        ) or 0
        treasures = list(row.treasures or [])
        base = score_for_outcome(ending, treasures, int(decisions))
        subjects.append(
            RankedSubject(
                subject=(member_a, member_b),
                tie_key=(int(base), len(treasures), -int(decisions)),
                order_key=(base, len(treasures), -int(decisions),
                           _completed_at_iso(row.completed_at), member_a, member_b),
                raw_metrics={
                    "base_points": int(base),
                    "success": bool((ending or {}).get("success", False)),
                    "ending_type": (ending or {}).get("type"),
                    "treasure_count": len(treasures),
                    "decisions_made": int(decisions),
                },
            )
        )
    return subjects


RESULT_FETCHERS: dict[str, Callable] = {
    "wordle": wordle_results,
    "anagram": anagram_results,
    "connections": connections_results,
    "trivia-drop": trivia_results,
    "reaction-time": reaction_time_results,
    "couple-questionnaire": couple_results,
    "two-key-escape": two_key_escape_results,
    "plot-twist": plot_twist_results,
}


def finalized_results(session, game_id: str, opportunity_id: str) -> list[RankedSubject]:
    """Eligible finalized results for one opportunity (invalid rows excluded)."""
    try:
        fetcher = RESULT_FETCHERS[game_id]
    except KeyError:
        competition_type_for(game_id)
        raise
    return fetcher(session, opportunity_id)
