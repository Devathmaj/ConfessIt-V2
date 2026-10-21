"""Per-game leaderboard categories: ranking native gameplay metrics across ended sessions."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select

from ..db_models import (
    AnagramGameRecord,
    AnagramUserGameRecord,
    ConnectionsGameRecord,
    ConnectionsSubmissionRecord,
    CoupleGameRecord,
    CoupleGameSessionRecord,
    PlotTwistGameRecord,
    PlotTwistSessionRecord,
    ReactionTimeGameRecord,
    ReactionTimeSubmissionRecord,
    TriviaDropOptionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
    UserRecord,
    WordleGameRecord,
    WordleUserGameRecord,
)
from ..postgres import Database
from .league_scoring import (
    PERIOD_ALL_TIME,
    PERIOD_VALUES,
    canonical_couple,
    period_bounds,
    quantize_points,
)


# ---------------------------------------------------------------------------
# Category registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CategoryDef:
    """One selectable leaderboard view for one game."""

    id: str
    label: str
    description: str
    # Scopes: 'period' (window-filtered) vs 'record' (all-time personal record).
    scope: str
    is_default: bool = False


CATEGORIES: dict[str, list[CategoryDef]] = {
    "wordle": [
        CategoryDef("performance", "Performance", "Cross-session solving performance.", "period", True),
        CategoryDef("most_solved", "Most Solved", "Most puzzles solved in the window.", "period"),
        CategoryDef("fewest_guesses", "Fewest Guesses", "Best single solve in the window.", "period"),
        CategoryDef("fastest_solve", "Fastest Solve", "Quickest wall-clock solve on record (all-time).", "record"),
    ],
    "anagram": [
        CategoryDef("performance", "Performance", "Cross-session solving performance.", "period", True),
        CategoryDef("most_solved", "Most Solved", "Most puzzles solved in the window.", "period"),
        CategoryDef("fewest_attempts", "Fewest Attempts", "Best single solve in the window.", "period"),
        CategoryDef("fastest_solve", "Fastest Solve", "Lowest active solve time on record (all-time).", "record"),
    ],
    "reaction-time": [
        CategoryDef("average", "Average Reaction Time", "Lowest average valid reaction in the window.", "period", True),
        CategoryDef("fastest", "All-Time Fastest", "Fastest valid reaction on record (all-time).", "record"),
    ],
    "connections": [
        CategoryDef("performance", "Performance", "Total puzzle quality in the window.", "period", True),
        CategoryDef("best_quality", "Best Quality", "Best single puzzle in the window.", "period"),
        # No fastest_completion: submissions lack per-player start timestamps.
    ],
    "trivia-drop": [
        CategoryDef("answer_performance", "Answer Performance", "Total answer performance in the window.", "period", True),
        CategoryDef("most_answered", "Most Answered", "Most questions answered in the window.", "period"),
        CategoryDef("fastest_answer", "Fastest Answer", "Quickest valid response on record (all-time).", "record"),
    ],
    "couple-questionnaire": [
        CategoryDef("score", "Score", "Proportional prediction accuracy.", "period", True),
    ],
    "two-key-escape": [
        CategoryDef("escape_performance", "Escape Performance", "Successful-escape performance in the window.", "period", True),
        CategoryDef("fastest_escape", "Fastest Escape", "Quickest successful escape on record (all-time).", "record"),
    ],
    "plot-twist": [
        CategoryDef("score", "Score", "Story outcome performance.", "period", True),
        CategoryDef("most_treasures", "Most Treasures", "Most discoveries in the window.", "period"),
    ],
}

# Games whose categories rank canonical couples rather than users.
COUPLE_CATEGORY_GAMES = frozenset({"couple-questionnaire", "two-key-escape", "plot-twist"})


def list_categories(game_id: str) -> list[dict]:
    """Category descriptors for one game (unknown games refused)."""
    from .league_adapters import competition_type_for

    competition_type_for(game_id)
    try:
        definitions = CATEGORIES[game_id]
    except KeyError:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Game {game_id!r} has no leaderboard categories.",
        )
    return [
        {
            "id": definition.id,
            "label": definition.label,
            "description": definition.description,
            "scope": definition.scope,
            "is_default": definition.is_default,
        }
        for definition in definitions
    ]


def category_def(game_id: str, category: str) -> CategoryDef:
    """One category descriptor, or a 400 naming the valid ids."""
    for definition in CATEGORIES.get(game_id, []):
        if definition.id == category:
            return definition
    from fastapi import HTTPException, status

    valid = [definition.id for definition in CATEGORIES.get(game_id, [])]
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unknown category {category!r} for game {game_id!r}. Valid: {valid}.",
    )


def validate_category_request(game_id: str, category: str, period: str) -> CategoryDef:
    """Refuse unknown games, categories and periods (mirrors board validation)."""
    from .league_adapters import competition_type_for

    competition_type_for(game_id)
    definition = category_def(game_id, category)
    if period not in PERIOD_VALUES:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown leaderboard period {period!r}.",
        )
    return definition


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

# Attempt multiplier: solving dominates; each extra attempt shaves a small fixed factor.
def _attempt_multiplier(attempts: int) -> float:
    return max(0.90, 1.00 - 0.02 * (int(attempts) - 1))


# Anagram active-time multiplier: bounded bonus that cannot cross attempt tiers.
def _anagram_time_multiplier(active_seconds: Any) -> float:
    try:
        seconds = float(active_seconds)
    except (TypeError, ValueError):
        return 0.94
    if seconds <= 60:
        return 1.00
    if seconds <= 300:
        return 0.98
    if seconds <= 900:
        return 0.96
    return 0.94


# Two-Key escape time multiplier: speed bonus retaining a 0.90 completion floor.
def _escape_time_multiplier(elapsed_seconds: float) -> float:
    return max(0.90, 1.0 - float(elapsed_seconds) / 3600.0)


def _window(start: Optional[date], end: Optional[date], day: Optional[date]) -> bool:
    """Whether a canonical day falls in the window (open window = all time)."""
    if start is None or end is None or day is None:
        return start is None or end is None
    return start <= day <= end


def _display_names(session, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    rows = session.scalars(
        select(UserRecord).where(UserRecord.id.in_(sorted(user_ids)))
    ).all()
    return {str(row.id): (row.name or "Player") for row in rows}


def _active_answers(session, game_id: str) -> set[str]:
    """Answers of currently live sessions, whose words stay redacted."""
    if game_id == "wordle":
        rows = session.scalars(
            select(WordleGameRecord.answer).where(
                WordleGameRecord.status.in_(("active", "pending")),
                WordleGameRecord.answer.is_not(None),
            )
        ).all()
        return {str(answer) for answer in rows if answer}
    if game_id == "anagram":
        rows = session.scalars(
            select(AnagramGameRecord.answer).where(
                AnagramGameRecord.status.in_(("active", "pending")),
                AnagramGameRecord.answer.is_not(None),
            )
        ).all()
        return {str(answer) for answer in rows if answer}
    return set()


def _competition_ranks(
    items: list[tuple[tuple, tuple, Any]],
) -> list[tuple[int, tuple, Any]]:
    """Competition ranking (1, 1, 3) over (order, tie, payload) triples."""
    ordered = sorted(items, key=lambda item: item[0])
    ranked: list[tuple[int, tuple, Any]] = []
    previous: Optional[tuple] = None
    current_rank = 0
    for position, (order, tie, payload) in enumerate(ordered, start=1):
        if previous is None or tie != previous:
            current_rank = position
            previous = tie
        ranked.append((current_rank, order, payload))
    return ranked


# ---------------------------------------------------------------------------
# Wordle
# ---------------------------------------------------------------------------

def _wordle_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    """Finalized Wordle participations in the window (ended sessions only)."""
    rows = session.execute(
        select(
            WordleUserGameRecord.user_id,
            WordleUserGameRecord.attempt_count,
            WordleUserGameRecord.status,
            WordleUserGameRecord.wordle_game_id,
            WordleUserGameRecord.started_at,
            WordleUserGameRecord.solved_at,
            WordleGameRecord.answer,
            WordleGameRecord.game_date,
            WordleGameRecord.period_start_date,
        )
        .join(
            WordleGameRecord,
            WordleGameRecord.id == WordleUserGameRecord.wordle_game_id,
        )
        .where(
            WordleGameRecord.status == "ended",
            WordleUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        duration = None
        if row.status == "won" and row.solved_at is not None and row.started_at is not None:
            duration = (row.solved_at - row.started_at).total_seconds()
            if duration < 0:
                duration = None
        results.append(
            {
                "user_id": str(row.user_id),
                "attempts": int(row.attempt_count),
                "solved": row.status == "won",
                "word": row.answer,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.wordle_game_id),
                "duration_s": duration,
            }
        )
    return results


def _wordle_subjects(rows: list[dict]) -> dict[str, dict]:
    """Per-user aggregates plus internal performance (kept server-side)."""
    subjects: dict[str, dict] = {}
    for row in rows:
        subject = subjects.setdefault(
            row["user_id"],
            {"solves": [], "attempts": 0, "perf": 0.0},
        )
        subject["attempts"] += 1
        if row["solved"]:
            subject["solves"].append(row)
            subject["perf"] += 100.0 * _attempt_multiplier(row["attempts"])
    for subject in subjects.values():
        solves = subject["solves"]
        subject["solved"] = len(solves)
        subject["solve_rate"] = (
            round(len(solves) / subject["attempts"], 4) if subject["attempts"] else 0.0
        )
        subject["avg_guesses"] = (
            round(sum(s["attempts"] for s in solves) / len(solves), 2) if solves else None
        )
        best = min(
            solves,
            key=lambda s: (s["attempts"], s["duration_s"] if s["duration_s"] is not None else float("inf")),
            default=None,
        )
        subject["best"] = best
        timed = [s["duration_s"] for s in solves if s["duration_s"] is not None]
        subject["fastest_s"] = min(timed) if timed else None
    return subjects


def _wordle_record(row: Optional[dict], live_answers: set[str]) -> Optional[dict]:
    if row is None:
        return None
    word = row["word"] if row["word"] not in live_answers else None
    return {
        "word": word,
        "guesses": row["attempts"],
        "duration_s": row["duration_s"],
        "date": row["date"],
        "opportunity_id": row["opportunity_id"],
    }


def _build_wordle(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _wordle_rows(session, start, end)
    live = _active_answers(session, "wordle")
    subjects = _wordle_subjects(rows)
    items = []
    for user_id, subject in subjects.items():
        if category == "performance":
            order = (
                -round(subject["perf"], 6),
                -subject["solved"],
                subject["avg_guesses"] if subject["avg_guesses"] is not None else float("inf"),
                user_id,
            )
            tie = (round(subject["perf"], 6), subject["solved"])
            metrics = {
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
                "solve_rate": subject["solve_rate"],
                "avg_guesses": subject["avg_guesses"],
                "best_guesses": subject["best"]["attempts"] if subject["best"] else None,
            }
            best_rows = sorted(
                subject["solves"],
                key=lambda s: (s["attempts"], s["date"] or ""),
            )[:5]
            records = [_wordle_record(s, live) for s in best_rows]
        elif category == "most_solved":
            order = (
                -subject["solved"],
                -subject["solve_rate"],
                subject["avg_guesses"] if subject["avg_guesses"] is not None else float("inf"),
                user_id,
            )
            tie = (subject["solved"], subject["solve_rate"])
            metrics = {
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
                "solve_rate": subject["solve_rate"],
                "avg_guesses": subject["avg_guesses"],
            }
            records = []
        elif category == "fewest_guesses":
            if not subject["solves"]:
                continue
            best = subject["best"]
            assert best is not None
            order = (
                best["attempts"],
                best["duration_s"] if best["duration_s"] is not None else float("inf"),
                -subject["solved"],
                user_id,
            )
            tie = (best["attempts"],)
            metrics = {
                "best_guesses": best["attempts"],
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
                "best_duration_s": best["duration_s"],
            }
            records = [_wordle_record(best, live)]
        elif category == "fastest_solve":
            if subject["fastest_s"] is None:
                continue
            fastest = min(
                (s for s in subject["solves"] if s["duration_s"] is not None),
                key=lambda s: (s["duration_s"], s["attempts"]),
            )
            order = (fastest["duration_s"], fastest["attempts"], user_id)
            tie = (fastest["duration_s"],)
            metrics = {
                "fastest_s": round(fastest["duration_s"], 2),
                "guesses": fastest["attempts"],
                "solved": subject["solved"],
            }
            records = [_wordle_record(fastest, live)]
        else:
            raise ValueError(category)
        items.append((order, tie, {"user_id": user_id, "metrics": metrics, "records": records}))
    return items

# ---------------------------------------------------------------------------
# Anagram (authoritative active time throughout)
# ---------------------------------------------------------------------------

def _anagram_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    rows = session.execute(
        select(
            AnagramUserGameRecord.user_id,
            AnagramUserGameRecord.attempt_count,
            AnagramUserGameRecord.status,
            AnagramUserGameRecord.anagram_game_id,
            AnagramUserGameRecord.active_time_seconds,
            AnagramUserGameRecord.solved_at,
            AnagramGameRecord.answer,
            AnagramGameRecord.game_date,
            AnagramGameRecord.period_start_date,
        )
        .join(
            AnagramGameRecord,
            AnagramGameRecord.id == AnagramUserGameRecord.anagram_game_id,
        )
        .where(
            AnagramGameRecord.status == "ended",
            AnagramUserGameRecord.status.in_(("won", "lost")),
        )
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        try:
            active = int(row.active_time_seconds) if row.active_time_seconds is not None else None
        except (TypeError, ValueError):
            active = None
        results.append(
            {
                "user_id": str(row.user_id),
                "attempts": int(row.attempt_count),
                "solved": row.status == "won",
                "active_s": active,
                "word": row.answer,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.anagram_game_id),
            }
        )
    return results


def _anagram_subjects(rows: list[dict]) -> dict[str, dict]:
    subjects: dict[str, dict] = {}
    for row in rows:
        subject = subjects.setdefault(
            row["user_id"],
            {"solves": [], "attempts": 0, "perf": 0.0},
        )
        subject["attempts"] += 1
        if row["solved"]:
            subject["solves"].append(row)
            subject["perf"] += (
                100.0 * _attempt_multiplier(row["attempts"])
                * _anagram_time_multiplier(row["active_s"])
            )
    for subject in subjects.values():
        solves = subject["solves"]
        subject["solved"] = len(solves)
        subject["solve_rate"] = (
            round(len(solves) / subject["attempts"], 4) if subject["attempts"] else 0.0
        )
        subject["avg_attempts"] = (
            round(sum(s["attempts"] for s in solves) / len(solves), 2) if solves else None
        )
        timed = [s["active_s"] for s in solves if s["active_s"] is not None]
        subject["avg_active_s"] = round(sum(timed) / len(timed), 2) if timed else None
        subject["best"] = min(
            solves,
            key=lambda s: (
                s["attempts"],
                s["active_s"] if s["active_s"] is not None else float("inf"),
            ),
            default=None,
        )
        subject["fastest_s"] = min(timed) if timed else None
    return subjects


def _anagram_record(row: Optional[dict], live_answers: set[str]) -> Optional[dict]:
    if row is None:
        return None
    return {
        "word": row["word"] if row["word"] not in live_answers else None,
        "attempts": row["attempts"],
        "active_s": row["active_s"],
        "date": row["date"],
        "opportunity_id": row["opportunity_id"],
    }


def _build_anagram(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _anagram_rows(session, start, end)
    live = _active_answers(session, "anagram")
    subjects = _anagram_subjects(rows)
    items = []
    for user_id, subject in subjects.items():
        if category == "performance":
            order = (
                -round(subject["perf"], 6),
                -subject["solved"],
                subject["avg_attempts"] if subject["avg_attempts"] is not None else float("inf"),
                user_id,
            )
            tie = (round(subject["perf"], 6), subject["solved"])
            metrics = {
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
                "solve_rate": subject["solve_rate"],
                "avg_attempts": subject["avg_attempts"],
                "avg_active_s": subject["avg_active_s"],
            }
            best_rows = sorted(
                subject["solves"],
                key=lambda s: (s["attempts"], s["date"] or ""),
            )[:5]
            records = [_anagram_record(s, live) for s in best_rows]
        elif category == "most_solved":
            order = (
                -subject["solved"],
                -subject["solve_rate"],
                subject["avg_attempts"] if subject["avg_attempts"] is not None else float("inf"),
                user_id,
            )
            tie = (subject["solved"], subject["solve_rate"])
            metrics = {
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
                "solve_rate": subject["solve_rate"],
                "avg_attempts": subject["avg_attempts"],
            }
            records = []
        elif category == "fewest_attempts":
            if not subject["solves"]:
                continue
            best = subject["best"]
            assert best is not None
            order = (
                best["attempts"],
                best["active_s"] if best["active_s"] is not None else float("inf"),
                -subject["solved"],
                user_id,
            )
            tie = (best["attempts"],)
            metrics = {
                "best_attempts": best["attempts"],
                "best_active_s": best["active_s"],
                "solved": subject["solved"],
                "opportunities": subject["attempts"],
            }
            records = [_anagram_record(best, live)]
        elif category == "fastest_solve":
            if subject["fastest_s"] is None:
                continue
            fastest = min(
                (s for s in subject["solves"] if s["active_s"] is not None),
                key=lambda s: (s["active_s"], s["attempts"]),
            )
            order = (fastest["active_s"], fastest["attempts"], user_id)
            tie = (fastest["active_s"],)
            metrics = {
                "fastest_s": fastest["active_s"],
                "attempts": fastest["attempts"],
                "solved": subject["solved"],
            }
            records = [_anagram_record(fastest, live)]
        else:
            raise ValueError(category)
        items.append((order, tie, {"user_id": user_id, "metrics": metrics, "records": records}))
    return items


# ---------------------------------------------------------------------------
# Reaction Time (valid results only, never ranks or other players)
# ---------------------------------------------------------------------------

def _reaction_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    rows = session.execute(
        select(
            ReactionTimeSubmissionRecord.user_id,
            ReactionTimeSubmissionRecord.reaction_time_ms,
            ReactionTimeSubmissionRecord.clicked_at,
            ReactionTimeSubmissionRecord.reaction_time_game_id,
            ReactionTimeGameRecord.game_date,
            ReactionTimeGameRecord.period_start_date,
        )
        .join(
            ReactionTimeGameRecord,
            ReactionTimeGameRecord.id == ReactionTimeSubmissionRecord.reaction_time_game_id,
        )
        .where(
            ReactionTimeGameRecord.status == "ended",
            ReactionTimeSubmissionRecord.status == "valid",
            ReactionTimeSubmissionRecord.reaction_time_ms.is_not(None),
        )
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        results.append(
            {
                "user_id": str(row.user_id),
                "ms": int(row.reaction_time_ms),
                "clicked_at": row.clicked_at,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.reaction_time_game_id),
            }
        )
    return results


def _build_reaction(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _reaction_rows(session, start, end)
    by_user: dict[str, list[dict]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row)
    items = []
    for user_id, attempts in by_user.items():
        if category == "average":
            average = round(sum(a["ms"] for a in attempts) / len(attempts), 2)
            order = (average, -len(attempts), user_id)
            tie = (average,)
            metrics = {"avg_ms": average, "attempts": len(attempts)}
            records = []
        elif category == "fastest":
            best = min(
                attempts,
                key=lambda a: (
                    a["ms"],
                    a["clicked_at"].isoformat() if a["clicked_at"] else "",
                ),
            )
            order = (
                best["ms"],
                best["clicked_at"].isoformat() if best["clicked_at"] else "",
                user_id,
            )
            tie = (best["ms"],)
            metrics = {
                "fastest_ms": best["ms"],
                "date": best["date"],
                "opportunity_id": best["opportunity_id"],
                "attempts": len(attempts),
            }
            records = [
                {
                    "reaction_ms": best["ms"],
                    "date": best["date"],
                    "opportunity_id": best["opportunity_id"],
                }
            ]
        else:
            raise ValueError(category)
        items.append((order, tie, {"user_id": user_id, "metrics": metrics, "records": records}))
    return items


# ---------------------------------------------------------------------------
# Connections (quality only: no per-player timing exists to bonus)
# ---------------------------------------------------------------------------

def _connections_quality(words: int, groups: int) -> float:
    return float(min(words * 4, 64) + min(groups * 9, 36))


def _connections_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    rows = session.execute(
        select(
            ConnectionsSubmissionRecord.user_id,
            ConnectionsSubmissionRecord.correct_groups,
            ConnectionsSubmissionRecord.correct_words,
            ConnectionsSubmissionRecord.submitted_at,
            ConnectionsSubmissionRecord.connections_game_id,
            ConnectionsGameRecord.game_date,
            ConnectionsGameRecord.period_start_date,
        )
        .join(
            ConnectionsGameRecord,
            ConnectionsGameRecord.id == ConnectionsSubmissionRecord.connections_game_id,
        )
        .where(ConnectionsGameRecord.status == "ended")
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        groups = int(row.correct_groups or 0)
        words = int(row.correct_words or 0)
        results.append(
            {
                "user_id": str(row.user_id),
                "groups": groups,
                "words": words,
                "quality": _connections_quality(words, groups),
                "submitted_at": row.submitted_at,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.connections_game_id),
            }
        )
    return results


def _build_connections(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _connections_rows(session, start, end)
    by_user: dict[str, list[dict]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row)
    items = []
    for user_id, puzzles in by_user.items():
        if category == "performance":
            total = round(sum(p["quality"] for p in puzzles), 2)
            best = max(puzzles, key=lambda p: (p["quality"], p["groups"], p["words"]))
            order = (-total, -best["quality"], user_id)
            tie = (total, best["quality"])
            metrics = {
                "puzzles": len(puzzles),
                "total_quality": total,
                "best_quality": best["quality"],
                "avg_quality": round(total / len(puzzles), 2),
            }
            records = []
        elif category == "best_quality":
            top = max(p["quality"] for p in puzzles)
            # Groups, then words, then earliest submission win ties.
            best = min(
                (p for p in puzzles if p["quality"] == top),
                key=lambda p: (
                    -p["groups"],
                    -p["words"],
                    p["submitted_at"].isoformat() if p["submitted_at"] else "",
                ),
            )
            order = (
                -best["quality"],
                -best["groups"],
                -best["words"],
                best["submitted_at"].isoformat() if best["submitted_at"] else "",
                user_id,
            )
            tie = (best["quality"], best["groups"], best["words"])
            metrics = {
                "best_quality": best["quality"],
                "groups": best["groups"],
                "words": best["words"],
                "date": best["date"],
                "puzzles": len(puzzles),
            }
            records = [
                {
                    "quality": best["quality"],
                    "groups": best["groups"],
                    "words": best["words"],
                    "date": best["date"],
                    "opportunity_id": best["opportunity_id"],
                }
            ]
        else:
            raise ValueError(category)
        items.append((order, tie, {"user_id": user_id, "metrics": metrics, "records": records}))
    return items


# ---------------------------------------------------------------------------
# Trivia Drop (per-question absolute scoring; ended questions only)
# ---------------------------------------------------------------------------

def _trivia_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    from .league_global_scoring import trivia_accuracy, trivia_speed_factor

    questions = session.execute(
        select(
            TriviaDropQuestionRecord.id,
            TriviaDropQuestionRecord.question_text,
            TriviaDropQuestionRecord.released_at,
        ).where(TriviaDropQuestionRecord.status == "ended")
    ).all()
    wanted: dict[str, dict] = {}
    for row in questions:
        released = row.released_at
        day = released.date() if released is not None and hasattr(released, "date") else None
        if not _window(start, end, day):
            continue
        wanted[str(row.id)] = {
            "text": row.question_text,
            "date": (day.isoformat() if day else None),
        }
    if not wanted:
        return []
    option_rows = session.execute(
        select(
            TriviaDropOptionRecord.question_id,
            TriviaDropOptionRecord.option_index,
            TriviaDropOptionRecord.is_correct,
        ).where(TriviaDropOptionRecord.question_id.in_(list(wanted)))
    ).all()
    keys: dict[str, set[int]] = {}
    universes: dict[str, set[int]] = {}
    for question_id, index, correct in option_rows:
        key = str(question_id)
        universes.setdefault(key, set()).add(int(index))
        if correct:
            keys.setdefault(key, set()).add(int(index))
    submissions = session.execute(
        select(
            TriviaDropSubmissionRecord.question_id,
            TriviaDropSubmissionRecord.user_id,
            TriviaDropSubmissionRecord.selected_options,
            TriviaDropSubmissionRecord.is_correct,
            TriviaDropSubmissionRecord.response_time_ms,
            TriviaDropSubmissionRecord.submitted_at,
        ).where(TriviaDropSubmissionRecord.question_id.in_(list(wanted)))
    ).all()
    results = []
    for row in submissions:
        key = str(row.question_id)
        question = wanted[key]
        universe = universes.get(key, set())
        correct_set = keys.get(key, set())
        if not universe and not correct_set:
            accuracy = 1.0 if row.is_correct else 0.0
        else:
            accuracy = trivia_accuracy(row.selected_options, correct_set, universe)
        try:
            picked = sorted({int(option) for option in (row.selected_options or [])})
        except (TypeError, ValueError):
            picked = []
        try:
            ms = int(row.response_time_ms)
        except (TypeError, ValueError):
            ms = 0
        speed = trivia_speed_factor(ms)
        answer_score = float(
            quantize_points(
                Decimal("80") * Decimal(str(accuracy))
                + Decimal("20") * Decimal(str(accuracy)) * speed
            )
        )
        results.append(
            {
                "user_id": str(row.user_id),
                "accuracy": accuracy,
                "correct": bool(row.is_correct),
                "ms": ms,
                "answer_score": answer_score,
                "question_text": question["text"],
                "selected_options": picked,
                "date": question["date"],
                "opportunity_id": key,
                "submitted_at": row.submitted_at,
            }
        )
    return results


def _build_trivia(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _trivia_rows(session, start, end)
    by_user: dict[str, list[dict]] = {}
    for row in rows:
        by_user.setdefault(row["user_id"], []).append(row)
    items = []
    for user_id, answers in by_user.items():
        if category == "answer_performance":
            total = round(sum(a["answer_score"] for a in answers), 2)
            correct = sum(1 for a in answers if a["correct"])
            order = (-total, -correct, user_id)
            tie = (total, correct)
            metrics = {
                "total_score": total,
                "answered": len(answers),
                "correct": correct,
            }
            best_rows = sorted(
                answers, key=lambda a: (-a["answer_score"], a["ms"])
            )[:5]
            records = [_trivia_record(a) for a in best_rows]
        elif category == "most_answered":
            correct = sum(1 for a in answers if a["correct"])
            answered = len(answers)
            avg_ms = round(sum(a["ms"] for a in answers) / answered, 2)
            order = (-answered, -correct, avg_ms, user_id)
            tie = (answered, correct)
            metrics = {
                "answered": answered,
                "correct": correct,
                "avg_response_ms": avg_ms,
            }
            records = []
        elif category == "fastest_answer":
            best = min(
                answers,
                key=lambda a: (
                    a["ms"],
                    a["submitted_at"].isoformat() if a["submitted_at"] else "",
                ),
            )
            order = (
                best["ms"],
                best["submitted_at"].isoformat() if best["submitted_at"] else "",
                user_id,
            )
            tie = (best["ms"],)
            metrics = {
                "fastest_ms": best["ms"],
                "correct": best["correct"],
                "answered": len(answers),
            }
            records = [_trivia_record(best)]
        else:
            raise ValueError(category)
        items.append((order, tie, {"user_id": user_id, "metrics": metrics, "records": records}))
    return items


def _trivia_record(answer: dict) -> dict:
    # Ended questions only: options and selection are safe; answer keys are omitted.
    return {
        "question_text": answer["question_text"],
        "selected_options": answer["selected_options"],
        "correct": answer["correct"],
        "accuracy": answer["accuracy"],
        "response_ms": answer["ms"],
        "answer_score": answer["answer_score"],
        "date": answer["date"],
        "opportunity_id": answer["opportunity_id"],
    }


# ---------------------------------------------------------------------------
# Two-Key Escape (completed escapes only; unfinished runs are absent)
# ---------------------------------------------------------------------------

def _two_key_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    rows = session.execute(
        select(
            TwoKeyEscapeSessionRecord.user_a_id,
            TwoKeyEscapeSessionRecord.user_b_id,
            TwoKeyEscapeSessionRecord.elapsed_seconds,
            TwoKeyEscapeSessionRecord.total_moves,
            TwoKeyEscapeSessionRecord.completed_at,
            TwoKeyEscapeSessionRecord.two_key_escape_game_id,
            TwoKeyEscapeGameRecord.game_date,
            TwoKeyEscapeGameRecord.period_start_date,
        )
        .join(
            TwoKeyEscapeGameRecord,
            TwoKeyEscapeGameRecord.id == TwoKeyEscapeSessionRecord.two_key_escape_game_id,
        )
        .where(
            TwoKeyEscapeGameRecord.status == "ended",
            TwoKeyEscapeSessionRecord.completed.is_(True),
            TwoKeyEscapeSessionRecord.elapsed_seconds.is_not(None),
        )
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        member_a, member_b = canonical_couple(str(row.user_a_id), str(row.user_b_id))
        elapsed = float(row.elapsed_seconds)
        results.append(
            {
                "couple": (member_a, member_b),
                "elapsed": elapsed,
                "perf": 100.0 * _escape_time_multiplier(elapsed),
                "moves": int(row.total_moves or 0),
                "completed_at": row.completed_at,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.two_key_escape_game_id),
            }
        )
    return results


def _build_two_key(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _two_key_rows(session, start, end)
    by_couple: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_couple.setdefault(row["couple"], []).append(row)
    items = []
    for couple, escapes in by_couple.items():
        if category == "escape_performance":
            total = round(sum(e["perf"] for e in escapes), 2)
            fastest = min(e["elapsed"] for e in escapes)
            average = round(sum(e["elapsed"] for e in escapes) / len(escapes), 2)
            order = (-total, -len(escapes), fastest, couple)
            tie = (total, len(escapes))
            metrics = {
                "escapes": len(escapes),
                "fastest_s": fastest,
                "avg_s": average,
            }
            records = []
        elif category == "fastest_escape":
            best = min(
                escapes,
                key=lambda e: (
                    e["elapsed"],
                    e["completed_at"].isoformat() if e["completed_at"] else "",
                ),
            )
            order = (
                best["elapsed"],
                best["completed_at"].isoformat() if best["completed_at"] else "",
                couple,
            )
            tie = (best["elapsed"],)
            metrics = {
                "fastest_s": best["elapsed"],
                "escapes": len(escapes),
                "moves": best["moves"],
                "date": best["date"],
            }
            records = [
                {
                    "elapsed_s": best["elapsed"],
                    "moves": best["moves"],
                    "date": best["date"],
                    "opportunity_id": best["opportunity_id"],
                }
            ]
        else:
            raise ValueError(category)
        items.append(
            (order, tie, {"couple": couple, "metrics": metrics, "records": records})
        )
    return items


# ---------------------------------------------------------------------------
# Plot Twist (completed stories only; unfinished runs are absent)
# ---------------------------------------------------------------------------

def _plot_twist_rows(session, start: Optional[date], end: Optional[date]) -> list[dict]:
    rows = session.execute(
        select(
            PlotTwistSessionRecord.user_a_id,
            PlotTwistSessionRecord.user_b_id,
            PlotTwistSessionRecord.treasures,
            PlotTwistSessionRecord.ending_id,
            PlotTwistSessionRecord.completed_at,
            PlotTwistSessionRecord.plot_twist_game_id,
            PlotTwistGameRecord.game_date,
            PlotTwistGameRecord.period_start_date,
        )
        .join(
            PlotTwistGameRecord,
            PlotTwistGameRecord.id == PlotTwistSessionRecord.plot_twist_game_id,
        )
        .where(
            PlotTwistGameRecord.status == "ended",
            PlotTwistSessionRecord.status == "completed",
        )
    ).all()
    results = []
    for row in rows:
        day = row.period_start_date or row.game_date
        if not _window(start, end, day):
            continue
        member_a, member_b = canonical_couple(str(row.user_a_id), str(row.user_b_id))
        results.append(
            {
                "couple": (member_a, member_b),
                "treasures": len(list(row.treasures or [])),
                "ending_id": row.ending_id,
                "completed_at": row.completed_at,
                "date": (row.game_date.isoformat() if row.game_date else None),
                "opportunity_id": str(row.plot_twist_game_id),
            }
        )
    return results


def _build_plot_twist(session, category: str, start: Optional[date], end: Optional[date]):
    rows = _plot_twist_rows(session, start, end)
    by_couple: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        by_couple.setdefault(row["couple"], []).append(row)
    items = []
    for couple, stories in by_couple.items():
        if category == "most_treasures":
            total = sum(s["treasures"] for s in stories)
            order = (-total, -len(stories), couple)
            tie = (total, len(stories))
            metrics = {"treasures": total, "stories": len(stories)}
            records = [
                {
                    "treasures": s["treasures"],
                    "ending_id": s["ending_id"],
                    "date": s["date"],
                    "opportunity_id": s["opportunity_id"],
                }
                for s in stories
            ]
        else:
            raise ValueError(category)
        items.append((order, tie, {"couple": couple, "metrics": metrics, "records": records}))
    return items


_BUILDERS = {
    "wordle": _build_wordle,
    "anagram": _build_anagram,
    "reaction-time": _build_reaction,
    "connections": _build_connections,
    "trivia-drop": _build_trivia,
    "two-key-escape": _build_two_key,
    "plot-twist": _build_plot_twist,
}


def _individual_entry(
    rank: int, user_id: str, names: dict[str, str], payload: dict
) -> dict:
    return {
        "rank": rank,
        "user_id": user_id,
        "display_name": names.get(user_id, "Player"),
        "member_a_id": None,
        "member_b_id": None,
        "member_a_name": None,
        "member_b_name": None,
        "metrics": payload["metrics"],
        "records": payload["records"],
    }


def _couple_entry(
    rank: int, couple: tuple[str, str], names: dict[str, str], payload: dict
) -> dict:
    member_a, member_b = couple
    return {
        "rank": rank,
        "user_id": None,
        "display_name": None,
        "member_a_id": member_a,
        "member_b_id": member_b,
        "member_a_name": names.get(member_a, "Player"),
        "member_b_name": names.get(member_b, "Player"),
        "metrics": payload["metrics"],
        "records": payload["records"],
    }


def _ranked_items(db, game_id: str, category: str, period: str, anchor: Optional[date]):
    """Full ranked (rank, payload) list for one category (no paging)."""
    from . import league_boards

    definition = validate_category_request(game_id, category, period)
    if game_id in ("couple-questionnaire", "plot-twist") and definition.id == "score":
        # Single score category: the absolute game board, mapped into the
        # category shape. No second query path for the same ranking.
        board = league_boards.game_board(
            db, game_id=game_id, competition_type="couple",
            period=period, anchor=anchor, skip=0, limit=10_000,
        )
        ranked = []
        for entry in board["entries"]:
            native = entry.get("native") or {}
            couple = (entry["member_a_id"], entry["member_b_id"])
            if game_id == "plot-twist":
                metrics = {
                    "score": entry["score"],
                    "treasures": native.get("treasure_count"),
                    "decisions": native.get("decisions_made"),
                }
            else:
                metrics = {
                    "score": entry["score"],
                    "correct": native.get("correct_predictions"),
                    "total": native.get("total_questions"),
                }
            payload = {"couple": couple, "metrics": metrics, "records": []}
            ranked.append((entry["rank"], payload))
        return definition, ranked
    if definition.scope == "record":
        start = end = None
    elif period == PERIOD_ALL_TIME:
        start = end = None
    else:
        bounds = period_bounds(period, anchor)
        start, end = bounds if bounds is not None else (None, None)
    with db.sessions() as session:
        items = _BUILDERS[game_id](session, category, start, end)
        return definition, [
            (rank, payload) for rank, _, payload in _competition_ranks(items)
        ]


def _entries_with_names(
    db, ranked: list[tuple[int, dict]], skip: int, limit: int
) -> tuple[list[dict], set[str]]:
    page = ranked[skip : skip + limit]
    ids: set[str] = set()
    for _, payload in page:
        if "couple" in payload:
            ids.update(payload["couple"])
        else:
            ids.add(payload["user_id"])
    with db.sessions() as session:
        names = _display_names(session, ids)
    entries = []
    for rank, payload in page:
        if "couple" in payload:
            entries.append(_couple_entry(rank, payload["couple"], names, payload))
        else:
            entries.append(_individual_entry(rank, payload["user_id"], names, payload))
    return entries, ids


def category_board(
    db: Database,
    *,
    game_id: str,
    category: str,
    period: str,
    anchor: Optional[date] = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    """One game's category leaderboard ranked over the full filtered set."""
    definition, ranked = _ranked_items(db, game_id, category, period, anchor)
    entries, _ = _entries_with_names(db, ranked, skip, limit)
    return {
        "game_id": game_id,
        "category": category,
        "scope": definition.scope,
        "period": "all_time" if definition.scope == "record" else period,
        "anchor": anchor.isoformat() if anchor is not None else None,
        "total": len(ranked),
        "entries": entries,
    }


def category_me(
    db: Database,
    *,
    game_id: str,
    category: str,
    user_id: str,
    period: str,
    anchor: Optional[date] = None,
) -> dict:
    """The viewer's own rows on one category board, with full-dataset ranks."""
    _, ranked = _ranked_items(db, game_id, category, period, anchor)
    mine = [
        (rank, payload)
        for rank, payload in ranked
        if (
            user_id in payload["couple"]
            if "couple" in payload
            else payload["user_id"] == user_id
        )
    ]
    entries, _ = _entries_with_names(db, mine, 0, len(mine))
    definition = category_def(game_id, category)
    return {
        "game_id": game_id,
        "category": category,
        "scope": definition.scope,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Headline records: one game's best result at a glance, for the admin overview
# ---------------------------------------------------------------------------

# Headline record board mapping for single top result per game.
HEADLINE_RECORD_CATEGORIES: dict[str, str] = {
    "wordle": "fastest_solve",
    "anagram": "fastest_solve",
    "reaction-time": "fastest",
    "connections": "best_quality",
    "trivia-drop": "fastest_answer",
    "couple-questionnaire": "score",
    "two-key-escape": "fastest_escape",
    "plot-twist": "most_treasures",
}


def record_highlights(db: Database, *, limit_per_game: int = 1) -> list[dict]:
    """Top of every game's headline record board across all-time history."""
    highlights = []
    for game_id, category in HEADLINE_RECORD_CATEGORIES.items():
        definition = category_def(game_id, category)
        board = category_board(
            db,
            game_id=game_id,
            category=category,
            period=PERIOD_ALL_TIME,
            skip=0,
            limit=limit_per_game,
        )
        highlights.append(
            {
                "game_id": game_id,
                "category": category,
                "label": definition.label,
                "scope": definition.scope,
                "entry": board["entries"][0] if board["entries"] else None,
            }
        )
    return highlights
