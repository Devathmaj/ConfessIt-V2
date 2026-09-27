"""League leaderboards: normalized standings read from the award ledger.

Aggregation rules (one place, shared by every endpoint):

* A subject's game-period score is the average of that subject's awarded
  points over the game's finalized opportunities in the window. Awarded
  points already carry the featured multiplier, so featured results lift
  the average before it is taken. Missed eligible opportunities contribute
  zero through the denominator, which counts every finalized opportunity
  whether the subject played it or not. Games with no finalized
  opportunity in the window contribute nothing and impose no penalty.
* A global score sums the applicable game-period contributions, never
  mixing individual games into couple standings or vice versa.
* Daily/weekly/monthly boards normalize over their own window -- they are
  not sums of daily display scores.
* The all-time board accumulates finalized *weekly* game-period
  contributions across history, so games that release often cannot dominate
  by frequency alone.

Boards are read from persisted awards only; nothing here recalculates raw
game results. Display ranks use competition ranking (1, 1, 3): tied
subjects share equal points and are never given distinct official ranks.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select

from ..db_models import GameScoreAwardRecord, UserRecord
from ..postgres import Database
from .league_scoring import (
    COUPLE_GAMES,
    COMPETITION_COUPLE,
    COMPETITION_INDIVIDUAL,
    INDIVIDUAL_GAMES,
    PERIOD_ALL_TIME,
    PERIOD_VALUES,
    period_bounds,
    quantize_points,
    week_start,
)
from .league_adapters import competition_type_for


def _live_awards():
    return GameScoreAwardRecord.superseded_at.is_(None)


def _window_filter(start: Optional[date], end: Optional[date]):
    if start is None or end is None:
        return None
    lower = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    upper = datetime(end.year, end.month, end.day, 23, 59, 59, 999999, tzinfo=timezone.utc)
    return (GameScoreAwardRecord.competition_at >= lower) & (
        GameScoreAwardRecord.competition_at <= upper
    )


def _award_rows(
    session,
    *,
    competition_type: str,
    game_ids: Optional[list[str]] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
):
    query = (
        select(
            GameScoreAwardRecord.game_id,
            GameScoreAwardRecord.opportunity_id,
            GameScoreAwardRecord.user_id,
            GameScoreAwardRecord.member_a_id,
            GameScoreAwardRecord.member_b_id,
            GameScoreAwardRecord.awarded_points,
            GameScoreAwardRecord.base_points,
            GameScoreAwardRecord.is_featured_snapshot,
            GameScoreAwardRecord.competition_at,
            GameScoreAwardRecord.raw_metrics,
            GameScoreAwardRecord.rank,
            GameScoreAwardRecord.participant_count,
        )
        .where(
            GameScoreAwardRecord.competition_type == competition_type,
            _live_awards(),
        )
        .order_by(GameScoreAwardRecord.competition_at.asc())
    )
    if game_ids is not None:
        query = query.where(GameScoreAwardRecord.game_id.in_(game_ids))
    window = _window_filter(start, end)
    if window is not None:
        query = query.where(window)
    return session.execute(query).all()


def _subject_key(row, competition_type: str) -> tuple:
    if competition_type == COMPETITION_COUPLE:
        return (str(row.member_a_id), str(row.member_b_id))
    return (str(row.user_id),)


def _game_period_scores(rows, competition_type: str) -> dict:
    """Per (game, subject) normalized contribution over the rows' window.

    Returns ``{game_id: {"opportunities": {opp_ids}, "scores": {subject: avg}}}``.
    """
    by_game: dict[str, dict] = {}
    for row in rows:
        game = by_game.setdefault(
            row.game_id, {"opportunities": set(), "earned": {}}
        )
        game["opportunities"].add(str(row.opportunity_id))
        key = _subject_key(row, competition_type)
        game["earned"][key] = game["earned"].get(key, Decimal("0")) + Decimal(str(row.awarded_points))
    result = {}
    for game_id, game in by_game.items():
        count = len(game["opportunities"])
        result[game_id] = {
            "opportunities": sorted(game["opportunities"]),
            "opportunity_count": count,
            "scores": {
                key: quantize_points(total / count) for key, total in game["earned"].items()
            },
        }
    return result


def _competition_ranks(scored: list[tuple]) -> list[tuple[int, tuple]]:
    """Competition ranking over ``(subject, score)`` sorted best-first.

    Equal scores share one rank; the next rank skips the tied places.
    Deterministic within ties by subject identity for stable presentation.
    """
    ordered = sorted(scored, key=lambda item: (-item[1], item[0]))
    ranked = []
    previous_score: Optional[Decimal] = None
    current_rank = 0
    for position, (subject, score) in enumerate(ordered, start=1):
        if previous_score is None or score != previous_score:
            current_rank = position
            previous_score = score
        ranked.append((current_rank, (subject, score)))
    return ranked


def _display_names(session, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    rows = session.scalars(
        select(UserRecord).where(UserRecord.id.in_(sorted(user_ids)))
    ).all()
    return {str(row.id): (row.name or "Player") for row in rows}


def validate_board_request(game_id: Optional[str], competition_type: str, period: str) -> str:
    """Refuse unknown games, mismatched competition types and bad periods."""
    if period not in PERIOD_VALUES:
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown leaderboard period {period!r}.",
        )
    if competition_type not in (COMPETITION_INDIVIDUAL, COMPETITION_COUPLE):
        from fastapi import HTTPException, status

        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown competition type {competition_type!r}.",
        )
    if game_id is not None:
        expected = competition_type_for(game_id)
        if expected != competition_type:
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Game {game_id!r} has no {competition_type} leaderboard.",
            )
    return competition_type


def _native_for_subject(rows, competition_type: str, subject: tuple) -> dict:
    """The subject's latest native metrics in the window, plus context."""
    latest = None
    featured = False
    played = 0
    for row in rows:
        if _subject_key(row, competition_type) != subject:
            continue
        played += 1
        featured = featured or bool(row.is_featured_snapshot)
        if latest is None or row.competition_at >= latest.competition_at:
            latest = row
    return {
        "raw_metrics": dict(latest.raw_metrics or {}) if latest is not None else {},
        "is_featured": featured,
        "opportunities_played": played,
    }


def game_board(
    db: Database,
    *,
    game_id: str,
    competition_type: str,
    period: str,
    anchor: Optional[date] = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    """One game's normalized leaderboard for one period."""
    validate_board_request(game_id, competition_type, period)
    bounds = period_bounds(period, anchor)
    start, end = bounds if bounds is not None else (None, None)
    with db.sessions() as session:
        if period == PERIOD_ALL_TIME:
            return _all_time_game_board(
                session, game_id, competition_type, skip=skip, limit=limit
            )
        rows = _award_rows(
            session, competition_type=competition_type,
            game_ids=[game_id], start=start, end=end,
        )
        per_game = _game_period_scores(rows, competition_type)
        game = per_game.get(game_id, {"opportunity_count": 0, "scores": {}})
        scored = list(game["scores"].items())
        ranked = _competition_ranks(scored)
        names = _display_names(
            session, {part for subject, _ in scored for part in subject}
        )
        entries = []
        for rank, (subject, score) in ranked:
            native = _native_for_subject(rows, competition_type, subject)
            entries.append(_subject_entry(
                competition_type, subject, names, rank, score, native, None
            ))
        total = len(entries)
        return {
            "game_id": game_id,
            "competition_type": competition_type,
            "period": period,
            "opportunity_count": game["opportunity_count"],
            "total": total,
            "entries": entries[skip:skip + limit],
        }


def _all_time_game_board(session, game_id: str, competition_type: str, skip: int, limit: int) -> dict:
    rows = _award_rows(session, competition_type=competition_type, game_ids=[game_id])
    totals = _accumulate_all_time(rows, competition_type, [game_id])
    game = totals.get(game_id, {})
    scored = list(game.items())
    ranked = _competition_ranks(scored)
    names = _display_names(session, {part for subject, _ in scored for part in subject})
    entries = []
    for rank, (subject, score) in ranked:
        native = _native_for_subject(rows, competition_type, subject)
        entries.append(_subject_entry(
            competition_type, subject, names, rank, score, native, None
        ))
    total = len(entries)
    return {
        "game_id": game_id,
        "competition_type": competition_type,
        "period": PERIOD_ALL_TIME,
        "opportunity_count": len({str(row.opportunity_id) for row in rows}),
        "total": total,
        "entries": entries[skip:skip + limit],
    }


def _accumulate_all_time(rows, competition_type: str, game_ids: list[str]) -> dict:
    """All-time per-game scores: sum of finalized weekly contributions."""
    weeks: dict = {}
    for row in rows:
        moment = row.competition_at
        day = moment.date() if hasattr(moment, "date") else moment
        monday = week_start(day)
        weeks.setdefault(monday, []).append(row)
    totals: dict[str, dict] = {game_id: {} for game_id in game_ids}
    for week_rows in weeks.values():
        per_game = _game_period_scores(week_rows, competition_type)
        for game_id in game_ids:
            game = per_game.get(game_id)
            if not game:
                continue
            for subject, score in game["scores"].items():
                totals[game_id][subject] = (
                    totals[game_id].get(subject, Decimal("0")) + score
                )
    return {
        game_id: {
            subject: quantize_points(total) for subject, total in scores.items()
        }
        for game_id, scores in totals.items()
    }


def global_board(
    db: Database,
    *,
    competition_type: str,
    period: str,
    anchor: Optional[date] = None,
    skip: int = 0,
    limit: int = 50,
) -> dict:
    """The combined normalized standings across a competition type's games."""
    validate_board_request(None, competition_type, period)
    game_ids = (
        list(INDIVIDUAL_GAMES)
        if competition_type == COMPETITION_INDIVIDUAL
        else list(COUPLE_GAMES)
    )
    bounds = period_bounds(period, anchor)
    start, end = bounds if bounds is not None else (None, None)
    with db.sessions() as session:
        rows = _award_rows(
            session, competition_type=competition_type,
            game_ids=game_ids, start=start, end=end,
        )
        if period == PERIOD_ALL_TIME:
            per_game_totals = _accumulate_all_time(rows, competition_type, game_ids)
        else:
            per_game = _game_period_scores(rows, competition_type)
            per_game_totals = {
                game_id: per_game.get(game_id, {"scores": {}})["scores"]
                for game_id in game_ids
            }
        combined: dict[tuple, Decimal] = {}
        breakdown: dict[tuple, dict] = {}
        for game_id in game_ids:
            for subject, score in per_game_totals.get(game_id, {}).items():
                combined[subject] = combined.get(subject, Decimal("0")) + score
                breakdown.setdefault(subject, {})[game_id] = score
        combined = {
            subject: quantize_points(total) for subject, total in combined.items()
        }
        ranked = _competition_ranks(list(combined.items()))
        names = _display_names(
            session, {part for subject in combined for part in subject}
        )
        entries = []
        for rank, (subject, score) in ranked:
            native = _native_for_subject(rows, competition_type, subject)
            per_game_scores = {
                game_id: float(breakdown[subject].get(game_id, Decimal("0")))
                for game_id in game_ids
                if subject in breakdown and game_id in breakdown[subject]
            }
            entries.append(_subject_entry(
                competition_type, subject, names, rank, score, native, per_game_scores
            ))
        total = len(entries)
        return {
            "competition_type": competition_type,
            "period": period,
            "total": total,
            "entries": entries[skip:skip + limit],
        }


def my_standing(
    db: Database,
    *,
    user_id: str,
    competition_type: str,
    period: str,
    anchor: Optional[date] = None,
) -> dict:
    """The caller's own entries on one board, with ranks."""
    board = global_board(
        db, competition_type=competition_type, period=period,
        anchor=anchor, skip=0, limit=10_000,
    )
    mine = []
    for entry in board["entries"]:
        if competition_type == COMPETITION_COUPLE:
            if user_id not in (entry["member_a_id"], entry["member_b_id"]):
                continue
        elif entry["user_id"] != user_id:
            continue
        mine.append(entry)
    return {
        "competition_type": competition_type,
        "period": period,
        "entries": mine,
    }


def _subject_entry(
    competition_type: str,
    subject: tuple,
    names: dict[str, str],
    rank: int,
    score: Decimal,
    native: dict,
    per_game: Optional[dict],
) -> dict:
    if competition_type == COMPETITION_COUPLE:
        member_a, member_b = subject
        entry = {
            "rank": rank,
            "member_a_id": member_a,
            "member_b_id": member_b,
            "member_a_name": names.get(member_a, "Player"),
            "member_b_name": names.get(member_b, "Player"),
            "score": float(score),
        }
    else:
        (user_id,) = subject
        entry = {
            "rank": rank,
            "user_id": user_id,
            "display_name": names.get(user_id, "Player"),
            "score": float(score),
        }
    entry["native"] = native["raw_metrics"]
    entry["is_featured"] = native["is_featured"]
    entry["opportunities_played"] = native["opportunities_played"]
    if per_game is not None:
        entry["per_game"] = per_game
    return entry


def leaderboard_summary(db: Database) -> dict:
    """Counts of finalized opportunities and live awards per game."""
    with db.sessions() as session:
        opp_counts = dict(
            session.execute(
                select(
                    GameScoreAwardRecord.game_id,
                    func.count(func.distinct(GameScoreAwardRecord.opportunity_id)),
                )
                .where(_live_awards())
                .group_by(GameScoreAwardRecord.game_id)
            ).all()
        )
        award_counts = dict(
            session.execute(
                select(
                    GameScoreAwardRecord.game_id,
                    func.count(),
                )
                .where(_live_awards())
                .group_by(GameScoreAwardRecord.game_id)
            ).all()
        )
    from ..limits import GAME_IDS

    return {
        game_id: {
            "opportunities": int(opp_counts.get(game_id, 0)),
            "awards": int(award_counts.get(game_id, 0)),
        }
        for game_id in GAME_IDS
    }
