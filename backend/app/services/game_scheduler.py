"""The automatic session reset loop: calendar-driven, idempotent, UTC.

Runs at (or just after) 00:01 UTC and may safely run as often as wanted: every
game is evaluated against its own period boundary, and a run that finds the
current session still inside its period changes nothing. Creating is never
lazy -- user visits validate defensively, but this loop is what opens periods.

Per game, per run, under one transaction-scoped advisory lock:

* hidden game -> create nothing (existing rows are left alone; play is refused
  by the availability check even if an old session lingers);
* current session still inside its period -> keep it (a second run for the same
  boundary is a no-op, never a duplicate);
* current session is pending and its word has arrived (scheduled content for its
  period start, or the game now runs ``random``) -> end it and open a configured
  session for the *same* period;
 * boundary reached or no current session -> end the old one and open the next
   period starting today: random draws a word immediately, manual consumes a
   scheduled word when one waits for today and otherwise opens pending, the
   questionnaire opens a new global period and deals every existing couple a
   fresh set of questions, and Reaction Time opens a new bare period (no
   content to configure, so it is playable the moment it opens).
 * Trivia Drop has no period at all: the same run releases due scheduled
   questions through ``trivia_service.release_due_questions`` -- at most one
   activation per run, ending the live question in the same transaction --
   so one background path serves every game and no second scheduler exists.

 The partial unique index on each game table (one active-or-pending row) is the
 backstop behind the lock: a run racing an admin Update past the lock still
 lands one winner and one integrity error, and the loser re-reads the winner.
 """

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


def _finalize_case_closed_safely(db: Database, game_id: str) -> None:
    """Reveal one closed Case Closed event, logging rather than raising.

    Finalizing is what scores every session and makes the solution -- and
    who the culprit was -- visible. A failure here must never stop the
    period rollover; the stranded scan retries whatever this call misses.
    """
    import logging

    try:
        from . import case_closed_gameplay

        case_closed_gameplay.finalize_event(db, game_id)
    except Exception:
        logging.getLogger(__name__).exception(
            "Case Closed finalization failed for event %s", game_id
        )


def _finalize_stranded_case_closed(db: Database) -> None:
    """Finish Case Closed events that ended without ever revealing the case.

    An event that died between ending and finalizing would otherwise keep
    its solution hidden -- and every participant un-told -- forever. The
    scan runs inside the case-closed lock, so two scheduler passes can
    never race the same reveal.
    """
    with db.sessions() as session:
        stranded = [
            str(game_id)
            for (game_id,) in session.execute(
                select(CaseClosedGameRecord.id).where(
                    CaseClosedGameRecord.status == "ended",
                    CaseClosedGameRecord.finalized.is_(False),
                )
            ).all()
        ]
    for game_id in stranded:
        _finalize_case_closed_safely(db, game_id)


def _snapshot_league_opportunity(session, game_id: str, outcome: dict) -> None:
    """Snapshot a freshly opened session's league opportunity, same transaction.

    Opening the session and recording its featured designation commit
    together, so the multiplier later awards reflect the spotlight in force
    when play opened -- never a designation changed afterwards. Best-effort
    by design: league bookkeeping must never break the session lifecycle,
    and the due scan backfills any snapshot this misses.
    """
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
    """Pending scheduled words whose dates fall in ``[start, end)``, earliest first.

    A scheduled date does not need to equal the session's start date: any date
    inside the half-open window being created qualifies. Ordering is by
    scheduled date with the row id as the stable tie-break, so selection is
    deterministic no matter how many entries share the window.
    """
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
    """Delete pending scheduled words dated before ``cutoff``. Returns the count.

    Used after a window takes its content: past entries and in-window entries
    that lost the earliest-date selection can never fire in a later window,
    so they are removed rather than left to linger. Entries on or after the
    cutoff belong to future windows and are preserved.
    """
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
    """Activate the window's word: earliest in ``[start, end)``, then discard the rest.

    Returns the selected word (its row is deleted), or None when the window
    holds nothing. Non-selected rows dated before the window end can never
    fire later and are discarded; rows on or after the end belong to future
    windows and are preserved.
    """
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
    db: Database, game_id: str, now: Optional[datetime] = None
) -> dict:
    """Evaluate one game's boundary and open the next period when due.

    The defensive entry point the request paths use: cheap when the current
    session is still inside its period (one locked read), decisive when the
    boundary has passed. Never creates a session for a hidden game.

    Reaction Time shares the session lifecycle: its sessions carry no
    content (no word to draw or schedule), so a new period simply opens an
    empty active session -- the questionnaire's global period without the
    per-couple dealing.

    Trivia Drop releases individually rather than on any cadence, so it
    delegates to its own release entry point instead of the period logic
    below. The delegation keeps one background path -- this function, called
    by boot and by the run-resets trigger -- serving every game.
    """
    if game_id == "trivia-drop":
        from . import trivia_service

        return trivia_service.release_due_questions(db, _now_utc(now))
    if game_id == "case-closed":
        # Case Closed now plays: pin the newest published package like
        # Plot Twist. With no published package there is nothing to open.
        # Per-couple sessions are created lazily on entering.
        moment = _now_utc(now)
        today = moment.date()
        try:
            with db.sessions.begin() as session:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": game_lock_key(game_id)},
                )
                # Events that ended without their reveal (a crash between
                # ending and finalizing) still owe every participant the
                # culprit; finish them inside the lock before the rollover
                # below decides what today's event is.
                _finalize_stranded_case_closed(db)
                config = get_config(session, game_id)
                if config.is_hidden:
                    return {"game_id": game_id, "action": "skipped_hidden"}
                cadence = config.reset_cadence
                current = _current_row(session, CaseClosedGameRecord)
                if current is not None and not game_periods.is_reset_due(
                    current.period_end_date, today
                ):
                    return {
                        "game_id": game_id,
                        "action": "current",
                        "session_id": str(current.id),
                    }
                if current is not None:
                    # Closing the case is what reveals the solution: every
                    # session is scored, correctness becomes visible and the
                    # participants are told who the culprit was. Ending the
                    # row without this would hide the solution forever.
                    _finalize_case_closed_safely(db, str(current.id))
                    _end_row(current, moment)
                start, end = game_periods.period_for_start(today, cadence)
                from ..db_models import CaseClosedPackageRecord

                published = session.scalar(
                    select(CaseClosedPackageRecord)
                    .where(CaseClosedPackageRecord.status == "published")
                    .order_by(CaseClosedPackageRecord.published_at.desc())
                    .limit(1)
                )
                if published is None:
                    # Keep the ended row ended; no playable event to open.
                    return {"game_id": game_id, "action": "coming_soon"}
                import uuid as _uuid_mod

                pkg = dict(published.package or {})
                scoring = pkg.get("time_scoring_config") or {}
                opened = CaseClosedGameRecord(
                    game_date=start,
                    status="active",
                    period_start_date=start,
                    period_end_date=end,
                    case_package_id=str(published.id),
                    seed=_uuid_mod.uuid4().hex,
                    scoring_half_life_seconds=int(scoring.get("half_life_seconds") or 43200),
                    scoring_max_points=int(scoring.get("max_points") or 100),
                    started_at=moment,
                    created_by="scheduler",
                )
                session.add(opened)
                session.flush()
                outcome = {
                    "game_id": game_id,
                    "action": "created",
                    "session_id": str(opened.id),
                    "status": opened.status,
                    "case_package_id": str(published.id),
                    "period_start": start.isoformat(),
                    "period_end": end.isoformat(),
                }
                _snapshot_league_opportunity(session, game_id, outcome)
                return outcome
        except IntegrityError as exc:
            from psycopg.errors import UniqueViolation

            if not isinstance(exc.orig, UniqueViolation):
                raise
            with db.sessions() as session:
                winner = _current_row(session, CaseClosedGameRecord)
                return {
                    "game_id": game_id,
                    "action": "current",
                    "session_id": str(winner.id) if winner is not None else None,
                }
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
        # surface loudly rather than masquerading as "nothing to do".
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
    if game_id == "plot-twist":
        # Plot Twist: a new global period pinning the newest published story
        # package. Couples play that pinned version for the whole period.
        # (Publishing also rotates the event immediately onto the new package;
        # this boundary path only matters at cadence rollover.) Per-couple
        # sessions are created lazily on entering, so nothing rolls over.
        # With no published package there is nothing playable to open.
        from ..db_models import PlotTwistPackageRecord

        published = session.scalar(
            select(PlotTwistPackageRecord)
            .where(PlotTwistPackageRecord.status == "published")
            .order_by(PlotTwistPackageRecord.published_at.desc())
            .limit(1)
        )
        if published is None:
            return {"game_id": game_id, "action": "coming_soon"}
        import uuid as _uuid_mod

        opened = PlotTwistGameRecord(
            game_date=start,
            status="active",
            period_start_date=start,
            period_end_date=end,
            story_package_id=str(published.id),
            seed=_uuid_mod.uuid4().hex,
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
            "story_package_id": str(published.id),
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


def ensure_game_sessions(
    db: Database, now: Optional[datetime] = None
) -> dict[str, dict]:
    """Evaluate every game's boundary; the 00:01 UTC reset entry point.

    After rolling sessions, closed events still missing league awards are
    finalized as a side effect (best-effort, logged, never reflected in the
    returned summary): awards belong to ended opportunities only, so the
    boundary run is the existing closure signal rather than a new timer.

    Strict by design: an unexpected failure in one game aborts the pass, so a
    bug surfaces loudly at boot, on the manual trigger, and in tests. The
    unattended cron path uses :func:`run_reset_cycle` instead, which isolates
    per-game failures so one game's bug can never starve the rest.
    """
    summary = {game_id: ensure_single_game(db, game_id, now) for game_id in GAME_IDS}
    try:
        from . import league_finalization

        league_finalization.finalize_due_opportunities(db)
    except Exception:
        import logging

        logging.getLogger(__name__).exception("League due scan failed after reset run")
    return summary


def run_reset_cycle(db: Database, now: Optional[datetime] = None) -> dict:
    """One full reset pass with per-game failure isolation, for the cron path.

    Calls the same per-game entry point as boot and the manual trigger
    (:func:`ensure_single_game`), so every update-handling rule holds here:
    per-game advisory locks serialize a tick racing an admin Update, the
    partial unique index refuses a duplicate active session, pending sessions
    activate in-period, hidden games are skipped, Trivia Drop releases through
    its own entry point, and closed league events are finalized afterwards.

    The one deliberate difference from :func:`ensure_game_sessions`: an
    unexpected failure in one game is logged and recorded as
    ``action: "error"`` while the pass continues with the remaining games and
    the league scan, instead of aborting them. An unattended 00:01 tick must
    never let one game's bug starve the other six.
    """
    import logging

    moment = _now_utc(now)
    games: dict[str, dict] = {}
    for game_id in GAME_IDS:
        try:
            games[game_id] = ensure_single_game(db, game_id, moment)
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
    league: dict = {"action": "ok"}
    try:
        from . import league_finalization

        league_finalization.finalize_due_opportunities(db)
    except Exception as exc:
        logging.getLogger(__name__).exception("League due scan failed after reset run")
        league = {"action": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {"games": games, "league": league, "at": moment.isoformat()}


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
    return not (isinstance(league, dict) and league.get("action") == "error")


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
    """Forced re-anchor after an admin switched ``game_id`` to a multi-day cadence.

    Ends the live session (if any) and opens a fresh window under the new
    cadence starting now, consuming the earliest scheduled entry inside that
    window -- or opening pending when the window holds none. Every *other*
    pending scheduled entry is silently discarded, including ones dated after
    the new window: the rhythm they were planned under no longer exists, so
    carrying any of them forward would let a stale word activate unexpectedly
    in a later session.

    Returns the new session summary, or None when no re-anchor applies: games
    without scheduled words or puzzles keep the old behavior (the new cadence
    simply takes effect at the next boundary), as do hidden games (which must
    not open sessions at all), random-mode games (whose draws never consult
    schedules), and games with no live session to re-anchor.

    The session work itself delegates to the game's Update operation with no
    supplied content, so locking, terminate-then-create, league snapshots and
    duplicate protection are identical to a manual reset.
    """
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
