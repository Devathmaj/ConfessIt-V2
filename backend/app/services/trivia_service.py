"""Configuring Trivia Drop questions: text, options, key, and release.

The rows this writes are *questions* -- text, four options, which of them are
correct -- and each released question is its own session. There is no attempt,
no selection and no score here, because none of those exist yet: every write
below is an administrator publishing something, and none of it is ever
triggered by a participant.

**There is no reset cadence and no random mode.** Each question carries its
own release: ``now`` deploys immediately (ending any live question),
``scheduled`` waits for the release loop, ``draft`` waits for an explicit
decision. A question with no release stays invisible -- the scheduler never
invents content, and the player surface never serves a board for one.

**The answer mode is derived, never configured.** Exactly one correct option
is a single-answer question, more than one is multiple-answer. No payload
carries a mode, so no mode can disagree with the key.

**Live and answered questions are immutable.** Only a draft with no
submissions may be edited; anything released or answered is history, and
corrections arrive as a new question. Scheduling never ends the current
session early -- the previous question ends when the new one actually goes
live, in the same transaction, so the two are never live together.
"""

from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    TriviaDropOptionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
)
from ..limits import (
    TRIVIA_OPTIONS_COUNT,
    TRIVIA_OPTION_TEXT_MAX,
    TRIVIA_QUESTION_TEXT_MAX,
)
from ..models import TriviaDropQuestion
from ..postgres import Database, is_valid_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(moment: datetime) -> datetime:
    """Interpret a naive timestamp as UTC; pass aware ones through.

    The admin screen labels its release control as UTC, so a naive
    ``datetime-local`` value converts consistently rather than by the
    operator's local clock. Storage stays UTC end to end.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def answer_mode_for(correct_count: int) -> str:
    """The derived mode: one correct is single-answer, several is multiple."""
    return "single" if correct_count == 1 else "multiple"


def validate_question_text(raw: object) -> str:
    """The trimmed question text, or why it cannot be published."""
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The question text must not be empty.",
        )
    text = raw.strip()
    if len(text) > TRIVIA_QUESTION_TEXT_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"The question is at most {TRIVIA_QUESTION_TEXT_MAX} characters.",
        )
    return text


def validate_options(raw_options: object) -> List[str]:
    """Four trimmed, distinct option texts, or why they cannot be published.

    Duplicates are compared after trimming and lowercasing, so "Mars",
    " mars " and "MARS" are one option rather than three.
    """
    if not isinstance(raw_options, list) or len(raw_options) != TRIVIA_OPTIONS_COUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"A question has exactly {TRIVIA_OPTIONS_COUNT} options.",
        )
    cleaned: List[str] = []
    seen: set[str] = set()
    for raw in raw_options:
        if not isinstance(raw, str) or not raw.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="All four options must be present and non-empty.",
            )
        text = raw.strip()
        if len(text) > TRIVIA_OPTION_TEXT_MAX:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Each option is at most {TRIVIA_OPTION_TEXT_MAX} characters.",
            )
        normalized = text.lower()
        if normalized in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Duplicate option {text!r}: each option may appear only once.",
            )
        seen.add(normalized)
        cleaned.append(text)
    return cleaned


def validate_correct_indexes(raw: object) -> List[int]:
    """The correct-option set as sorted unique indexes, or why it is invalid.

    At least one correct option is required; every index must name one of the
    four options. Duplicates are normalized away -- naming an option twice is
    the same key stated twice, not a second correct answer.
    """
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Designate at least one correct option.",
        )
    indexes: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each correct option must name one of the four options.",
            )
        if entry < 0 or entry >= TRIVIA_OPTIONS_COUNT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each correct option must name one of the four options.",
            )
        indexes.add(entry)
    return sorted(indexes)


def _as_model(
    row: TriviaDropQuestionRecord, options: List[TriviaDropOptionRecord]
) -> TriviaDropQuestion:
    """A question as the admin screen reads it, with its key and derived mode."""
    ordered = sorted(options, key=lambda option: option.option_index)
    correct = [option.option_index for option in ordered if option.is_correct]
    return TriviaDropQuestion(
        id=str(row.id),
        question_text=row.question_text,
        options=[
            {
                "option_index": option.option_index,
                "option_text": option.option_text,
                "is_correct": bool(option.is_correct),
            }
            for option in ordered
        ],
        correct_indexes=correct,
        answer_mode=answer_mode_for(len(correct)),
        status=row.status or "draft",
        scheduled_for=row.scheduled_for,
        released_at=row.released_at,
        ended_at=row.ended_at,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _active_row(session) -> Optional[TriviaDropQuestionRecord]:
    """The one live question, or None. The partial unique index says there is
    at most one; this read never assumes it, it just takes the first."""
    return session.scalar(
        select(TriviaDropQuestionRecord)
        .where(TriviaDropQuestionRecord.status == "active")
        .limit(1)
    )


def _get_row(session, question_id: str) -> TriviaDropQuestionRecord:
    """One question by id, or why it cannot be named.

    A malformed id is a 400 and a well-formed one that matches nothing is a
    404, so a typo in a URL and a withdrawn question are told apart rather
    than both reported as "not found".
    """
    if not is_valid_id(question_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Trivia Drop question id"
        )
    row = session.get(TriviaDropQuestionRecord, str(question_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trivia Drop question not found"
        )
    return row


def _options_for(session, question_id: str) -> List[TriviaDropOptionRecord]:
    return list(
        session.scalars(
            select(TriviaDropOptionRecord).where(
                TriviaDropOptionRecord.question_id == question_id
            )
        ).all()
    )


def _end_row(row: TriviaDropQuestionRecord, moment: datetime) -> None:
    row.status = "ended"
    row.ended_at = moment


def _attach_options(session, question_id: str, options: List[str], correct: List[int]) -> None:
    correct_set = set(correct)
    for index, text in enumerate(options):
        session.add(
            TriviaDropOptionRecord(
                question_id=str(question_id),
                option_index=index,
                option_text=text,
                is_correct=index in correct_set,
            )
        )
    session.flush()


def _activate(session, row: TriviaDropQuestionRecord, moment: datetime) -> None:
    """End the live question, if any, and activate ``row`` -- atomically.

    The previous session stops accepting answers in the same transaction the
    new one goes live, so there is no window in which both accept answers and
    no window in which the game is accidentally dark. The caller holds the
    game's advisory lock; the partial unique index is the backstop behind it.

    The end is flushed before the activation is written: the unit of work
    makes no promise about statement order inside one flush, and an INSERT of
    the new live row emitted before the UPDATE ending the old one would trip
    the single-active index on a transition that is perfectly legal.
    """
    current = _active_row(session)
    if current is not None and str(current.id) != str(row.id):
        _end_row(current, moment)
        session.flush()
    row.status = "active"
    row.released_at = moment
    row.ended_at = None
    session.flush()


def _check_schedule_conflict(session, scheduled_for: datetime, exclude_id: Optional[str] = None) -> None:
    """Refuse a release time another scheduled question already holds.

    Two questions for one instant would race the activation -- one replacing
    the other within the same run -- so the ambiguity is refused up front and
    the administrator picks distinct times instead.
    """
    query = select(TriviaDropQuestionRecord).where(
        TriviaDropQuestionRecord.status == "scheduled",
        TriviaDropQuestionRecord.scheduled_for == scheduled_for,
    )
    if exclude_id is not None:
        query = query.where(TriviaDropQuestionRecord.id != str(exclude_id))
    clash = session.scalar(query.limit(1))
    if clash is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another question is already scheduled for that release time.",
        )


def create_question(
    db: Database,
    *,
    question_text: str,
    options: List[str],
    correct_indexes: List[int],
    release_mode: str = "draft",
    scheduled_for: Optional[datetime] = None,
    created_by: str,
    now: Optional[datetime] = None,
) -> TriviaDropQuestion:
    """Author one question: store it, and optionally release it right away.

    ``release_mode="now"`` validates everything and deploys in the same
    transaction, ending any live question. ``"scheduled"`` needs a future
    ``scheduled_for`` and waits for the release loop. ``"draft"`` stores it
    for later. The whole exchange runs under the game's advisory lock.
    """
    from sqlalchemy import text as _text

    from .game_configs import game_lock_key

    cleaned_text = validate_question_text(question_text)
    cleaned_options = validate_options(options)
    cleaned_correct = validate_correct_indexes(correct_indexes)
    moment = now or _now()
    release_at: Optional[datetime] = None
    if release_mode == "scheduled":
        if scheduled_for is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A scheduled release needs a release date and time.",
            )
        release_at = _as_utc(scheduled_for)
        if release_at <= moment:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A scheduled release must be in the future.",
            )
    elif release_mode not in ("now", "draft"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unknown release mode. Deploy now, schedule, or save as draft.",
        )

    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("trivia-drop")},
            )
            if release_at is not None:
                _check_schedule_conflict(session, release_at)
            row = TriviaDropQuestionRecord(
                question_text=cleaned_text,
                status="scheduled" if release_at is not None else "draft",
                scheduled_for=release_at,
                created_by=created_by,
            )
            session.add(row)
            session.flush()
            _attach_options(session, str(row.id), cleaned_options, cleaned_correct)
            if release_mode == "now":
                _activate(session, row, moment)
            return _as_model(row, _options_for(session, str(row.id)))
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Trivia Drop release collided with a concurrent change. Try again.",
        ) from exc


def update_draft(
    db: Database,
    question_id: str,
    *,
    question_text: Optional[str] = None,
    options: Optional[List[str]] = None,
    correct_indexes: Optional[List[int]] = None,
) -> TriviaDropQuestion:
    """Revise a draft in place: text, options and key are all replaceable.

    Only a draft with no submissions may be edited. Anything released or
    answered is immutable for its session -- corrections arrive as a new
    question, because rewriting a live key would invalidate the correctness
    already recorded against it.
    """
    with db.sessions.begin() as session:
        row = _get_row(session, question_id)
        if row.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only a draft can be edited. Release a new question to correct a live one.",
            )
        answered = session.scalar(
            select(TriviaDropSubmissionRecord.id)
            .where(TriviaDropSubmissionRecord.question_id == row.id)
            .limit(1)
        )
        if answered is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This question already has answers and cannot be edited.",
            )
        existing = _options_for(session, str(row.id))
        existing_correct = sorted(
            option.option_index for option in existing if option.is_correct
        )
        new_options = (
            validate_options(options)
            if options is not None
            else [option.option_text for option in sorted(existing, key=lambda o: o.option_index)]
        )
        new_correct = (
            validate_correct_indexes(correct_indexes)
            if correct_indexes is not None
            else existing_correct
        )
        if question_text is not None:
            row.question_text = validate_question_text(question_text)
        for option in existing:
            session.delete(option)
        session.flush()
        _attach_options(session, str(row.id), new_options, new_correct)
        return _as_model(row, _options_for(session, str(row.id)))


def deploy_question(db: Database, question_id: str, now: Optional[datetime] = None) -> TriviaDropQuestion:
    """Deploy one draft or scheduled question right now ("Deploy Now").

    Ends the live question, if any, and activates this one in the same
    transaction. Atomic under the game's advisory lock, so a Deploy racing a
    scheduled release cannot leave two live questions.
    """
    from sqlalchemy import text as _text

    from .game_configs import game_lock_key

    moment = now or _now()
    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("trivia-drop")},
            )
            row = _get_row(session, question_id)
            if row.status == "active":
                return _as_model(row, _options_for(session, str(row.id)))
            if row.status not in ("draft", "scheduled"):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Only a draft or scheduled question can be deployed.",
                )
            _activate(session, row, moment)
            return _as_model(row, _options_for(session, str(row.id)))
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A Trivia Drop release collided with a concurrent change. Try again.",
        ) from exc


def schedule_question(
    db: Database, question_id: str, scheduled_for: datetime, now: Optional[datetime] = None
) -> TriviaDropQuestion:
    """Hold one draft for a future release. The live question is untouched.

    Scheduling never ends the current session: the previous question ends
    when this one actually goes live, not when it is scheduled.
    """
    moment = now or _now()
    release_at = _as_utc(scheduled_for)
    if release_at <= moment:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A scheduled release must be in the future.",
        )
    with db.sessions.begin() as session:
        row = _get_row(session, question_id)
        if row.status != "draft":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only a draft can be scheduled.",
            )
        _check_schedule_conflict(session, release_at, exclude_id=str(row.id))
        row.status = "scheduled"
        row.scheduled_for = release_at
        session.flush()
        return _as_model(row, _options_for(session, str(row.id)))


def reschedule_question(
    db: Database, question_id: str, scheduled_for: datetime, now: Optional[datetime] = None
) -> TriviaDropQuestion:
    """Move one scheduled question's release. Ended sessions are untouched."""
    moment = now or _now()
    release_at = _as_utc(scheduled_for)
    if release_at <= moment:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A scheduled release must be in the future.",
        )
    with db.sessions.begin() as session:
        row = _get_row(session, question_id)
        if row.status != "scheduled":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only a scheduled question can be rescheduled.",
            )
        _check_schedule_conflict(session, release_at, exclude_id=str(row.id))
        row.scheduled_for = release_at
        session.flush()
        return _as_model(row, _options_for(session, str(row.id)))


def cancel_question(db: Database, question_id: str) -> TriviaDropQuestion:
    """Withdraw a draft or scheduled question. History is never rewritten."""
    with db.sessions.begin() as session:
        row = _get_row(session, question_id)
        if row.status not in ("draft", "scheduled"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Only a draft or scheduled question can be cancelled.",
            )
        row.status = "cancelled"
        session.flush()
        return _as_model(row, _options_for(session, str(row.id)))


def list_questions(db: Database) -> List[TriviaDropQuestion]:
    """Every question, newest first -- drafts, scheduled, live and history alike."""
    with db.sessions() as session:
        rows = session.scalars(
            select(TriviaDropQuestionRecord).order_by(
                TriviaDropQuestionRecord.created_at.desc()
            )
        ).all()
        return [_as_model(row, _options_for(session, str(row.id))) for row in rows]


def get_question(db: Database, question_id: str) -> TriviaDropQuestion:
    """One question by id, with its key -- admin eyes only."""
    with db.sessions() as session:
        row = _get_row(session, question_id)
        return _as_model(row, _options_for(session, str(row.id)))


def get_active_question(db: Database) -> Optional[TriviaDropQuestion]:
    """The live question for the admin dashboard, if any."""
    with db.sessions() as session:
        row = _active_row(session)
        if row is None:
            return None
        return _as_model(row, _options_for(session, str(row.id)))


def release_due_questions(db: Database, now: Optional[datetime] = None) -> dict:
    """Activate due scheduled questions: the release loop's entry point.

    Called by the same background path as the period games (boot ensure plus
    the run-resets trigger), so scheduled Trivia Drop releases need no second
    scheduler. Hidden games release nothing -- the row waits until the game is
    visible again. Idempotent per run: at most one question activates per
    execution (the earliest due), so a backlog never ends two questions in
    one pass and a repeated run with nothing due changes nothing.
    """
    from sqlalchemy import text as _text

    from .game_configs import game_lock_key, get_config

    moment = now or _now()
    try:
        with db.sessions.begin() as session:
            session.execute(
                _text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": game_lock_key("trivia-drop")},
            )
            config = get_config(session, "trivia-drop")
            if config.is_hidden:
                return {"game_id": "trivia-drop", "action": "skipped_hidden"}
            due = session.scalar(
                select(TriviaDropQuestionRecord)
                .where(
                    TriviaDropQuestionRecord.status == "scheduled",
                    TriviaDropQuestionRecord.scheduled_for <= moment,
                )
                .order_by(
                    TriviaDropQuestionRecord.scheduled_for.asc(),
                    TriviaDropQuestionRecord.created_at.asc(),
                )
                .limit(1)
            )
            if due is None:
                current = _active_row(session)
                return {
                    "game_id": "trivia-drop",
                    "action": "current" if current is not None else "none",
                    "question_id": str(current.id) if current is not None else None,
                }
            _activate(session, due, moment)
            return {
                "game_id": "trivia-drop",
                "action": "released",
                "question_id": str(due.id),
            }
    except IntegrityError as exc:
        from psycopg.errors import UniqueViolation

        if not isinstance(exc.orig, UniqueViolation):
            raise
        with db.sessions() as session:
            winner = _active_row(session)
            return {
                "game_id": "trivia-drop",
                "action": "current",
                "question_id": str(winner.id) if winner is not None else None,
            }
