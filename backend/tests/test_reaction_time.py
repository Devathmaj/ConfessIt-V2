"""The Reaction Time game: spotlight featuring, scheduler sessions, five submissions.

The properties worth pinning are the ones a careless implementation would get
quietly wrong:

**Featuring is a spotlight only.** ``POST /admin/reaction-time`` pins one
game per date in the shared spotlight table without touching any session --
the same split the Couple Questionnaire keeps. Sessions roll on the game's
own cadence with a fresh five-submission budget per player each period.

**Playability comes from the session, not the spotlight.** A current active
session is playable whether or not the day is featured -- the game opens like
every other game, through availability plus the reset loop, never through a
per-date admin gate.

**One start click is one consumed submission.** ``POST /start`` inserts the
pending row before the red wait begins, so a false start still spent its
submission and a refresh mid-wait cannot grant another one.

**Five submissions per user per session, enforced server-side.** The attempt
number is assigned from the stored count, a sixth start is refused, and two
concurrent starts serialize to one winner and one refusal.

**Completed rows are immutable.** Settling a pending attempt writes it once;
a second submit -- or another user's id -- is refused rather than
overwritten.

**Statistics come from the rows.** Best is the minimum valid reaction time,
average the mean of valid reactions only, and both are null until the caller
has a valid attempt. Nothing the client sends can mint an attempt or invent
a statistic.

Sessions are opened for the real today throughout: the reset loop counts
calendar days, so fixed-date fixtures would read as ended before the first
guess lands.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.accounts import AccountType
from app.db_models import FeaturedGameRecord, ReactionTimeGameRecord, ReactionTimeSubmissionRecord
from app.main import app
from app.models import UserDetails
from app.services import game_scheduler
from app.services.auth_service import get_current_user
from conftest import override_get_admin_user, override_get_current_user


def today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def day_iso(offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def _set_admin() -> None:
    app.dependency_overrides[get_current_user] = override_get_admin_user


def _set_user(user: UserDetails) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def make_player(db, user_id: str, regno: str) -> UserDetails:
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', 'other') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "name": "Player", "email": f"{regno}@t.com"},
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name="Player",
        email=f"{regno}@t.com",
        year=2,
        department="Class",
        gender="other",
        account_type=AccountType.USERS.value,
    )


def feature(admin: TestClient, payload: dict):
    return admin.post("/admin/reaction-time", json=payload)


def feature_today(admin: TestClient):
    response = feature(admin, {})
    assert response.status_code == 201, response.text
    return response.json()


def ensure_session(db):
    """Open the current session for the real today, like the reset loop would."""
    info = game_scheduler.ensure_single_game(db, "reaction-time")
    assert info["action"] in ("created", "current"), info
    return info["session_id"]


def start(client: TestClient, game_id: str):
    return client.post("/games/reaction-time/start", json={"game_id": game_id})


def complete(client: TestClient, payload: dict):
    return client.post("/games/reaction-time/complete", json=payload)


def state(client: TestClient):
    response = client.get("/games/reaction-time")
    assert response.status_code == 200, response.text
    return response.json()


# --- featuring a day is a spotlight pin ----------------------------------------


def test_featuring_a_day_stores_the_day_and_the_creator(admin_client: TestClient, db):
    """Featuring writes the spotlight table, never a session."""
    created = feature(admin_client, {})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["game_date"] == today_iso()
    assert body["created_by"] == "adminuser"

    with db.sessions() as session:
        row = session.get(FeaturedGameRecord, body["id"])
        assert row is not None
        assert row.game_id == "reaction-time"

    # ...and no session was touched.
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReactionTimeGameRecord)) == 0


def test_a_payload_cannot_carry_puzzle_fields(admin_client: TestClient):
    """No timing, difficulty, attempts or mode: ``extra="forbid"`` refuses them."""
    assert feature(admin_client, {"attempts": 10}).status_code == 422
    assert feature(admin_client, {"mode": "custom"}).status_code == 422
    assert feature(admin_client, {"delay_ms": 500}).status_code == 422
    assert feature(admin_client, {"created_by": "someone-else"}).status_code == 422


def test_a_second_feature_for_the_same_day_is_refused(admin_client: TestClient):
    assert feature(admin_client, {}).status_code == 201
    assert feature(admin_client, {}).status_code == 409

    listing = admin_client.get("/admin/reaction-time")
    assert len(listing.json()) == 1


def test_the_spotlight_seats_one_game_per_date(admin_client: TestClient):
    """Another game already featured today blocks Reaction Time, and vice versa."""
    assert feature_today(admin_client)
    clash = admin_client.post(
        "/admin/featured", json={"game_date": today_iso(), "game_id": "wordle"}
    )
    assert clash.status_code == 409


def test_featured_days_are_listed_newest_first_and_readable_by_id(admin_client: TestClient):
    earlier = day_iso(-2)
    later = day_iso(-1)
    created = feature(admin_client, {"game_date": earlier})
    assert created.status_code == 201, created.text
    feature(admin_client, {"game_date": later})

    listing = admin_client.get("/admin/reaction-time")
    assert [entry["game_date"] for entry in listing.json()] == [later, earlier]

    detail = admin_client.get(f"/admin/reaction-time/{created.json()['id']}")
    assert detail.status_code == 200
    assert detail.json() == created.json()


def test_an_unknown_id_is_404_and_a_malformed_one_is_400(admin_client: TestClient):
    assert (
        admin_client.get("/admin/reaction-time/11111111-1111-1111-1111-111111111111").status_code
        == 404
    )
    assert admin_client.get("/admin/reaction-time/not-an-id").status_code == 400


def test_who_may_feature_a_day(admin_client, moderator_client, client):
    assert moderator_client.get("/admin/reaction-time").status_code == 403
    assert feature(moderator_client, {}).status_code == 403
    assert client.get("/admin/reaction-time").status_code == 403
    assert feature(client, {}).status_code == 403


# --- sessions roll on the game's own cadence -----------------------------------


def test_the_game_owns_a_lifecycle_row_with_no_content_mode(admin_client: TestClient):
    listing = admin_client.get("/admin/games/config")
    entry = next(game for game in listing.json() if game["game_id"] == "reaction-time")
    assert entry["enabled"] is True
    assert entry["is_hidden"] is False
    assert entry["content_mode"] is None

    refused = admin_client.put(
        "/admin/games/config/reaction-time", json={"content_mode": "random"}
    )
    assert refused.status_code == 400


def test_the_reset_loop_opens_a_bare_playable_session(db):
    info = game_scheduler.ensure_single_game(db, "reaction-time")
    assert info["action"] == "created"
    assert info["status"] == "active"
    assert info["period_start"] == today_iso()

    with db.sessions() as session:
        row = session.get(ReactionTimeGameRecord, info["session_id"])
        assert row.status == "active"
        assert row.period_start_date is not None
        assert row.period_end_date is not None

    rerun = game_scheduler.ensure_single_game(db, "reaction-time")
    assert rerun["action"] == "current"
    assert rerun["session_id"] == info["session_id"]


def test_history_may_share_a_date(db):
    """Ended sessions keep their days: the single-active rule is the partial
    unique index, not the date column."""
    first = game_scheduler.ensure_single_game(db, "reaction-time")
    assert first["action"] == "created"
    with db.sessions.begin() as session:
        row = session.get(ReactionTimeGameRecord, first["session_id"])
        row.status = "ended"
    second = game_scheduler.ensure_single_game(db, "reaction-time")
    assert second["action"] == "created"
    assert second["session_id"] != first["session_id"]
    with db.sessions() as session:
        assert (
            session.scalar(select(func.count()).select_from(ReactionTimeGameRecord)) == 2
        )


def test_the_current_session_is_readable_for_the_admin_card(admin_client: TestClient, db):
    missing = admin_client.get("/admin/reaction-time/session")
    assert missing.status_code == 404

    session_id = ensure_session(db)
    current = admin_client.get("/admin/reaction-time/session")
    assert current.status_code == 200
    assert current.json()["id"] == session_id
    assert current.json()["period_start_date"] == today_iso()


def test_a_hidden_game_pauses_session_creation(admin_client: TestClient, db):
    admin_client.put("/admin/games/config/reaction-time", json={"is_hidden": True})
    summary = game_scheduler.ensure_game_sessions(db)
    assert summary["reaction-time"]["action"] == "skipped_hidden"
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ReactionTimeGameRecord)) == 0


# --- playability comes from the session, not the spotlight ---------------------


def test_no_session_reads_as_empty_and_unplayable(db):
    user = make_player(db, "77777777-7777-7777-7777-777777777777", "emptyplayer")
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    body = state(player)
    assert body["game_id"] is None
    assert body["submissions_used"] == 0
    assert body["submissions_remaining"] == 5
    assert body["is_finished"] is False
    assert body["best_ms"] is None
    assert body["average_ms"] is None

    entry = next(
        game for game in player.get("/games/select").json()["games"]
        if game["game_id"] == "reaction-time"
    )
    assert entry["playable"] is False


def test_a_session_is_playable_without_any_spotlight(db):
    """Featuring is never the gate: the reset loop alone opens the game."""
    user = make_player(db, "10101010-1010-1010-1010-101010101010", "spotlessplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    body = state(player)
    assert body["game_id"] == session_id
    assert body["submissions_remaining"] == 5

    entry = next(
        game for game in player.get("/games/select").json()["games"]
        if game["game_id"] == "reaction-time"
    )
    assert entry["playable"] is True
    assert entry["featured"] is False

    response = start(player, session_id)
    assert response.status_code == 200, response.text


def test_featuring_marks_the_spotlight_but_changes_no_session(admin_client: TestClient, db):
    user = make_player(db, "20202020-2020-2020-2020-202020202020", "litplayer")
    session_id = ensure_session(db)
    feature_today(admin_client)

    _set_user(user)
    player = TestClient(app, follow_redirects=False)
    entry = next(
        game for game in player.get("/games/select").json()["games"]
        if game["game_id"] == "reaction-time"
    )
    assert entry["playable"] is True
    assert entry["featured"] is True
    assert state(player)["game_id"] == session_id


def test_disabled_and_hidden_games_refuse_direct_play(admin_client: TestClient, db):
    user = make_player(db, "66666666-6666-6666-6666-666666666666", "availplayer")
    ensure_session(db)

    _set_user(user)
    player = TestClient(app, follow_redirects=False)
    assert state(player)["game_id"] is not None

    _set_admin()
    assert admin_client.put("/admin/games/config/reaction-time", json={"enabled": False}).status_code == 200

    _set_user(user)
    refused = player.get("/games/reaction-time")
    assert refused.status_code == 403
    assert refused.json()["code"] == "game_disabled"

    body = player.get("/games/select")
    entry = next(game for game in body.json()["games"] if game["game_id"] == "reaction-time")
    assert entry["playable"] is False

    _set_admin()
    assert admin_client.put(
        "/admin/games/config/reaction-time", json={"enabled": True, "is_hidden": True}
    ).status_code == 200

    _set_user(user)
    hidden = player.get("/games/reaction-time")
    assert hidden.status_code == 403
    assert hidden.json()["code"] == "game_hidden"
    assert all(
        game["game_id"] != "reaction-time" for game in player.get("/games/select").json()["games"]
    )


# --- the five-submission game --------------------------------------------------


def test_a_start_click_consumes_a_submission_immediately(db):
    user = make_player(db, "88888888-8888-8888-8888-888888888888", "startplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    response = start(player, session_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submissions_used"] == 1
    assert body["submissions_remaining"] == 4
    assert body["is_finished"] is False
    assert body["pending_submission"] is not None
    assert body["pending_submission"]["attempt_number"] == 1
    assert body["pending_submission"]["status"] == "pending"

    with db.sessions() as session:
        rows = session.scalars(
            select(ReactionTimeSubmissionRecord).where(
                ReactionTimeSubmissionRecord.reaction_time_game_id == session_id,
                ReactionTimeSubmissionRecord.user_id == user.id,
            )
        ).all()
        assert len(rows) == 1
        assert rows[0].attempt_number == 1
        assert rows[0].status == "pending"


def test_a_second_start_waits_for_the_pending_attempt(db):
    user = make_player(db, "99999999-9999-9999-9999-999999999999", "pendingplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    assert start(player, session_id).status_code == 200
    # The pending attempt already spent its submission: no new row.
    assert start(player, session_id).status_code == 409
    assert state(player)["submissions_used"] == 1


def test_a_valid_reaction_scores_and_a_false_start_does_not(db):
    user = make_player(db, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "scoreplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    pending = start(player, session_id).json()["pending_submission"]
    settled = complete(
        player, {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 241}
    )
    assert settled.status_code == 200, settled.text
    body = settled.json()
    assert body["valid_count"] == 1
    assert body["false_start_count"] == 0
    assert body["best_ms"] == 241
    assert body["average_ms"] == 241

    pending = start(player, session_id).json()["pending_submission"]
    assert pending["attempt_number"] == 2
    early = complete(
        player,
        {"submission_id": pending["submission_id"], "status": "too_early", "elapsed_ms": 384},
    )
    assert early.status_code == 200, early.text
    body = early.json()
    assert body["submissions_used"] == 2
    assert body["valid_count"] == 1
    assert body["false_start_count"] == 1
    # The 384 ms is informational: best and average ignore it.
    assert body["best_ms"] == 241
    assert body["average_ms"] == 241
    assert body["attempts"][1]["status"] == "too_early"
    assert body["attempts"][1]["elapsed_ms"] == 384
    assert body["attempts"][1]["reaction_time_ms"] is None


def test_best_is_the_minimum_and_average_ignores_false_starts(db):
    user = make_player(db, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "statplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    for reaction in (281, 247):
        pending = start(player, session_id).json()["pending_submission"]
        assert complete(
            player,
            {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": reaction},
        ).status_code == 200
    pending = start(player, session_id).json()["pending_submission"]
    assert complete(
        player, {"submission_id": pending["submission_id"], "status": "too_early", "elapsed_ms": 500}
    ).status_code == 200
    for reaction in (263, 239):
        pending = start(player, session_id).json()["pending_submission"]
        assert complete(
            player,
            {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": reaction},
        ).status_code == 200

    body = state(player)
    assert body["submissions_used"] == 5
    assert body["is_finished"] is True
    assert body["valid_count"] == 4
    assert body["false_start_count"] == 1
    assert body["best_ms"] == 239
    assert body["average_ms"] == (281 + 247 + 263 + 239) / 4
    assert [attempt["attempt_number"] for attempt in body["attempts"]] == [1, 2, 3, 4, 5]


def test_no_sixth_attempt_in_one_session(db):
    user = make_player(db, "cccccccc-cccc-cccc-cccc-cccccccccccc", "fiveplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    for _ in range(5):
        pending = start(player, session_id).json()["pending_submission"]
        assert complete(
            player,
            {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 250},
        ).status_code == 200

    assert start(player, session_id).status_code == 409
    assert state(player)["submissions_remaining"] == 0


def test_a_new_session_means_a_fresh_budget_of_five(db):
    user = make_player(db, "dddddddd-dddd-dddd-dddd-dddddddddddd", "freshplayer")
    first_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    pending = start(player, first_id).json()["pending_submission"]
    complete(player, {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 250})
    assert state(player)["submissions_used"] == 1

    with db.sessions.begin() as session:
        row = session.get(ReactionTimeGameRecord, first_id)
        row.status = "ended"
    second_id = ensure_session(db)
    assert second_id != first_id

    body = state(player)
    assert body["game_id"] == second_id
    assert body["submissions_used"] == 0
    assert body["attempts"] == []


def test_a_stale_screen_cannot_play_an_ended_session(db):
    user = make_player(db, "22222222-3333-4444-5555-666666666666", "staleplayer")
    first_id = ensure_session(db)
    with db.sessions.begin() as session:
        row = session.get(ReactionTimeGameRecord, first_id)
        row.status = "ended"
    second_id = ensure_session(db)
    assert second_id != first_id

    _set_user(user)
    player = TestClient(app, follow_redirects=False)
    assert start(player, first_id).status_code == 409
    assert start(player, "11111111-1111-1111-1111-111111111111").status_code == 404
    assert start(player, "not-an-id").status_code == 400
    assert state(player)["submissions_used"] == 0


def test_a_pending_attempt_does_not_survive_its_session(db):
    user = make_player(db, "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee", "strandplayer")
    first_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    pending = start(player, first_id).json()["pending_submission"]
    with db.sessions.begin() as session:
        row = session.get(ReactionTimeGameRecord, first_id)
        row.status = "ended"
    ensure_session(db)

    # The old pending belongs to history now: settling it is refused rather
    # than written into the wrong session.
    assert complete(
        player, {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 250}
    ).status_code == 409


def test_concurrent_starts_mint_one_attempt(db):
    user = make_player(db, "ffffffff-ffff-ffff-ffff-ffffffffffff", "raceplayer")
    session_id = ensure_session(db)
    _set_user(user)
    barrier = threading.Barrier(2)

    def attempt(_: int):
        barrier.wait(timeout=30)
        return TestClient(app, follow_redirects=False).post(
            "/games/reaction-time/start", json={"game_id": session_id}
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))
    assert sorted(outcome.status_code for outcome in outcomes) == [200, 409]

    _set_user(user)
    assert state(TestClient(app, follow_redirects=False))["submissions_used"] == 1


def test_settled_attempts_are_immutable_and_private(db):
    user = make_player(db, "12345678-1234-1234-1234-123456789012", "ownplayer")
    other = make_player(db, "23456789-2345-3456-4567-567890123456", "otherplayer")
    session_id = ensure_session(db)

    _set_user(user)
    player = TestClient(app, follow_redirects=False)
    pending = start(player, session_id).json()["pending_submission"]
    assert complete(
        player,
        {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 241},
    ).status_code == 200

    # Settling twice is refused -- a valid score cannot be rewritten.
    assert complete(
        player,
        {"submission_id": pending["submission_id"], "status": "too_early", "elapsed_ms": 100},
    ).status_code == 409
    assert state(player)["best_ms"] == 241

    # Another player's id is not found, not settled.
    _set_user(other)
    stranger = TestClient(app, follow_redirects=False)
    assert complete(
        stranger,
        {"submission_id": pending["submission_id"], "status": "too_early", "elapsed_ms": 100},
    ).status_code == 404


def test_measurements_outside_the_honest_bounds_are_refused(db):
    user = make_player(db, "11111111-2222-3333-4444-555555555555", "boundplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    pending = start(player, session_id).json()["pending_submission"]
    assert complete(
        player, {"submission_id": pending["submission_id"], "status": "valid"}
    ).status_code == 400
    assert complete(
        player, {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 0}
    ).status_code == 400
    assert complete(
        player,
        {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 60_001},
    ).status_code == 400
    # The attempt is still pending after three refusals: nothing was stored.
    assert state(player)["pending_submission"]["submission_id"] == pending["submission_id"]
    assert complete(
        player,
        {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 241},
    ).status_code == 200


def test_each_session_scopes_its_own_submissions(db):
    user = make_player(db, "33333333-4444-5555-6666-777777777777", "twodayplayer")
    session_id = ensure_session(db)
    _set_user(user)
    player = TestClient(app, follow_redirects=False)

    pending = start(player, session_id).json()["pending_submission"]
    complete(player, {"submission_id": pending["submission_id"], "status": "valid", "reaction_time_ms": 241})

    with db.sessions() as session:
        rows = session.scalars(
            select(ReactionTimeSubmissionRecord).where(
                ReactionTimeSubmissionRecord.user_id == user.id
            )
        ).all()
        assert len(rows) == 1
        assert str(rows[0].reaction_time_game_id) == session_id
