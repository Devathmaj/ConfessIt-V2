"""Trivia Drop: individually released questions, one answer each, ranked by speed.

Each question is its own session -- there is no reset cadence. An
administrator authors four options with one or more correct (the mode is
derived, never configured), deploys immediately or schedules a UTC release,
and each player answers the live question at most once. Response time runs
from the question's actual release, not from page load, and correctness is
exact set equality with no partial credit.

Identities are switched explicitly per phase (``_set_admin`` / ``_set_user``)
because the auth override is application-global.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.db_models import (
    TriviaDropOptionRecord,
    TriviaDropQuestionRecord,
    TriviaDropSubmissionRecord,
)
from app.main import app
from app.models import UserDetails
from app.services import game_scheduler, trivia_service
from app.services.auth_service import get_current_user
from conftest import override_get_admin_user

UTC = timezone.utc

PLAYER_UUID = "44444444-4444-4444-4444-444444444444"
OTHER_UUID = "55555555-5555-5555-5555-555555555555"
THIRD_UUID = "66666666-6666-6666-6666-666666666666"

OPTIONS = ["Earth", "Mars", "Venus", "Jupiter"]
MULTI_OPTIONS = ["Red", "Green", "Blue", "Yellow"]


def _set_admin() -> None:
    app.dependency_overrides[get_current_user] = override_get_admin_user


def _set_user(user: UserDetails) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def make_admin_client() -> TestClient:
    _set_admin()
    return TestClient(app, follow_redirects=False)


def make_player(db, user_id: str, regno: str, name: str = "Player") -> UserDetails:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', 'other') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "name": name, "email": f"{regno}@t.com"},
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=name,
        email=f"{regno}@t.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )


def make_player_client(user: UserDetails) -> TestClient:
    _set_user(user)
    return TestClient(app, follow_redirects=False)


def set_config(admin: TestClient, game_id: str, payload: dict) -> dict:
    _set_admin()
    response = admin.put(f"/admin/games/config/{game_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def seed_question(
    db,
    *,
    text_body: str = "Which planet is known as the Red Planet?",
    options: list = OPTIONS,
    correct: list = [1],
    status: str = "active",
    scheduled_for=None,
    released_at=None,
    ended_at=None,
):
    """Insert a question straight into the tables, with its four options."""
    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        row = TriviaDropQuestionRecord(
            question_text=text_body,
            status=status,
            scheduled_for=scheduled_for,
            released_at=released_at if released_at is not None else (now if status in ("active", "ended") else None),
            ended_at=ended_at if ended_at is not None else (now if status == "ended" else None),
            created_by="adminuser",
        )
        session.add(row)
        session.flush()
        question_id = str(row.id)
        for index, option_text in enumerate(options):
            session.add(
                TriviaDropOptionRecord(
                    question_id=question_id,
                    option_index=index,
                    option_text=option_text,
                    is_correct=index in set(correct),
                )
            )
        session.flush()
        return {"id": question_id}


def live_question_id(db) -> str | None:
    with db.sessions() as session:
        row = session.scalar(
            select(TriviaDropQuestionRecord).where(
                TriviaDropQuestionRecord.status == "active"
            )
        )
        return str(row.id) if row is not None else None


def submission_for(db, question_id: str, user_id: str):
    with db.sessions() as session:
        return session.scalar(
            select(TriviaDropSubmissionRecord).where(
                TriviaDropSubmissionRecord.question_id == question_id,
                TriviaDropSubmissionRecord.user_id == user_id,
            )
        )


# --- 1. admin authoring and validation ------------------------------------------


def test_a_single_answer_question_is_valid(db):
    admin = make_admin_client()
    response = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Which planet is known as the Red Planet?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "draft",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer_mode"] == "single"
    assert body["correct_indexes"] == [1]
    assert body["status"] == "draft"


def test_a_multiple_answer_question_is_valid_and_needs_no_mode(db):
    admin = make_admin_client()
    response = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Which are primary colors?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [0, 2],
            "release_mode": "draft",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["answer_mode"] == "multiple"
    assert body["correct_indexes"] == [0, 2]


def test_no_separate_answer_mode_configuration_exists(db):
    admin = make_admin_client()
    _set_admin()
    # A mode field is not part of the contract: it is refused, not stored.
    response = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Which planet is known as the Red Planet?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "draft",
            "answer_mode": "single",
        },
    )
    assert response.status_code == 422


def test_a_question_without_a_correct_option_is_rejected(db):
    admin = make_admin_client()
    _set_admin()
    assert (
        admin.post(
            "/admin/trivia-drop",
            json={"question_text": "Q?", "options": OPTIONS, "correct_indexes": []},
        ).status_code
        == 400
    )


def test_empty_text_and_bad_options_are_rejected(db):
    admin = make_admin_client()
    _set_admin()
    assert (
        admin.post(
            "/admin/trivia-drop",
            json={"question_text": "   ", "options": OPTIONS, "correct_indexes": [1]},
        ).status_code
        == 400
    )
    short = admin.post(
        "/admin/trivia-drop",
        json={"question_text": "Q?", "options": OPTIONS[:3], "correct_indexes": [1]},
    )
    assert short.status_code == 400
    blank = list(OPTIONS)
    blank[2] = "  "
    assert (
        admin.post(
            "/admin/trivia-drop",
            json={"question_text": "Q?", "options": blank, "correct_indexes": [1]},
        ).status_code
        == 400
    )
    assert admin.get("/admin/trivia-drop").json()["questions"] == []


def test_duplicate_options_are_rejected_case_insensitively(db):
    admin = make_admin_client()
    _set_admin()
    duplicate = ["Mars", "Venus", "  mars ", "Jupiter"]
    response = admin.post(
        "/admin/trivia-drop",
        json={"question_text": "Q?", "options": duplicate, "correct_indexes": [0]},
    )
    assert response.status_code == 400
    assert "uplicate" in response.json()["detail"]


def test_foreign_correct_option_references_are_rejected(db):
    admin = make_admin_client()
    _set_admin()
    assert (
        admin.post(
            "/admin/trivia-drop",
            json={"question_text": "Q?", "options": OPTIONS, "correct_indexes": [7]},
        ).status_code
        == 400
    )


def test_duplicate_correct_indexes_are_normalized(db):
    admin = make_admin_client()
    response = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Which planet is known as the Red Planet?",
            "options": OPTIONS,
            "correct_indexes": [1, 1],
            "release_mode": "draft",
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["correct_indexes"] == [1]


def test_drafts_are_not_publicly_available(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviadraft")
    client = make_player_client(user)
    _set_admin()
    admin.post(
        "/admin/trivia-drop",
        json={"question_text": "Hidden?", "options": OPTIONS, "correct_indexes": [0]},
    )
    _set_user(user)
    body = client.get("/games/trivia-drop").json()
    assert body["question_id"] is None
    assert body["options"] == []


def test_scheduled_questions_stay_unavailable_before_release(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviasched")
    client = make_player_client(user)
    _set_admin()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    created = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Soon?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future,
        },
    )
    assert created.status_code == 201, created.text
    _set_user(user)
    body = client.get("/games/trivia-drop").json()
    assert body["question_id"] is None
    # And the scheduled content itself is admin-only.
    assert client.get(f"/admin/trivia-drop/{created.json()['id']}").status_code == 403


def test_a_draft_can_be_edited_but_a_live_one_cannot(db):
    admin = make_admin_client()
    created = admin.post(
        "/admin/trivia-drop",
        json={"question_text": "Draft?", "options": OPTIONS, "correct_indexes": [0]},
    ).json()
    _set_admin()
    edited = admin.put(
        f"/admin/trivia-drop/{created['id']}",
        json={"question_text": "Edited?", "correct_indexes": [0, 1]},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["question_text"] == "Edited?"
    assert edited.json()["answer_mode"] == "multiple"

    deployed = admin.post(f"/admin/trivia-drop/{created['id']}/deploy")
    assert deployed.status_code == 200, deployed.text
    refused = admin.put(
        f"/admin/trivia-drop/{created['id']}", json={"question_text": "Too late?"}
    )
    assert refused.status_code == 409


def test_trivia_drop_has_no_content_mode_and_no_reset_cadence(db):
    admin = make_admin_client()
    _set_admin()
    assert (
        admin.put(
            "/admin/games/config/trivia-drop", json={"content_mode": "random"}
        ).status_code
        == 400
    )
    refused = admin.put(
        "/admin/games/config/trivia-drop", json={"reset_cadence": "weekly"}
    )
    assert refused.status_code == 400


# --- 2. release and session lifecycle -------------------------------------------


def test_deploy_now_activates_and_ends_the_previous_question(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviadeploy")
    client = make_player_client(user)

    _set_admin()
    first = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "First?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "now",
        },
    ).json()
    _set_user(user)
    assert client.post(
        "/games/trivia-drop/submit",
        json={"question_id": first["id"], "selected_indexes": [1]},
    ).status_code == 200

    _set_admin()
    second = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Second?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [0, 2],
            "release_mode": "now",
        },
    ).json()
    assert second["id"] != first["id"]

    with db.sessions() as session:
        old = session.get(TriviaDropQuestionRecord, first["id"])
        assert old.status == "ended"
        assert old.ended_at is not None
        new = session.get(TriviaDropQuestionRecord, second["id"])
        assert new.status == "active"
        assert new.released_at is not None
        # The old session keeps its options, key and submissions.
        assert (
            session.scalar(
                select(TriviaDropQuestionRecord).where(
                    TriviaDropQuestionRecord.status == "active"
                )
            ).id
            == second["id"]
        )
    before = submission_for(db, first["id"], PLAYER_UUID)
    assert before is not None and before.is_correct is True
    # The previous question no longer accepts answers.
    _set_user(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": first["id"], "selected_indexes": [1]},
        ).status_code
        == 409
    )


def test_scheduling_does_not_end_the_active_session_early(db):
    admin = make_admin_client()
    _set_admin()
    live = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Live?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "now",
        },
    ).json()
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    waiting = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Later?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future.isoformat(),
        },
    )
    assert waiting.status_code == 201, waiting.text

    # Before its time the scheduled question changes nothing.
    summary = game_scheduler.ensure_single_game(
        db, "trivia-drop", datetime.now(timezone.utc)
    )
    assert summary["action"] == "current"
    assert summary["question_id"] == live["id"]
    assert live_question_id(db) == live["id"]

    # At release the old session ends as the scheduled one goes live.
    released = game_scheduler.ensure_single_game(db, "trivia-drop", future + timedelta(seconds=5))
    assert released["action"] == "released"
    assert released["question_id"] == waiting.json()["id"]
    assert live_question_id(db) == waiting.json()["id"]
    with db.sessions() as session:
        old = session.get(TriviaDropQuestionRecord, live["id"])
        assert old.status == "ended" and old.ended_at is not None
        new = session.get(TriviaDropQuestionRecord, waiting.json()["id"])
        # Response time runs from the actual activation, not the intention.
        assert new.released_at >= new.scheduled_for


def test_repeated_scheduler_runs_do_not_duplicate_activation(db):
    admin = make_admin_client()
    _set_admin()
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    waiting = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Due?",
            "options": OPTIONS,
            "correct_indexes": [2],
            "release_mode": "scheduled",
            "scheduled_for": future.isoformat(),
        },
    ).json()
    moment = future + timedelta(minutes=1)
    first = game_scheduler.ensure_single_game(db, "trivia-drop", moment)
    assert first["action"] == "released"
    second = game_scheduler.ensure_single_game(db, "trivia-drop", moment + timedelta(minutes=5))
    assert second["action"] == "current"
    assert second["question_id"] == waiting["id"]
    with db.sessions() as session:
        actives = session.scalars(
            select(TriviaDropQuestionRecord).where(
                TriviaDropQuestionRecord.status == "active"
            )
        ).all()
        assert len(actives) == 1


def test_conflicting_schedules_are_rejected(db):
    admin = make_admin_client()
    _set_admin()
    future = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
    first = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "First?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future,
        },
    )
    assert first.status_code == 201, first.text
    clash = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Second?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [1],
            "release_mode": "scheduled",
            "scheduled_for": future,
        },
    )
    assert clash.status_code == 409


def test_concurrent_deploys_cannot_overlap_active_sessions(db):
    admin = make_admin_client()
    _set_admin()
    first_id = admin.post(
        "/admin/trivia-drop",
        json={"question_text": "A?", "options": OPTIONS, "correct_indexes": [0]},
    ).json()["id"]
    second_id = admin.post(
        "/admin/trivia-drop",
        json={"question_text": "B?", "options": MULTI_OPTIONS, "correct_indexes": [1]},
    ).json()["id"]

    barrier = threading.Barrier(2)

    def attempt(question_id: str) -> None:
        barrier.wait(timeout=30)
        trivia_service.deploy_question(db, question_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt, question_id) for question_id in (first_id, second_id)]:
            future.result(timeout=60)

    with db.sessions() as session:
        actives = session.scalars(
            select(TriviaDropQuestionRecord).where(
                TriviaDropQuestionRecord.status == "active"
            )
        ).all()
        assert len(actives) == 1


def test_the_single_active_rule_is_a_database_constraint(db):
    seed_question(db, text_body="One?")
    with pytest.raises(IntegrityError):
        seed_question(db, text_body="Two?")


def test_scheduled_times_follow_utc(db):
    admin = make_admin_client()
    _set_admin()
    # 22:00 at +05:30 is 16:30 UTC -- stored converted. The date is relative so
    # this stays a future release whenever the suite runs.
    scheduled_utc = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=16, minute=30, second=0, microsecond=0
    )
    ist = timezone(timedelta(hours=5, minutes=30))
    response = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Zones?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": scheduled_utc.astimezone(ist).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    stored = datetime.fromisoformat(response.json()["scheduled_for"])
    assert stored == scheduled_utc


def test_hidden_games_release_nothing(db):
    admin = make_admin_client()
    set_config(admin, "trivia-drop", {"is_hidden": True})
    _set_admin()
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    waiting = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Held?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future.isoformat(),
        },
    ).json()
    summary = game_scheduler.ensure_single_game(db, "trivia-drop", future + timedelta(minutes=1))
    assert summary["action"] == "skipped_hidden"
    assert live_question_id(db) is None
    with db.sessions() as session:
        row = session.get(TriviaDropQuestionRecord, waiting["id"])
        assert row.status == "scheduled"


def test_enabled_and_hidden_states_keep_their_distinct_meanings(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviaavail")
    client = make_player_client(user)
    seed_question(db)

    set_config(admin, "trivia-drop", {"enabled": False})
    _set_user(user)
    selection = client.get("/games/select").json()
    entry = next(game for game in selection["games"] if game["game_id"] == "trivia-drop")
    assert entry["playable"] is False
    assert client.get("/games/trivia-drop").status_code == 403

    set_config(admin, "trivia-drop", {"enabled": True, "is_hidden": True})
    _set_user(user)
    selection = client.get("/games/select").json()
    assert all(game["game_id"] != "trivia-drop" for game in selection["games"])
    assert client.get("/games/trivia-drop").status_code == 403


def test_run_resets_covers_trivia_releases(db):
    admin = make_admin_client()
    set_config(admin, "trivia-drop", {"enabled": True, "is_hidden": False})
    _set_admin()
    past_due = datetime.now(timezone.utc) + timedelta(minutes=5)
    admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Due?",
            "options": OPTIONS,
            "correct_indexes": [3],
            "release_mode": "scheduled",
            "scheduled_for": past_due.isoformat(),
        },
    )
    summary = game_scheduler.ensure_game_sessions(db, past_due + timedelta(minutes=1))
    assert summary["trivia-drop"]["action"] == "released"
    assert live_question_id(db) is not None


def test_past_releases_are_rejected_but_a_scheduled_question_may_deploy_early(db):
    admin = make_admin_client()
    _set_admin()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    refused = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Late?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": past,
        },
    )
    assert refused.status_code == 400
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    waiting = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Early?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future,
        },
    ).json()
    deployed = admin.post(f"/admin/trivia-drop/{waiting['id']}/deploy")
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["status"] == "active"
    assert live_question_id(db) == waiting["id"]


def test_scheduled_release_can_be_rescheduled_or_cancelled(db):
    admin = make_admin_client()
    _set_admin()
    future = datetime.now(timezone.utc) + timedelta(hours=4)
    created = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Movable?",
            "options": OPTIONS,
            "correct_indexes": [0],
            "release_mode": "scheduled",
            "scheduled_for": future.isoformat(),
        },
    ).json()
    later = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    moved = admin.post(f"/admin/trivia-drop/{created['id']}/reschedule", json={"scheduled_for": later})
    assert moved.status_code == 200, moved.text
    assert moved.json()["scheduled_for"] is not None
    cancelled = admin.post(f"/admin/trivia-drop/{created['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    # A cancelled question never releases.
    summary = game_scheduler.ensure_single_game(
        db, "trivia-drop", datetime.now(timezone.utc) + timedelta(hours=7)
    )
    assert summary["action"] in ("current", "none")


# --- 3. answering ----------------------------------------------------------------


def test_the_live_question_hides_the_key(db):
    seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviakey")
    client = make_player_client(user)
    response = client.get("/games/trivia-drop")
    assert response.status_code == 200
    body = response.json()
    assert body["answer_mode"] == "single"
    assert [option["option_index"] for option in body["options"]] == [0, 1, 2, 3]
    lowered = response.text.lower()
    assert "is_correct" not in lowered
    assert "correct_indexes" not in lowered


def test_the_multiple_instruction_reveals_no_count(db):
    seed_question(db, text_body="Colors?", options=MULTI_OPTIONS, correct=[0, 2])
    user = make_player(db, PLAYER_UUID, "triviamode")
    client = make_player_client(user)
    body = client.get("/games/trivia-drop").json()
    assert body["answer_mode"] == "multiple"
    assert "2" not in (body.get("message") or "")


def test_a_correct_single_answer_is_recorded(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviasingle")
    client = make_player_client(user)
    response = client.post(
        "/games/trivia-drop/submit",
        json={"question_id": game["id"], "selected_indexes": [1]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted"] is True
    # Confirmation only: no correctness, no time, no key.
    assert set(body.keys()) == {"question_id", "submitted", "submitted_at", "message"}
    row = submission_for(db, game["id"], PLAYER_UUID)
    assert row is not None and row.is_correct is True
    assert row.selected_options == [1]
    assert row.response_time_ms >= 0
    # Answered players read a confirmation, never a board.
    assert client.get("/games/trivia-drop").json()["submitted"] is True
    # And never a second answer.
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [2]},
        ).status_code
        == 409
    )


def test_an_incorrect_single_answer_is_persisted_as_incorrect(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviawrong")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [2]},
        ).status_code
        == 200
    )
    row = submission_for(db, game["id"], PLAYER_UUID)
    assert row.is_correct is False
    assert row.selected_options == [2]


def test_empty_and_invalid_selections_are_refused(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviabad")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": []},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [9]},
        ).status_code
        == 400
    )
    assert submission_for(db, game["id"], PLAYER_UUID) is None


def test_duplicate_selections_are_normalized(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviadup")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [1, 1]},
        ).status_code
        == 200
    )
    assert submission_for(db, game["id"], PLAYER_UUID).selected_options == [1]


def test_multiple_answer_correctness_is_exact_set_equality(db):
    game = seed_question(db, text_body="Colors?", options=MULTI_OPTIONS, correct=[0, 2])
    cases = [
        ([0, 2], True),
        ([2, 0], True),  # order never matters
        ([0], False),  # partial
        ([0, 1, 2], False),  # superset
        ([1, 3], False),  # only wrong
    ]
    for position, (selection, expected) in enumerate(cases):
        user_id = f"7777777{position}-7777-7777-7777-777777777777"
        user = make_player(db, user_id, f"triviaset{position}")
        client = make_player_client(user)
        assert (
            client.post(
                "/games/trivia-drop/submit",
                json={"question_id": game["id"], "selected_indexes": selection},
            ).status_code
            == 200
        )
        assert submission_for(db, game["id"], user_id).is_correct is expected


def test_concurrent_submissions_share_one_row(db):
    game = seed_question(db)
    make_player(db, PLAYER_UUID, "triviarace")
    barrier = threading.Barrier(2)
    outcomes: list = []
    lock = threading.Lock()

    def attempt() -> None:
        from app.services import trivia_gameplay as gameplay

        barrier.wait(timeout=30)
        try:
            outcome = gameplay.submit_answer(db, PLAYER_UUID, game["id"], [1])
        except Exception as exc:  # noqa: BLE001 -- the refusal is the assertion
            outcome = exc
        with lock:
            outcomes.append(outcome)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result(timeout=60)

    assert len(outcomes) == 2
    with db.sessions() as session:
        rows = session.scalars(
            select(TriviaDropSubmissionRecord).where(
                TriviaDropSubmissionRecord.question_id == game["id"],
                TriviaDropSubmissionRecord.user_id == PLAYER_UUID,
            )
        ).all()
    assert len(rows) == 1


def test_client_supplied_scores_and_users_are_never_trusted(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviatrust")
    client = make_player_client(user)
    response = client.post(
        "/games/trivia-drop/submit",
        json={"question_id": game["id"], "selected_indexes": [1], "is_correct": True},
    )
    assert response.status_code == 422
    assert submission_for(db, game["id"], PLAYER_UUID) is None


def test_submissions_for_ended_questions_are_refused(db):
    game = seed_question(
        db,
        status="ended",
        released_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ended_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    user = make_player(db, PLAYER_UUID, "trivialate")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [1]},
        ).status_code
        == 409
    )
    # Drafts and future scheduled questions never accept answers either --
    # direct calls cannot bypass the release.
    draft = seed_question(db, status="draft")
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": draft["id"], "selected_indexes": [1]},
        ).status_code
        == 409
    )
    future = seed_question(
        db,
        status="scheduled",
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": future["id"], "selected_indexes": [1]},
        ).status_code
        == 409
    )
    assert submission_for(db, game["id"], PLAYER_UUID) is None
    assert submission_for(db, draft["id"], PLAYER_UUID) is None


def test_the_schema_refuses_a_second_row_for_one_player(db):
    game = seed_question(db)
    make_player(db, PLAYER_UUID, "triviaschema")
    with db.sessions.begin() as session:
        session.add(
            TriviaDropSubmissionRecord(
                question_id=game["id"],
                user_id=PLAYER_UUID,
                selected_options=[1],
                is_correct=True,
                response_time_ms=1000,
            )
        )
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                TriviaDropSubmissionRecord(
                    question_id=game["id"],
                    user_id=PLAYER_UUID,
                    selected_options=[2],
                    is_correct=False,
                    response_time_ms=2000,
                )
            )


def test_staff_cannot_play(admin_client: TestClient, moderator_client: TestClient):
    assert admin_client.get("/games/trivia-drop").status_code == 403
    assert moderator_client.get("/games/trivia-drop").status_code == 403


def test_anonymous_callers_are_refused(db):
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        client = TestClient(app, follow_redirects=False)
        assert client.get("/games/trivia-drop").status_code == 401
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override


# --- 4. response time and ranking --------------------------------------------------


def test_response_time_runs_from_the_actual_release(db):
    released = datetime.now(timezone.utc) - timedelta(seconds=65)
    game = seed_question(db, released_at=released)
    user = make_player(db, PLAYER_UUID, "triviatime")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [1]},
        ).status_code
        == 200
    )
    row = submission_for(db, game["id"], PLAYER_UUID)
    # About sixty-five seconds of elapsed play -- not seconds since page load.
    assert 60_000 <= row.response_time_ms < 120_000


def test_a_delayed_release_still_measures_from_activation(db):
    admin = make_admin_client()
    _set_admin()
    future = datetime.now(timezone.utc) + timedelta(minutes=30)
    waiting = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Late?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "scheduled",
            "scheduled_for": future.isoformat(),
        },
    ).json()
    # The loop runs ten minutes late: activation, not intention, starts the clock.
    actual = future + timedelta(minutes=10)
    game_scheduler.ensure_single_game(db, "trivia-drop", actual)
    user = make_player(db, PLAYER_UUID, "trivialate2")
    client = make_player_client(user)
    _set_user(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": waiting["id"], "selected_indexes": [1]},
        ).status_code
        == 200
    )
    row = submission_for(db, waiting["id"], PLAYER_UUID)
    assert row.response_time_ms < 10_000


def test_results_rank_correct_first_then_fastest_with_a_tiebreaker(db):
    released = datetime.now(timezone.utc) - timedelta(hours=1)
    ended = datetime.now(timezone.utc) - timedelta(minutes=1)
    game = seed_question(db, released_at=released, status="ended", ended_at=ended)
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    entries = [
        # (user, correct, response_ms, submitted_at) -- deliberately out of order.
        (PLAYER_UUID, "fa", "Fast", False, 5_000, base),
        (OTHER_UUID, "sl", "Slow", True, 60_000, base + timedelta(seconds=1)),
        (THIRD_UUID, "md", "Mid", True, 5_000, base + timedelta(seconds=2)),
    ]
    for user_id, regno, name, correct, response_ms, submitted_at in entries:
        make_player(db, user_id, regno, name)
        with db.sessions.begin() as session:
            session.add(
                TriviaDropSubmissionRecord(
                    question_id=game["id"],
                    user_id=user_id,
                    selected_options=[1] if correct else [0],
                    is_correct=correct,
                    submitted_at=submitted_at,
                    response_time_ms=response_ms,
                )
            )
    user = make_player(db, "88888888-8888-8888-8888-888888888888", "triviarank")
    client = make_player_client(user)
    body = client.get(f"/games/trivia-drop/results/{game['id']}").json()
    assert body["correct_indexes"] == [1]
    assert [entry["display_name"] for entry in body["leaderboard"]] == ["Mid", "Slow", "Fast"]
    assert [entry["rank"] for entry in body["leaderboard"]] == [1, 2, 3]
    # The same speed settles by submission instant: Mid answered first.
    assert body["leaderboard"][0]["response_time_ms"] == 5_000
    assert body["my_submission"] is None


def test_live_questions_refuse_results_but_ended_ones_share_them(db):
    game = seed_question(db)
    user = make_player(db, PLAYER_UUID, "triviaresults")
    client = make_player_client(user)
    assert client.get(f"/games/trivia-drop/results/{game['id']}").status_code == 409
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": game["id"], "selected_indexes": [1]},
        ).status_code
        == 200
    )
    # Ended by the next release: history keeps its key, answers and board.
    admin = make_admin_client()
    _set_admin()
    admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Next?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [3],
            "release_mode": "now",
        },
    )
    _set_user(user)
    body = client.get(f"/games/trivia-drop/results/{game['id']}").json()
    assert body["correct_indexes"] == [1]
    assert body["my_submission"] is not None
    assert body["my_submission"]["is_correct"] is True
    assert body["my_submission"]["selected_indexes"] == [1]
    assert len(body["leaderboard"]) == 1


def test_unknown_and_unreleased_questions_have_no_results(db):
    user = make_player(db, PLAYER_UUID, "trivialookup")
    client = make_player_client(user)
    assert (
        client.get("/games/trivia-drop/results/11111111-1111-1111-1111-111111111111").status_code
        == 404
    )
    draft = seed_question(db, status="draft")
    assert client.get(f"/games/trivia-drop/results/{draft['id']}").status_code == 404


# --- 5. previous question --------------------------------------------------------------


def test_previous_is_empty_when_nothing_has_ended(db):
    user = make_player(db, PLAYER_UUID, "triviaprevnone")
    client = make_player_client(user)
    body = client.get("/games/trivia-drop/previous").json()
    assert body["question_id"] is None
    assert body["options"] == []
    assert body["my_submission"] is None

    # A live question is not a previous one: still nothing to show.
    seed_question(db)
    assert client.get("/games/trivia-drop/previous").json()["question_id"] is None


def test_previous_returns_the_latest_ended_question_with_its_answer(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviaprev")
    client = make_player_client(user)

    _set_admin()
    first = admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "First?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "now",
        },
    ).json()
    _set_user(user)
    assert (
        client.post(
            "/games/trivia-drop/submit",
            json={"question_id": first["id"], "selected_indexes": [1]},
        ).status_code
        == 200
    )
    _set_admin()
    admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Second?",
            "options": MULTI_OPTIONS,
            "correct_indexes": [0, 2],
            "release_mode": "now",
        },
    )
    _set_user(user)
    previous = client.get("/games/trivia-drop/previous").json()
    assert previous["question_id"] == first["id"]
    assert previous["question_text"] == "First?"
    assert previous["correct_indexes"] == [1]
    assert [option["option_text"] for option in previous["options"]] == OPTIONS
    assert previous["my_submission"]["is_correct"] is True
    assert previous["my_submission"]["selected_indexes"] == [1]
    assert previous["my_submission"]["response_time_ms"] >= 0


def test_previous_picks_the_latest_by_end_time(db):
    user = make_player(db, PLAYER_UUID, "triviaprevorder")
    client = make_player_client(user)
    now = datetime.now(timezone.utc)
    seed_question(
        db,
        text_body="Older?",
        status="ended",
        released_at=now - timedelta(hours=3),
        ended_at=now - timedelta(hours=2),
    )
    newer = seed_question(
        db,
        text_body="Newer?",
        options=MULTI_OPTIONS,
        correct=[3],
        status="ended",
        released_at=now - timedelta(hours=1),
        ended_at=now - timedelta(minutes=30),
    )
    previous = client.get("/games/trivia-drop/previous").json()
    assert previous["question_id"] == newer["id"]
    assert previous["correct_indexes"] == [3]
    assert previous["my_submission"] is None


def test_previous_respects_availability(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviaprevhidden")
    client = make_player_client(user)
    seed_question(
        db,
        status="ended",
        released_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ended_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    set_config(admin, "trivia-drop", {"is_hidden": True})
    _set_user(user)
    assert client.get("/games/trivia-drop/previous").status_code == 403


# --- 6. selection scope --------------------------------------------------------------


def test_selection_discovery_and_availability(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "triviadiscover")
    client = make_player_client(user)

    _set_user(user)
    selection = client.get("/games/select").json()
    entry = next(game for game in selection["games"] if game["game_id"] == "trivia-drop")
    assert entry["playable"] is False  # nothing live yet

    _set_admin()
    admin.post(
        "/admin/trivia-drop",
        json={
            "question_text": "Live?",
            "options": OPTIONS,
            "correct_indexes": [1],
            "release_mode": "now",
        },
    )
    _set_user(user)
    selection = client.get("/games/select").json()
    entry = next(game for game in selection["games"] if game["game_id"] == "trivia-drop")
    assert entry["playable"] is True


def test_concurrent_reschedules_to_one_instant_leave_one_holder(db):
    """ENG-005: two simultaneous reschedules to one instant answer 200+409, never two holders.

    The conflict check is read-then-write, so without serialization both pass it and
    both land. Reschedule runs under the game's advisory lock like create/deploy,
    so the loser re-reads the winner's committed row and gets the promised 409.
    """
    from fastapi import HTTPException

    first = trivia_service.create_question(
        db, question_text="First?", options=OPTIONS, correct_indexes=[0],
        created_by="admin",
    )
    second = trivia_service.create_question(
        db, question_text="Second?", options=MULTI_OPTIONS, correct_indexes=[1],
        created_by="admin",
    )
    now = datetime.now(timezone.utc)
    trivia_service.schedule_question(db, first.id, now + timedelta(hours=3))
    trivia_service.schedule_question(db, second.id, now + timedelta(hours=4))

    instant = now + timedelta(hours=5)
    barrier = threading.Barrier(2)
    outcomes: list = []

    def attempt(question_id: str) -> None:
        barrier.wait(timeout=30)
        try:
            trivia_service.reschedule_question(db, question_id, instant)
            outcomes.append(200)
        except HTTPException as exc:
            outcomes.append(exc.status_code)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt, question_id) for question_id in (first.id, second.id)]:
            future.result(timeout=60)

    assert sorted(outcomes) == [200, 409]
    with db.sessions() as session:
        holders = session.scalars(
            select(TriviaDropQuestionRecord).where(
                TriviaDropQuestionRecord.status == "scheduled",
                TriviaDropQuestionRecord.scheduled_for == instant,
            )
        ).all()
        assert len(holders) == 1
