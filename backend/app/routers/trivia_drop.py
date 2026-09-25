# app/routers/trivia_drop.py

from fastapi import APIRouter, Depends

from ..dependencies import get_db
from ..models import (
    TriviaDropCurrentQuestion,
    TriviaDropPreviousQuestion,
    TriviaDropResults,
    TriviaDropSubmitRequest,
    TriviaDropSubmitResponse,
    UserDetails,
)
from ..postgres import Database
from ..services import trivia_gameplay
from ..services.authorization import require_participant

router = APIRouter(prefix="/games/trivia-drop", tags=["Trivia Drop"])

# The player-facing surface of the feature. Every route resolves the caller as
# a participant and reads and writes only that caller's own submission: there
# is no way to answer another player's question, and no way to ask what the
# key is -- the live question carries display options plus the derived mode
# the screen needs, and the submit response is a confirmation that carries no
# correctness and no time. Correctness and the leaderboard surface on the
# ended question's results, never while it is live.


@router.get("", response_model=TriviaDropCurrentQuestion)
async def current_trivia(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TriviaDropCurrentQuestion:
    """The live question and this player's submission state.

    Reading this never creates a submission and never consumes one: a fresh
    player reads the live question, a returning one reads the same question,
    and an answered player reads a confirmation rather than a board. With no
    live question the answer is the waiting state -- never an invented puzzle
    and never a preview of a scheduled one.
    """
    return trivia_gameplay.current_trivia(db, user.id)


@router.post("/submit", response_model=TriviaDropSubmitResponse)
async def submit(
    payload: TriviaDropSubmitRequest,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TriviaDropSubmitResponse:
    """Settle one complete selection against the live question, for this player.

    Exactly one accepted submission per player per question: checking boxes is
    not a submission, and a second submit -- concurrent, retried or direct --
    is refused. A multiple-answer selection travels as one complete set in
    this one request. The response is a confirmation only.
    """
    return trivia_gameplay.submit_answer(
        db, user.id, payload.question_id, list(payload.selected_indexes)
    )


@router.get("/previous", response_model=TriviaDropPreviousQuestion)
async def previous(
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TriviaDropPreviousQuestion:
    """The most recently ended question, with its answer key revealed.

    Reading this never creates anything. Ended sessions belong to history, so
    the key is public here; with no ended question yet the answer is empty
    and the screen shows nothing at all.
    """
    return trivia_gameplay.previous_trivia(db, user.id)


@router.get("/results/{question_id}", response_model=TriviaDropResults)
async def results(
    question_id: str,
    db: Database = Depends(get_db),
    user: UserDetails = Depends(require_participant),
) -> TriviaDropResults:
    """One ended question with its key revealed and its leaderboard settled.

    Served only once the session has ended: a live question is refused here,
    because its key and per-answer correctness belong to history, not to play.
    Correct answers rank above incorrect ones, faster above slower, with the
    authoritative submission instant breaking ties.
    """
    return trivia_gameplay.question_results(db, user.id, question_id)
