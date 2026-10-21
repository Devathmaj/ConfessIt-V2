"""The automatic session reset loop: calendar-driven, idempotent, UTC."""

import random
from datetime import date, datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, or_, select, text
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    AnagramGameRecord,
    CaseClosedGameRecord,
    ConnectionsGameRecord,
    ConnectionsScheduledPuzzleRecord,
    ConnectionsWordRecord,
    CoupleGameQuestionRecord,
    CoupleGameRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    GameConfigRecord,
    PlotTwistGameRecord,
    ReactionTimeGameRecord,
    ScheduledGameContentRecord,
    TwoKeyEscapeGameRecord,
    WordleGameRecord,
)
from ..limits import (
    COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
    COUPLE_SESSION_QUESTIONS,
    COUPLE_SESSION_SHARED_QUESTIONS,
    GAME_IDS,
)
from ..postgres import Database
from . import game_periods
from .game_configs import game_lock_key, get_config

_GAME_MODELS: dict[str, Any] = {
    "wordle": WordleGameRecord,
    "anagram": AnagramGameRecord,
    "couple-questionnaire": CoupleGameRecord,
    "reaction-time": ReactionTimeGameRecord,
    "connections": ConnectionsGameRecord,
    "two-key-escape": TwoKeyEscapeGameRecord,
    "plot-twist": PlotTwistGameRecord,
    "case-closed": CaseClosedGameRecord,
}


def _now_utc(now: Optional[datetime] = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _current_row(session, model):
    """The one active-or-pending session row, or None."""
    return session.scalar(
        select(model).where(model.status.in_(("active", "pending"))).limit(1)
    )


def _end_row(row, now: datetime) -> None:
    row.status = "ended"
    row.ended_at = now


def _snapshot_league_opportunity(session, game_id: str, outcome: dict) -> None:
    """Snapshot a freshly opened session's league opportunity, same transaction."""
    session_id = outcome.get("session_id") if isinstance(outcome, dict) else None
    if not session_id:
        return
    from . import league_finalization

    league_finalization.snapshot_opened_safely(
        session, game_id=game_id, opportunity_id=str(session_id)
    )


def _random_wordle_answer() -> str:
    from .wordle_service import solution_words

    return random.choice(solution_words())


def _random_anagram_answer() -> str:
    from .anagram_service import anagram_words
    from ..limits import ANAGRAM_RANDOM_MIN_LENGTH

    eligible = [word for word in anagram_words() if len(word) >= ANAGRAM_RANDOM_MIN_LENGTH]
    if not eligible:
        raise RuntimeError("The Anagram word list has no eligible word to draw.")
    return random.choice(eligible)


def _window_scheduled_words(session, game_id: str, start: date, end: date) -> list:
    """Pending scheduled words whose dates fall in ``[start, end)``, earliest first."""
    return session.scalars(
        select(ScheduledGameContentRecord)
        .where(
            ScheduledGameContentRecord.game_id == game_id,
            ScheduledGameContentRecord.target_date >= start,
            ScheduledGameContentRecord.target_date < end,
        )
        .order_by(
            ScheduledGameContentRecord.target_date.asc(),
            ScheduledGameContentRecord.id.asc(),
        )
    ).all()


def _discard_scheduled_words_before(session, game_id: str, cutoff: date) -> int:
    """Delete pending scheduled words dated before ``cutoff``. Returns the count."""
    rows = session.scalars(
        select(ScheduledGameContentRecord).where(
            ScheduledGameContentRecord.game_id == game_id,
            ScheduledGameContentRecord.target_date < cutoff,
        )
    ).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def _discard_all_scheduled_words(session, game_id: str) -> int:
    """Delete every pending scheduled word for ``game_id``. Returns the count.

    Only for forced re-anchoring (a cadence change redefining the rhythm):
    old dates no longer align with any future window, so carrying any of them
    forward would let a stale word activate unexpectedly later.
    """
    rows = session.scalars(
        select(ScheduledGameContentRecord).where(
            ScheduledGameContentRecord.game_id == game_id,
        )
    ).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def _window_scheduled_puzzles(session, start: date, end: date) -> list:
    """Pending scheduled Connections puzzles dated in ``[start, end)``, earliest first.

    Connections has no game column on its schedule table (it is the only
    writer), so the window alone scopes the selection. Same earliest-date,
    id-tie-break determinism as the word games.
    """
    return session.scalars(
        select(ConnectionsScheduledPuzzleRecord)
        .where(
            ConnectionsScheduledPuzzleRecord.target_date >= start,
            ConnectionsScheduledPuzzleRecord.target_date < end,
        )
        .order_by(
            ConnectionsScheduledPuzzleRecord.target_date.asc(),
            ConnectionsScheduledPuzzleRecord.id.asc(),
        )
    ).all()


def _discard_scheduled_puzzles_before(session, cutoff: date) -> int:
    """Delete pending scheduled puzzles dated before ``cutoff``. Returns the count."""
    rows = session.scalars(
        select(ConnectionsScheduledPuzzleRecord).where(
            ConnectionsScheduledPuzzleRecord.target_date < cutoff,
        )
    ).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def _discard_all_scheduled_puzzles(session) -> int:
    """Delete every pending scheduled Connections puzzle. Returns the count.

    Only for forced re-anchoring (a cadence change redefining the rhythm).
    """
    rows = session.scalars(select(ConnectionsScheduledPuzzleRecord)).all()
    for row in rows:
        session.delete(row)
    return len(rows)


def _consume_earliest_word(session, game_id: str, start: date, end: date) -> Optional[str]:
    """Activate the window's word: earliest in ``[start, end)``, then discard the rest."""
    rows = _window_scheduled_words(session, game_id, start, end)
    if not rows:
        return None
    word = rows[0].word
    session.delete(rows[0])
    _discard_scheduled_words_before(session, game_id, end)
    return word


def _consume_earliest_puzzle(session, start: date, end: date) -> Optional[list]:
    """Activate the window's Connections puzzle, then discard the rest, as above."""
    rows = _window_scheduled_puzzles(session, start, end)
    if not rows:
        return None
    groups = [list(group) for group in rows[0].groups]
    session.delete(rows[0])
    _discard_scheduled_puzzles_before(session, end)
    return groups


def _open_connections_session(
    session,
    *,
    groups: Optional[list],
    period_start: date,
    period_end: date,
    now: datetime,
    created_by: Optional[str] = None,
):
    """Insert one Connections session row plus its sixteen word rows (flush, no commit)."""
    row = ConnectionsGameRecord(
        game_date=period_start,
        status="active" if groups else "pending",
        period_start_date=period_start,
        period_end_date=period_end,
        started_at=now,
        created_by=created_by or "scheduler",
    )
    session.add(row)
    session.flush()
    if groups:
        for group_index, group in enumerate(groups):
            for position, display in enumerate(group):
                session.add(
                    ConnectionsWordRecord(
                        connections_game_id=str(row.id),
                        word_display=display,
                        word_normalized=display.strip().lower(),
                        group_index=group_index,
                        position_in_group=position,
                    )
                )
        session.flush()
    return row


def _open_word_session(
    session,
    model,
    *,
    game_id: str,
    mode: str,
    answer: Optional[str],
    period_start: date,
    period_end: date,
    now: datetime,
    created_by: Optional[str] = None,
):
    """Insert one word-session row and return it (flush, no commit)."""
    status_value = "active" if answer else "pending"
    values: dict[str, Any] = {
        "game_date": period_start,
        "mode": mode,
        "answer": answer,
        "status": status_value,
        "period_start_date": period_start,
        "period_end_date": period_end,
        "started_at": now,
    }
    if created_by is not None and hasattr(model, "created_by"):
        values["created_by"] = created_by
    row = model(**values)
    session.add(row)
    session.flush()
    return row


def deal_pair_questions(session, pair_session_id: str) -> list:
    """Draw a fresh 25-question set for one couple pair session.

    Five shared plus ten exclusive per player, drawn from the seeded bank.
    Shared by the scheduler rollover and by mutual-selection creation so the
    two paths can never deal different shapes.
    """
    bank_ids = [str(value) for value in session.scalars(select(CoupleGameQuestionRecord.id)).all()]
    if len(bank_ids) < COUPLE_SESSION_QUESTIONS:
        raise RuntimeError("The Couple question bank is not ready yet.")
    chosen = random.sample(bank_ids, COUPLE_SESSION_QUESTIONS)
    shared = set(random.sample(chosen, COUPLE_SESSION_SHARED_QUESTIONS))
    remaining = [question_id for question_id in chosen if question_id not in shared]
    random.shuffle(remaining)
    exclusive = COUPLE_SESSION_EXCLUSIVE_PER_PLAYER
    assignment = (
        [(question_id, "shared") for question_id in shared]
        + [(question_id, "user_a") for question_id in remaining[:exclusive]]
        + [(question_id, "user_b") for question_id in remaining[exclusive:]]
    )
    rows = [
        CoupleGameSessionQuestionRecord(
            session_id=str(pair_session_id),
            question_id=question_id,
            assignment=kind,
        )
        for question_id, kind in assignment
    ]
    session.add_all(rows)
    session.flush()
    return rows


def _roll_couple_pairs(session, previous_global_id: str, new_global_id: str) -> int:
    """Open a fresh pair session with fresh questions for every existing couple.

    Couples are the distinct pairs of the period that just ended. Progress never
    carries over: each new pair session starts unanswered with its own 25
    questions, and the old rows stay attached to the ended global session.
    """
    previous_pairs = session.scalars(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == str(previous_global_id)
        )
    ).all()
    created = 0
    seen: set[tuple[str, str]] = set()
    for pair in previous_pairs:
        key = (str(pair.user_a_id), str(pair.user_b_id))
        if key in seen:
            continue
        seen.add(key)
        new_pair = CoupleGameSessionRecord(
            couple_game_id=str(new_global_id),
            user_a_id=str(pair.user_a_id),
            user_b_id=str(pair.user_b_id),
            status="active",
        )
        session.add(new_pair)
        session.flush()
        deal_pair_questions(session, str(new_pair.id))
        created += 1
    return created


def ensure_single_game(
    db: Database, game_id: str, now: Optional[datetime] = None, *, startup: bool = False
) -> dict:
    """Evaluate one game's boundary and open the next period when due."""
    if game_id == "trivia-drop":
        from . import trivia_service

        return trivia_service.release_due_questions(db, _now_utc(now))
    if game_id in ("plot-twist", "case-closed"):
        from . import ai_game_automation

        return ai_game_automation.ensure_cycle(db, game_id, now, startup=startup)
    moment = _now_utc(now)
    today = moment.date()
    model = _GAME_MODELS[game_id]

    try:
        with db.sessions.begin() as session:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key(game_id)},
            )
            config = get_config(session, game_id)
            if config.is_hidden:
                return {"game_id": game_id, "action": "skipped_hidden"}
            cadence = config.reset_cadence
            current = _current_row(session, model)

            if current is not None and not game_periods.is_reset_due(
                current.period_end_date, today
            ):
                resolved = _maybe_activate_pending(
                    session, game_id, model, config, current, moment
                )
                if resolved is not None:
                    _snapshot_league_opportunity(session, game_id, resolved)
                    return resolved
                return {
                    "game_id": game_id,
                    "action": "current",
                    "session_id": str(current.id),
                }

            previous_global_id = str(current.id) if current is not None else None
            if current is not None:
                _end_row(current, moment)
            outcome = _open_period(
                session, game_id, model, config, cadence, today, moment,
                previous_global_id=previous_global_id,
            )
            _snapshot_league_opportunity(session, game_id, outcome)
            return outcome
    except IntegrityError as exc:
        # Only a duplicate-active race is benign: a concurrent run or an admin
        # Update won the insert past the lock, and the partial unique index
        # refused the duplicate -- re-read the winner. Any other integrity
        # failure (a NOT NULL, a CHECK, a foreign key) is a real bug and must
        from psycopg.errors import UniqueViolation

        if not isinstance(exc.orig, UniqueViolation):
            raise
        with db.sessions() as session:
            winner = _current_row(session, model)
            return {
                "game_id": game_id,
                "action": "current",
                "session_id": str(winner.id) if winner is not None else None,
            }


def _maybe_activate_pending(
    session, game_id: str, model, config, current, moment: datetime
) -> Optional[dict]:
    """Turn a pending session into a configured one when its word has arrived.

    The pending row ends and a new active row opens for the *same* period, so
    the cadence boundary never moves: the rule is terminate-then-create even
    here, never a silent mutation of the pending row.
    """
    if current.status != "pending" or game_id not in ("wordle", "anagram", "connections"):
        return None
    if game_id == "connections":
        groups = _consume_earliest_puzzle(
            session, current.period_start_date, current.period_end_date
        )
        if not groups:
            return None
        _end_row(current, moment)
        opened = _open_connections_session(
            session,
            groups=groups,
            period_start=current.period_start_date,
            period_end=current.period_end_date,
            now=moment,
            created_by="scheduler",
        )
        return {"game_id": game_id, "action": "activated", "session_id": str(opened.id)}
    content_mode = config.content_mode or "manual"
    session_mode = "random" if content_mode == "random" else "custom"
    word: Optional[str] = None
    if content_mode == "random":
        word = _random_wordle_answer() if game_id == "wordle" else _random_anagram_answer()
    else:
        word = _consume_earliest_word(
            session, game_id, current.period_start_date, current.period_end_date
        )
    if not word:
        return None
    _end_row(current, moment)
    opened = _open_word_session(
        session,
        model,
        game_id=game_id,
        mode=session_mode,
        answer=word,
        period_start=current.period_start_date,
        period_end=current.period_end_date,
        now=moment,
        created_by="scheduler",
    )
    return {"game_id": game_id, "action": "activated", "session_id": str(opened.id)}


def _open_period(
    session,
    game_id: str,
    model,
    config,
    cadence: str,
    today: date,
    moment: datetime,
    previous_global_id: Optional[str] = None,
) -> dict:
    """Open the period starting ``today`` for ``game_id`` and return its summary."""
    start, end = game_periods.period_for_start(today, cadence)
    if game_id in ("wordle", "anagram"):
        content_mode = config.content_mode or "manual"
        # Session ``mode`` is provenance (custom/random), not the content mode
        # (manual/random): a drawn word is random either way, while a scheduled
        # or later-typed word is custom.
        session_mode = "random" if content_mode == "random" else "custom"
        answer: Optional[str] = None
        if content_mode == "random":
            answer = (
                _random_wordle_answer()
                if game_id == "wordle"
                else _random_anagram_answer()
            )
        else:
            # Window-based consumption: any entry dated inside the new
            # window qualifies, earliest first; the rest below the window
            # end are discarded, later windows keep theirs.
            answer = _consume_earliest_word(session, game_id, start, end)
        opened = _open_word_session(
            session,
            model,
            game_id=game_id,
            mode=session_mode,
            answer=answer,
            period_start=start,
            period_end=end,
            now=moment,
            created_by="scheduler",
        )
        return {
            "game_id": game_id,
            "action": "created",
            "session_id": str(opened.id),
            "status": opened.status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    if game_id == "connections":
        # Connections is manual-only: consume the earliest scheduled puzzle
        # dated inside the new window, otherwise open a pending session that
        # stays unplayable until an administrator supplies its sixteen words.
        # Never draw, reuse or invent words.
        groups = _consume_earliest_puzzle(session, start, end)
        opened = _open_connections_session(
            session,
            groups=groups,
            period_start=start,
            period_end=end,
            now=moment,
            created_by="scheduler",
        )
        return {
            "game_id": game_id,
            "action": "created",
            "session_id": str(opened.id),
            "status": opened.status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    if game_id == "two-key-escape":
        # Two-Key Escape: a new global period with a fresh maze template.
        # The edge length is the stored grid-size configuration (persisted
        # across periods, defaulting to 12x12); per-couple games are created
        # lazily on first role submission, so nothing rolls over.
        from . import two_key_escape_gameplay

        opened = two_key_escape_gameplay.create_global_session(
            session,
            period_start=start,
            period_end=end,
            now=moment,
            created_by="scheduler",
        )
        return {
            "game_id": game_id,
            "action": "created",
            "session_id": str(opened.id),
            "status": opened.status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    if game_id == "reaction-time":
        # Reaction Time: a new bare period. There is no word to draw, no
        # schedule to consume and no manual mode to wait for -- the session
        # is always playable the moment it opens, and each session carries
        # its own five-submission budget per player.
        opened = ReactionTimeGameRecord(
            game_date=start,
            status="active",
            period_start_date=start,
            period_end_date=end,
            started_at=moment,
            created_by="scheduler",
        )
        session.add(opened)
        session.flush()
        return {
            "game_id": game_id,
            "action": "created",
            "session_id": str(opened.id),
            "status": opened.status,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
        }
    # Couple Questionnaire: a new global period, then a fresh pair session per
    # existing couple with a fresh question set each.
    opened = CoupleGameRecord(
        game_date=start,
        status="active",
        period_start_date=start,
        period_end_date=end,
        started_at=moment,
        created_by="scheduler",
    )
    session.add(opened)
    session.flush()
    rolled = 0
    if previous_global_id is not None:
        rolled = _roll_couple_pairs(session, previous_global_id, str(opened.id))
    return {
        "game_id": game_id,
        "action": "created",
        "session_id": str(opened.id),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "couples_rolled": rolled,
    }


def _notify_pending_session(
    db: Database, game_id: str, moment: datetime
) -> None:
    """Record one admin notice when this game's current session is pending.

    Only the scheduler path calls this, so the notice carries the timestamp of
    the pass rather than an earlier boot that happened to create the session.
    Repeated passes for the same session are idempotent on the session id.
    """
    if game_id not in ("wordle", "anagram", "connections"):
        return
    try:
        with db.sessions() as session:
            config = session.scalar(
                select(GameConfigRecord).where(GameConfigRecord.game_id == game_id)
            )
            if config is not None and (config.is_hidden or not config.enabled):
                return
            if game_id in ("wordle", "anagram"):
                content_mode = (config.content_mode if config is not None else None) or "manual"
                if content_mode != "manual":
                    return
            current = _current_row(session, _GAME_MODELS[game_id])
            if current is None or current.status != "pending":
                return
            session_id = str(current.id)
        from .staff_notification_service import notify_game_pending

        notify_game_pending(
            db, game_id=game_id, session_id=session_id, created_at=moment
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Could not record the pending-session notice for %s", game_id
        )


def ensure_game_sessions(
    db: Database, now: Optional[datetime] = None, *, startup: bool = False
) -> dict[str, dict]:
    """Evaluate every game's boundary; the 00:01 UTC reset entry point."""
    summary = {}
    for game_id in GAME_IDS:
        if startup and game_id in ("plot-twist", "case-closed"):
            summary[game_id] = ensure_single_game(db, game_id, now, startup=True)
        else:
            summary[game_id] = ensure_single_game(db, game_id, now)
    try:
        from . import league_finalization

        league_finalization.finalize_due_opportunities(db)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("League due scan failed after reset run")
    return summary


def run_reset_cycle(db: Database, now: Optional[datetime] = None) -> dict:
    """One full reset pass with per-game failure isolation, for the cron path."""
    import logging

    moment = _now_utc(now)
    games: dict[str, dict] = {}
    for game_id in GAME_IDS:
        try:
            games[game_id] = ensure_single_game(db, game_id, moment)
            # Notice timestamp is this pass, not an earlier boot (see
            # _notify_pending_session).
            _notify_pending_session(db, game_id, moment)
        except Exception as exc:
            logging.getLogger(__name__).exception(
                "Scheduled reset failed for game %s; continuing with the rest",
                game_id,
            )
            games[game_id] = {
                "game_id": game_id,
                "action": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
    # A featured pick is a date row, so the daily pass is the only place that
    # can notice today has none. Best-effort: the reset itself has committed.
    try:
        from .staff_notification_service import notify_featured_game_missing

        notify_featured_game_missing(db, now=moment)
    except Exception:
        logging.getLogger(__name__).exception(
            "Could not record the missing-featured-game notice"
        )
    league: dict = {"action": "ok"}
    try:
        from . import league_finalization

        league_finalization.finalize_due_opportunities(db)
    except Exception as exc:
        logging.getLogger(__name__).exception("League due scan failed after reset run")
        league = {"action": "error", "error": f"{type(exc).__name__}: {exc}"}

    # Chat retention rides the same daily tick. It needs only the database and
    # the indexed deadlines already stored on each row -- never the encryption
    # keys -- so the scheduler can run it without being a trusted decryptor.
    # Failure is isolated like a game failure: the rest of the pass already
    chat_cleanup: dict = {"action": "ok"}
    try:
        from .chat_retention_service import cleanup_expired_chat_messages

        result = cleanup_expired_chat_messages(db, now=moment)
        chat_cleanup = {"action": "ok", **result}
    except Exception as exc:
        logging.getLogger(__name__).exception("Chat retention cleanup failed after reset run")
        chat_cleanup = {"action": "error", "error": f"{type(exc).__name__}: {exc}"}
    summary = {
        "games": games,
        "league": league,
        "chat_cleanup": chat_cleanup,
        "at": moment.isoformat(),
    }

    # The pass records its own heartbeat (see reset_heartbeat), so a tick that
    # stops running leaves a trace instead of silently stalling the rollovers.
    # The cron path is the only writer. Best-effort: the work is already
    # committed here, and a heartbeat that cannot be written must not fail a
    try:
        from .reset_heartbeat import record_reset_outcome

        record_reset_outcome(
            db,
            succeeded=reset_cycle_succeeded(summary),
            error=_reset_cycle_error(summary),
            now=moment,
        )
    except Exception:
        logging.getLogger(__name__).exception("Could not record the reset heartbeat")

    return summary


def _reset_cycle_error(summary: dict) -> Optional[str]:
    """A short description of why a pass failed, for the heartbeat row.

    Every failure the pass isolated, in one line: which game or stage, and
    what it said. The writer clips to the column's width.
    """
    parts = []
    for game_id, info in (summary.get("games") or {}).items():
        if isinstance(info, dict) and info.get("action") == "error":
            parts.append(f"{game_id}: {info.get('error')}")
    for stage in ("league", "chat_cleanup"):
        info = summary.get(stage)
        if isinstance(info, dict) and info.get("action") == "error":
            parts.append(f"{stage}: {info.get('error')}")
    return "; ".join(parts) or None


def reset_cycle_succeeded(summary: dict) -> bool:
    """Whether a :func:`run_reset_cycle` pass finished without any failure."""
    if not isinstance(summary, dict):
        return False
    games = summary.get("games")
    if not isinstance(games, dict):
        return False
    if any(
        isinstance(info, dict) and info.get("action") == "error"
        for info in games.values()
    ):
        return False
    league = summary.get("league")
    if isinstance(league, dict) and league.get("action") == "error":
        return False
    chat_cleanup = summary.get("chat_cleanup")
    return not (isinstance(chat_cleanup, dict) and chat_cleanup.get("action") == "error")


def has_current_session(db: Database, game_id: str) -> bool:
    """Whether ``game_id`` holds an active-or-pending session row right now."""
    model = _GAME_MODELS.get(game_id)
    if model is None:
        return False
    with db.sessions() as session:
        return _current_row(session, model) is not None


def reanchor_for_cadence_change(
    db: Database, game_id: str, *, now: Optional[datetime] = None
) -> Optional[dict]:
    """Forced re-anchor after an admin switched ``game_id`` to a multi-day cadence."""
    if game_id not in ("wordle", "anagram", "connections"):
        return None
    moment = _now_utc(now)
    with db.sessions() as session:
        config = get_config(session, game_id)
        if config.is_hidden:
            return None
        if (config.content_mode or "manual") != "manual":
            return None
        model = _GAME_MODELS[game_id]
        if _current_row(session, model) is None:
            return None
    if game_id == "wordle":
        from .wordle_service import update_wordle_session

        result = update_wordle_session(
            db,
            mode=None,
            word=None,
            token=None,
            now=moment,
            discard_future_schedules=True,
        )
        return {"game_id": game_id, "action": "reanchored", "session_id": result.id}
    if game_id == "anagram":
        from .anagram_service import update_anagram_session

        result = update_anagram_session(
            db,
            mode=None,
            word=None,
            token=None,
            updated_by="admin",
            now=moment,
            discard_future_schedules=True,
        )
        return {"game_id": game_id, "action": "reanchored", "session_id": result.id}
    from .connections_service import update_connections_session

    result = update_connections_session(
        db, groups=None, updated_by="admin", now=moment, discard_future_schedules=True
    )
    return {"game_id": game_id, "action": "reanchored", "session_id": result.id}


def active_session_counts(db: Database) -> dict[str, int]:
    """How many live sessions each game table holds (observability).

    Period games count active-or-pending rows; Trivia Drop counts the one live
    question, since drafts and scheduled questions are not sessions yet.
    """
    from ..db_models import TriviaDropQuestionRecord

    with db.sessions() as session:
        out: dict[str, int] = {}
        for game_id, model in _GAME_MODELS.items():
            out[game_id] = int(
                session.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.status.in_(("active", "pending")))
                )
                or 0
            )
        out["trivia-drop"] = int(
            session.scalar(
                select(func.count())
                .select_from(TriviaDropQuestionRecord)
                .where(TriviaDropQuestionRecord.status == "active")
            )
            or 0
        )
        return out
