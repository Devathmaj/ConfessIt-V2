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


def _native_by_subject(rows, competition_type: str) -> dict:
    """Every subject's latest native metrics in the window, plus context.

    One O(rows) pass keyed by subject, instead of one O(rows) scan per subject
    (ENG-003): boards with hundreds of subjects were re-walking the whole award
    window for every ranked row, on top of materialising an entry per row only
    to discard all but the requested page.
    """
    latest: dict = {}
    featured: dict = {}
    played: dict = {}
    for row in rows:
        subject = _subject_key(row, competition_type)
        played[subject] = played.get(subject, 0) + 1
        if row.is_featured_snapshot:
            featured[subject] = True
        current = latest.get(subject)
        if current is None or row.competition_at >= current.competition_at:
            latest[subject] = row
    natives = {}
    for subject in played:
        row = latest[subject]
        natives[subject] = {
            "raw_metrics": dict(row.raw_metrics or {}),
            "is_featured": bool(featured.get(subject, False)),
            "opportunities_played": played[subject],
        }
    return natives


# A subject that somehow reaches entry building without a row (unreachable
# through the builders below, which rank only scored subjects): the old
# per-subject scan answered this with empties rather than raising, so the
# lookup sites below keep that contract instead of KeyErroring.
_EMPTY_NATIVE = {"raw_metrics": {}, "is_featured": False, "opportunities_played": 0}


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
        # Ranks are competition ranks over the full set, so ranking stays
        # whole-board; only entry materialisation is paged (ENG-003). Names
        # and native stats are read for the page alone rather than for every
        # ranked subject.
        ranked = _competition_ranks(scored)
        total = len(ranked)
        page = ranked[skip:skip + limit]
        natives = _native_by_subject(rows, competition_type)
        names = _display_names(
            session, {part for _, item in page for part in item[0]}
        )
        entries = [
            _subject_entry(
                competition_type, subject, names, rank, score,
                natives.get(subject, _EMPTY_NATIVE), None,
            )
            for rank, (subject, score) in page
        ]
        return {
            "game_id": game_id,
            "competition_type": competition_type,
            "period": period,
            "opportunity_count": game["opportunity_count"],
            "total": total,
            "entries": entries,
        }


def _all_time_game_board(session, game_id: str, competition_type: str, skip: int, limit: int) -> dict:
    rows = _award_rows(session, competition_type=competition_type, game_ids=[game_id])
    totals = _accumulate_all_time(rows, competition_type, [game_id])
    game = totals.get(game_id, {})
    scored = list(game.items())
    ranked = _competition_ranks(scored)
    total = len(ranked)
    page = ranked[skip:skip + limit]
    natives = _native_by_subject(rows, competition_type)
    names = _display_names(session, {part for _, item in page for part in item[0]})
    entries = [
        _subject_entry(
            competition_type, subject, names, rank, score,
            natives.get(subject, _EMPTY_NATIVE), None,
        )
        for rank, (subject, score) in page
    ]
    return {
        "game_id": game_id,
        "competition_type": competition_type,
        "period": PERIOD_ALL_TIME,
        "opportunity_count": len({str(row.opportunity_id) for row in rows}),
        "total": total,
        "entries": entries,
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


def _subject_matches_user(subject: tuple, competition_type: str, user_id: str) -> bool:
    """Whether this ranked subject involves ``user_id`` (for per-user reads)."""
    if competition_type == COMPETITION_COUPLE:
        return user_id in subject
    return len(subject) == 1 and subject[0] == user_id


def global_board(
    db: Database,
    *,
    competition_type: str,
    period: str,
    anchor: Optional[date] = None,
    skip: int = 0,
    limit: int = 50,
    only_user_id: Optional[str] = None,
) -> dict:
    """The combined normalized standings across a competition type's games.

    ``only_user_id`` narrows entry materialisation to one user's subjects
    (used by :func:`my_standing`): ranks are still computed over the whole
    board first, so a caller's rank stays its global rank rather than its rank
    among itself.
    """
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
        total = len(ranked)
        if only_user_id is not None:
            ranked = [
                item for item in ranked
                if _subject_matches_user(item[1], competition_type, only_user_id)
            ]
        page = ranked[skip:skip + limit]
        natives = _native_by_subject(rows, competition_type)
        names = _display_names(
            session, {part for _, item in page for part in item[0]}
        )
        entries = []
        for rank, (subject, score) in page:
            per_game_scores = {
                game_id: float(breakdown[subject].get(game_id, Decimal("0")))
                for game_id in game_ids
                if subject in breakdown and game_id in breakdown[subject]
            }
            entries.append(_subject_entry(
                competition_type, subject, names, rank, score,
                natives.get(subject, _EMPTY_NATIVE), per_game_scores,
            ))
        return {
            "competition_type": competition_type,
            "period": period,
            "total": total,
            "entries": entries,
        }


def my_standing(
    db: Database,
    *,
    user_id: str,
    competition_type: str,
    period: str,
    anchor: Optional[date] = None,
) -> dict:
    """The caller's own entries on one board, with global ranks.

    Ranks the whole board but materialises only the caller's subjects
    (ENG-003): the old version built an entry -- native scan, names and all --
    for every subject on the board only to discard all but one.
    """
    board = global_board(
        db, competition_type=competition_type, period=period,
        anchor=anchor, skip=0, limit=10_000, only_user_id=user_id,
    )
    return {
        "competition_type": competition_type,
        "period": period,
        "entries": board["entries"],
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
