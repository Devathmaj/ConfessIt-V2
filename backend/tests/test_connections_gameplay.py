"""The Connections game: admin puzzles, sessions, play, and scoring.

The puzzle itself is configured through ``POST /admin/connections`` and the
scheduled puzzles below; this file is the whole feature -- configuration,
participation and the shared session lifecycle. The properties worth pinning:

**Manual content only.** Every session needs an administrator to supply its
own sixteen words. The scheduler never draws, reuses or invents words: a
session with no valid puzzle stays pending and unplayable, and no random
puzzle is ever generated automatically.

**One submission per player per puzzle.** A ``connections_submissions`` row
pairs a user with a session, and the unique pair is the schema's rule, not a
service check. Two concurrent submissions race that insert and exactly one of
them owns the row.

**Scores are computed and persisted, never displayed.** The submit response is
a confirmation carrying no score, no correctness and no membership; the two
persisted numbers (correct groups 0-4, correct words 0-16 by best mapping)
are read back from the database here, never from the API.

Identities are switched explicitly per phase (``_set_admin`` / ``_set_user``)
because the auth override is application-global.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from itertools import permutations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.accounts import AccountType
from app.db_models import (
    ConnectionsGameRecord,
    ConnectionsSubmissionRecord,
    ConnectionsWordRecord,
)
from app.limits import MAX_FUTURE_SCHEDULE_DAYS
from app.main import app
from app.models import UserDetails
from app.services import game_scheduler
from app.services.auth_service import get_current_user
from app.services.connections_gameplay import score_arrangement, shuffle_words_for_player
from conftest import override_get_admin_user

UTC = timezone.utc

MON = date(2026, 10, 5)
TUE = date(2026, 10, 6)
WED = date(2026, 10, 7)
THU = date(2026, 10, 8)
FRI = date(2026, 10, 9)

PLAYER_UUID = "44444444-4444-4444-4444-444444444444"
OTHER_UUID = "55555555-5555-5555-5555-555555555555"

GROUPS = [
    ["Apple", "Banana", "Cherry", "Date"],
    ["Elm", "Fir", "Grove", "Hazel"],
    ["Ivory", "Jade", "Kite", "Lemon"],
    ["Maple", "North", "Ocean", "Piano"],
]

OTHER_GROUPS = [
    ["Quilt", "River", "Stone", "Tiger"],
    ["Umbra", "Violin", "Whale", "Xenon"],
    ["Yacht", "Zebra", "Amber", "Brook"],
    ["Cedar", "Dune", "Ember", "Flint"],
]


def at(day: date, hour: int = 0, minute: int = 1) -> datetime:
    """An aware UTC instant on ``day`` -- the scheduler's 00:01 by default."""
    return datetime.combine(day, time(hour, minute), tzinfo=UTC)


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


def seed_session(db, groups=GROUPS, day=None, status="active"):
    """Open a session straight into the tables, with its sixteen word rows."""
    day = day or datetime.now(timezone.utc).date()
    with db.sessions.begin() as session:
        game = ConnectionsGameRecord(
            game_date=day,
            status=status,
            period_start_date=day,
            period_end_date=day + timedelta(days=1),
            started_at=datetime.now(timezone.utc),
            created_by="adminuser",
        )
        session.add(game)
        session.flush()
        game_id = str(game.id)
        if status == "active":
            for group_index, group in enumerate(groups):
                for display in group:
                    session.add(
                        ConnectionsWordRecord(
                            connections_game_id=game_id,
                            word_display=display,
                            word_normalized=display.strip().lower(),
                            group_index=group_index,
                        )
                    )
        session.flush()
        return {"id": game_id, "groups": groups}


def set_config(admin: TestClient, game_id: str, payload: dict) -> dict:
    _set_admin()
    response = admin.put(f"/admin/games/config/{game_id}", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def active_rows(db) -> list:
    with db.sessions() as session:
        return session.scalars(
            select(ConnectionsGameRecord).where(
                ConnectionsGameRecord.status.in_(("active", "pending"))
            )
        ).all()


def submission_for(db, game_id: str, user_id: str):
    with db.sessions() as session:
        return session.scalar(
            select(ConnectionsSubmissionRecord).where(
                ConnectionsSubmissionRecord.connections_game_id == game_id,
                ConnectionsSubmissionRecord.user_id == user_id,
            )
        )


# --- 1. admin configuration ----------------------------------------------------


def test_admin_can_publish_a_valid_puzzle(db):
    admin = make_admin_client()
    response = admin.post("/admin/connections", json={"groups": GROUPS})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["groups"] == GROUPS
    assert body["status"] == "active"


def test_admin_rejects_missing_words_and_bad_group_sizes(db):
    admin = make_admin_client()
    _set_admin()
    # Only three groups.
    short = admin.post("/admin/connections", json={"groups": GROUPS[:3]})
    assert short.status_code == 400
    # A group of three.
    bad_group = [list(group) for group in GROUPS]
    bad_group[0] = bad_group[0][:3]
    response = admin.post("/admin/connections", json={"groups": bad_group})
    assert response.status_code == 400
    # Nothing was stored.
    assert admin.get("/admin/connections").json()["games"] == []


def test_admin_rejects_case_insensitive_duplicates(db):
    admin = make_admin_client()
    _set_admin()
    duplicate = [list(group) for group in GROUPS]
    duplicate[3][0] = "  apple  "
    response = admin.post("/admin/connections", json={"groups": duplicate})
    assert response.status_code == 400
    assert "uplicate" in response.json()["detail"]


def test_admin_rejects_empty_words(db):
    admin = make_admin_client()
    _set_admin()
    blank = [list(group) for group in GROUPS]
    blank[1][2] = "   "
    assert admin.post("/admin/connections", json={"groups": blank}).status_code == 400


def test_connections_has_no_random_content_mode(db):
    admin = make_admin_client()
    _set_admin()
    refused = admin.put("/admin/games/config/connections", json={"content_mode": "random"})
    assert refused.status_code == 400
    # Manual is the only accepted mode.
    ok = admin.put("/admin/games/config/connections", json={"content_mode": "manual"})
    assert ok.status_code == 200, ok.text


def test_no_random_puzzle_is_generated_automatically(db):
    info = game_scheduler.ensure_single_game(db, "connections", at(THU))
    assert info["action"] == "created"
    assert info["status"] == "pending"
    with db.sessions() as session:
        words = session.scalars(select(ConnectionsWordRecord)).all()
        assert words == []


def test_unconfigured_sessions_stay_unavailable(db):
    user = make_player(db, PLAYER_UUID, "connplayer")
    client = make_player_client(user)

    # No session at all: an empty, non-playable read.
    body = client.get("/games/connections").json()
    assert body["game_id"] is None
    assert body["words"] == []
    assert body["submitted"] is False

    # Pending session: a 200 with the pending sentence, never a board.
    seed_session(db, day=datetime.now(timezone.utc).date(), status="pending")
    body = client.get("/games/connections").json()
    assert body["session_status"] == "pending"
    assert body["words"] == []
    assert "not been set" in (body["message"] or "")


def test_direct_submit_against_an_unconfigured_session_is_refused(db):
    user = make_player(db, PLAYER_UUID, "conndirect")
    client = make_player_client(user)
    pending = seed_session(db, day=datetime.now(timezone.utc).date(), status="pending")
    response = client.post(
        "/games/connections/submit",
        json={"game_id": pending["id"], "groups": GROUPS},
    )
    assert response.status_code == 409


def test_confirmed_updates_preserve_historical_submissions(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "connhistory")
    client = make_player_client(user)

    _set_admin()
    first = admin.post("/admin/connections", json={"groups": GROUPS}).json()
    _set_user(user)
    submitted = client.post(
        "/games/connections/submit",
        json={"game_id": first["id"], "groups": GROUPS},
    )
    assert submitted.status_code == 200, submitted.text
    before = submission_for(db, first["id"], PLAYER_UUID)
    assert before is not None
    assert (before.correct_groups, before.correct_words) == (4, 16)

    # The Update ends the first session and opens a new one with new words.
    _set_admin()
    second = admin.post("/admin/connections", json={"groups": OTHER_GROUPS}).json()
    assert second["id"] != first["id"]
    assert second["groups"] == OTHER_GROUPS

    with db.sessions() as session:
        old = session.get(ConnectionsGameRecord, first["id"])
        assert old.status == "ended"
        # The old puzzle's words are untouched by the Update.
        words = session.scalars(
            select(ConnectionsWordRecord).where(
                ConnectionsWordRecord.connections_game_id == first["id"]
            )
        ).all()
        assert len(words) == 16
    # The earlier submission still references the original session and scores.
    after = submission_for(db, first["id"], PLAYER_UUID)
    assert (after.correct_groups, after.correct_words) == (4, 16)
    assert after.connections_game_id == first["id"]


# --- 2. session lifecycle ------------------------------------------------------


def test_reset_cadences_behave_like_the_shared_scheduling_system(db):
    from app.services import game_periods

    cases = {
        "daily": (MON, MON + timedelta(days=1)),
        "every_2_days": (MON, MON + timedelta(days=2)),
        "every_3_days": (MON, MON + timedelta(days=3)),
        "every_5_days": (MON, MON + timedelta(days=5)),
        "weekly": (WED, date(2026, 10, 12)),  # mid-week still ends next Monday
    }
    admin = make_admin_client()
    for cadence, (start, end) in cases.items():
        assert game_periods.period_for_start(start, cadence) == (start, end)
        set_config(admin, "connections", {"reset_cadence": cadence})
        info = game_scheduler.ensure_single_game(db, "connections", at(start))
        assert info["action"] == "created", cadence
        assert info["period_start"] == start.isoformat()
        assert info["period_end"] == end.isoformat()
        with db.sessions.begin() as session:
            for row in session.scalars(select(ConnectionsGameRecord)).all():
                row.status = "ended"
        set_config(admin, "connections", {"reset_cadence": "daily"})


def test_date_calculations_respect_utc(db):
    # 23:58 UTC on THU under a daily cadence still ends at the FRI boundary,
    # never 24 hours later.
    admin = make_admin_client()
    set_config(admin, "connections", {"reset_cadence": "daily"})
    late = datetime.combine(THU, time(23, 58), tzinfo=UTC)
    info = game_scheduler.ensure_single_game(db, "connections", late)
    assert info["period_start"] == THU.isoformat()
    assert info["period_end"] == FRI.isoformat()


def test_resetting_opens_the_correct_session_without_duplicating(db):
    admin = make_admin_client()
    set_config(admin, "connections", {"reset_cadence": "daily"})
    first = game_scheduler.ensure_single_game(db, "connections", at(THU))
    # A second run inside the same period is a no-op, never a duplicate.
    again = game_scheduler.ensure_single_game(db, "connections", at(THU, 12, 0))
    assert again["action"] == "current"
    assert again["session_id"] == first["session_id"]
    assert len(active_rows(db)) == 1
    # Past the boundary the old session ends and the next pending one opens.
    rolled = game_scheduler.ensure_single_game(db, "connections", at(FRI))
    assert rolled["action"] == "created"
    assert rolled["session_id"] != first["session_id"]
    assert len(active_rows(db)) == 1


def test_a_scheduled_puzzle_activates_its_session(db):
    admin = make_admin_client()
    set_config(admin, "connections", {"reset_cadence": "daily"})
    today = datetime.now(timezone.utc).date()
    _set_admin()
    scheduled = admin.post(
        "/admin/connections/schedule",
        json={"target_date": today.isoformat(), "groups": GROUPS},
    )
    assert scheduled.status_code == 201, scheduled.text
    info = game_scheduler.ensure_single_game(db, "connections", at(today))
    assert info["status"] == "active"
    with db.sessions() as session:
        words = session.scalars(
            select(ConnectionsWordRecord).where(
                ConnectionsWordRecord.connections_game_id == info["session_id"]
            )
        ).all()
        assert len(words) == 16


def test_scheduling_is_bounded_by_the_shared_horizon(db):
    admin = make_admin_client()
    horizon = date.today() + timedelta(days=MAX_FUTURE_SCHEDULE_DAYS)
    beyond = horizon + timedelta(days=1)
    _set_admin()
    ok = admin.post(
        "/admin/connections/schedule",
        json={"target_date": horizon.isoformat(), "groups": GROUPS},
    )
    assert ok.status_code == 201, ok.text
    refused = admin.post(
        "/admin/connections/schedule",
        json={"target_date": beyond.isoformat(), "groups": GROUPS},
    )
    assert refused.status_code == 400


def test_enabled_and_hidden_states_keep_their_distinct_meanings(db):
    admin = make_admin_client()
    user = make_player(db, PLAYER_UUID, "connavail")
    client = make_player_client(user)
    seed_session(db)

    # Disabled but visible: selection marks it unavailable, direct play refused,
    # yet the lifecycle keeps the session.
    set_config(admin, "connections", {"enabled": False})
    _set_user(user)
    selection = client.get("/games/select").json()
    entry = next(game for game in selection["games"] if game["game_id"] == "connections")
    assert entry["playable"] is False
    assert client.get("/games/connections").status_code == 403
    assert len(active_rows(db)) == 1

    # Hidden: absent from selection, direct play refused, no new sessions.
    set_config(admin, "connections", {"enabled": True, "is_hidden": True})
    _set_user(user)
    selection = client.get("/games/select").json()
    assert all(game["game_id"] != "connections" for game in selection["games"])
    assert client.get("/games/connections").status_code == 403
    with db.sessions.begin() as session:
        for row in session.scalars(select(ConnectionsGameRecord)).all():
            row.status = "ended"
    summary = game_scheduler.ensure_single_game(db, "connections", at(THU))
    assert summary["action"] == "skipped_hidden"
    assert active_rows(db) == []


def test_concurrent_resets_do_not_duplicate_sessions(db):
    admin = make_admin_client()
    set_config(admin, "connections", {"reset_cadence": "daily"})
    barrier = threading.Barrier(2)
    results = []
    lock = threading.Lock()

    def attempt() -> None:
        barrier.wait(timeout=30)
        outcome = game_scheduler.ensure_single_game(db, "connections", at(THU))
        with lock:
            results.append(outcome)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(attempt) for _ in range(2)]:
            future.result(timeout=60)

    assert len(results) == 2
    assert len(active_rows(db)) == 1


# --- 3. play ------------------------------------------------------------------


def test_each_user_gets_a_randomized_but_stable_word_order(db):
    first = seed_session(db)
    user_a = make_player(db, PLAYER_UUID, "conna")
    user_b = make_player(db, OTHER_UUID, "connb")
    displays = [word for group in GROUPS for word in group]

    order_a = shuffle_words_for_player(displays, PLAYER_UUID, first["id"])
    order_b = shuffle_words_for_player(displays, OTHER_UUID, first["id"])
    assert sorted(order_a) == sorted(displays)
    assert sorted(order_b) == sorted(displays)
    # Deterministic per player, stable across calls, differing across players.
    assert order_a == shuffle_words_for_player(displays, PLAYER_UUID, first["id"])
    assert order_a != order_b

    client_a = make_player_client(user_a)
    first_read = client_a.get("/games/connections").json()
    second_read = client_a.get("/games/connections").json()
    assert first_read["words"] == second_read["words"]
    assert sorted(first_read["words"]) == sorted(displays)


def test_incomplete_assignments_cannot_be_submitted(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "connincomplete")
    client = make_player_client(user)
    incomplete = [list(group) for group in GROUPS]
    incomplete[3] = incomplete[3][:3]  # one word short
    response = client.post(
        "/games/connections/submit",
        json={"game_id": game["id"], "groups": incomplete},
    )
    assert response.status_code == 400
    assert submission_for(db, game["id"], PLAYER_UUID) is None


def test_unknown_words_are_refused(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "connunknown")
    client = make_player_client(user)
    forged = [list(group) for group in GROUPS]
    forged[0][0] = "NotAWord"
    assert (
        client.post(
            "/games/connections/submit", json={"game_id": game["id"], "groups": forged}
        ).status_code
        == 400
    )


def test_a_valid_arrangement_submits_once_and_confirms_without_scores(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "connsubmit")
    client = make_player_client(user)

    response = client.post(
        "/games/connections/submit",
        json={"game_id": game["id"], "groups": GROUPS},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["submitted"] is True
    # The confirmation discloses nothing: no scores, no correctness, no
    # membership, no rank.
    assert set(body.keys()) == {"game_id", "submitted", "submitted_at", "message"}
    lowered = response.text.lower()
    for forbidden in ("correct_groups", "correct_words", "score", "rank", "answer"):
        assert forbidden not in lowered

    row = submission_for(db, game["id"], PLAYER_UUID)
    assert row is not None
    assert (row.correct_groups, row.correct_words) == (4, 16)

    # After submitting, the screen is a confirmation, never an editable board.
    reread = client.get("/games/connections").json()
    assert reread["submitted"] is True
    assert reread["words"] == []

    # A second submit is refused.
    again = client.post(
        "/games/connections/submit",
        json={"game_id": game["id"], "groups": GROUPS},
    )
    assert again.status_code == 409


def test_concurrent_submissions_share_one_row(db):
    game = seed_session(db)
    make_player(db, PLAYER_UUID, "connrace")
    barrier = threading.Barrier(2)
    outcomes: list = []
    lock = threading.Lock()

    def attempt() -> None:
        from app.services import connections_gameplay as gameplay
        from app.postgres import get_postgres_client

        barrier.wait(timeout=30)
        try:
            outcome = gameplay.submit_arrangement(
                db, PLAYER_UUID, game["id"], [list(group) for group in GROUPS]
            )
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
            select(ConnectionsSubmissionRecord).where(
                ConnectionsSubmissionRecord.connections_game_id == game["id"],
                ConnectionsSubmissionRecord.user_id == PLAYER_UUID,
            )
        ).all()
    assert len(rows) == 1


def test_correct_groups_ignore_arbitrary_group_ordering(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "connorder")
    client = make_player_client(user)
    rotated = [GROUPS[2], GROUPS[0], GROUPS[3], GROUPS[1]]
    assert client.post(
        "/games/connections/submit", json={"game_id": game["id"], "groups": rotated}
    ).status_code == 200
    row = submission_for(db, game["id"], PLAYER_UUID)
    assert (row.correct_groups, row.correct_words) == (4, 16)


def test_correct_words_use_the_best_one_to_one_mapping(db):
    # Submitted group 0 holds three words from canonical group 0 plus one from
    # canonical group 1; submitted group 1 holds the mirror. Exact-match
    # counting would score zero words here; the best mapping scores eight.
    canonical = [set(group) for group in GROUPS]
    submitted = [
        [GROUPS[0][0], GROUPS[0][1], GROUPS[0][2], GROUPS[1][0]],
        [GROUPS[1][1], GROUPS[1][2], GROUPS[1][3], GROUPS[0][3]],
        list(GROUPS[2]),
        list(GROUPS[3]),
    ]
    correct_groups, correct_words = score_arrangement(submitted, [list(group) for group in GROUPS])
    assert correct_groups == 2
    # Brute-force the optimum independently: the best mapping keeps the two
    # exact groups (8 words) plus 3+3 on the crossed pair = 14.
    best = 0
    submitted_sets = [set(group) for group in submitted]
    for mapping in permutations(range(4)):
        total = sum(
            len(submitted_sets[index] & canonical[mapping[index]]) for index in range(4)
        )
        best = max(best, total)
    assert correct_words == best == 14


def test_scoring_persists_accurately_through_the_api(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "connscore")
    client = make_player_client(user)
    submitted = [
        [GROUPS[0][0], GROUPS[0][1], GROUPS[0][2], GROUPS[1][0]],
        [GROUPS[1][1], GROUPS[1][2], GROUPS[1][3], GROUPS[0][3]],
        list(GROUPS[2]),
        list(GROUPS[3]),
    ]
    assert (
        client.post(
            "/games/connections/submit", json={"game_id": game["id"], "groups": submitted}
        ).status_code
        == 200
    )
    row = submission_for(db, game["id"], PLAYER_UUID)
    assert (row.correct_groups, row.correct_words) == (2, 14)


def test_client_supplied_scores_are_never_trusted(db):
    game = seed_session(db)
    user = make_player(db, PLAYER_UUID, "conntrust")
    client = make_player_client(user)
    # Extra keys are refused outright rather than accepted and dropped.
    response = client.post(
        "/games/connections/submit",
        json={"game_id": game["id"], "groups": GROUPS, "correct_groups": 100},
    )
    assert response.status_code == 422
    assert submission_for(db, game["id"], PLAYER_UUID) is None


def test_the_schema_refuses_a_second_row_for_one_player(db):
    game = seed_session(db)
    make_player(db, PLAYER_UUID, "connschema")
    with db.sessions.begin() as session:
        session.add(
            ConnectionsSubmissionRecord(
                connections_game_id=game["id"],
                user_id=PLAYER_UUID,
                groups=GROUPS,
                correct_groups=4,
                correct_words=16,
            )
        )
    with pytest.raises(IntegrityError):
        with db.sessions.begin() as session:
            session.add(
                ConnectionsSubmissionRecord(
                    connections_game_id=game["id"],
                    user_id=PLAYER_UUID,
                    groups=GROUPS,
                    correct_groups=0,
                    correct_words=0,
                )
            )


def test_submissions_against_ended_sessions_are_refused(db):
    game = seed_session(db, status="ended")
    user = make_player(db, PLAYER_UUID, "connended")
    client = make_player_client(user)
    assert (
        client.post(
            "/games/connections/submit", json={"game_id": game["id"], "groups": GROUPS}
        ).status_code
        == 409
    )


def test_staff_cannot_play(admin_client: TestClient, moderator_client: TestClient):
    assert admin_client.get("/games/connections").status_code == 403
    assert moderator_client.get("/games/connections").status_code == 403


def test_anonymous_callers_are_refused(db):
    override = app.dependency_overrides.pop(get_current_user, None)
    try:
        client = TestClient(app, follow_redirects=False)
        assert client.get("/games/connections").status_code == 401
    finally:
        if override is not None:
            app.dependency_overrides[get_current_user] = override
