"""Participant gameplay and answer settlement service for Trivia Drop."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    TriviaDropOptionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
    UserRecord,
)
from ..limits import TRIVIA_OPTIONS_COUNT
from ..models import (
    TriviaDropCurrentQuestion,
    TriviaDropPreviousQuestion,
    TriviaDropResults,
    TriviaDropSubmitResponse,
)
from ..postgres import Database, is_valid_id
from .trivia_service import answer_mode_for


def _options_for(session, question_id: str) -> list[TriviaDropOptionRecord]:
    return sorted(
        session.scalars(
            select(TriviaDropOptionRecord).where(
                TriviaDropOptionRecord.question_id == question_id
            )
        ).all(),
        key=lambda option: option.option_index,
    )


def _correct_set(options: list[TriviaDropOptionRecord]) -> set[int]:
    return {option.option_index for option in options if option.is_correct}


def _active_row(session) -> Optional[TriviaDropQuestionRecord]:
    """The one live question, or None."""
    return session.scalar(
        select(TriviaDropQuestionRecord)
        .where(TriviaDropQuestionRecord.status == "active")
        .limit(1)
    )


def current_trivia(db: Database, user_id: str) -> TriviaDropCurrentQuestion:
    """Return the live question and submission status for the current player."""
    from .game_configs import check_playable

    with db.sessions.begin() as session:
        check_playable(session, "trivia-drop")

    with db.sessions() as session:
        live = _active_row(session)
        if live is None:
            return TriviaDropCurrentQuestion(
                message="No question is live right now. Check back soon.",
            )
        options = _options_for(session, str(live.id))
        if len(options) != TRIVIA_OPTIONS_COUNT:
            # Unplayable question without valid options degrades to waiting state.
            return TriviaDropCurrentQuestion(
                message="No question is live right now. Check back soon.",
            )
        submitted = (
            session.scalar(
                select(TriviaDropSubmissionRecord.id).where(
                    TriviaDropSubmissionRecord.question_id == live.id,
                    TriviaDropSubmissionRecord.user_id == user_id,
                )
            )
            is not None
        )
        return TriviaDropCurrentQuestion(
            question_id=str(live.id),
            question_text=live.question_text,
            options=[
                {"option_index": option.option_index, "option_text": option.option_text}
                for option in options
            ],
            answer_mode=answer_mode_for(len(_correct_set(options))),
            released_at=live.released_at,
            submitted=submitted,
            session_status="submitted" if submitted else "active",
        )


def _get_live_question(session, question_id: str) -> tuple[TriviaDropQuestionRecord, list[TriviaDropOptionRecord]]:
    """The named question, live and fully configured -- or why it cannot be answered."""
    from .game_configs import check_playable, lock_game_for_play

    # Hold lifecycle lock across liveness check and write.
    lock_game_for_play(session, "trivia-drop")
    if not is_valid_id(question_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Trivia Drop question id"
        )
    check_playable(session, "trivia-drop")
    question = session.get(TriviaDropQuestionRecord, str(question_id))
    if question is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Trivia Drop question not found"
        )
    if question.status != "active":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That question is not accepting answers.",
        )
    live = _active_row(session)
    if live is None or str(live.id) != str(question.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That question is not accepting answers.",
        )
    options = _options_for(session, str(question.id))
    if len(options) != TRIVIA_OPTIONS_COUNT:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="That question is not accepting answers.",
        )
    return question, options


def _normalize_selection(raw: object) -> list[int]:
    """Validate and normalize selected answer option indices."""
    if not isinstance(raw, list) or not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Select at least one answer before submitting.",
        )
    chosen: set[int] = set()
    for entry in raw:
        if isinstance(entry, bool) or not isinstance(entry, int):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each selected answer must be one of this question's options.",
            )
        if entry < 0 or entry >= TRIVIA_OPTIONS_COUNT:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Each selected answer must be one of this question's options.",
            )
        chosen.add(entry)
    return sorted(chosen)


def submit_answer(
    db: Database, user_id: str, question_id: str, raw_selection: list[int]
) -> TriviaDropSubmitResponse:
    """Record and score a player's submission for the live trivia question."""
    selected = _normalize_selection(raw_selection)
    with db.sessions.begin() as session:
        question, options = _get_live_question(session, question_id)

        existing = session.scalar(
            select(TriviaDropSubmissionRecord).where(
                TriviaDropSubmissionRecord.question_id == question.id,
                TriviaDropSubmissionRecord.user_id == user_id,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Your answer for this question has already been recorded.",
            )

        correct = selected == sorted(_correct_set(options))
        now = datetime.now(timezone.utc)
        elapsed_ms = int((now - question.released_at).total_seconds() * 1000)
        try:
            row = TriviaDropSubmissionRecord(
                question_id=str(question.id),
                user_id=user_id,
                selected_options=selected,
                is_correct=correct,
                submitted_at=now,
                response_time_ms=max(elapsed_ms, 0),
            )
            session.add(row)
            session.flush()
        except IntegrityError as exc:
            from psycopg.errors import UniqueViolation

            if not isinstance(exc.orig, UniqueViolation):
                raise
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Your answer for this question has already been recorded.",
            ) from exc
        return TriviaDropSubmitResponse(
            question_id=str(question.id),
            submitted=True,
            submitted_at=now,
        )


def previous_trivia(db: Database, user_id: str) -> TriviaDropPreviousQuestion:
    """Return the most recently ended trivia question with answers and player result."""
    from .game_configs import check_playable

    with db.sessions.begin() as session:
        check_playable(session, "trivia-drop")
    with db.sessions() as session:
        previous = session.scalar(
            select(TriviaDropQuestionRecord)
            .where(TriviaDropQuestionRecord.status == "ended")
            .order_by(
                TriviaDropQuestionRecord.ended_at.desc(),
                TriviaDropQuestionRecord.created_at.desc(),
            )
            .limit(1)
        )
        if previous is None:
            return TriviaDropPreviousQuestion()
        options = _options_for(session, str(previous.id))
        correct = sorted(_correct_set(options))
        row = session.scalar(
            select(TriviaDropSubmissionRecord).where(
                TriviaDropSubmissionRecord.question_id == previous.id,
                TriviaDropSubmissionRecord.user_id == user_id,
            )
        )
        mine = (
            {
                "is_correct": bool(row.is_correct),
                "response_time_ms": row.response_time_ms,
                "submitted_at": row.submitted_at,
                "selected_indexes": sorted(row.selected_options),
            }
            if row is not None
            else None
        )
        return TriviaDropPreviousQuestion(
            question_id=str(previous.id),
            question_text=previous.question_text,
            options=[
                {"option_index": option.option_index, "option_text": option.option_text}
                for option in options
            ],
            correct_indexes=correct,
            answer_mode=answer_mode_for(len(correct)),
            released_at=previous.released_at,
            ended_at=previous.ended_at,
            my_submission=mine,
        )


def question_results(db: Database, user_id: str, question_id: str) -> TriviaDropResults:
    """Return settled results and leaderboard rankings for an ended trivia question."""
    from .game_configs import check_playable

    if not is_valid_id(question_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Trivia Drop question id"
        )
    with db.sessions.begin() as session:
        check_playable(session, "trivia-drop")
    with db.sessions() as session:
        question = session.get(TriviaDropQuestionRecord, str(question_id))
        if question is None or question.status in ("draft", "scheduled", "cancelled"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Trivia Drop results not found.",
            )
        if question.status == "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Results are available once this question has ended.",
            )
        options = _options_for(session, str(question.id))
        correct = sorted(_correct_set(options))
        rows = session.scalars(
            select(TriviaDropSubmissionRecord).where(
                TriviaDropSubmissionRecord.question_id == question.id
            )
        ).all()
        user_ids = sorted({str(row.user_id) for row in rows})
        names: dict[str, str] = {}
        if user_ids:
            for user in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(user_ids))
            ).all():
                names[str(user.id)] = user.name
        ordered = sorted(
            rows,
            key=lambda row: (
                not row.is_correct,
                row.response_time_ms,
                row.submitted_at,
                str(row.id),
            ),
        )
        leaderboard = [
            {
                "rank": position,
                "user_id": str(row.user_id),
                "display_name": names.get(str(row.user_id), "Player"),
                "is_correct": bool(row.is_correct),
                "response_time_ms": row.response_time_ms,
                "submitted_at": row.submitted_at,
                "selected_indexes": sorted(row.selected_options),
            }
            for position, row in enumerate(ordered, start=1)
        ]
        mine = next(
            (entry for entry in leaderboard if entry["user_id"] == user_id), None
        )
        return TriviaDropResults(
            question_id=str(question.id),
            question_text=question.question_text,
            options=[
                {"option_index": option.option_index, "option_text": option.option_text}
                for option in options
            ],
            correct_indexes=correct,
            answer_mode=answer_mode_for(len(correct)),
            released_at=question.released_at,
            ended_at=question.ended_at,
            my_submission=mine,
            leaderboard=leaderboard,
        )
