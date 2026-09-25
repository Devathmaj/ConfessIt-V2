"""Playing Two-Key Escape end to end, through HTTP where a player would be.

These tests pin the properties a careless implementation gets quietly wrong:

**Pairing is reused, never re-decided.** Couples come from the Couple
Questionnaire's mutual selection alone: no mutual session, no game. The
selection flow itself is exercised (A chooses B, B chooses A) rather than
assumed, so this suite fails if the escape game ever grows its own pairing.

**Roles submit, then finalize once.** Confirming writes only the caller's
submission; distinct pairs finalize atomically, identical pairs surface the
conflict, and finalized roles are immutable against repeats and races.

**The board is server-side.** Moves and keys are validated against stored
rows under lock: illegal moves change nothing at all, the operator cannot
move, the navigator cannot spend keys, cooldowns and the movement gate are
enforced from server timestamps, and blocking never traps the player.

**Scoring measures start-to-finish time, nothing else.** The first valid
move stamps the start and the exit stamps the end; time away counts by
decision. Completed couples outrank the rest by elapsed time, the score is
derived from it, and completion finalizes exactly once.
"""

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from app.accounts import AccountType
from app.db_models import (
    CoupleGameRecord,
    CoupleGameSessionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
)
from app.limits import two_key_escape_key_charges
from app.main import app
from app.models import UserDetails
from app.services import two_key_escape_gameplay
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
D_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
E_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"


def seed_questionnaire(db):
    """Open today's global questionnaire period straight into the table."""
    day = datetime.now(timezone.utc).date()
    game = CoupleGameRecord(
        game_date=day,
        created_by="adminuser",
        status="active",
        period_start_date=day,
        period_end_date=day + timedelta(days=1),
        started_at=datetime.now(timezone.utc),
    )
    with db.sessions.begin() as session:
        session.add(game)
        session.flush()
        return str(game.id)


def seed_escape(db):
    """Open today's escape period through the scheduler, like boot does."""
    from app.services import game_scheduler

    outcome = game_scheduler.ensure_single_game(db, "two-key-escape")
    assert outcome["action"] in ("created", "current"), outcome
    with db.sessions() as session:
        game = session.scalar(
            select(TwoKeyEscapeGameRecord)
            .where(TwoKeyEscapeGameRecord.status.in_(("active", "pending")))
            .limit(1)
        )
        assert game is not None
        return str(game.id)


def bfs_path(state, avoid_locked=True):
    """A traversable route from the player to the exit in the given state."""
    size = state["grid_size"]
    walls = {tuple(cell) for cell in state["walls"]}
    locked = {tuple(cell) for cell in state["locked_cells"]} if avoid_locked else set()
    blocked = {tuple(cell) for cell in state["blocked_cells"]}
    start = (state["player_row"], state["player_col"])
    exit_cell = (state["exit_row"], state["exit_col"])
    deltas = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
    seen = {start: None}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        if cell == exit_cell:
            break
        for name, (dr, dc) in deltas.items():
            nxt = (cell[0] + dr, cell[1] + dc)
            if not (0 <= nxt[0] < size and 0 <= nxt[1] < size):
                continue
            if nxt in walls or nxt in locked or nxt in blocked or nxt in seen:
                continue
            seen[nxt] = (cell, name)
            queue.append(nxt)
    if exit_cell not in seen:
        return None
    moves = []
    cell = exit_cell
    while cell != start:
        prev, name = seen[cell]
        moves.append(name)
        cell = prev
    return list(reversed(moves))


def open_pair(http, alice, bob):
    """Mutual questionnaire selection for two users, through HTTP."""
    client_for(http, alice).post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": bob.id}
    )
    response = client_for(http, bob).post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": alice.id}
    )
    assert response.status_code == 200, response.text
    return response


def finalize_roles(http, navigator, operator):
    client_for(http, navigator).post(
        "/games/two-key-escape/role-submit", json={"role": "navigator"}
    )
    response = client_for(http, operator).post(
        "/games/two-key-escape/role-submit", json={"role": "key_operator"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["roles_finalized"] is True
    return response


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


def escape_row(db, **filters):
    with db.sessions() as session:
        return session.scalar(select(TwoKeyEscapeSessionRecord).filter_by(**filters))


# --- fixtures ------------------------------------------------------------


import pytest


@pytest.fixture
def http():
    """One shared client; the caller identity is switched before every request."""
    client = TestClient(app, follow_redirects=False)
    try:
        yield client
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def setup_pair(db, http, alice, bob):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    open_pair(http, alice, bob)


def test_no_couple_without_mutual_selection(db, http):
    """A caller with no mutual-selection session reads no_couple, not a board."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    stranger = make_user(db, C_ID, "escape_c", "Cleo")

    state = client_for(http, alice).get("/games/two-key-escape")
    assert state.status_code == 200, state.text
    assert state.json()["stage"] == "no_couple"

    # One-sided selection is still no couple: choosing is not starting.
    pick = client_for(http, alice).post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": stranger.id}
    )
    assert pick.status_code == 200, pick.text
    state = client_for(http, alice).get("/games/two-key-escape")
    assert state.json()["stage"] == "no_couple"


def test_escape_lists_the_questionnaire_candidates(db, http):
    """Settling the pairing here chooses from the same people, minus self."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")

    escape_list = client_for(http, alice).get("/games/two-key-escape/partners")
    assert escape_list.status_code == 200, escape_list.text
    questionnaire_list = client_for(http, alice).get("/games/couple-questionnaire/partners")
    assert questionnaire_list.status_code == 200, questionnaire_list.text
    assert escape_list.json() == questionnaire_list.json()
    assert all(entry["id"] != alice.id for entry in escape_list.json())
    assert any(entry["id"] == bob.id for entry in escape_list.json())


def test_pairing_settled_across_both_games(db, http):
    """One side picks here, the other there: mutual is mutual either way."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")

    first = client_for(http, alice).post(
        "/games/two-key-escape/selection", json={"selected_user_id": bob.id}
    )
    assert first.status_code == 200, first.text
    assert first.json()["stage"] == "no_couple"
    assert first.json()["my_selection"]["selected_user_name"] == "Bob"

    second = client_for(http, bob).post(
        "/games/couple-questionnaire/selection", json={"selected_user_id": alice.id}
    )
    assert second.status_code == 200, second.text
    assert second.json()["session_id"] is not None

    as_alice = client_for(http, alice).get("/games/two-key-escape").json()
    assert as_alice["stage"] == "selecting_roles"
    assert as_alice["partner"]["id"] == bob.id


def test_pairing_fully_in_escape_opens_questionnaire(db, http):
    """A pair formed entirely here is the questionnaire's own session."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    dora = make_user(db, D_ID, "escape_d", "Dora")
    erin = make_user(db, E_ID, "escape_e", "Erin")

    client_for(http, dora).post(
        "/games/two-key-escape/selection", json={"selected_user_id": erin.id}
    )
    done = client_for(http, erin).post(
        "/games/two-key-escape/selection", json={"selected_user_id": dora.id}
    )
    assert done.status_code == 200, done.text
    assert done.json()["stage"] == "selecting_roles"

    questionnaire = client_for(http, dora).get("/games/couple-questionnaire").json()
    assert questionnaire["session_id"] is not None
    assert questionnaire["partner"]["id"] == erin.id
    # And the maze game is ready for roles on both sides.
    assert client_for(http, erin).get("/games/two-key-escape").json()["stage"] == "selecting_roles"


def test_pending_pick_visible_in_both_games(db, http):
    """A one-sided pick made here is the same pending pick over there."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")

    client_for(http, alice).post(
        "/games/two-key-escape/selection", json={"selected_user_id": bob.id}
    )
    escape = client_for(http, alice).get("/games/two-key-escape").json()
    questionnaire = client_for(http, alice).get("/games/couple-questionnaire").json()
    assert escape["my_selection"]["selected_user_id"] == bob.id
    assert questionnaire["my_selection"]["selected_user_id"] == bob.id


def test_mutual_selection_opens_escape_roles(db, http):
    """The questionnaire's mutual selection is what opens role selection."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")

    open_pair(http, alice, bob)
    state = client_for(http, alice).get("/games/two-key-escape")
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["stage"] == "selecting_roles"
    assert body["partner"]["name"] == "Bob"
    assert body["grid_size"] in (12, 13, 14, 15)
    assert body["key_charges"] == two_key_escape_key_charges(body["grid_size"])
    assert len(body["keys"]) == body["key_charges"]


def test_distinct_roles_finalize(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)

    first = client_for(http, alice).post(
        "/games/two-key-escape/role-submit", json={"role": "navigator"}
    )
    assert first.status_code == 200, first.text
    # One submission alone finalizes nothing, and names the pending side.
    assert first.json()["roles_finalized"] is False
    assert first.json()["my_submitted_role"] == "navigator"
    assert first.json()["partner_submitted_role"] is None

    second = client_for(http, bob).post(
        "/games/two-key-escape/role-submit", json={"role": "key_operator"}
    )
    assert second.status_code == 200, second.text
    assert second.json()["roles_finalized"] is True
    assert second.json()["stage"] == "playing"

    as_alice = client_for(http, alice).get("/games/two-key-escape").json()
    as_bob = client_for(http, bob).get("/games/two-key-escape").json()
    assert as_alice["my_role"] == "navigator"
    assert as_bob["my_role"] == "key_operator"
    # Both see the same authoritative board.
    for field in ("player_row", "player_col", "walls", "locked_cells", "state_version"):
        assert as_alice[field] == as_bob[field]


def test_same_roles_conflict_then_change_resolves(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)

    client_for(http, alice).post("/games/two-key-escape/role-submit", json={"role": "navigator"})
    conflicted = client_for(http, bob).post(
        "/games/two-key-escape/role-submit", json={"role": "navigator"}
    )
    assert conflicted.status_code == 200, conflicted.text
    body = conflicted.json()
    assert body["roles_finalized"] is False
    assert body["stage"] == "selecting_roles"
    assert body["conflict"] == (
        "Both players selected Navigator. "
        "One player must change their role before the game can begin."
    )
    # The conflicting selections stay visible to both sides.
    as_alice = client_for(http, alice).get("/games/two-key-escape").json()
    assert as_alice["my_submitted_role"] == "navigator"
    assert as_alice["partner_submitted_role"] == "navigator"

    resolved = client_for(http, bob).post(
        "/games/two-key-escape/role-change", json={"role": "key_operator"}
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["roles_finalized"] is True
    as_alice = client_for(http, alice).get("/games/two-key-escape").json()
    as_bob = client_for(http, bob).get("/games/two-key-escape").json()
    assert as_alice["my_role"] == "navigator"
    assert as_bob["my_role"] == "key_operator"


def test_finalized_roles_are_immutable(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    refused = client_for(http, bob).post(
        "/games/two-key-escape/role-change", json={"role": "navigator"}
    )
    assert refused.status_code == 409, refused.text

    # Repeating the submission is a no-op returning the locked-in assignment.
    repeat = client_for(http, alice).post(
        "/games/two-key-escape/role-submit", json={"role": "navigator"}
    )
    assert repeat.status_code == 200, repeat.text
    assert repeat.json()["my_role"] == "navigator"
    assert client_for(http, bob).get("/games/two-key-escape").json()["my_role"] == "key_operator"


def test_simultaneous_finalization_assigns_once(db, http):
    """Two racers finalizing together still mint one assignment, never two.

    Exercised at the service layer like the questionnaire's own race test:
    the auth override is process-global, so two HTTP clients cannot hold two
    identities at once. Convergence is asserted on the next read, which is
    also what the screen does on every entry.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda pair: two_key_escape_gameplay.submit_role(db, pair[0], pair[1]),
                [(alice, "navigator"), (bob, "key_operator")],
            )
        )

    with db.sessions() as session:
        count = session.scalar(select(func.count()).select_from(TwoKeyEscapeSessionRecord))
        assert count == 1
    as_alice = client_for(http, alice).get("/games/two-key-escape").json()
    as_bob = client_for(http, bob).get("/games/two-key-escape").json()
    assert as_alice["roles_finalized"] is True
    assert as_alice["my_role"] == "navigator"
    assert as_bob["my_role"] == "key_operator"


def test_stranger_cannot_access_couple_game(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    stranger = make_user(db, C_ID, "escape_c", "Cleo")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    # No couple session: no board, and writes are refused, not applied.
    assert client_for(http, stranger).get("/games/two-key-escape").json()["stage"] == "no_couple"
    assert client_for(http, stranger).post(
        "/games/two-key-escape/move", json={"direction": "down"}
    ).status_code == 404
    assert client_for(http, stranger).post(
        "/games/two-key-escape/key", json={"key_index": 0}
    ).status_code == 404


def test_couples_have_isolated_state(db, http):
    """One couple's moves never affect another couple's board."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    dora = make_user(db, D_ID, "escape_d", "Dora")
    erin = make_user(db, E_ID, "escape_e", "Erin")
    open_pair(http, alice, bob)
    open_pair(http, dora, erin)
    finalize_roles(http, alice, bob)
    finalize_roles(http, dora, erin)

    before = client_for(http, dora).get("/games/two-key-escape").json()
    path = bfs_path(client_for(http, alice).get("/games/two-key-escape").json())
    assert path, "the generated maze must be solvable"
    moved = client_for(http, alice).post("/games/two-key-escape/move", json={"direction": path[0]})
    assert moved.status_code == 200, moved.text
    after = client_for(http, dora).get("/games/two-key-escape").json()
    assert (after["player_row"], after["player_col"]) == (before["player_row"], before["player_col"])
    assert after["total_moves"] == 0


def test_all_grid_sizes_generate_solvable_mazes():
    for size in (12, 13, 14, 15):
        template = two_key_escape_gameplay.generate_maze(size, f"test-seed-{size}")
        assert len(template["walls"]) >= 0
        assert len(template["locked_cells"]) == two_key_escape_key_charges(size)
        assert template["start"] == [0, 0]
        assert template["exit"] == [size - 1, size - 1]
        # Solvable with the locked cells treated as blocked: the route
        # exists before any key is spent.
        assert two_key_escape_gameplay._bfs_reaches(
            size,
            {tuple(cell) for cell in template["walls"]},
            {tuple(cell) for cell in template["locked_cells"]},
            (0, 0),
            (size - 1, size - 1),
        )


def test_same_seed_reproduces_template():
    first = two_key_escape_gameplay.generate_maze(13, "stable")
    second = two_key_escape_gameplay.generate_maze(13, "stable")
    assert first["walls"] == second["walls"]
    assert first["locked_cells"] == second["locked_cells"]


def test_key_charges_derive_from_grid_size():
    assert two_key_escape_key_charges(12) == 12
    assert two_key_escape_key_charges(13) == 13
    assert two_key_escape_key_charges(14) == 14
    assert two_key_escape_key_charges(15) == 15


def test_movement_valid_and_invalid(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    before = client_for(http, alice).get("/games/two-key-escape").json()
    # Up from the start corner always leaves the maze.
    refused = client_for(http, alice).post("/games/two-key-escape/move", json={"direction": "up"})
    assert refused.status_code == 409, refused.text
    after = client_for(http, alice).get("/games/two-key-escape").json()
    assert (after["player_row"], after["player_col"]) == (before["player_row"], before["player_col"])
    assert after["total_moves"] == before["total_moves"]
    assert after["moves_since_last_key"] == 0
    # A refused move changes nothing at all -- not even the clock.
    row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
    assert row.first_move_at is None

    # A legal step advances exactly one cell, banks the movement gate, and
    # stamps the start clock.
    path = bfs_path(after)
    assert path, "the generated maze must be solvable"
    moved = client_for(http, alice).post("/games/two-key-escape/move", json={"direction": path[0]})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["total_moves"] == 1
    assert body["moves_since_last_key"] == 1
    assert escape_row(db, user_a_id=A_ID, user_b_id=B_ID).first_move_at is not None

    # The Key Operator cannot move the character.
    forbidden = client_for(http, bob).post("/games/two-key-escape/move", json={"direction": path[0]})
    assert forbidden.status_code == 403, forbidden.text


def test_navigator_cannot_use_keys(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    refused = client_for(http, alice).post("/games/two-key-escape/key", json={"key_index": 0})
    assert refused.status_code == 403, refused.text
    row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
    assert row.total_keys == 0
    assert row.last_key_at is None


def test_key_unlocks_blocks_and_starts_cooldown(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    before = client_for(http, bob).get("/games/two-key-escape").json()
    target = before["keys"][0]
    assert target["used"] is False

    used = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert used.status_code == 200, used.text
    body = used.json()
    assert body["keys"][0]["used"] is True
    assert body["keys_remaining"] == body["key_charges"] - 1
    # The designated cell unlocked...
    assert [target["target_row"], target["target_col"]] not in body["locked_cells"]
    # ...and a different eligible cell became blocked.
    assert len(body["blocked_cells"]) == 1
    blocked = tuple(body["blocked_cells"][0])
    assert blocked != (target["target_row"], target["target_col"])
    assert blocked != (body["exit_row"], body["exit_col"])
    assert blocked != (0, 0)
    assert blocked != (body["player_row"], body["player_col"])
    # The maze stays solvable from the player's cell.
    assert two_key_escape_gameplay._bfs_reaches(
        body["grid_size"],
        {tuple(cell) for cell in body["walls"]},
        {tuple(cell) for cell in body["blocked_cells"]},
        (body["player_row"], body["player_col"]),
        (body["exit_row"], body["exit_col"]),
    )
    assert body["cooldown_seconds_remaining"] > 0
    assert body["moves_since_last_key"] == 0
    assert body["total_keys"] == 1

    # A used key cannot be reused, and nothing is consumed by the refusal.
    again = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert again.status_code == 409, again.text
    row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
    assert row.total_keys == 1

    # Unknown keys are refused without consuming anything.
    missing = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 999})
    assert missing.status_code == 404, missing.text
    assert escape_row(db, user_a_id=A_ID, user_b_id=B_ID).total_keys == 1


def test_key_cooldown_and_movement_gate(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    first = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert first.status_code == 200, first.text

    # The second key during the server-side cooldown is refused.
    assert client_for(http, bob).post(
        "/games/two-key-escape/key", json={"key_index": 1}
    ).status_code == 409

    # A reconnect cannot reset the cooldown: the stamp survives a fresh read.
    reread = client_for(http, bob).get("/games/two-key-escape").json()
    assert reread["cooldown_seconds_remaining"] > 0

    # Age the stamp past the cooldown: the movement gate still holds.
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    gated = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert gated.status_code == 409, gated.text
    assert "move" in gated.json()["detail"].lower()

    # One valid Navigator step satisfies the gate; the next key lands.
    path = bfs_path(client_for(http, alice).get("/games/two-key-escape").json())
    assert path, "the maze must stay solvable after a key"
    moved = client_for(http, alice).post("/games/two-key-escape/move", json={"direction": path[0]})
    assert moved.status_code == 200, moved.text
    second = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert second.status_code == 200, second.text
    assert second.json()["total_keys"] == 2


def test_completion_finalizes_once_and_freezes(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    # Walk the solvable route to the exit, one validated step at a time.
    state = client_for(http, alice).get("/games/two-key-escape").json()
    for _ in range(state["grid_size"] * state["grid_size"] * 2):
        state = client_for(http, alice).get("/games/two-key-escape").json()
        if state["stage"] == "completed":
            break
        path = bfs_path(state)
        assert path, "the maze must stay solvable"
        response = client_for(http, alice).post(
            "/games/two-key-escape/move", json={"direction": path[0]}
        )
        assert response.status_code == 200, response.text
    done = client_for(http, alice).get("/games/two-key-escape").json()
    assert done["stage"] == "completed"
    assert done["result"]["completed"] is True
    assert done["result"]["total_moves"] > 0
    assert done["result"]["score"] > 0
    completed_at = done["result"]["completed_at"]

    # Nothing modifies a finalized result: not moves, not keys.
    assert client_for(http, alice).post(
        "/games/two-key-escape/move", json={"direction": "up"}
    ).status_code == 409
    assert client_for(http, bob).post(
        "/games/two-key-escape/key", json={"key_index": 0}
    ).status_code == 409
    frozen = client_for(http, alice).get("/games/two-key-escape").json()
    assert frozen["result"]["completed_at"] == completed_at
    assert frozen["result"]["total_moves"] == done["result"]["total_moves"]


def test_state_survives_refresh_and_reconnect(db, http):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    path = bfs_path(client_for(http, alice).get("/games/two-key-escape").json())
    client_for(http, alice).post("/games/two-key-escape/move", json={"direction": path[0]})
    first = client_for(http, alice).get("/games/two-key-escape").json()
    second = client_for(http, bob).get("/games/two-key-escape").json()
    assert first["state_version"] == second["state_version"] == 1
    assert (first["player_row"], first["player_col"]) == (second["player_row"], second["player_col"])


def _direct_pair_row(db, escape_game_id, questionnaire_game_id, user_a_id, user_b_id,
                     completed=False, total_moves=0, total_keys=0, elapsed_seconds=None):
    from app.db_models import CoupleGameSessionRecord as Pair

    now = datetime.now(timezone.utc)
    with db.sessions.begin() as session:
        pair = Pair(
            couple_game_id=questionnaire_game_id,
            user_a_id=user_a_id,
            user_b_id=user_b_id,
            status="completed" if completed else "active",
        )
        session.add(pair)
        session.flush()
        pair_id = str(pair.id)
        session.add(
            TwoKeyEscapeSessionRecord(
                two_key_escape_game_id=escape_game_id,
                couple_session_id=pair_id,
                user_a_id=user_a_id,
                user_b_id=user_b_id,
                user_a_submitted_role="navigator",
                user_b_submitted_role="key_operator",
                navigator_user_id=user_a_id,
                key_operator_user_id=user_b_id,
                player_row=0,
                player_col=0,
                first_move_at=(now - timedelta(seconds=elapsed_seconds)) if completed else None,
                completed=completed,
                completed_at=now if completed else None,
                total_moves=total_moves,
                total_keys=total_keys,
                elapsed_seconds=elapsed_seconds,
                score=two_key_escape_gameplay._score_for(elapsed_seconds)
                if completed and elapsed_seconds is not None
                else 0,
            )
        )


def test_leaderboard_ranks_completed_then_fastest(db, http):
    seed_couple_questions(db)
    questionnaire_id = seed_questionnaire(db)
    escape_id = seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    dora = make_user(db, D_ID, "escape_d", "Dora")
    erin = make_user(db, E_ID, "escape_e", "Erin")
    cleo = make_user(db, C_ID, "escape_c", "Cleo")
    finn = make_user(
        db, "ffffffff-ffff-ffff-ffff-ffffffffffff", "escape_f", "Finn"
    )
    # The slower completion has fewer moves and keys, and still loses: only
    # start-to-finish time ranks.
    _direct_pair_row(db, escape_id, questionnaire_id, A_ID, B_ID,
                     completed=True, total_moves=12, total_keys=1, elapsed_seconds=600.0)
    _direct_pair_row(db, escape_id, questionnaire_id, D_ID, E_ID,
                     completed=True, total_moves=40, total_keys=9, elapsed_seconds=90.0)
    _direct_pair_row(db, escape_id, questionnaire_id, C_ID, finn.id,
                     completed=False, total_moves=3, total_keys=0)

    board = client_for(http, alice).get("/games/two-key-escape/leaderboard")
    assert board.status_code == 200, board.text
    entries = board.json()
    assert [entry["completed"] for entry in entries] == [True, True, False]
    assert entries[0]["elapsed_seconds"] == 90.0
    assert entries[1]["elapsed_seconds"] == 600.0
    assert entries[2]["elapsed_seconds"] is None
    assert entries[0]["rank"] == 1
    # Less time always produces the better score.
    assert entries[0]["score"] > entries[1]["score"] > entries[2]["score"]


def test_elapsed_time_includes_time_away(db, http):
    """Logging out mid-game neither resets nor pauses the clock.

    The start stamp survives untouched: backdating it two hours before
    completion settles an elapsed time of about two hours, proving idle and
    offline time counts by decision.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    path = bfs_path(client_for(http, alice).get("/games/two-key-escape").json())
    client_for(http, alice).post("/games/two-key-escape/move", json={"direction": path[0]})
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.first_move_at = datetime.now(timezone.utc) - timedelta(hours=2)

    for _ in range(15 * 15 * 2):
        state = client_for(http, alice).get("/games/two-key-escape").json()
        if state["stage"] == "completed":
            break
        path = bfs_path(state)
        assert path, "the maze must stay solvable"
        response = client_for(http, alice).post(
            "/games/two-key-escape/move", json={"direction": path[0]}
        )
        assert response.status_code == 200, response.text
    done = client_for(http, alice).get("/games/two-key-escape").json()
    assert done["stage"] == "completed"
    assert done["result"]["elapsed_seconds"] >= 2 * 3600
    assert done["result"]["score"] > 0


def test_grid_size_config_persists_across_periods(db, http, admin_client):
    """The admin's grid size survives the reset; the new maze uses it."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    first_id = seed_escape(db)
    with db.sessions() as session:
        first = session.get(TwoKeyEscapeGameRecord, first_id)
        assert int(first.grid_size) == 12

    updated = admin_client.put(
        "/admin/games/config/two-key-escape", json={"grid_size": 14}
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["grid_size"] == 14

    # Other games refuse the knob: it belongs to Two-Key Escape alone.
    assert admin_client.put(
        "/admin/games/config/wordle", json={"grid_size": 14}
    ).status_code == 400
    assert admin_client.put(
        "/admin/games/config/two-key-escape", json={"grid_size": 11}
    ).status_code == 400

    # Ending the period and re-running the reset opens a fresh 14x14 maze
    # while the stored configuration stays 14.
    with db.sessions.begin() as session:
        game = session.get(TwoKeyEscapeGameRecord, first_id)
        game.status = "ended"
        game.ended_at = datetime.now(timezone.utc)
    from app.services import game_scheduler

    outcome = game_scheduler.ensure_single_game(db, "two-key-escape")
    assert outcome["action"] == "created", outcome
    with db.sessions() as session:
        current = session.scalar(
            select(TwoKeyEscapeGameRecord)
            .where(TwoKeyEscapeGameRecord.status == "active")
            .limit(1)
        )
        assert int(current.grid_size) == 14
        assert len(current.locked_cells) == 14
        config = session.scalar(
            select(func.count()).select_from(TwoKeyEscapeGameRecord)
        )
        assert config >= 2
    configs = admin_client.get("/admin/games/config").json()
    assert next(entry for entry in configs if entry["game_id"] == "two-key-escape")["grid_size"] == 14


def test_hidden_game_refuses_play(db, http, admin_client):
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")

    admin_client.put("/admin/games/config/two-key-escape", json={"is_hidden": True})
    refused = client_for(http, alice).get("/games/two-key-escape")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "game_hidden"


def test_expired_session_refuses_writes(db, http):
    """Moves and keys after the period boundary are refused, not applied."""
    seed_couple_questions(db)
    questionnaire_id = seed_questionnaire(db)
    escape_id = seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    with db.sessions.begin() as session:
        game = session.get(TwoKeyEscapeGameRecord, escape_id)
        game.period_end_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert client_for(http, alice).post(
        "/games/two-key-escape/move", json={"direction": "down"}
    ).status_code in (404, 409)
    assert client_for(http, bob).post(
        "/games/two-key-escape/key", json={"key_index": 0}
    ).status_code in (404, 409)
