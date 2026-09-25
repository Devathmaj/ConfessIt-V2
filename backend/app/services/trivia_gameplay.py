"""Answering the live Trivia Drop question: the participant-facing half.

``trivia_service`` authors and releases questions. This module is everything
that happens *afterwards*: reading the live question without learning its
key, settling one complete selection against it, and -- once the session has
ended -- reading its settled results.

Four rules hold at the bottom of everything here.

**One accepted submission per player per question.** A
``trivia_drop_submissions`` row pairs a user with a question, and the unique
pair is the schema's rule, not a service check. Two concurrent submissions
race that insert and exactly one of them owns the row; the loser is refused
as already answered. A multiple-answer selection travels as one complete set
in one request -- several checked boxes never become several rows.

**The key never leaves the server while the question is live.** A read emits
display options plus the derived mode (single vs multiple) the screen needs
for its instruction and its controls -- never the correct indexes, never even
how many of them there are. The submit response is a confirmation carrying no
correctness and no time; both surface on the ended question's results.

**Response time starts at release, not at page load.** ``response_time_ms``
is ``submitted_at - released_at`` from server timestamps only: a player who
opens the question ten minutes after release and answers in five seconds
banks ten minutes and five seconds, because the ranking rewards answering
soon after release, not reacting fast after opening.

**Correctness is exact set equality.** A multiple-answer submission is
correct only when the selected set matches the complete correct set -- no
partial credit, no per-option points. Order never matters; duplicates are
normalized away before the comparison.
"""

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
    """The live question and this player's submission state, creating nothing.

    Reading the screen must leave no trace: opening it twice costs nothing,
    and a player who only looks at the live question has not answered
    anything. A submitted player reads a confirmation rather than a board; a
    player arriving with no live question reads the waiting state rather than
    an invented puzzle or a reused one. Upcoming scheduled questions are never
    named here -- surprise releases stay surprising.

    Availability is enforced before anything else: hidden and disabled games
    refuse with a coded 403 rather than a board.
    """
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
            # A live question with no valid options is not playable: the same
            # waiting behavior as no live question, never a partial board.
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
    from .game_configs import check_playable

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
    """One complete selection as sorted unique indexes, or why it is refused.

    At least one option must be chosen; every index must name one of the four
    options. Duplicates are normalized away -- checking a box twice is one
    choice stated twice. Nothing here consumes the submission, because nothing
    is stored until the insert below.
    """
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
    """Settle one complete selection against the live question, for this player.

    The whole exchange runs in one transaction. The submission row is inserted
    under the unique ``(question, user)`` pair, which serializes concurrent
    answers on the same question: exactly one insert wins, and the loser is
    refused as already answered. Correctness is exact set equality against
    the stored key, and the response time is ``submitted_at - released_at``
    from server timestamps -- so nothing the client sends can mark itself
    correct or fast. The response carries no correctness at all, only the
    confirmation the screen shows.
    """
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


def question_results(db: Database, user_id: str, question_id: str) -> TriviaDropResults:
    """One ended question with its key revealed and its leaderboard settled.

    Served only once the session has ended: the correct indexes and the
    per-submission correctness would expose a live key, so a live question is
    refused here and drafts, scheduled and cancelled questions read as absent.
    Ranking is correctness first, response speed second, the authoritative
    submission instant as the deterministic tiebreaker. Every number here is
    persisted server-side; the frontend ranks nothing.
    """
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
