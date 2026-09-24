"""Playing the Reaction Time game: the participant-facing half of the feature.

Sessions are opened by the reset loop under the game's own cadence and are
independent of the featured spotlight: a session exists -- and is playable --
whether or not Reaction Time is featured today, exactly like the other
games. This module is everything that happens *afterwards*: reading the
current session's game without creating anything, consuming one of the five
per-session submissions on the blue-screen click, and settling the pending
attempt as a valid reaction or a false start.

Four rules hold at the bottom of everything here.

**One start click is one consumed submission.** The pending row is born the
moment the blue screen is clicked -- ``POST /start`` inserts it -- so the
submission is spent before the red wait even begins. A false start still
consumed its submission, because the attempt had already started; only
completed valid reactions count toward best/average.

**Five submissions per user per session, enforced in the database.** The
attempt number is assigned server-side from the stored count, and a
``UniqueConstraint(game, user, attempt_number)`` plus an
``attempt_number BETWEEN 1 AND 5`` check hold the shape. Creation runs under
a transaction-scoped advisory lock keyed by session and user, so two rapid
starts serialize instead of both reading "four used" and minting attempts
five and six. A new session means a fresh budget of five.

**Completed rows are immutable.** Settling a pending attempt writes it once;
a second submit for the same id is refused rather than overwritten, so a
``too_early`` can never become ``valid`` and a reaction time can never be
edited after the fact.

**Statistics come from the rows, never from the client.** Best is the minimum
valid reaction time, average the mean of valid reaction times only, and both
are null when the caller has no valid attempt. The frontend measures with a
high-resolution clock; the backend validates the bounds and persists.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from ..db_models import ReactionTimeGameRecord, ReactionTimeSubmissionRecord
from ..limits import (
    REACTION_TIME_MAX_ELAPSED_MS,
    REACTION_TIME_MAX_SUBMISSIONS,
    REACTION_TIME_MAX_VALID_MS,
    REACTION_TIME_MIN_VALID_MS,
)
from ..models import (
    ReactionTimeAttempt,
    ReactionTimeCompleteRequest,
    ReactionTimeStartRequest,
    ReactionTimeState,
)
from ..postgres import Database, is_valid_id


def _today():
    """Today's date in UTC, the same clock every other service in the app uses."""
    return datetime.now(timezone.utc).date()


def _as_attempt(row: ReactionTimeSubmissionRecord) -> ReactionTimeAttempt:
    return ReactionTimeAttempt(
        submission_id=str(row.id),
        attempt_number=row.attempt_number,
        status=row.status,
        reaction_time_ms=row.reaction_time_ms,
        elapsed_ms=row.elapsed_ms,
        started_at=row.started_at,
        green_at=row.green_at,
        clicked_at=row.clicked_at,
        created_at=row.created_at,
    )


def _build_state(
    game: Optional[ReactionTimeGameRecord],
    rows: list[ReactionTimeSubmissionRecord],
) -> ReactionTimeState:
    """Derive the caller's place in the current session from the stored rows."""
    ordered = sorted(rows, key=lambda row: row.attempt_number)
    attempts = [_as_attempt(row) for row in ordered]
    valid = [
        row.reaction_time_ms
        for row in ordered
        if row.status == "valid" and row.reaction_time_ms is not None
    ]
    false_starts = sum(1 for row in ordered if row.status == "too_early")
    pending = next((item for item in attempts if item.status == "pending"), None)
    used = len(ordered)
    best = float(min(valid)) if valid else None
    average = float(sum(valid) / len(valid)) if valid else None
    return ReactionTimeState(
        game_id=str(game.id) if game is not None else None,
        game_date=game.game_date if game is not None else None,
        max_submissions=REACTION_TIME_MAX_SUBMISSIONS,
        submissions_used=used,
        submissions_remaining=max(0, REACTION_TIME_MAX_SUBMISSIONS - used),
        is_finished=used >= REACTION_TIME_MAX_SUBMISSIONS,
        attempts=attempts,
        valid_count=len(valid),
        false_start_count=false_starts,
        best_ms=best,
        average_ms=average,
        pending_submission=pending,
    )


def _empty_state() -> ReactionTimeState:
    return ReactionTimeState(
        game_id=None,
        game_date=None,
        max_submissions=REACTION_TIME_MAX_SUBMISSIONS,
        submissions_used=0,
        submissions_remaining=REACTION_TIME_MAX_SUBMISSIONS,
        is_finished=False,
        attempts=[],
        valid_count=0,
        false_start_count=0,
        best_ms=None,
        average_ms=None,
        pending_submission=None,
    )


def _current_session_row(session) -> Optional[ReactionTimeGameRecord]:
    """The one active-or-pending session, or None when the game has none."""
    return session.scalar(
        select(ReactionTimeGameRecord)
        .where(ReactionTimeGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )


def _playable_session(session) -> Optional[ReactionTimeGameRecord]:
    """The current session when it is active and its period has begun.

    Sessions are opened by the reset loop under the game's own cadence and
    are independent of the featured spotlight: a session exists whether or
    not Reaction Time is featured today. Availability is enforced by the
    callers through ``check_playable`` before this is ever consulted.
    """
    from . import game_periods

    game = _current_session_row(session)
    if game is None or game.status != "active":
        return None
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        return None
    if game_periods.is_reset_due(game.period_end_date, _today()):
        return None
    return game


def _lock(session, game_id: str, user_id: str) -> None:
    """Serialize attempt writes for one caller on one session.

    Counting rows does not lock the gap a concurrent insert would fill, so
    two starts racing past the same count need a lock they both agree on. A
    transaction-scoped advisory lock keyed by session and user is that
    agreement: the second starter waits for the first one's commit, then
    counts the row the winner left behind.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"reaction-time:{game_id}:{user_id}"},
    )


def current_state(db: Database, user_id: str) -> ReactionTimeState:
    """This caller's place in the current Reaction Time session.

    Reading this never creates anything: a fresh caller reads zero
    submissions and a blue start screen, a returning caller reads the
    submissions they left -- a refresh, a navigation away and back, or a
    logout and return all resume from the stored rows rather than from
    frontend memory. A game with no session reads as an empty state; reads
    never create sessions.

    Availability is enforced before anything else: hidden and disabled games
    refuse with a coded 403 rather than a state.
    """
    from .game_configs import check_playable

    # Availability first, in its own committed transaction: hidden and
    # disabled games refuse here, and the config row is persisted exactly
    # once.
    with db.sessions.begin() as session:
        check_playable(session, "reaction-time")

    with db.sessions() as session:
        game = _playable_session(session)
        if game is None:
            return _empty_state()
        rows = session.scalars(
            select(ReactionTimeSubmissionRecord)
            .where(
                ReactionTimeSubmissionRecord.reaction_time_game_id == game.id,
                ReactionTimeSubmissionRecord.user_id == user_id,
            )
            .order_by(ReactionTimeSubmissionRecord.attempt_number.asc())
        ).all()
        return _build_state(game, list(rows))


def start_attempt(
    db: Database, user_id: str, payload: ReactionTimeStartRequest
) -> ReactionTimeState:
    """Consume one submission: the blue-screen click that starts an attempt.

    The pending row is inserted inside the same transaction that counted the
    caller's existing submissions, under the per-caller advisory lock -- so a
    refresh, a remount, a direct API call or two concurrent requests can never
    mint a sixth attempt. A caller with a still-pending attempt is refused
    until that attempt is settled: the pending row already spent its
    submission, and a new row would spend another one for the same click
    sequence. Only the current session accepts starts; a stale screen naming
    an ended session is refused.
    """
    if not is_valid_id(payload.game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Reaction Time game id",
        )
    try:
        with db.sessions.begin() as session:
            from . import game_periods
            from .game_configs import check_playable

            check_playable(session, "reaction-time")
            game = session.get(ReactionTimeGameRecord, str(payload.game_id))
            if game is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Reaction Time game not found",
                )
            if game.status != "active":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That session has ended. Open the current game to keep playing.",
                )
            start = game.period_start_date or game.game_date
            if start is not None and start > _today():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="That game has not started yet.",
                )
            if game_periods.is_reset_due(game.period_end_date, _today()):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That session has ended. Open the current game to keep playing.",
                )
            current = _current_session_row(session)
            if current is None or str(current.id) != str(game.id):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That session has ended. Open the current game to keep playing.",
                )
            _lock(session, str(game.id), user_id)
            rows = session.scalars(
                select(ReactionTimeSubmissionRecord)
                .where(
                    ReactionTimeSubmissionRecord.reaction_time_game_id == game.id,
                    ReactionTimeSubmissionRecord.user_id == user_id,
                )
                .order_by(ReactionTimeSubmissionRecord.attempt_number.asc())
            ).all()
            rows = list(rows)
            pending = next(
                (row for row in rows if row.status == "pending"), None
            )
            if pending is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Finish your current attempt before starting another.",
                )
            if len(rows) >= REACTION_TIME_MAX_SUBMISSIONS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="No attempts left for this game.",
                )
            now = datetime.now(timezone.utc)
            row = ReactionTimeSubmissionRecord(
                reaction_time_game_id=str(game.id),
                user_id=user_id,
                attempt_number=len(rows) + 1,
                status="pending",
                started_at=now,
            )
            session.add(row)
            session.flush()
            return _build_state(game, [*rows, row])
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No attempts left for this game.",
        ) from exc


def complete_attempt(
    db: Database, user_id: str, payload: ReactionTimeCompleteRequest
) -> ReactionTimeState:
    """Settle a pending attempt with its measured result, exactly once.

    Ownership comes from the authenticated caller, never from the payload:
    the submission must belong to this user, must still be pending, and must
    belong to the current session -- a session that has since rolled leaves
    its pendings behind rather than settling them into history. A settled
    row is never rewritten. ``valid`` requires a reaction time inside the
    honest bounds; ``too_early`` stores only informational elapsed time that
    is excluded from every statistic.
    """
    if not is_valid_id(payload.submission_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Reaction Time submission id",
        )
    if payload.status not in ("valid", "too_early"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown result status.",
        )
    if payload.status == "valid":
        ms = payload.reaction_time_ms
        if (
            not isinstance(ms, int)
            or isinstance(ms, bool)
            or not (REACTION_TIME_MIN_VALID_MS <= ms <= REACTION_TIME_MAX_VALID_MS)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"A reaction time is {REACTION_TIME_MIN_VALID_MS}.."
                    f"{REACTION_TIME_MAX_VALID_MS} milliseconds."
                ),
            )
    else:
        elapsed = payload.elapsed_ms
        if elapsed is not None and (
            not isinstance(elapsed, int)
            or isinstance(elapsed, bool)
            or not (0 <= elapsed <= REACTION_TIME_MAX_ELAPSED_MS)
        ):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Elapsed time is 0.."
                    f"{REACTION_TIME_MAX_ELAPSED_MS} milliseconds."
                ),
            )
    with db.sessions.begin() as session:
        from .game_configs import check_playable

        check_playable(session, "reaction-time")
        row = session.get(
            ReactionTimeSubmissionRecord, str(payload.submission_id)
        )
        if row is None or str(row.user_id) != str(user_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Reaction Time submission not found",
            )
        if row.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That attempt is already recorded.",
            )
        game = session.get(
            ReactionTimeGameRecord, str(row.reaction_time_game_id)
        )
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Reaction Time game not found",
            )
        _lock(session, str(game.id), user_id)
        # Re-read under the lock: a concurrent settle racing this one must
        # see the winner's write rather than settling the same row twice.
        session.refresh(row)
        if row.status != "pending":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That attempt is already recorded.",
            )
        current = _playable_session(session)
        if current is None or str(current.id) != str(game.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That session has ended. Open the current game to keep playing.",
            )
        now = datetime.now(timezone.utc)
        if payload.status == "valid":
            row.status = "valid"
            row.reaction_time_ms = payload.reaction_time_ms
            row.clicked_at = payload.clicked_at or now
            row.green_at = payload.green_at or row.clicked_at
        else:
            row.status = "too_early"
            row.elapsed_ms = payload.elapsed_ms
            row.clicked_at = payload.clicked_at or now
        session.flush()
        rows = session.scalars(
            select(ReactionTimeSubmissionRecord)
            .where(
                ReactionTimeSubmissionRecord.reaction_time_game_id == game.id,
                ReactionTimeSubmissionRecord.user_id == user_id,
            )
            .order_by(ReactionTimeSubmissionRecord.attempt_number.asc())
        ).all()
        return _build_state(game, list(rows))
