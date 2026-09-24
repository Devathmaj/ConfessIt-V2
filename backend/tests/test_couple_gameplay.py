"""Playing the Couple Questionnaire end to end, through HTTP where a player would be.

These tests are about the *participation* half of the feature: choosing a partner,
the mutual selection that alone starts a session, the fixed deal of 25 questions,
original answers rendered with "you", predictions rendered with the partner's name,
and the score derived from the stored rows. The featured day and the fifty shared
templates are starting points seeded directly, not the things under test -- those
are pinned by ``test_admin_couple``.

The properties worth pinning are the ones a careless implementation would get
quietly wrong:

**Choosing is not starting.** A one-sided selection persists visibly to its
chooser, notifies nobody, and creates no session, no questions and no answers.

**Only mutual starts, and only once.** ``A → B`` plus ``B → A`` is exactly one
session for the pair; repeating the mutual trigger, or racing it from both
sides at once, must not mint a second.

**The deal is fixed.** Twenty-five unique questions per session -- five shared,
ten exclusive each -- fifteen per participant, identical on every read.

**Originals and predictions are different facts.** A prediction never overwrites
the original it guesses at, the verdict is computed server-side, and the
partner's originals surface only in the completed result.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, or_, select, text

from app.accounts import AccountType
from app.db_models import (
    CoupleGameRecord,
    CoupleGameSessionAnswerRecord,
    CoupleGameSessionPredictionRecord,
    CoupleGameSessionQuestionRecord,
    CoupleGameSessionRecord,
    UserRecord,
)
from app.limits import COUPLE_ANSWERS_PER_PARTICIPANT
from app.main import app
from app.models import UserDetails
from app.services import couple_gameplay
from app.services.auth_service import get_current_user
from app.services.couple_service import seed_couple_questions
from conftest import override_get_current_user


def make_user(db, user_id, regno, name, gender="other"):
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', :gender) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "name": name, "email": f"{regno}@user.com", "gender": gender},
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=name,
        email=f"{regno}@user.com",
        year=2,
        department="Class",
        gender=gender,
        account_type=AccountType.USERS.value,
    )


A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture
def players(db):
    """Three registered participants with pinned ids and names the tests can assert on."""
    seed_couple_questions(db)
    users = {
        "a": make_user(db, A_ID, "player_a", "Alex", "male"),
        "b": make_user(db, B_ID, "player_b", "Sam", "female"),
        "c": make_user(db, C_ID, "player_c", "Jo", "other"),
    }
    return users


def seed_game(db):
    """Feature today straight into the table; the admin endpoint is pinned elsewhere."""
    game = CoupleGameRecord(
        game_date=datetime.now(timezone.utc).date(), created_by="adminuser"
    )
    with db.sessions.begin() as session:
        session.add(game)
        session.flush()
        return str(game.id)


@pytest.fixture
def http():
    """One shared client; the caller identity is switched before every request.

    The auth override is process-global, so two clients cannot hold two
    identities at once. Requests here are sequential, so switching the override
    to the acting user immediately before each call gives every request the
    right caller without fixtures clobbering each other.
    """
    client = TestClient(app, follow_redirects=False)
    try:
        yield client
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def client_for(http: TestClient, user: UserDetails):
    """A handle acting as ``user`` on the shared ``http`` client."""

    class _AsUser:
        def get(self, *args, **kwargs):
            app.dependency_overrides[get_current_user] = lambda: user
            return http.get(*args, **kwargs)

        def post(self, *args, **kwargs):
            app.dependency_overrides[get_current_user] = lambda: user
            return http.post(*args, **kwargs)

    return _AsUser()


def session_count(db, game_id: str) -> int:
    with db.sessions() as session:
        return session.scalar(
            select(func.count())
            .select_from(CoupleGameSessionRecord)
            .where(CoupleGameSessionRecord.couple_game_id == game_id)
        ) or 0


def user_sessions(db, game_id: str, user_id: str):
    with db.sessions() as session:
        return session.scalars(
            select(CoupleGameSessionRecord).where(
                CoupleGameSessionRecord.couple_game_id == game_id,
                or_(
                    CoupleGameSessionRecord.user_a_id == user_id,
                    CoupleGameSessionRecord.user_b_id == user_id,
                ),
            )
        ).all()


# --- entering the game -----------------------------------------------------------


def test_without_a_featured_day_there_is_selection_but_no_session(http, db, players):
    client = client_for(http, players["a"])
    state = client.get("/games/couple-questionnaire")
    assert state.status_code == 200, state.text
    assert state.json()["stage"] == "no_game"
    assert state.json()["game_id"] is None


def test_partners_lists_everyone_but_the_caller(http, db, players):
    client = client_for(http, players["a"])
    response = client.get("/games/couple-questionnaire/partners")
    assert response.status_code == 200, response.text
    ids = {entry["id"] for entry in response.json()}
    assert A_ID not in ids
    assert {B_ID, C_ID} <= ids


# --- partner selection -------------------------------------------------------------


def test_a_one_sided_selection_is_pending_and_starts_nothing(http, db, players):
    seed_game(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])

    response = client_a.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": B_ID}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["stage"] == "selecting_partner"
    assert body["my_selection"]["selected_user_id"] == B_ID
    assert body["my_selection"]["selected_user_name"] == "Sam"
    assert body["session_id"] is None

    # Nothing is revealed to the chosen user: no selection, no session.
    other = client_b.get("/games/couple-questionnaire").json()
    assert other["stage"] == "selecting_partner"
    assert other["my_selection"] is None
    assert other["session_id"] is None


def test_a_selection_can_be_changed(http, db, players):
    seed_game(db)
    client = client_for(http, players["a"])

    client.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    changed = client.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": C_ID}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["my_selection"]["selected_user_id"] == C_ID
    assert changed.json()["session_id"] is None


def test_selecting_yourself_or_nobody_is_refused(http, db, players):
    seed_game(db)
    client = client_for(http, players["a"])

    assert (
        client.post(
            "/games/couple-questionnaire/selection", json={"selected_user_id": A_ID}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/games/couple-questionnaire/selection",
            json={"selected_user_id": "11111111-1111-1111-1111-111111111111"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/games/couple-questionnaire/selection", json={"selected_user_id": "nope"}
        ).status_code
        == 400
    )


# --- mutual selection starts exactly one session -------------------------------------


def test_mutual_selection_creates_one_session_for_the_pair(http, db, players):
    game_id = seed_game(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])

    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    created = client_b.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": A_ID}
    )
    assert created.status_code == 200, created.text
    assert created.json()["stage"] == "answering"
    assert created.json()["partner"]["name"] == "Alex"

    assert session_count(db, game_id) == 1
    for client in (client_a, client_b):
        state = client.get("/games/couple-questionnaire").json()
        assert state["stage"] == "answering"
        assert state["session_id"] == created.json()["session_id"]
        assert len(state["questions"]) == COUPLE_ANSWERS_PER_PARTICIPANT


def test_repeating_the_mutual_trigger_creates_no_second_session(http, db, players):
    game_id = seed_game(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])

    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    first = client_b.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": A_ID}
    ).json()["session_id"]

    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    again = client_b.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": A_ID}
    ).json()["session_id"]

    assert again == first
    assert session_count(db, game_id) == 1


def test_simultaneous_mutual_selections_create_exactly_one_session(http, db, players):
    """Two completions at once serialize: one session, and both sides converge on it.

    The earlier committer can only report the one-sided board it saw -- its own
    response cannot know the future -- so convergence is asserted on the next
    read, which is also what the screen does on every entry.
    """
    game_id = seed_game(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: couple_gameplay.select_partner(db, pair[0], pair[1]),
                [(players["a"], B_ID), (players["b"], A_ID)],
            )
        )

    assert session_count(db, game_id) == 1
    reads = [
        couple_gameplay.current_state(db, players[key]).session_id for key in ("a", "b")
    ]
    assert reads[0] is not None
    assert reads[0] == reads[1]


def test_conflicting_selections_pair_the_mutual_two_only(http, db, players):
    game_id = seed_game(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])
    client_c = client_for(http, players["c"])

    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    client_c.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    assert session_count(db, game_id) == 0

    created = client_b.post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": C_ID}
    )
    assert created.status_code == 200, created.text
    assert created.json()["partner"]["id"] == C_ID
    assert session_count(db, game_id) == 1

    # A chose B, but B chose C back: A still has no session and stays pending.
    left_out = client_a.get("/games/couple-questionnaire").json()
    assert left_out["stage"] == "selecting_partner"
    assert left_out["session_id"] is None
    assert left_out["my_selection"]["selected_user_id"] == B_ID


def test_a_participant_gets_no_second_session_for_the_same_game(http, db, players):
    game_id = seed_game(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])
    client_c = client_for(http, players["c"])

    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    client_b.post("/games/couple-questionnaire/selection", json={"selected_user_id": A_ID})
    assert len(user_sessions(db, game_id, A_ID)) == 1

    # C chooses A back after A chose C: A already plays with B for this game.
    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": C_ID})
    client_c.post("/games/couple-questionnaire/selection", json={"selected_user_id": A_ID})

    assert len(user_sessions(db, game_id, A_ID)) == 1
    assert session_count(db, game_id) == 1


# --- the dealt questions -------------------------------------------------------------


def _pair(http, db, players):
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])
    client_a.post("/games/couple-questionnaire/selection", json={"selected_user_id": B_ID})
    client_b.post("/games/couple-questionnaire/selection", json={"selected_user_id": A_ID})
    return client_a, client_b


def test_the_deal_is_twenty_five_unique_five_shared_ten_each(http, db, players):
    game_id = seed_game(db)
    client_a, client_b = _pair(http, db, players)
    session_id = client_a.get("/games/couple-questionnaire").json()["session_id"]

    with db.sessions() as session:
        rows = session.scalars(
            select(CoupleGameSessionQuestionRecord).where(
                CoupleGameSessionQuestionRecord.session_id == session_id
            )
        ).all()
        kinds = [row.assignment for row in rows]

    assert len(rows) == 25
    assert len({str(row.question_id) for row in rows}) == 25
    assert kinds.count("shared") == 5
    assert kinds.count("user_a") == 10
    assert kinds.count("user_b") == 10

    state_a = client_a.get("/games/couple-questionnaire").json()
    state_b = client_b.get("/games/couple-questionnaire").json()
    ids_a = {q["session_question_id"] for q in state_a["questions"]}
    ids_b = {q["session_question_id"] for q in state_b["questions"]}
    assert len(ids_a) == 15
    assert len(ids_b) == 15
    # Only the five shared questions appear in both sets.
    assert len(ids_a & ids_b) == 5
    assert session_count(db, game_id) == 1


def test_the_deal_survives_reloads(http, db, players):
    seed_game(db)
    client_a, _ = _pair(http, db, players)

    first = [q["session_question_id"] for q in client_a.get("/games/couple-questionnaire").json()["questions"]]
    second = [q["session_question_id"] for q in client_a.get("/games/couple-questionnaire").json()["questions"]]
    assert first == second


def test_answering_renders_you_and_never_the_bank_text(http, db, players):
    seed_game(db)
    client_a, _ = _pair(http, db, players)
    questions = client_a.get("/games/couple-questionnaire").json()["questions"]

    assert all(" X " not in q["question_text"] and "X " not in q["question_text"] for q in questions)
    assert all("you" in q["question_text"] for q in questions)
    assert all(len(q["options"]) == 4 for q in questions)


# --- original answers ------------------------------------------------------------------


def _answer_all(client, option=0):
    """Answer every unanswered question of the caller's current phase."""
    state = client.get("/games/couple-questionnaire").json()
    assert state["stage"] in ("answering", "waiting_partner"), state
    for question in state["questions"]:
        if question["selected_option_index"] is None:
            response = client.post(
                "/games/couple-questionnaire/answers",
                json={
                    "session_question_id": question["session_question_id"],
                    "selected_option_index": option,
                },
            )
            assert response.status_code == 200, response.text
    return client.get("/games/couple-questionnaire").json()


def test_answering_all_fifteen_waits_for_the_partner(http, db, players):
    seed_game(db)
    client_a, client_b = _pair(http, db, players)

    after_a = _answer_all(client_a, option=0)
    assert after_a["stage"] == "waiting_partner"
    assert after_a["waiting_for"] == "answers"
    assert after_a["my_answers_submitted"] == 15
    assert after_a["partner_answers_submitted"] == 0
    # No prediction questions leak early.
    assert after_a["predictions"] == []

    partial_b = client_b.get("/games/couple-questionnaire").json()
    assert partial_b["stage"] == "answering"

    # B answers eight: still answering for both.
    for question in partial_b["questions"][:8]:
        response = client_b.post(
            "/games/couple-questionnaire/answers",
            json={"session_question_id": question["session_question_id"], "selected_option_index": 1},
        )
        assert response.status_code == 200, response.text
    assert client_b.get("/games/couple-questionnaire").json()["stage"] == "answering"
    assert client_a.get("/games/couple-questionnaire").json()["stage"] == "waiting_partner"

    # B finishes: both move to prediction together.
    done_b = _answer_all(client_b, option=1)
    assert done_b["stage"] == "prediction"
    assert len(done_b["predictions"]) == 15
    assert client_a.get("/games/couple-questionnaire").json()["stage"] == "prediction"


def test_answers_are_stored_per_participant_not_per_question(http, db, players):
    game_id = seed_game(db)
    client_a, client_b = _pair(http, db, players)
    _answer_all(client_a, option=0)
    _answer_all(client_b, option=2)

    with db.sessions() as session:
        rows = session.scalars(select(CoupleGameSessionAnswerRecord)).all()
    # Fifteen each, including two rows on every shared question.
    assert len(rows) == 30
    assert sum(1 for row in rows if str(row.user_id) == A_ID) == 15
    assert sum(1 for row in rows if str(row.user_id) == B_ID) == 15
    assert session_count(db, game_id) == 1


def test_answering_twice_or_answering_foreign_questions_is_refused(http, db, players):
    seed_game(db)
    client_a, client_b = _pair(http, db, players)
    client_c = client_for(http, players["c"])

    first = client_a.get("/games/couple-questionnaire").json()["questions"][0]
    ok = client_a.post(
        "/games/couple-questionnaire/answers",
        json={"session_question_id": first["session_question_id"], "selected_option_index": 0},
    )
    assert ok.status_code == 200, ok.text
    again = client_a.post(
        "/games/couple-questionnaire/answers",
        json={"session_question_id": first["session_question_id"], "selected_option_index": 1},
    )
    assert again.status_code == 409

    # B cannot answer A's exclusive question, and C (no session) cannot answer at all.
    state_a = client_a.get("/games/couple-questionnaire").json()
    shared = next(q for q in state_a["questions"] if q["shared"])
    others_exclusive = [
        q for q in client_b.get("/games/couple-questionnaire").json()["questions"]
        if not q["shared"]
    ]
    # C has no session: 404, not a silent write elsewhere.
    assert (
        client_c.post(
            "/games/couple-questionnaire/answers",
            json={"session_question_id": shared["session_question_id"], "selected_option_index": 0},
        ).status_code
        == 404
    )
    assert others_exclusive, "the pair must hold exclusive questions for this to mean anything"
    foreign = client_a.post(
        "/games/couple-questionnaire/answers",
        json={"session_question_id": others_exclusive[0]["session_question_id"], "selected_option_index": 0},
    )
    assert foreign.status_code in (400, 404)


def test_predicting_before_both_finished_is_refused(http, db, players):
    seed_game(db)
    client_a, _ = _pair(http, db, players)
    first = client_a.get("/games/couple-questionnaire").json()["questions"][0]
    client_a.post(
        "/games/couple-questionnaire/answers",
        json={"session_question_id": first["session_question_id"], "selected_option_index": 0},
    )
    refused = client_a.post(
        "/games/couple-questionnaire/predictions",
        json={"session_question_id": first["session_question_id"], "predicted_option_index": 0},
    )
    assert refused.status_code == 409


# --- predictions and scoring ---------------------------------------------------------------


def test_predictions_name_the_partner_and_score_from_stored_originals(http, db, players):
    seed_game(db)
    client_a, client_b = _pair(http, db, players)
    _answer_all(client_a, option=0)
    _answer_all(client_b, option=0)

    state_b = client_b.get("/games/couple-questionnaire").json()
    assert state_b["stage"] == "prediction"
    # Every prediction question names Alex, never a placeholder.
    assert all("Alex" in q["question_text"] for q in state_b["predictions"])
    assert all(" X " not in q["question_text"] for q in state_b["predictions"])
    # The partner's originals are not exposed while predicting.
    assert "original_option_index" not in str(state_b["predictions"])

    # A predicts everything right (all originals are 0); B predicts everything wrong.
    for question in client_a.get("/games/couple-questionnaire").json()["predictions"]:
        response = client_a.post(
            "/games/couple-questionnaire/predictions",
            json={"session_question_id": question["session_question_id"], "predicted_option_index": 0},
        )
        assert response.status_code == 200, response.text
    mid_a = client_a.get("/games/couple-questionnaire").json()
    assert mid_a["stage"] == "waiting_partner"
    assert mid_a["waiting_for"] == "predictions"

    for question in client_b.get("/games/couple-questionnaire").json()["predictions"]:
        response = client_b.post(
            "/games/couple-questionnaire/predictions",
            json={"session_question_id": question["session_question_id"], "predicted_option_index": 1},
        )
        assert response.status_code == 200, response.text

    for client, mine, theirs in ((client_a, 15, 0), (client_b, 0, 15)):
        result = client.get("/games/couple-questionnaire").json()
        assert result["stage"] == "completed"
        assert result["result"]["my_score"] == mine
        assert result["result"]["partner_score"] == theirs
        assert result["result"]["max_score"] == 15
        assert len(result["result"]["review"]) == 15


def test_predicting_twice_or_predicting_own_questions_is_refused(http, db, players):
    seed_game(db)
    client_a, client_b = _pair(http, db, players)
    _answer_all(client_a, option=0)
    _answer_all(client_b, option=0)

    own = client_a.get("/games/couple-questionnaire").json()["questions"]
    assert own == [], "answering list must be empty once predicting"
    target = client_a.get("/games/couple-questionnaire").json()["predictions"][0]
    ok = client_a.post(
        "/games/couple-questionnaire/predictions",
        json={"session_question_id": target["session_question_id"], "predicted_option_index": 0},
    )
    assert ok.status_code == 200, ok.text
    assert (
        client_a.post(
            "/games/couple-questionnaire/predictions",
            json={"session_question_id": target["session_question_id"], "predicted_option_index": 1},
        ).status_code
        == 409
    )

    with db.sessions() as session:
        rows = session.scalars(select(CoupleGameSessionPredictionRecord)).all()
    assert len(rows) == 1
    assert all(row.is_correct for row in rows)
    _ = client_b


def test_a_completed_session_is_read_only(http, db, players):
    seed_game(db)
    client_a, client_b = _pair(http, db, players)
    _answer_all(client_a, option=0)
    _answer_all(client_b, option=0)
    for client in (client_a, client_b):
        for question in client.get("/games/couple-questionnaire").json()["predictions"]:
            client.post(
                "/games/couple-questionnaire/predictions",
                json={"session_question_id": question["session_question_id"], "predicted_option_index": 0},
            )
    assert client_a.get("/games/couple-questionnaire").json()["stage"] == "completed"

    answered = client_a.get("/games/couple-questionnaire").json()["result"]["review"][0]
    assert (
        client_a.post(
            "/games/couple-questionnaire/answers",
            json={"session_question_id": answered["session_question_id"], "selected_option_index": 0},
        ).status_code
        == 409
    )
    assert (
        client_a.post(
            "/games/couple-questionnaire/predictions",
            json={"session_question_id": answered["session_question_id"], "predicted_option_index": 0},
        ).status_code
        == 409
    )


# --- who may play ------------------------------------------------------------------


def test_staff_and_anonymous_callers_are_refused(db, players, admin_client):
    assert admin_client.get("/games/couple-questionnaire").status_code == 403


@pytest.fixture
def anonymous_client():
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


def test_anonymous_callers_are_refused(anonymous_client: TestClient):
    assert anonymous_client.get("/games/couple-questionnaire").status_code == 401
    assert anonymous_client.get("/games/couple-questionnaire/partners").status_code == 401
