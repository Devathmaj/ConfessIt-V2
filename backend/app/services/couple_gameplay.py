"""Playing the Couple Questionnaire: the participant-facing half of the feature.

``couple_service`` marks a day as featured and seeds the fifty shared templates.
This module is everything that happens *afterwards*: choosing a partner without
telling them, recognising two independent choices as mutual, dealing each mutual
pair its own fixed set of 25 questions, collecting each side's original answers,
asking each side to predict the other's, and scoring the predictions against the
stored originals.

Five rules hold at the bottom of everything here.

**A selection is pending, never a session.** Choosing someone writes one
``couple_game_selections`` row keyed by the chooser -- no notification, no
session, no questions, nothing the chosen user can see. Changing the choice
updates the row, so the previous pick simply stops being active.

**Mutual selection is the only thing that starts a game.** ``A → B`` plus
``B → A`` creates exactly one session holding both users and the featured game
it belongs to. ``A → B`` plus ``C → B`` plus ``B → C`` is a game for ``B + C``
and nothing for A, because B never chose A back.

**One pair, one session per game -- and one session per participant per game.**
The pair is stored in canonical order with a unique constraint, and creation
additionally refuses anyone who already plays in another session for the same
featured game. Creation runs under one transaction-scoped advisory lock, so two
simultaneous mutual completions serialize: the later committer sees the
earlier one's committed choice and creates the one session, instead of both
reading a one-sided board and creating none -- or minting two.

**The session's questions are dealt once and read forever.** Twenty-five bank
ids are drawn at creation -- five shared, ten exclusive per player -- and stored
in ``couple_game_session_questions``. Every later read joins those rows; a
refresh can never re-deal them. The prediction order is a seeded shuffle of the
partner's fifteen, so it reads randomly yet identically on every load.

**Originals and predictions are different rows.** An original answer is what a
participant picked for their own question; a prediction is what the partner
guessed they picked. Both are insert-only, the verdict is computed server-side
at submit time against the stored original, and the partner's originals travel
to a reader only inside the completed result.
"""

import random
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    CoupleGameQuestionRecord,
    CoupleGameRecord,
    CoupleGameSelectionRecord,
    CoupleGameSessionAnswerRecord,
    CoupleGameSessionPredictionRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    UserRecord,
)
from ..limits import (
    COUPLE_ANSWERS_PER_PARTICIPANT,
    COUPLE_SESSION_EXCLUSIVE_PER_PLAYER,
    COUPLE_SESSION_QUESTIONS,
    COUPLE_SESSION_SHARED_QUESTIONS,
)
from ..models import (
    Cohort,
    CoupleAnswerRequest,
    CoupleCurrentState,
    CouplePartnerCandidate,
    CouplePartnerPage,
    CouplePartnerView,
    CouplePredictionRequest,
    CouplePredictionView,
    CoupleQuestionView,
    CoupleResultView,
    CoupleReviewItem,
    CoupleSelectionView,
    UserDetails,
)
from ..postgres import Database, is_valid_id
from ..utils import ilike_contains
from .couple_service import format_couple_question

# The answering-phase rendering of the bank placeholder: the person answering is
# the one being asked about, so ``X`` reads as "you". Presentation only -- the
# stored template is never rewritten -- and standalone-``X`` only, like the
# partner-name substitution in ``couple_service``.
_YOU_PATTERN = re.compile(r"\bX\b")

# Serialises session creation across concurrent selections. Two callers
# completing a mutual selection at the same instant each read "one-sided" when
# neither has committed yet, so without this both walk away with no session and
# nobody ever retries them into one. Holding one transaction-scoped advisory
# lock around the mutual check means the later committer always sees the
# earlier one's committed choice -- exactly one session, never two, never zero.
# Arbitrary but fixed, namespaced beside the account-cap keys: an advisory lock
# is a number the callers agree on, and this one must not be reused elsewhere.
_COUPLE_SESSION_LOCK_KEY = 7_250_001

# The page the partner picker gets when it asks for none, and the ceiling it is
# clamped to. Mirrors ``LOVE_NOTE_RECIPIENTS_MAX_LIMIT``: the endpoint refuses
# anything larger, so no client -- present or future, well-meaning or not --
# can ask for every participant at once and make the backend materialise them
# all.
COUPLE_PARTNERS_PAGE_DEFAULT = 50
COUPLE_PARTNERS_MAX_LIMIT = 100


def _today():
    """Today's date in UTC, the same clock every other service in the app uses."""
    return datetime.now(timezone.utc).date()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _latest_playable_game(session) -> Optional[CoupleGameRecord]:
    """The current questionnaire session, or None when the game has none.

    Sessions are opened by the reset loop under the game's own cadence and are
    independent of the featured spotlight: a session exists whether or not the
    questionnaire is featured today. Availability is enforced by the callers
    through ``check_playable`` before this is ever consulted.
    """
    from . import game_periods

    game = session.scalar(
        select(CoupleGameRecord)
        .where(CoupleGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )
    if game is None or game.status != "active":
        return None
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        return None
    if game_periods.is_reset_due(game.period_end_date, _today()):
        return None
    return game


def _require_playable(session) -> None:
    """Refuse hidden and disabled questionnaire access with a coded 403."""
    from .game_configs import check_playable

    check_playable(session, "couple-questionnaire")


def _owner_of(assignment: str, session_row: CoupleGameSessionRecord) -> Optional[str]:
    """The user id answering a question with this assignment, or None if shared."""
    if assignment == "user_a":
        return str(session_row.user_a_id)
    if assignment == "user_b":
        return str(session_row.user_b_id)
    return None


def _is_mine(assignment: str, session_row: CoupleGameSessionRecord, user_id: str) -> bool:
    """Whether this assignment is one of ``user_id``'s fifteen questions to answer."""
    if assignment == "shared":
        return True
    owner = _owner_of(assignment, session_row)
    return owner == user_id


# --- reading -------------------------------------------------------------------


def list_partners(
    db: Database,
    user: UserDetails,
    *,
    skip: int = 0,
    limit: int = COUPLE_PARTNERS_PAGE_DEFAULT,
    query: Optional[str] = None,
    gender_mode: str = "different",
    year: Optional[int] = None,
    department: Optional[str] = None,
) -> CouplePartnerPage:
    """One page of participants the caller may choose, filtered and counted in SQL.

    Every filter is a WHERE and the page is a LIMIT/OFFSET on the database
    itself, with ``total`` a COUNT under the same filters, so the backend reads
    and serialises at most ``limit`` rows however big the participant table
    gets -- the picker never downloads the table to search or filter it in the
    browser.

    What the page holds, and why:

    * the caller is excluded -- nobody partners with themselves;
    * ``query`` is a case-insensitive substring match on the name, run in SQL;
    * ``gender_mode`` is the discovery aid the screen's toggle drives:
      ``"different"`` (the default) hides the caller's own gender -- and only
      that, so an ``other`` caller still sees everyone -- while ``"same"``
      shows only it. Neither is a rule about who may be chosen: the chosen id
      is validated on its own, and nothing about playing is decided here;
    * ``year``/``department`` are the cohort filter, exact (department
      case-insensitively), so the screen's single "CSE 1" option means both
      halves at once.

    No opt-out filtering: blocked accounts stay listed for the same reason
    love-note recipients do -- visibility is not entitlement.
    """
    with db.sessions.begin() as session:
        _require_playable(session)

    skip = max(0, int(skip or 0))
    limit = max(1, min(int(limit or COUPLE_PARTNERS_PAGE_DEFAULT), COUPLE_PARTNERS_MAX_LIMIT))

    filters: list = [UserRecord.id != user.id]
    cleaned_query = (query or "").strip()
    if cleaned_query:
        filters.append(ilike_contains(UserRecord.name, cleaned_query))
    if gender_mode == "same":
        filters.append(UserRecord.gender == user.gender)
    elif user.gender in ("male", "female"):
        filters.append(UserRecord.gender != user.gender)
    if year is not None:
        filters.append(UserRecord.year == year)
    cleaned_department = (department or "").strip()
    if cleaned_department:
        filters.append(func.lower(UserRecord.department) == cleaned_department.lower())

    with db.sessions() as session:
        total = session.scalar(select(func.count()).select_from(UserRecord).where(*filters)) or 0
        rows = session.scalars(
            select(UserRecord)
            .where(*filters)
            # ``id`` seconds the name so two participants who share one still
            # come back in the same order on every page: an unordered tail
            # reshuffles between the count and the read, and a load-more list
            # that reorders under the reader shows some people twice and skips
            # others.
            .order_by(UserRecord.name.asc(), UserRecord.id.asc())
            .offset(skip)
            .limit(limit)
        ).all()
    return CouplePartnerPage(
        partners=[
            CouplePartnerCandidate(
                id=str(row.id),
                name=row.name,
                gender=row.gender,
                year=row.year,
                department=row.department,
            )
            for row in rows
        ],
        total=total,
        skip=skip,
        limit=limit,
    )


def list_partner_cohorts(db: Database) -> List[Cohort]:
    """Every ``(year, department)`` pair a partner candidate can belong to.

    The same distinct pairs the love-note recipient filter offers, from the
    same query (``matchmaking_slot_service.list_participant_cohorts``): the
    picker's single "CSE 1" option is a pair, so it is fed by the pairs that
    actually exist in ``users`` rather than a hand-typed list that can drift.
    """
    from .matchmaking_slot_service import list_participant_cohorts

    return [Cohort(**row) for row in list_participant_cohorts(db)]


def _assemble_state(session, user: UserDetails) -> CoupleCurrentState:
    """Where the caller stands, derived from stored rows and nothing else.

    Reading creates nothing: a fresh caller reads ``selecting_partner`` with no
    selection, and a returning one reads the same questions, predictions and
    score they left. The partner's original answers are never carried except
    inside the completed ``result`` -- that is the single moment the feature
    exists to reveal them.
    """
    _require_playable(session)
    game = _latest_playable_game(session)

    selection = session.scalar(
        select(CoupleGameSelectionRecord).where(
            CoupleGameSelectionRecord.user_id == user.id
        )
    )
    my_selection: Optional[CoupleSelectionView] = None
    if selection is not None:
        chosen = session.get(UserRecord, str(selection.selected_user_id))
        if chosen is not None:
            my_selection = CoupleSelectionView(
                selected_user_id=str(chosen.id),
                selected_user_name=chosen.name,
            )

    if game is None:
        return CoupleCurrentState(
            stage="no_game",
            my_selection=my_selection,
        )

    session_row = session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == game.id,
            or_(
                CoupleGameSessionRecord.user_a_id == user.id,
                CoupleGameSessionRecord.user_b_id == user.id,
            ),
        )
    )
    if session_row is None:
        return CoupleCurrentState(
            stage="selecting_partner",
            game_id=str(game.id),
            game_date=game.game_date,
            my_selection=my_selection,
        )

    partner_id = (
        str(session_row.user_b_id)
        if str(session_row.user_a_id) == user.id
        else str(session_row.user_a_id)
    )
    partner_row = session.get(UserRecord, partner_id)
    partner = (
        CouplePartnerView(id=partner_id, name=partner_row.name)
        if partner_row is not None
        else CouplePartnerView(id=partner_id, name="Partner")
    )

    bank = {
        str(row.id): row
        for row in session.scalars(select(CoupleGameQuestionRecord)).all()
    }
    sq_rows = session.scalars(
        select(CoupleGameSessionQuestionRecord)
        .where(CoupleGameSessionQuestionRecord.session_id == session_row.id)
        .order_by(CoupleGameSessionQuestionRecord.created_at.asc())
    ).all()
    # Bank sequence order for answering: the same fifteen in the same order on
    # every read, so the screen cannot drift between visits.
    sq_rows = sorted(sq_rows, key=lambda sq: bank[str(sq.question_id)].sequence_number)

    answers = {
        (str(row.session_question_id), str(row.user_id)): row.selected_option_index
        for row in session.scalars(
            select(CoupleGameSessionAnswerRecord).where(
                CoupleGameSessionAnswerRecord.session_id == session_row.id
            )
        ).all()
    }
    predictions = {
        (str(row.session_question_id), str(row.predictor_user_id)): row
        for row in session.scalars(
            select(CoupleGameSessionPredictionRecord).where(
                CoupleGameSessionPredictionRecord.session_id == session_row.id
            )
        ).all()
    }

    my_sq = [
        sq for sq in sq_rows if _is_mine(sq.assignment, session_row, user.id)
    ]
    partner_sq = [
        sq for sq in sq_rows if _is_mine(sq.assignment, session_row, partner_id)
    ]
    my_answers = sum(
        1 for sq in my_sq if (str(sq.id), user.id) in answers
    )
    partner_answers = sum(
        1 for sq in partner_sq if (str(sq.id), partner_id) in answers
    )
    my_predictions = sum(
        1 for sq in partner_sq if (str(sq.id), user.id) in predictions
    )

    base = dict(
        game_id=str(game.id),
        game_date=game.game_date,
        my_selection=my_selection,
        session_id=str(session_row.id),
        partner=partner,
        my_answers_submitted=my_answers,
        my_answers_total=COUPLE_ANSWERS_PER_PARTICIPANT,
        partner_answers_submitted=partner_answers,
        partner_answers_total=COUPLE_ANSWERS_PER_PARTICIPANT,
        my_predictions_submitted=my_predictions,
        my_predictions_total=COUPLE_ANSWERS_PER_PARTICIPANT,
    )

    if session_row.status == "completed":
        my_correct = sum(
            1
            for sq in partner_sq
            if (pred := predictions.get((str(sq.id), user.id))) is not None
            and pred.is_correct
        )
        partner_correct = sum(
            1
            for sq in my_sq
            if (pred := predictions.get((str(sq.id), partner_id))) is not None
            and pred.is_correct
        )
        review = []
        for sq in partner_sq:
            original = answers.get((str(sq.id), partner_id))
            mine = predictions.get((str(sq.id), user.id))
            if original is None or mine is None:
                continue
            template = bank[str(sq.question_id)]
            review.append(
                CoupleReviewItem(
                    session_question_id=str(sq.id),
                    question_id=str(sq.question_id),
                    question_text=format_couple_question(
                        template.question_text, partner.name
                    ),
                    options=[
                        template.option_a,
                        template.option_b,
                        template.option_c,
                        template.option_d,
                    ],
                    shared=sq.assignment == "shared",
                    original_option_index=original,
                    predicted_option_index=mine.predicted_option_index,
                    correct=mine.is_correct,
                )
            )
        return CoupleCurrentState(
            stage="completed",
            result=CoupleResultView(
                my_score=my_correct,
                partner_score=partner_correct,
                max_score=COUPLE_ANSWERS_PER_PARTICIPANT,
                my_correct_count=my_correct,
                my_prediction_count=my_predictions,
                partner_correct_count=partner_correct,
                partner_prediction_count=sum(
                    1 for sq in my_sq if (str(sq.id), partner_id) in predictions
                ),
                completed_at=session_row.completed_at,
                review=review,
            ),
            **base,
        )

    if session_row.status == "prediction":
        ordered = _prediction_order(str(session_row.id), user.id, partner_sq)
        return CoupleCurrentState(
            stage=(
                "prediction" if my_predictions < COUPLE_ANSWERS_PER_PARTICIPANT
                else "waiting_partner"
            ),
            waiting_for=(
                None
                if my_predictions < COUPLE_ANSWERS_PER_PARTICIPANT
                else "predictions"
            ),
            predictions=[
                _prediction_view(sq, bank, partner.name, predictions.get((str(sq.id), user.id)))
                for sq in ordered
            ],
            **base,
        )

    # Active: original answers still being collected.
    return CoupleCurrentState(
        stage=(
            "answering" if my_answers < COUPLE_ANSWERS_PER_PARTICIPANT
            else "waiting_partner"
        ),
        waiting_for=(
            None if my_answers < COUPLE_ANSWERS_PER_PARTICIPANT else "answers"
        ),
        questions=[
            _question_view(sq, bank, answers.get((str(sq.id), user.id)))
            for sq in my_sq
        ],
        **base,
    )


def _question_view(sq, bank, selected: Optional[int]) -> CoupleQuestionView:
    template = bank[str(sq.question_id)]
    return CoupleQuestionView(
        session_question_id=str(sq.id),
        question_id=str(sq.question_id),
        question_text=_YOU_PATTERN.sub("you", template.question_text),
        options=[template.option_a, template.option_b, template.option_c, template.option_d],
        shared=sq.assignment == "shared",
        selected_option_index=selected,
    )


def _prediction_view(sq, bank, partner_name: str, prediction) -> CouplePredictionView:
    template = bank[str(sq.question_id)]
    return CouplePredictionView(
        session_question_id=str(sq.id),
        question_id=str(sq.question_id),
        question_text=format_couple_question(template.question_text, partner_name),
        options=[template.option_a, template.option_b, template.option_c, template.option_d],
        shared=sq.assignment == "shared",
        predicted_option_index=(
            prediction.predicted_option_index if prediction is not None else None
        ),
    )


def _prediction_order(session_id: str, predictor_user_id: str, partner_sq: list) -> list:
    """The partner's questions in a stable random order for this predictor.

    Seeded by the session and the predictor, so the order reads randomly yet
    identically on every load -- a refresh cannot reshuffle unanswered questions
    -- and differs between the two players and between sessions.
    """
    ordered = list(partner_sq)
    random.Random(f"{session_id}:{predictor_user_id}").shuffle(ordered)
    return ordered


def current_state(db: Database, user: UserDetails) -> CoupleCurrentState:
    """This caller's place in the Couple Questionnaire, without creating a row.

    Reads never create sessions: the reset loop owns the global period, and a
    day with no session reads as ``no_game`` until the loop opens one.
    """
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions() as session:
        return _assemble_state(session, user)


# --- partner selection -----------------------------------------------------------


def _get_my_session_for_update(session, game_id: str, user_id: str):
    return session.scalar(
        select(CoupleGameSessionRecord)
        .where(
            CoupleGameSessionRecord.couple_game_id == game_id,
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
        )
        .with_for_update()
    )


def select_partner(
    db: Database, user: UserDetails, selected_user_id: str
) -> CoupleCurrentState:
    """Record this caller's partner choice, creating a session only on mutual selection.

    The choice itself is an upsert of the caller's one selection row: no
    notification goes out, no session is created, and nothing is revealed to the
    chosen user. When the chosen user has already chosen this caller back, the
    pair is mutual and one session is created holding both users and the current
    featured game -- unless either side already plays in another session for
    that game, in which case no second session is made.

    The mutual check runs under one transaction-scoped advisory lock, so two
    simultaneous completions serialize: the later committer sees the earlier
    one's committed choice and creates the one session, while the earlier one
    -- which could only see a one-sided board -- created none. Exactly one
    session either way, never two and never zero.
    """
    if not is_valid_id(selected_user_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id"
        )
    if str(selected_user_id) == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot select yourself as a partner.",
        )

    with db.sessions.begin() as session:
        _require_playable(session)

    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        # ENG-001: hold the lifecycle lock for the whole selection -- and take
        # it *before* the pair lock below, the same order every gameplay
        # transaction uses -- so a period rollover cannot end the global the
        # new pair session is about to attach to.
        lock_game_for_play(session, "couple-questionnaire")
        target = session.get(UserRecord, str(selected_user_id))
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
            )

        session.execute(
            pg_insert(CoupleGameSelectionRecord)
            .values(user_id=user.id, selected_user_id=str(target.id))
            .on_conflict_do_update(
                index_elements=[CoupleGameSelectionRecord.user_id],
                set_={"selected_user_id": str(target.id)},
            )
        )

        game = _latest_playable_game(session)
        if game is not None:
            # Serialise with every other selection before reading the board:
            # reads taken before this point may predate a concurrent commit, so
            # the game and both selections are re-read under the lock.
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": _COUPLE_SESSION_LOCK_KEY},
            )
            game = _latest_playable_game(session)

        if game is not None:
            locked = {
                str(row.user_id): row
                for row in session.scalars(
                    select(CoupleGameSelectionRecord).where(
                        CoupleGameSelectionRecord.user_id.in_(
                            [user.id, str(target.id)]
                        )
                    )
                ).all()
            }
            mine = locked.get(user.id)
            theirs = locked.get(str(target.id))
            assert mine is not None, "the upsert must leave a selection"
            if theirs is not None and str(theirs.selected_user_id) == user.id:
                _maybe_create_session(session, game, user.id, str(target.id))

    with db.sessions() as session:
        return _assemble_state(session, user)


def _maybe_create_session(session, game: CoupleGameRecord, first_id: str, second_id: str) -> None:
    """Create the pair's session for this game, or leave the existing state alone.

    Called under the session-creation advisory lock, so a concurrent mutual
    completion cannot interleave: exactly one caller inserts, and every other
    caller finds the committed row -- session and dealt questions together.
    """
    user_a_id, user_b_id = sorted([first_id, second_id])

    pair_session = session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == game.id,
            CoupleGameSessionRecord.user_a_id == user_a_id,
            CoupleGameSessionRecord.user_b_id == user_b_id,
        )
    )
    if pair_session is not None:
        return

    # One session per participant per game: either side already playing elsewhere
    # for this featured day blocks a second one.
    other = session.scalar(
        select(CoupleGameSessionRecord.id)
        .where(
            CoupleGameSessionRecord.couple_game_id == game.id,
            or_(
                CoupleGameSessionRecord.user_a_id.in_([user_a_id, user_b_id]),
                CoupleGameSessionRecord.user_b_id.in_([user_a_id, user_b_id]),
            ),
        )
        .limit(1)
    )
    if other is not None:
        return

    # Insert-or-nothing: a concurrent mutual completion racing past the pair
    # check above lands here instead of minting a second session. The rowcount
    # of an ON CONFLICT DO NOTHING is not meaningful through this driver, so
    # the re-read below -- not the count -- decides who deals the questions.
    session.execute(
        pg_insert(CoupleGameSessionRecord)
        .values(
            couple_game_id=str(game.id),
            user_a_id=user_a_id,
            user_b_id=user_b_id,
            status="active",
        )
        .on_conflict_do_nothing(
            index_elements=[
                CoupleGameSessionRecord.couple_game_id,
                CoupleGameSessionRecord.user_a_id,
                CoupleGameSessionRecord.user_b_id,
            ]
        )
    )

    created = session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == game.id,
            CoupleGameSessionRecord.user_a_id == user_a_id,
            CoupleGameSessionRecord.user_b_id == user_b_id,
        )
    )
    assert created is not None, "the insert-or-nothing must leave the pair's row"

    already_dealt = session.scalar(
        select(func.count())
        .select_from(CoupleGameSessionQuestionRecord)
        .where(CoupleGameSessionQuestionRecord.session_id == created.id)
    )
    if already_dealt:
        return

    from .game_scheduler import deal_pair_questions

    try:
        deal_pair_questions(session, str(created.id))
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The question bank is not ready yet. Try again in a moment.",
        ) from exc


# --- original answers ------------------------------------------------------------


def submit_answer(
    db: Database, user: UserDetails, payload: CoupleAnswerRequest
) -> CoupleCurrentState:
    """Store one of this caller's own answers, advancing the session when all thirty land.

    The question must be one of the fifteen this caller was assigned in their
    session for the current featured game -- derived from the caller, so naming
    another session's ids answers nothing. Answers are insert-only: a second
    submit for the same question is a 409, and a prediction never touches this
    table, so the original stays the canonical correct answer it will be scored
    against. When the thirtieth original (both sides' fifteen) is stored, the
    session moves to ``prediction`` in the same transaction.
    """
    if not is_valid_id(payload.session_question_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid question id"
        )

    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        # ENG-001: hold the lifecycle lock across latest-game resolution and
        # the answer write, so a rollover cannot retire the global mid-submit.
        lock_game_for_play(session, "couple-questionnaire")
        _require_playable(session)
        game = _latest_playable_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No questionnaire session yet"
            )
        session_row = _get_my_session_for_update(session, str(game.id), user.id)
        if session_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no session in the current questionnaire yet",
            )
        if session_row.status == "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This session is already completed.",
            )
        if session_row.status != "active":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Answering is over for this session.",
            )

        sq = session.scalar(
            select(CoupleGameSessionQuestionRecord).where(
                CoupleGameSessionQuestionRecord.id == str(payload.session_question_id),
                CoupleGameSessionQuestionRecord.session_id == session_row.id,
            )
        )
        if sq is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That question is not part of your session.",
            )
        if not _is_mine(sq.assignment, session_row, user.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="That question is not assigned to you.",
            )

        # Insert-only: the unique pair is what turns a double submit into a
        # 409 instead of a second answer, and a SAVEPOINT keeps the refusal
        # from rolling back the surrounding transaction.
        try:
            with session.begin_nested():
                session.add(
                    CoupleGameSessionAnswerRecord(
                        session_id=str(session_row.id),
                        session_question_id=str(sq.id),
                        user_id=user.id,
                        selected_option_index=payload.selected_option_index,
                    )
                )
                session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already answered that question.",
            ) from exc

        total = session.scalar(
            select(func.count())
            .select_from(CoupleGameSessionAnswerRecord)
            .where(CoupleGameSessionAnswerRecord.session_id == session_row.id)
        )
        if total is not None and total >= 2 * COUPLE_ANSWERS_PER_PARTICIPANT:
            session_row.status = "prediction"

    with db.sessions() as fresh:
        return _assemble_state(fresh, user)


# --- predictions -----------------------------------------------------------------


def submit_prediction(
    db: Database, user: UserDetails, payload: CouplePredictionRequest
) -> CoupleCurrentState:
    """Store one prediction of the partner's answer, scoring it against the original.

    The question must be one of the fifteen the *partner* answered -- predicting
    one's own questions is refused -- and the partner's original for it must
    already exist, which the ``prediction`` status guarantees for every question
    at once. The verdict is computed here, inside the transaction, from the
    stored original: a client can neither mark itself correct nor learn the
    original before the completed result reveals it. When the thirtieth
    prediction lands, the session completes in the same transaction.
    """
    if not is_valid_id(payload.session_question_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid question id"
        )

    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        # ENG-001: hold the lifecycle lock across latest-game resolution and
        # the prediction write, so a rollover cannot retire the global mid-submit.
        lock_game_for_play(session, "couple-questionnaire")
        _require_playable(session)
        game = _latest_playable_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="No questionnaire session yet"
            )
        session_row = _get_my_session_for_update(session, str(game.id), user.id)
        if session_row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no session in the current questionnaire yet",
            )
        if session_row.status == "completed":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This session is already completed.",
            )
        if session_row.status != "prediction":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Both participants must finish answering before predictions begin.",
            )

        sq = session.scalar(
            select(CoupleGameSessionQuestionRecord).where(
                CoupleGameSessionQuestionRecord.id == str(payload.session_question_id),
                CoupleGameSessionQuestionRecord.session_id == session_row.id,
            )
        )
        if sq is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="That question is not part of your session.",
            )
        # A shared question is answered by both sides, so it is "mine" to
        # answer -- but a prediction names the *partner's* answer to it, a
        # different row on the same session question. Owner None (shared) or the
        # partner means the partner has an original here to predict; an
        # exclusive of mine means there is nothing of theirs to guess at.
        partner_id = (
            str(session_row.user_b_id)
            if str(session_row.user_a_id) == user.id
            else str(session_row.user_a_id)
        )
        if _owner_of(sq.assignment, session_row) not in (None, partner_id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="You can only predict your partner's answers.",
            )

        original = session.scalar(
            select(CoupleGameSessionAnswerRecord).where(
                CoupleGameSessionAnswerRecord.session_question_id == str(sq.id),
                CoupleGameSessionAnswerRecord.user_id == partner_id,
            )
        )
        if original is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Your partner has not answered that question yet.",
            )

        correct = payload.predicted_option_index == original.selected_option_index
        try:
            with session.begin_nested():
                session.add(
                    CoupleGameSessionPredictionRecord(
                        session_id=str(session_row.id),
                        session_question_id=str(sq.id),
                        predictor_user_id=user.id,
                        predicted_option_index=payload.predicted_option_index,
                        is_correct=correct,
                    )
                )
                session.flush()
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="You have already predicted that question.",
            ) from exc

        total = session.scalar(
            select(func.count())
            .select_from(CoupleGameSessionPredictionRecord)
            .where(CoupleGameSessionPredictionRecord.session_id == session_row.id)
        )
        if total is not None and total >= 2 * COUPLE_ANSWERS_PER_PARTICIPANT:
            session_row.status = "completed"
            session_row.completed_at = _now()

    with db.sessions() as fresh:
        return _assemble_state(fresh, user)
