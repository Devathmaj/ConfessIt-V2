# app/routers/admin_trivia_drop.py

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status

from ..accounts import Capability
from ..dependencies import get_db
from ..models import (
    AccountDetails,
    TriviaDropQuestion,
    TriviaDropQuestionCreate,
    TriviaDropQuestionUpdate,
    TriviaDropScheduleRequest,
)
from ..postgres import Database
from ..services import trivia_service
from ..services.authorization import require_capability
from ..services.trivia_service import TRIVIA_QUESTIONS_MAX_LIMIT

router = APIRouter(prefix="/admin/trivia-drop", tags=["Admin - Trivia Drop"])

# Every route below is gated on the one capability that means "may publish a
# question", and the read routes are gated too for the reason the other games'
# are: the correct-option set is the thing the whole feature exists to keep out
# of the player-facing surface while a question is live, and a list endpoint
# open to any caller would be that key, exposed.


@router.post("", response_model=TriviaDropQuestion, status_code=status.HTTP_201_CREATED)
def create_trivia_question(
    payload: TriviaDropQuestionCreate,
    db: Database = Depends(get_db),
    account: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Author one question: draft it, schedule it, or deploy it right now.

    ``release_mode="now"`` validates everything and activates the question in
    the same transaction, ending any live question. ``"scheduled"`` needs a
    future ``scheduled_for`` and waits for the release loop. ``"draft"``
    stores it for later. The answer mode is derived from how many correct
    options are named -- it is never configured separately.

    The caller's ``regno`` is recorded as ``created_by`` rather than taken
    from the payload: an administrator cannot publish a question attributed to
    somebody else.
    """
    return trivia_service.create_question(
        db,
        question_text=payload.question_text,
        options=list(payload.options),
        correct_indexes=list(payload.correct_indexes),
        release_mode=payload.release_mode,
        scheduled_for=payload.scheduled_for,
        created_by=account.regno,
    )


@router.get("/current", response_model=TriviaDropQuestion)
def current_trivia_question(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """The live question, or 404 when none is live.

    Declared before ``/{question_id}`` so ``current`` is never read as an id.
    """
    from fastapi import HTTPException

    current = trivia_service.get_active_question(db)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No Trivia Drop question is live")
    return current


@router.post("/{question_id}/deploy", response_model=TriviaDropQuestion)
def deploy_trivia_question(
    question_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Deploy one draft or scheduled question right now.

    Ends the live question, if any, and activates this one in the same
    transaction, so the two are never live together.
    """
    return trivia_service.deploy_question(db, question_id)


@router.post("/{question_id}/schedule", response_model=TriviaDropQuestion)
def schedule_trivia_question(
    question_id: str,
    payload: TriviaDropScheduleRequest,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Hold one draft for a future release. The live question is untouched."""
    return trivia_service.schedule_question(db, question_id, payload.scheduled_for)


@router.post("/{question_id}/reschedule", response_model=TriviaDropQuestion)
def reschedule_trivia_question(
    question_id: str,
    payload: TriviaDropScheduleRequest,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Move one scheduled question's release to a new future time."""
    return trivia_service.reschedule_question(db, question_id, payload.scheduled_for)


@router.post("/{question_id}/cancel", response_model=TriviaDropQuestion)
def cancel_trivia_question(
    question_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Withdraw a draft or scheduled question. History is never rewritten."""
    return trivia_service.cancel_question(db, question_id)


@router.put("/{question_id}", response_model=TriviaDropQuestion)
def update_trivia_draft(
    question_id: str,
    payload: TriviaDropQuestionUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """Revise a draft with no answers. Live and answered questions are immutable."""
    return trivia_service.update_draft(
        db,
        question_id,
        question_text=payload.question_text,
        options=list(payload.options) if payload.options is not None else None,
        correct_indexes=list(payload.correct_indexes)
        if payload.correct_indexes is not None
        else None,
    )


@router.get("", response_model=Dict[str, Any])
def list_trivia_questions(
    skip: int = Query(0, ge=0, description="How many questions to skip."),
    limit: int = Query(50, ge=1, le=TRIVIA_QUESTIONS_MAX_LIMIT, description="How many questions to return. Clamped so one request can never materialise the whole bank at once."),
    status: Optional[str] = Query(
        None,
        description="Comma-separated lifecycle subset to read (draft, scheduled, active, ended, cancelled): the working set and the history are two reads, not one full list.",
    ),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> Dict[str, Any]:
    """One page of every question, newest first, with the total beside it.

    ``status`` optionally narrows to a lifecycle subset so the screen reads its small
    working set (drafts, scheduled, live) separately from its unbounded history
    (ended, cancelled) instead of loading the bank to split it by hand.
    """
    statuses = [part.strip() for part in (status or "").split(",") if part.strip()] or None
    return trivia_service.list_questions(db, skip=skip, limit=limit, statuses=statuses)


@router.get("/{question_id}", response_model=TriviaDropQuestion)
def get_trivia_question(
    question_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.GAMES_MANAGE)),
) -> TriviaDropQuestion:
    """One question by id, with its answer key."""
    return trivia_service.get_question(db, question_id)
