"""League finalization: snapshots, awards, and the idempotent due scan.

An official scoring opportunity flows through two durable steps:

1. **Snapshot** (at activation): one ``game_score_opportunities`` row records
   the canonical timestamp and whether the game held the featured spotlight
   that day. Later featured changes never touch this row.
2. **Finalize** (at closure): eligible finalized native results are ranked
   with the game's own comparison, placements become base points, the
   snapshotted multiplier is applied once, and one award row per subject is
   written.

Finalization is idempotent: an advisory lock serializes concurrent runs on
one opportunity, and partial unique indexes refuse a second live award for
the same subject. Retried requests and repeated scheduler runs settle on
the same rows.

Corrections never overwrite: ``reprocess_opportunity`` marks the live
awards superseded and issues replacements, so history stays auditable.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db_models import GameScoreAwardRecord, GameScoreOpportunityRecord
from ..limits import GAME_IDS
from ..postgres import Database
from . import league_adapters as adapters
from .league_scoring import (
    COMPETITION_COUPLE,
    SCORING_VERSION,
    awarded_points,
    averaged_tie_points,
    base_points_for_rank,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lock_key(game_id: str, opportunity_id: str) -> str:
    return f"league:{game_id}:{opportunity_id}"


def snapshot_opportunity(
    session,
    *,
    game_id: str,
    opportunity_id: str,
) -> GameScoreOpportunityRecord:
    """Record one opportunity with its featured snapshot (insert-or-nothing).

    Intended to run inside the activation transaction: opening a session and
    snapshotting its featured status commit together, so the designation the
    award later multiplies is the one in force when play opened.
    """
    info = adapters.opportunity_info(session, game_id, opportunity_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No scoring opportunity {opportunity_id!r} for game {game_id!r}.",
        )
    is_featured = adapters.featured_on(session, game_id, info.activation_date)
    session.execute(
        pg_insert(GameScoreOpportunityRecord)
        .values(
            game_id=game_id,
            opportunity_id=opportunity_id,
            competition_type=info.competition_type,
            activation_date=info.activation_date,
            competition_at=info.competition_at,
            is_featured_snapshot=is_featured,
            scoring_version=SCORING_VERSION,
        )
        .on_conflict_do_nothing(
            constraint="uq_score_opportunity_game_event",
        )
    )
    row = session.scalar(
        select(GameScoreOpportunityRecord).where(
            GameScoreOpportunityRecord.game_id == game_id,
            GameScoreOpportunityRecord.opportunity_id == opportunity_id,
        )
    )
    assert row is not None, "the snapshot insert-or-nothing must leave the row"
    return row


def snapshot_opened_safely(session, *, game_id: str, opportunity_id: str) -> None:
    """Best-effort snapshot for a just-opened session or released question.

    Activation paths (scheduler opens, admin Updates, trivia releases) call
    this inside their own transaction so the snapshot commits atomically
    with the event it describes. Failures are logged, never raised: league
    bookkeeping must not break the lifecycle, and the due scan backfills
    any snapshot this misses.
    """
    try:
        snapshot_opportunity(session, game_id=game_id, opportunity_id=str(opportunity_id))
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "League snapshot failed for %s opportunity %s", game_id, opportunity_id
        )


def _ranked_awards(subjects: list, is_featured: bool) -> list[dict]:
    """Order subjects best-first, share tied positions' averaged points."""
    ordered = sorted(subjects, key=lambda item: item.order_key, reverse=True)
    total = len(ordered)
    awards: list[dict] = []
    index = 0
    while index < total:
        tie_key = ordered[index].tie_key
        group = []
        while index + len(group) < total and ordered[index + len(group)].tie_key == tie_key:
            group.append(ordered[index + len(group)])
        positions = list(range(index + 1, index + len(group) + 1))
        if len(group) == 1 and total == 1:
            base = base_points_for_rank(1, 1)
        elif len(group) == 1:
            base = base_points_for_rank(positions[0], total)
        else:
            base = averaged_tie_points(positions, total)
        final, multiplier = awarded_points(base, is_featured)
        for item in group:
            awards.append(
                {
                    "subject": item.subject,
                    "rank": positions[0],
                    "participant_count": total,
                    "base_points": base,
                    "multiplier": multiplier,
                    "awarded_points": final,
                    "raw_metrics": item.raw_metrics,
                }
            )
        index += len(group)
    return awards


def _write_awards(session, game_id: str, opportunity_id: str, snapshot) -> dict:
    """Rank the event's finalized results and persist one live award per subject.

    Shared by :func:`finalize_opportunity` and :func:`reprocess_opportunity` so the
    recompute cannot drift between the two paths. Runs inside the caller's
    transaction, under the caller's advisory lock: the per-subject existence check
    and insert cannot race a concurrent finalizer holding the same lock, and a
    duplicate request settles on the surviving live row.
    """
    subjects = adapters.finalized_results(session, game_id, opportunity_id)
    snapshot.finalized_at = _now()
    session.flush()
    if not subjects:
        return {
            "action": "no_results",
            "awards": 0,
            "participants": 0,
        }
    computed = _ranked_awards(subjects, bool(snapshot.is_featured_snapshot))
    written = 0
    for entry in computed:
        values: dict = {
            "game_id": game_id,
            "opportunity_id": opportunity_id,
            "competition_type": snapshot.competition_type,
            "raw_metrics": entry["raw_metrics"],
            "rank": entry["rank"],
            "participant_count": entry["participant_count"],
            "base_points": entry["base_points"],
            "featured_multiplier": entry["multiplier"],
            "awarded_points": entry["awarded_points"],
            "is_featured_snapshot": bool(snapshot.is_featured_snapshot),
            "competition_at": snapshot.competition_at,
            "scoring_version": SCORING_VERSION,
            "finalized_at": _now(),
        }
        if snapshot.competition_type == COMPETITION_COUPLE:
            member_a, member_b = entry["subject"]
            values["member_a_id"] = member_a
            values["member_b_id"] = member_b
            existing = session.scalar(
                select(GameScoreAwardRecord.id).where(
                    GameScoreAwardRecord.game_id == game_id,
                    GameScoreAwardRecord.opportunity_id == opportunity_id,
                    GameScoreAwardRecord.member_a_id == member_a,
                    GameScoreAwardRecord.member_b_id == member_b,
                    GameScoreAwardRecord.superseded_at.is_(None),
                )
            )
        else:
            (user_id,) = entry["subject"]
            values["user_id"] = user_id
            existing = session.scalar(
                select(GameScoreAwardRecord.id).where(
                    GameScoreAwardRecord.game_id == game_id,
                    GameScoreAwardRecord.opportunity_id == opportunity_id,
                    GameScoreAwardRecord.user_id == user_id,
                    GameScoreAwardRecord.superseded_at.is_(None),
                )
            )
        if existing is None:
            session.add(GameScoreAwardRecord(**values))
            written += 1
    return {
        "action": "finalized",
        "awards": written,
        "participants": len(subjects),
    }


def finalize_opportunity(db: Database, game_id: str, opportunity_id: str) -> dict:
    """Rank one closed opportunity and persist its awards (idempotent).

    Refuses events that are not closed in game terms: ranking while others
    can still submit would crown a first place too early. An event with no
    eligible results still records its snapshot but issues no awards.
    """
    if game_id not in GAME_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown game {game_id!r}.",
        )
    with db.sessions.begin() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": _lock_key(game_id, opportunity_id)},
        )
        if not adapters.is_event_closed(session, game_id, opportunity_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That event is not closed yet; awards are issued at closure.",
            )
        snapshot = snapshot_opportunity(
            session, game_id=game_id, opportunity_id=opportunity_id
        )
        outcome = _write_awards(session, game_id, opportunity_id, snapshot)
        return {
            "game_id": game_id,
            "opportunity_id": opportunity_id,
            "is_featured": bool(snapshot.is_featured_snapshot),
            **outcome,
        }


def finalize_due_opportunities(
    db: Database, game_ids: Optional[list[str]] = None
) -> dict[str, dict]:
    """Finalize every closed event still missing awards (the repair scan).

    The scheduler calls this after rolling sessions; the admin finalize
    endpoint calls it explicitly. Each opportunity finalizes under its own
    lock and transaction, so one unfinalizable event never blocks the rest.
    """
    summary: dict[str, dict] = {}
    for game_id in game_ids or list(GAME_IDS):
        snapshots = 0
        finalized = 0
        with db.sessions() as session:
            known = {
                str(opportunity_id)
                for (opportunity_id,) in session.execute(
                    select(GameScoreOpportunityRecord.opportunity_id).where(
                        GameScoreOpportunityRecord.game_id == game_id
                    )
                ).all()
            }
            due = adapters.pending_opportunities(session, game_id, known)
        for opportunity_id in due:
            with db.sessions.begin() as session:
                try:
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
                        {"key": _lock_key(game_id, opportunity_id)},
                    )
                    snapshot_opportunity(
                        session, game_id=game_id, opportunity_id=opportunity_id
                    )
                    snapshots += 1
                except HTTPException as exc:
                    # Skipped, not failed: an event with no scorable opportunity
                    # (a deleted session, a question that never released) stays
                    # skipped every pass. Silent skips made unfinalizable events
                    # invisible, so the reason travels to the log with the event.
                    import logging

                    logging.getLogger(__name__).warning(
                        "League due scan skipped snapshot for %s opportunity %s: %s",
                        game_id, opportunity_id, exc.detail,
                    )
                    continue
        with db.sessions() as session:
            missing = [
                str(opportunity_id)
                for (opportunity_id,) in session.execute(
                    select(GameScoreOpportunityRecord.opportunity_id).where(
                        GameScoreOpportunityRecord.game_id == game_id,
                        GameScoreOpportunityRecord.finalized_at.is_(None),
                    )
                ).all()
            ]
        for opportunity_id in missing:
            try:
                finalize_opportunity(db, game_id, opportunity_id)
                finalized += 1
            except HTTPException as exc:
                # Same visibility contract as the snapshot loop above: an event
                # that is still open (or otherwise unrankable) is retried on a
                # later pass, and each pass says so rather than swallowing it.
                import logging

                logging.getLogger(__name__).warning(
                    "League due scan skipped finalization for %s opportunity %s: %s",
                    game_id, opportunity_id, exc.detail,
                )
                continue
        summary[game_id] = {
            "snapshots": snapshots,
            "finalized": finalized,
        }
    return summary


def finalize_game_safely(db: Database, game_id: str) -> None:
    """Best-effort due scan for one game after a publishing action.

    Admin Updates and trivia releases close events deliberately; without
    this, the ended event would wait for the next reset run before its
    awards exist. Failures are logged, never raised: the due scan retries
    anything this misses on its next pass.
    """
    try:
        finalize_due_opportunities(db, [game_id])
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "League due scan failed after publish for %s", game_id
        )


def reprocess_opportunity(db: Database, game_id: str, opportunity_id: str) -> dict:
    """Supersede an opportunity's live awards and issue replacements.

    The explicit, auditable correction path: original rows stay in the
    ledger marked superseded, replacements carry a fresh ``finalized_at``,
    and leaderboard queries read live rows only. A later featured change
    never triggers this silently -- an operator calls it deliberately.

    Supersede and recompute commit atomically in one transaction (ENG-006):
    the old code superseded in one transaction and recomputed in a second, so
    a recompute that refused (an event that reopened, a result that vanished)
    left the originals superseded with no live awards until the next retry.
    Here any failure rolls both halves back and the board keeps showing the
    originals.
    """
    if game_id not in GAME_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown game {game_id!r}.",
        )
    with db.sessions.begin() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": _lock_key(game_id, opportunity_id)},
        )
        snapshot = session.scalar(
            select(GameScoreOpportunityRecord).where(
                GameScoreOpportunityRecord.game_id == game_id,
                GameScoreOpportunityRecord.opportunity_id == opportunity_id,
            )
        )
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That opportunity has no league snapshot to reprocess.",
            )
        live = session.scalars(
            select(GameScoreAwardRecord).where(
                GameScoreAwardRecord.game_id == game_id,
                GameScoreAwardRecord.opportunity_id == opportunity_id,
                GameScoreAwardRecord.superseded_at.is_(None),
            )
        ).all()
        if not live:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That opportunity has no live awards to reprocess.",
            )
        if not adapters.is_event_closed(session, game_id, opportunity_id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That event is not closed yet; awards are issued at closure.",
            )
        moment = _now()
        for row in live:
            row.superseded_at = moment
        session.flush()
        outcome = _write_awards(session, game_id, opportunity_id, snapshot)
        return {
            "game_id": game_id,
            "opportunity_id": opportunity_id,
            "is_featured": bool(snapshot.is_featured_snapshot),
            **outcome,
        }
