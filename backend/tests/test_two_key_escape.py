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
move, the navigator cannot spend keys, the cooldown is enforced from server
timestamps, and blocking never traps the player. No movement is ever
required between key uses.

**Keys are mandatory.** Chamber walls allow no keyless route, each lock
opens a different lock, only one stays open at a time, and re-locking under
the Navigator's feet is refused.

**Scoring measures start-to-finish time, nothing else.** The first valid
move stamps the start and the exit stamps the end; time away counts by
decision. Completed couples outrank the rest by elapsed time -- there is
no score, like every other game -- and completion finalizes exactly once.
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


def probe_step(http, navigator, rotation=0, avoid=frozenset(), strict=True):
    """One valid Navigator step, skipping any ``avoid`` cells.

    Chamber walls mean "walk toward the exit" is not always defined, so
    tests that just need *a* legal move probe the four directions instead.
    ``avoid`` names cells to step around -- the open lock cell, so a later
    key is not refused for occupancy. A valid step always exists: the middle
    stays open and the player can always step back, so total refusal means
    the board is genuinely wrong (raised when strict, else None).
    """
    state = client_for(http, navigator).get("/games/two-key-escape").json()
    pr, pc = state["player_row"], state["player_col"]
    deltas = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}
    directions = ["up", "down", "left", "right"]
    for index in range(4):
        direction = directions[(rotation + index) % 4]
        dr, dc = deltas[direction]
        if (pr + dr, pc + dc) in avoid:
            continue
        response = client_for(http, navigator).post(
            "/games/two-key-escape/move", json={"direction": direction}
        )
        if response.status_code == 200:
            return response
    if strict:
        raise AssertionError("navigator boxed with no valid move")
    return None


def preimage_key(db, target_cell):
    """The key index whose hidden mapping opens ``target_cell``.

    Read straight from the period row -- the very thing players must
    discover by trial, which is exactly what the toast test covers.
    """
    wanted = [int(target_cell[0]), int(target_cell[1])]
    for index, cell in enumerate(_mapping(db)):
        if cell == wanted:
            return index
    raise AssertionError(f"no key opens {wanted}")


def _age_cooldown(db):
    """Move the key cooldown into the past, so the next key is its own test."""
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)


def _template_locks(state):
    """Every lock the maze was dealt, open or shut."""
    return {(key["target_row"], key["target_col"]) for key in state["keys"]}


def _open_cells(state):
    """Template locks currently unlocked (at most one)."""
    return _template_locks(state) - {(r, c) for r, c in state["locked_cells"]}


def wall_columns(state):
    """Wall columns: every cell walled or locked, strictly interior."""
    n = state["grid_size"]
    walls = {tuple(cell) for cell in state["walls"]}
    locked = _template_locks(state)
    return [
        col
        for col in range(1, n - 1)
        if all((row, col) in walls or (row, col) in locked for row in range(n))
    ]


_STEP_DELTAS = {"up": (-1, 0), "down": (1, 0), "left": (0, -1), "right": (0, 1)}


def lockread_route(state):
    """Cells from the player to the exit, reading through every lock.

    The same proof the server runs when placing a block: walls and blocked
    cells are real, locks are future key uses. None means the board is
    genuinely unplayable -- a real signal, never a test artifact.
    """
    size = state["grid_size"]
    walls = {tuple(cell) for cell in state["walls"]}
    blocked = {tuple(cell) for cell in state["blocked_cells"]}
    start = (state["player_row"], state["player_col"])
    exit_cell = (state["exit_row"], state["exit_col"])
    if start == exit_cell:
        return [start]
    seen = {start: None}
    queue = deque([start])
    while queue:
        cell = queue.popleft()
        if cell == exit_cell:
            break
        for dr, dc in _STEP_DELTAS.values():
            nxt = (cell[0] + dr, cell[1] + dc)
            if not (0 <= nxt[0] < size and 0 <= nxt[1] < size):
                continue
            if nxt in walls or nxt in blocked or nxt in seen:
                continue
            seen[nxt] = cell
            queue.append(nxt)
    if exit_cell not in seen:
        return None
    route = [exit_cell]
    while route[-1] != start:
        route.append(seen[route[-1]])
    return list(reversed(route))


def _step_toward(from_cell, to_cell):
    dr, dc = to_cell[0] - from_cell[0], to_cell[1] - from_cell[1]
    for name, (vr, vc) in _STEP_DELTAS.items():
        if (dr, dc) == (vr, vc):
            return name
    raise AssertionError(f"non-adjacent step {from_cell} -> {to_cell}")


def play_through(http, db, alice, bob):
    """Complete a maze the way smart play does: follow the lock-readable
    route, opening locks as they come up.

    Ordinary steps are walked; locked steps are opened first (preimage key,
    cooldown aged, gate satisfied by the walking itself, stepping off any
    open cell first). Keys are reusable, so no lock is ever lost -- if this
    loop ever finds no route, that is a genuinely unplayable board.
    Returns the completed state, tracking every gate stepped through.
    """
    stepped_gates: set[tuple[int, int]] = set()
    first = client_for(http, alice).get("/games/two-key-escape").json()
    size = first["grid_size"]
    columns = wall_columns(first)
    gate_cells = {
        (key["target_row"], key["target_col"])
        for key in first["keys"]
        if key["target_col"] in columns
    }
    for _ in range(size * size * 6):
        state = client_for(http, alice).get("/games/two-key-escape").json()
        if state["stage"] == "completed":
            return state, stepped_gates
        locked = {(r, c) for r, c in state["locked_cells"]}
        route = lockread_route(state)
        assert route is not None and len(route) > 1, "a lock-readable route must exist"
        player = (state["player_row"], state["player_col"])
        nxt = route[1]
        if nxt not in locked:
            moved = client_for(http, alice).post(
                "/games/two-key-escape/move",
                json={"direction": _step_toward(player, nxt)},
            )
            assert moved.status_code == 200, moved.text
            if nxt in gate_cells:
                stepped_gates.add(nxt)
            continue
        # Locked step: step off any open cell first (occupancy), then open.
        # No movement is ever required between keys -- only the cooldown.
        open_now = _open_cells(state)
        if player in open_now:
            stepped = probe_step(http, alice, avoid=open_now, strict=False)
            assert stepped is not None and stepped.status_code == 200, stepped
            continue
        _age_cooldown(db)
        used = client_for(http, bob).post(
            "/games/two-key-escape/key",
            json={"key_index": preimage_key(db, [nxt[0], nxt[1]])},
        )
        assert used.status_code == 200, used.text
    raise AssertionError("the exit was never reached")


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
    moved = probe_step(http, alice)
    assert moved.status_code == 200, moved.text
    after = client_for(http, dora).get("/games/two-key-escape").json()
    assert (after["player_row"], after["player_col"]) == (before["player_row"], before["player_col"])
    assert after["total_moves"] == 0


def test_all_grid_sizes_chamber_every_wall_but_stay_playable():
    for size in (12, 13, 14, 15):
        template = two_key_escape_gameplay.generate_maze(size, f"test-seed-{size}")
        assert len(template["walls"]) >= 0
        assert len(template["locked_cells"]) == two_key_escape_key_charges(size)
        assert template["start"] == [0, 0]
        assert template["exit"] == [size - 1, size - 1]
        walls = {tuple(cell) for cell in template["walls"]}
        locked = {tuple(cell) for cell in template["locked_cells"]}
        gates = [tuple(template["locked_cells"][index]) for index in template["gate_indexes"]]
        # Full-height wall columns, pierced only by locked gates: no way
        # around, and every crossing needs a key.
        columns = [col for col in range(1, size - 1) if all(
            (row, col) in walls or (row, col) in locked for row in range(size)
        )]
        assert len(columns) >= 1
        assert all(cell[1] in columns for cell in gates)
        assert len(gates) == 2 * len(columns)
        # Every lock has guaranteed staging: an ordinary neighbor that
        # blocks can never take, so stepping off an open lock always works.
        staging: set[tuple[int, int]] = set()
        for row, col in locked:
            steps = {
                (row + drow, col + dcol)
                for drow, dcol in ((-1, 0), (1, 0), (0, -1), (0, 1))
                if 0 <= row + drow < size
                and 0 <= col + dcol < size
                and (row + drow, col + dcol) not in walls
                and (row + drow, col + dcol) not in locked
            }
            assert steps, f"lock {(row, col)} has no staging cell"
            staging |= steps
        # No keyless route exists through the chamber walls...
        assert not two_key_escape_gameplay._bfs_reaches(
            size, walls, locked, (0, 0), (size - 1, size - 1)
        )
        # ...while the same maze with every lock open is solvable.
        assert two_key_escape_gameplay._bfs_reaches(
            size, walls, set(), (0, 0), (size - 1, size - 1)
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
    # A refused move changes nothing at all -- not even the clock.
    row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
    assert row.first_move_at is None

    # A legal step advances exactly one cell and stamps the start clock.
    # (From the start chamber a step is always available: walls divide
    # chambers, never the start.)
    moved = probe_step(http, alice)
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["total_moves"] == 1
    assert escape_row(db, user_a_id=A_ID, user_b_id=B_ID).first_move_at is not None

    # The Key Operator cannot move the character.
    forbidden = client_for(http, bob).post("/games/two-key-escape/move", json={"direction": "down"})
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


def _mapping(db):
    """The hidden key mapping of the current escape period, by key index."""
    with db.sessions() as session:
        game = session.scalar(
            select(TwoKeyEscapeGameRecord)
            .where(TwoKeyEscapeGameRecord.status == "active")
            .limit(1)
        )
        assert game is not None and game.key_mapping is not None
        locked = [[int(row), int(col)] for row, col in game.locked_cells]
        return [locked[index] for index in game.key_mapping]


def test_key_mapping_is_a_derangement():
    """Every generated mapping is a permutation with no lock opening itself."""
    for size in (12, 13, 14, 15):
        for seed in ("alpha", "beta", "gamma"):
            template = two_key_escape_gameplay.generate_maze(size, f"mapping-{seed}-{size}")
            charges = two_key_escape_key_charges(size)
            mapping = template["key_mapping"]
            assert sorted(mapping) == list(range(charges))
            assert all(target != index for index, target in enumerate(mapping))


def test_key_opens_a_different_lock(db, http):
    """Clicking a lock never opens itself: the mapped lock opens instead."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    before = client_for(http, bob).get("/games/two-key-escape").json()
    clicked = [before["keys"][0]["target_row"], before["keys"][0]["target_col"]]
    opened = _mapping(db)[0]
    assert opened != clicked

    used = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert used.status_code == 200, used.text
    body = used.json()
    # The clicked lock stays shut; the mapped one opened.
    assert clicked in body["locked_cells"]
    assert opened not in body["locked_cells"]
    # ...and a different eligible cell became blocked.
    assert len(body["blocked_cells"]) == 1
    blocked = tuple(body["blocked_cells"][0])
    assert blocked != tuple(opened)
    assert blocked != (body["exit_row"], body["exit_col"])
    assert blocked != (0, 0)
    assert blocked != (body["player_row"], body["player_col"])
    # The maze stays solvable from the player's cell, reading through the
    # gates like the server proof does.
    _assert_truly_solvable(body)
    assert body["cooldown_seconds_remaining"] > 0
    assert body["total_keys"] == 1

    # Unknown keys are refused without changing anything.
    missing = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 999})
    assert missing.status_code == 404, missing.text
    assert escape_row(db, user_a_id=A_ID, user_b_id=B_ID).total_keys == 1


def test_keys_are_reusable_once_relocked(db, http):
    """No lock is ever permanently lost: the same key opens again later.

    Open one lock, open another (sending the first back), satisfy the
    cooldown, then spend the first key again: it lands, because keys are
    throttled by the cooldown alone, never consumed and never gated on
    movement.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)
    opened = _mapping(db)

    first = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert first.status_code == 200, first.text
    assert opened[0] not in first.json()["locked_cells"]

    _age_cooldown(db)
    probe_step(http, alice, avoid={tuple(opened[0])}, strict=False)
    second = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert second.status_code == 200, second.text
    assert second.json()["locked_cells"].count(opened[0]) == 1

    _age_cooldown(db)
    probe_step(http, alice, avoid={tuple(opened[1])}, strict=False)
    third = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    assert third.status_code == 200, third.text
    body = third.json()
    assert opened[0] not in body["locked_cells"]
    assert opened[1] in body["locked_cells"]
    assert body["total_keys"] == 3


def test_second_key_sends_first_lock_back(db, http):
    """Only one lock stays open: the next key shuts the previous one again."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)
    opened = _mapping(db)

    client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    first = client_for(http, alice).get("/games/two-key-escape").json()
    assert opened[0] not in first["locked_cells"]

    # Satisfy the cooldown, then open another. The setup moves stop
    # anywhere but on the open cell itself -- standing on it would refuse
    # the next key for occupancy, which is the next test.
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    # Roam one step without stepping onto the open lock: standing on it
    # would refuse the next key for occupancy, which is the next test.
    moved = probe_step(http, alice, avoid={tuple(opened[0])}, strict=False)
    assert moved is not None and moved.status_code == 200, moved
    state = client_for(http, alice).get("/games/two-key-escape").json()
    assert (state["player_row"], state["player_col"]) != tuple(opened[0])
    second = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert second.status_code == 200, second.text
    body = second.json()
    # Exactly the newest lock is open; the first went back into its place.
    assert opened[1] not in body["locked_cells"]
    assert opened[0] in body["locked_cells"]
    assert body["total_keys"] == 2
    # ...and the resulting board is still solvable, reading through the gates.
    _assert_truly_solvable(body)


def test_key_refused_while_navigator_on_open_cell(db, http):
    """Re-locking under the Navigator's feet is refused without consuming."""
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)
    opened = _mapping(db)

    client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 0})
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        row.player_row, row.player_col = opened[0]

    refused = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert refused.status_code == 409, refused.text
    assert "step off" in refused.json()["detail"].lower()
    row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
    assert row.total_keys == 1
    # Nothing moved: the open cell stayed open, nothing newly blocked.
    assert [list(cell) for cell in row.unlocked_cells] == [opened[0]]
    assert len(row.blocked_cells) == 1


def _assert_truly_solvable(state):
    """The server proof, mirrored: exit reachable reading through all locks.

    Sound for the same reason the server's is -- keys are reusable, so no
    lock is ever permanently lost and a route through locks is a route the
    operator can always reopen. A board failing this has no route at all,
    locks or otherwise, and no key may land on it.
    """
    assert two_key_escape_gameplay._bfs_reaches(
        state["grid_size"],
        {tuple(cell) for cell in state["walls"]},
        {tuple(cell) for cell in state["blocked_cells"]},
        (state["player_row"], state["player_col"]),
        (state["exit_row"], state["exit_col"]),
    )


def test_every_key_keeps_maze_truly_solvable(db, http):
    """Regression: every accepted block must keep a route through locks.

    Locks are reopenable, walls are not: the proof must tell them apart.
    Several fresh mazes, several keys each, moving and unlocking between
    them: a lock-readable route must survive every single activation.
    """
    from app.db_models import Base

    for _ in range(5):
        with db.engine.begin() as connection:
            tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
            connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
        seed_couple_questions(db)
        seed_questionnaire(db)
        seed_escape(db)
        alice = make_user(db, A_ID, "escape_a", "Alice")
        bob = make_user(db, B_ID, "escape_b", "Bob")
        open_pair(http, alice, bob)
        finalize_roles(http, alice, bob)

        opened: set[tuple[int, int]] = set()
        for key_index in range(3):
            with db.sessions.begin() as session:
                row = session.scalar(
                    select(TwoKeyEscapeSessionRecord).where(
                        TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                        TwoKeyEscapeSessionRecord.user_b_id == B_ID,
                    )
                )
                row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)
            state = client_for(http, alice).get("/games/two-key-escape").json()
            if state["stage"] == "completed":
                break
            # Roam without stepping onto the open lock: standing on it
            # would refuse the next key for occupancy, by design.
            moved = probe_step(http, alice, rotation=key_index, avoid=opened, strict=False)
            assert moved is not None and moved.status_code == 200, moved
            used = client_for(http, bob).post(
                "/games/two-key-escape/key", json={"key_index": key_index}
            )
            assert used.status_code == 200, used.text
            opened = {tuple(_mapping(db)[key_index])}
            state = client_for(http, alice).get("/games/two-key-escape").json()
            if state["stage"] == "completed":
                break
            _assert_truly_solvable(state)


def test_every_open_lock_has_a_step_off(db, http):
    """Leapfrog footing: standing on any open lock always has a move.

    For every lock on the board: open it, stand on it, seal every other
    neighbor, and prove a step still lands. This is the composition piece
    the other proofs assume -- without guaranteed staging, crossing two
    adjacent locks in sequence could strand the Navigator on relocking
    ground with nowhere to step.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    state = client_for(http, alice).get("/games/two-key-escape").json()
    size = state["grid_size"]
    walls = {tuple(cell) for cell in state["walls"]}
    template = {(key["target_row"], key["target_col"]) for key in state["keys"]}
    assert len(template) == state["key_charges"]
    for lock in sorted(template):
        neighbours = [
            (lock[0] + dr, lock[1] + dc)
            for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1))
            if 0 <= lock[0] + dr < size and 0 <= lock[1] + dc < size
        ]
        staging = [
            cell for cell in neighbours
            if cell not in walls and cell not in template
        ]
        assert staging, f"lock {lock} has no staging cell"
        with db.sessions.begin() as session:
            row = session.scalar(
                select(TwoKeyEscapeSessionRecord).where(
                    TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                    TwoKeyEscapeSessionRecord.user_b_id == B_ID,
                )
            )
            row.unlocked_cells = [[lock[0], lock[1]]]
            row.player_row, row.player_col = lock
            row.blocked_cells = [
                [cell[0], cell[1]]
                for cell in neighbours
                if cell not in staging
                and cell not in walls
                and cell not in template
            ]
        stepped = probe_step(http, alice, strict=False)
        assert stepped is not None and stepped.status_code == 200, stepped
        landed = (stepped.json()["player_row"], stepped.json()["player_col"])
        assert landed in staging, f"stepped from {lock} to {landed}, outside staging"


def test_staging_cells_are_never_blocked(db, http):
    """Blocks land everywhere but staging: several keys, zero overlap.

    No movement between keys either -- none is required -- so the player
    never leaves the start corner while four blocks land elsewhere.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    for key_index in range(4):
        _age_cooldown(db)
        used = client_for(http, bob).post(
            "/games/two-key-escape/key", json={"key_index": key_index}
        )
        assert used.status_code == 200, used.text
    with db.sessions() as session:
        game = session.scalar(
            select(TwoKeyEscapeGameRecord)
            .where(TwoKeyEscapeGameRecord.status == "active")
            .limit(1)
        )
        staging = two_key_escape_gameplay._staging_cells(game)
        row = escape_row(db, user_a_id=A_ID, user_b_id=B_ID)
        blocked = {(cell[0], cell[1]) for cell in row.blocked_cells}
    assert len(blocked) == 4
    assert blocked & staging == set()


def test_block_proof_reads_through_locks_but_not_walls():
    """The proof treats locks as reopenable and walls as forever.

    A corridor whose only crossing is locked still accepts a block -- the
    operator can always reopen that lock, keys being reusable. A player
    walled into a lock-free pocket refuses one instead: no key could ever
    open the way out, so none may land.
    """
    walls = {(1, 0), (1, 2)}
    assert (
        two_key_escape_gameplay._pick_blocked_cell(
            3,
            walls,
            {(1, 1)},
            set(),
            (0, 0),
            (0, 2),
            (2, 0),
            "proof-seed",
        )
        is not None
    )
    sealed_walls = {(1, 0), (1, 2), (0, 1), (2, 1)}
    assert (
        two_key_escape_gameplay._pick_blocked_cell(
            3, sealed_walls, set(), set(), (0, 0), (0, 2), (0, 0), "proof-seed"
        )
        is None
    )


def test_legacy_null_mapping_falls_back_to_rotation():
    """Rows predating the mapping column still open a different lock."""
    from types import SimpleNamespace

    game = SimpleNamespace(
        locked_cells=[[0, 1], [1, 2], [2, 3]],
        key_mapping=None,
    )
    assert two_key_escape_gameplay._key_mapping(game) == [1, 2, 0]


def test_key_cooldown_enforced_without_any_movement(db, http):
    """The cooldown alone throttles keys: no movement is ever required.

    Use one key, watch the next refused mid-cooldown, age the stamp, and
    spend again with the Navigator never having moved -- the throttle is
    time, not steps. A reconnect still cannot reset it.
    """
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

    # Age the stamp past the cooldown: the next key lands with zero moves
    # made by anyone since the first.
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.last_key_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    second = client_for(http, bob).post("/games/two-key-escape/key", json={"key_index": 1})
    assert second.status_code == 200, second.text
    assert second.json()["total_keys"] == 2
    assert escape_row(db, user_a_id=A_ID, user_b_id=B_ID).total_moves == 0


def test_completion_crosses_every_wall_and_freezes(db, http):
    """No keyless route exists; each wall is crossed through a gate; then done.

    The full playthrough opens at least one gate per wall (keys are reusable,
    so rediscovery never strands the couple), walks each chamber, and proves
    the maze completes exactly the way the generator promised it would.
    """
    seed_couple_questions(db)
    seed_questionnaire(db)
    seed_escape(db)
    alice = make_user(db, A_ID, "escape_a", "Alice")
    bob = make_user(db, B_ID, "escape_b", "Bob")
    open_pair(http, alice, bob)
    finalize_roles(http, alice, bob)

    state = client_for(http, alice).get("/games/two-key-escape").json()
    assert bfs_path(state) is None, "chamber walls must allow no keyless route"
    columns = wall_columns(state)
    assert len(columns) >= 1

    done, stepped = play_through(http, db, alice, bob)
    assert done["stage"] == "completed"
    # At least one gate per wall was stepped through: keys were mandatory,
    # and more than one of them.
    stepped_columns = {cell[1] for cell in stepped}
    assert set(columns) <= stepped_columns, f"walls {columns} vs stepped {stepped}"
    assert done["result"]["completed"] is True
    assert done["result"]["total_moves"] > 0
    assert done["result"]["total_keys"] >= len(wall_columns(done))
    assert done["result"]["elapsed_seconds"] >= 0
    assert "score" not in done["result"]
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

    probe_step(http, alice)
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
    # No score anywhere: time alone ranks, and nothing else is carried.
    assert all("score" not in entry for entry in entries)


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

    probe_step(http, alice)
    with db.sessions.begin() as session:
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.user_a_id == A_ID,
                TwoKeyEscapeSessionRecord.user_b_id == B_ID,
            )
        )
        row.first_move_at = datetime.now(timezone.utc) - timedelta(hours=2)

    done, _stepped = play_through(http, db, alice, bob)
    assert done["stage"] == "completed"
    assert done["result"]["elapsed_seconds"] >= 2 * 3600
    assert "score" not in done["result"]


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
