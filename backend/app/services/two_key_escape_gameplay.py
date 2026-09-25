"""Playing Two-Key Escape: the participant-facing half of the feature.

The pairing half belongs to the Couple Questionnaire and is reused, not
reimplemented: a couple here is a mutual-selection session in
``couple_game_sessions`` for the current global questionnaire period, read
through ``couple_gameplay``'s own lookup. This module never decides who forms
a couple, never lists partner candidates, and never creates a pairing -- it
resolves the caller's existing couple session and plays the maze on top of
it. No mutual-selection session, no game.

Five rules hold at the bottom of everything here.

**One global maze template per period, one live overlay per couple.** The
reset loop opens each period with a single generated maze (edge length from
the stored grid-size configuration, deterministic seed, persisted walls and
locked cells). Every couple in the period plays that same template, and each
couple's position, unlocked cells, blocked cells, consumed keys, cooldown
and counters live on its own ``two_key_escape_sessions`` row -- so one
couple's actions never touch another's, and a refresh reads the same walls.

**Roles are submitted, then finalized exactly once.** Clicking a role option
is local-only on the page; confirming sends one submission for the caller.
When both submissions are stored and distinct, the assignment finalizes
atomically in the same transaction. Identical submissions finalize nothing
and surface a conflict both sides can see. Finalized roles are immutable.

**Clients submit actions, never state.** Movement and key use arrive as
``{direction}`` / ``{key_index}`` and are validated against the stored row
under ``FOR UPDATE``: bounds, walls, locks, blocks, role, cooldown and the
movement gate are all re-checked server-side, and ``state_version`` bumps on
every accepted action.

**A key trades one cell for another, never a solution.** Each lock opens a
*different* lock, per the maze's hidden mapping -- the clicked lock itself
never opens, so players must discover which lock opens the cell they need.
Only one lock stays open at a time: opening another sends the previous one
back (refused while the Navigator stands on it). Blocking picks a different
traversable cell by game logic and is accepted only if breadth-first search
still reaches the exit from the player's position on the resulting board.
Start, exit and the player's cell are never blocked.

**Scoring measures start-to-finish time, nothing else.** The Navigator's
first valid move stamps the start; reaching the exit stamps the end. What
happens between -- logouts, tab switches, hours away -- simply counts, by
decision rather than by accident. Completion ranks above anything
incomplete; among completed couples the shorter elapsed time wins.
"""

import random
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ..db_models import (
    CoupleGameSelectionRecord,
    CoupleGameSessionRecord,
    TwoKeyEscapeGameRecord,
    TwoKeyEscapeSessionRecord,
    UserRecord,
)
from ..limits import (
    TWO_KEY_ESCAPE_GRID_SIZES,
    TWO_KEY_ESCAPE_KEY_COOLDOWN_SECONDS,
    TWO_KEY_ESCAPE_ROLES,
    two_key_escape_key_charges,
)
from ..models import (
    CouplePartnerCandidate,
    CouplePartnerView,
    CoupleSelectionView,
    TwoKeyEscapeKeyView,
    TwoKeyEscapeLeaderboardEntry,
    TwoKeyEscapeResultView,
    TwoKeyEscapeState,
    UserDetails,
)
from ..postgres import Database, is_valid_id

GAME_ID = "two-key-escape"

# Serialises escape-row creation across concurrent role submissions, beside
# the couple session lock. An advisory lock is a number the callers agree on,
# and this one must not be reused elsewhere.
_ESCAPE_SESSION_LOCK_KEY = 7_250_002

_DIRECTIONS: dict[str, tuple[int, int]] = {
    "up": (-1, 0),
    "down": (1, 0),
    "left": (0, -1),
    "right": (0, 1),
}

_ROLE_LABELS = {"navigator": "Navigator", "key_operator": "Key Operator"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today():
    return _now().date()


# --- the maze ------------------------------------------------------------


def _bfs_reaches(
    grid_size: int,
    walls: set[tuple[int, int]],
    blocked: set[tuple[int, int]],
    start: tuple[int, int],
    exit: tuple[int, int],
) -> bool:
    """Whether ``exit`` is reachable from ``start`` avoiding walls and blocks."""
    if start == exit:
        return True
    if start in walls or start in blocked or exit in walls or exit in blocked:
        return False
    seen = {start}
    queue = deque([start])
    while queue:
        row, col = queue.popleft()
        for drow, dcol in _DIRECTIONS.values():
            nxt = (row + drow, col + dcol)
            if not (0 <= nxt[0] < grid_size and 0 <= nxt[1] < grid_size):
                continue
            if nxt in walls or nxt in blocked or nxt in seen:
                continue
            if nxt == exit:
                return True
            seen.add(nxt)
            queue.append(nxt)
    return False


def generate_maze(grid_size: int, seed: str) -> dict[str, Any]:
    """Generate a solvable square maze template, deterministically from ``seed``.

    The start is the top-left corner and the exit the bottom-right. A random
    depth-first path is kept clear first, so a solution always exists; then
    walls and the key charges for this size are placed on cells off that
    path. The route is verified with the locked cells treated as blocked, so
    every emitted template has a valid route before any key is spent --
    unlocking opens shortcuts and alternatives, never the only way through.
    Retried with a derived stream until the layout validates, so every
    emitted template is valid by construction.
    """
    if grid_size not in TWO_KEY_ESCAPE_GRID_SIZES:
        raise ValueError(f"Unknown grid size {grid_size!r}.")
    charges = two_key_escape_key_charges(grid_size)
    start = (0, 0)
    exit_cell = (grid_size - 1, grid_size - 1)

    for attempt in range(100):
        rng = random.Random(f"two-key-escape:{seed}:{grid_size}:{attempt}")
        # A random DFS route from start to exit on the open grid: always
        # exists, and every cell on it stays clear below.
        path: list[tuple[int, int]] = []
        visited: set[tuple[int, int]] = set()

        def _dfs(cell: tuple[int, int]) -> bool:
            path.append(cell)
            visited.add(cell)
            if cell == exit_cell:
                return True
            neighbours = [
                (cell[0] + drow, cell[1] + dcol)
                for drow, dcol in _DIRECTIONS.values()
            ]
            rng.shuffle(neighbours)
            for nxt in neighbours:
                if not (0 <= nxt[0] < grid_size and 0 <= nxt[1] < grid_size):
                    continue
                if nxt in visited:
                    continue
                if _dfs(nxt):
                    return True
            path.pop()
            return False

        _dfs(start)
        path_set = set(path)
        # Walls and locked cells live off the kept route only, so the route
        # always survives them.
        candidates = [
            (row, col)
            for row in range(grid_size)
            for col in range(grid_size)
            if (row, col) not in path_set
            and (row, col) != start
            and (row, col) != exit_cell
        ]
        rng.shuffle(candidates)
        wall_count = min(len(candidates), int(len(candidates) * 0.35))
        walls = set(candidates[:wall_count])
        rest = [cell for cell in candidates[wall_count:] if cell not in walls]
        if len(rest) < charges:
            continue
        locked = rest[:charges]
        # Sattolo's shuffle: a single random cycle, which for two or more
        # keys is a derangement -- key ``i`` always opens a *different*
        # lock, so clicking a lock never opens itself.
        mapping = list(range(charges))
        for i in range(charges - 1, 0, -1):
            j = rng.randrange(i)
            mapping[i], mapping[j] = mapping[j], mapping[i]
        if _bfs_reaches(grid_size, walls, set(locked), start, exit_cell):
            return {
                "grid_size": grid_size,
                "seed": seed,
                "start": [start[0], start[1]],
                "exit": [exit_cell[0], exit_cell[1]],
                "walls": [[row, col] for row, col in sorted(walls)],
                "locked_cells": [[row, col] for row, col in locked],
                "key_mapping": mapping,
            }
    raise RuntimeError("Could not generate a solvable maze.")


def _pairs(values: Any) -> list[list[int]]:
    return [[int(row), int(col)] for row, col in (values or [])]


def _key_mapping(game: TwoKeyEscapeGameRecord) -> list[int]:
    """Which locked cell each key actually opens, by template index.

    The stored derangement, validated before use: a row whose mapping is
    missing (predating the column) or fails validation falls back to a
    deterministic rotation, which for two or more keys is likewise never
    the clicked lock itself.
    """
    locked = _pairs(game.locked_cells)
    stored = game.key_mapping or []
    if (
        len(stored) == len(locked)
        and sorted(int(value) for value in stored) == list(range(len(locked)))
        and all(int(value) != index for index, value in enumerate(stored))
    ):
        return [int(value) for value in stored]
    return [(index + 1) % len(locked) for index in range(len(locked))]


def _as_set(values: Any) -> set[tuple[int, int]]:
    return {(int(row), int(col)) for row, col in (values or [])}


# --- session lifecycle ---------------------------------------------------


def _latest_escape_game(session) -> Optional[TwoKeyEscapeGameRecord]:
    """The current escape period, or None when the loop opened none."""
    from . import game_periods

    game = session.scalar(
        select(TwoKeyEscapeGameRecord)
        .where(TwoKeyEscapeGameRecord.status.in_(("active", "pending")))
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
    """Refuse hidden and disabled escape access with a coded 403."""
    from .game_configs import check_playable

    check_playable(session, GAME_ID)


def _my_couple_session(session, game_id: str, user_id: str):
    """This caller's mutual-selection session, in canonical member order.

    The pairing lives in ``couple_game_sessions`` and is read here, never
    re-decided: the questionnaire's mutual selection is the single mechanism
    that says which two users form a couple.
    """
    from . import couple_gameplay

    questionnaire = couple_gameplay._latest_playable_game(session)
    if questionnaire is None:
        return None
    return session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == questionnaire.id,
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
        )
    )


def create_global_session(
    session,
    *,
    period_start,
    period_end,
    now: datetime,
    created_by: str = "scheduler",
) -> TwoKeyEscapeGameRecord:
    """Open the period starting ``period_start`` with a fresh maze template.

    The edge length is the stored grid-size configuration (defaulting to
    12x12 before any admin opens it); the layout is generated and validated
    server-side from a fresh seed and persisted on the row, so every couple
    in the period plays the same maze and a later period deals a new one
    while the configured size persists.
    """
    from ..limits import TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE
    from .game_configs import get_config

    config = get_config(session, GAME_ID)
    grid_size = int(config.grid_size or TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE)
    template = generate_maze(grid_size, str(uuid.uuid4()))
    row = TwoKeyEscapeGameRecord(
        game_date=period_start,
        status="active",
        period_start_date=period_start,
        period_end_date=period_end,
        started_at=now,
        created_by=created_by,
        grid_size=grid_size,
        maze_seed=template["seed"],
        start_row=template["start"][0],
        start_col=template["start"][1],
        exit_row=template["exit"][0],
        exit_col=template["exit"][1],
        walls=template["walls"],
        locked_cells=template["locked_cells"],
        key_mapping=template["key_mapping"],
    )
    session.add(row)
    session.flush()
    return row


# --- reading -------------------------------------------------------------


def _cooldown_remaining(row: TwoKeyEscapeSessionRecord, now: datetime) -> float:
    if row.last_key_at is None:
        return 0.0
    last = row.last_key_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    remaining = TWO_KEY_ESCAPE_KEY_COOLDOWN_SECONDS - (now - last).total_seconds()
    return max(0.0, remaining)


def _assemble_state(
    session,
    game: TwoKeyEscapeGameRecord,
    couple: CoupleGameSessionRecord,
    row: Optional[TwoKeyEscapeSessionRecord],
    user: UserDetails,
) -> TwoKeyEscapeState:
    """Where the caller stands, derived from stored rows and nothing else."""
    partner_id = (
        str(couple.user_b_id)
        if str(couple.user_a_id) == user.id
        else str(couple.user_a_id)
    )
    partner_row = session.get(UserRecord, partner_id)
    partner = CouplePartnerView(
        id=partner_id, name=partner_row.name if partner_row is not None else "Partner"
    )
    grid_size = int(game.grid_size)
    charges = two_key_escape_key_charges(grid_size)
    locked = _pairs(game.locked_cells)

    base = dict(
        game_id=str(game.id),
        partner=partner,
        grid_size=grid_size,
        key_charges=charges,
        exit_row=int(game.exit_row),
        exit_col=int(game.exit_col),
        walls=_pairs(game.walls),
    )
    if row is None:
        return TwoKeyEscapeState(
            stage="selecting_roles",
            session_id=None,
            my_role=None,
            my_submitted_role=None,
            partner_submitted_role=None,
            roles_finalized=False,
            conflict=None,
            player_row=int(game.start_row),
            player_col=int(game.start_col),
            locked_cells=locked,
            blocked_cells=[],
            keys=[
                TwoKeyEscapeKeyView(
                    key_index=index, target_row=cell[0], target_col=cell[1], used=False
                )
                for index, cell in enumerate(locked)
            ],
            keys_remaining=charges,
            cooldown_seconds_remaining=0,
            movement_required=False,
            moves_since_last_key=0,
            total_moves=0,
            total_keys=0,
            state_version=0,
            result=None,
            **base,
        )

    my_submitted = (
        row.user_a_submitted_role
        if str(couple.user_a_id) == user.id
        else row.user_b_submitted_role
    )
    partner_submitted = (
        row.user_b_submitted_role
        if str(couple.user_a_id) == user.id
        else row.user_a_submitted_role
    )
    finalized = (
        row.navigator_user_id is not None and row.key_operator_user_id is not None
    )
    my_role = None
    if finalized:
        my_role = (
            "navigator" if str(row.navigator_user_id) == user.id else "key_operator"
        )
    conflict = None
    if (
        not finalized
        and my_submitted is not None
        and partner_submitted is not None
        and my_submitted == partner_submitted
    ):
        conflict = (
            f"Both players selected {_ROLE_LABELS[my_submitted]}. "
            "One player must change their role before the game can begin."
        )
    unlocked = _as_set(row.unlocked_cells)
    effective_locked = [cell for cell in locked if tuple(cell) not in unlocked]
    blocked = _pairs(row.blocked_cells)
    used = set(row.used_key_indexes or [])
    now = _now()
    movement_required = bool(row.has_used_key) and int(row.moves_since_last_key) < 1
    completed = bool(row.completed)
    result = TwoKeyEscapeResultView(
        completed=completed,
        total_moves=int(row.total_moves),
        total_keys=int(row.total_keys),
        elapsed_seconds=row.elapsed_seconds,
        score=int(row.score),
        completed_at=row.completed_at,
    )
    return TwoKeyEscapeState(
        stage="completed" if completed else ("playing" if finalized else "selecting_roles"),
        session_id=str(row.id),
        my_role=my_role,  # type: ignore[arg-type]
        my_submitted_role=my_submitted,  # type: ignore[arg-type]
        partner_submitted_role=partner_submitted,  # type: ignore[arg-type]
        roles_finalized=finalized,
        conflict=conflict,
        player_row=int(row.player_row),
        player_col=int(row.player_col),
        locked_cells=effective_locked,
        blocked_cells=blocked,
        keys=[
            TwoKeyEscapeKeyView(
                key_index=index, target_row=cell[0], target_col=cell[1], used=index in used
            )
            for index, cell in enumerate(locked)
        ],
        keys_remaining=max(0, charges - len(used)),
        cooldown_seconds_remaining=_cooldown_remaining(row, now),
        movement_required=movement_required and not completed,
        moves_since_last_key=int(row.moves_since_last_key),
        total_moves=int(row.total_moves),
        total_keys=int(row.total_keys),
        state_version=int(row.state_version),
        result=result if (completed or finalized) else None,
        **base,
    )


def _my_selection(session, user: UserDetails) -> Optional[CoupleSelectionView]:
    """This caller's pending partner choice, if they made one.

    Read from the questionnaire's own selection row, so a pick made here
    and a pick made there are the same fact.
    """
    selection = session.scalar(
        select(CoupleGameSelectionRecord).where(
            CoupleGameSelectionRecord.user_id == user.id
        )
    )
    if selection is None:
        return None
    chosen = session.get(UserRecord, str(selection.selected_user_id))
    if chosen is None:
        return None
    return CoupleSelectionView(
        selected_user_id=str(chosen.id), selected_user_name=chosen.name
    )


def current_state(db: Database, user: UserDetails) -> TwoKeyEscapeState:
    """This caller's place in Two-Key Escape, without creating anything.

    Reads never create sessions: the reset loop owns the global period, the
    questionnaire owns the pairing, and a caller with no couple session reads
    ``no_couple`` -- carrying their pending partner choice, if any -- until
    mutual selection gives them one.
    """
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions() as session:
        game = _latest_escape_game(session)
        if game is None:
            return TwoKeyEscapeState(stage="no_couple", my_selection=_my_selection(session, user))
        couple = _my_couple_session(session, str(game.id), user.id)
        if couple is None:
            return TwoKeyEscapeState(
                stage="no_couple",
                game_id=str(game.id),
                my_selection=_my_selection(session, user),
            )
        row = session.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.two_key_escape_game_id == game.id,
                TwoKeyEscapeSessionRecord.couple_session_id == couple.id,
            )
        )
        return _assemble_state(session, game, couple, row, user)


# --- pairing (reused, not reimplemented) ---------------------------------


def partner_candidates(db: Database, user: UserDetails) -> list[CouplePartnerCandidate]:
    """Every participant the caller may choose, excluding only themselves.

    The questionnaire's own candidate list, not a second one: settling the
    pairing here or there chooses from the same people under the same rules.
    """
    from . import couple_gameplay

    return couple_gameplay.list_partners(db, user)


def choose_partner(db: Database, user: UserDetails, selected_user_id: str) -> TwoKeyEscapeState:
    """Record this caller's partner choice through the questionnaire's selection.

    This is the questionnaire's mutual-selection write, not a parallel one:
    a changed mind replaces the previous pick, and a mutual pick creates the
    one session for the pair -- visible in both games at once, because both
    games read the same session row. Answers this caller's escape state, so
    the screen advances in one round trip.
    """
    from . import couple_gameplay

    couple_gameplay.select_partner(db, user, selected_user_id)
    return current_state(db, user)


# --- roles ---------------------------------------------------------------


def _get_row_for_update(session, game_id: str, couple_id: str):
    return session.scalar(
        select(TwoKeyEscapeSessionRecord)
        .where(
            TwoKeyEscapeSessionRecord.two_key_escape_game_id == game_id,
            TwoKeyEscapeSessionRecord.couple_session_id == couple_id,
        )
        .with_for_update()
    )


def _validate_role(role: str) -> str:
    if role not in TWO_KEY_ESCAPE_ROLES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown role {role!r}. Expected navigator or key_operator.",
        )
    return role


def _try_finalize(row: TwoKeyEscapeSessionRecord) -> None:
    """Finalize the assignment when both submissions are stored and distinct.

    Idempotent: a finalized row is left untouched, so duplicate and
    simultaneous finalization attempts cannot create duplicate assignments
    or overwrite the locked-in roles.
    """
    if row.navigator_user_id is not None and row.key_operator_user_id is not None:
        return
    mine_a = row.user_a_submitted_role
    mine_b = row.user_b_submitted_role
    if mine_a is None or mine_b is None or mine_a == mine_b:
        return
    if mine_a == "navigator":
        row.navigator_user_id = row.user_a_id
        row.key_operator_user_id = row.user_b_id
    else:
        row.navigator_user_id = row.user_b_id
        row.key_operator_user_id = row.user_a_id


def _get_or_create_row(session, game, couple) -> TwoKeyEscapeSessionRecord:
    """This couple's escape row, creating it lazily on first role submission.

    Creation races serialize on the advisory lock plus the unique pair: the
    loser re-reads the winner's row instead of minting a second game.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _ESCAPE_SESSION_LOCK_KEY}
    )
    game = _latest_escape_game(session)
    if game is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="No Two-Key Escape session yet"
        )
    session.execute(
        pg_insert(TwoKeyEscapeSessionRecord)
        .values(
            two_key_escape_game_id=str(game.id),
            couple_session_id=str(couple.id),
            user_a_id=str(couple.user_a_id),
            user_b_id=str(couple.user_b_id),
            player_row=int(game.start_row),
            player_col=int(game.start_col),
        )
        .on_conflict_do_nothing(
            index_elements=[
                TwoKeyEscapeSessionRecord.two_key_escape_game_id,
                TwoKeyEscapeSessionRecord.couple_session_id,
            ]
        )
    )
    row = _get_row_for_update(session, str(game.id), str(couple.id))
    assert row is not None, "the insert-or-nothing must leave the couple's row"
    return row


def submit_role(db: Database, user: UserDetails, role: str) -> TwoKeyEscapeState:
    """Record this caller's confirmed role choice, finalizing on distinct pair.

    Confirming is a write of the caller's own submission only; the other
    side's choice is never touched. When both submissions are stored and
    differ, the assignment finalizes atomically in the same transaction.
    Identical submissions finalize nothing and surface the conflict instead.
    Repeating a submission after finalization is a no-op returning the state.
    """
    _validate_role(role)
    with db.sessions.begin() as session:
        _require_playable(session)
        game = _latest_escape_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Two-Key Escape session yet",
            )
        couple = _my_couple_session(session, str(game.id), user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no couple session yet. Choose each other in the Couple Questionnaire first.",
            )
        row = _get_or_create_row(session, game, couple)
        if row.completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This game is already completed.",
            )
        if str(couple.user_a_id) == user.id:
            row.user_a_submitted_role = role
        else:
            row.user_b_submitted_role = role
        _try_finalize(row)
        session.flush()
        game_id, couple_id = str(game.id), str(couple.id)
    with db.sessions() as fresh:
        game = fresh.get(TwoKeyEscapeGameRecord, game_id)
        couple = fresh.get(CoupleGameSessionRecord, couple_id)
        row = fresh.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.two_key_escape_game_id == game_id,
                TwoKeyEscapeSessionRecord.couple_session_id == couple_id,
            )
        )
        assert game is not None and couple is not None
        return _assemble_state(fresh, game, couple, row, user)


def change_role(db: Database, user: UserDetails, role: str) -> TwoKeyEscapeState:
    """Change this caller's confirmed choice before finalization.

    Allowed only while the assignment is still open: the caller's submission
    is replaced, then validated against the partner's stored choice, and a
    now-distinct pair finalizes atomically. Once finalized, the control is
    gone server-side as well as on the page.
    """
    _validate_role(role)
    with db.sessions.begin() as session:
        _require_playable(session)
        game = _latest_escape_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Two-Key Escape session yet",
            )
        couple = _my_couple_session(session, str(game.id), user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no couple session yet. Choose each other in the Couple Questionnaire first.",
            )
        row = _get_row_for_update(session, str(game.id), str(couple.id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have not submitted a role yet.",
            )
        if row.completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This game is already completed.",
            )
        if row.navigator_user_id is not None and row.key_operator_user_id is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Roles are already finalized and cannot be changed.",
            )
        if str(couple.user_a_id) == user.id:
            row.user_a_submitted_role = role
        else:
            row.user_b_submitted_role = role
        _try_finalize(row)
        session.flush()
        game_id, couple_id = str(game.id), str(couple.id)
    with db.sessions() as fresh:
        game = fresh.get(TwoKeyEscapeGameRecord, game_id)
        couple = fresh.get(CoupleGameSessionRecord, couple_id)
        row = fresh.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.two_key_escape_game_id == game_id,
                TwoKeyEscapeSessionRecord.couple_session_id == couple_id,
            )
        )
        assert game is not None and couple is not None
        return _assemble_state(fresh, game, couple, row, user)


# --- movement ------------------------------------------------------------


def _locked_and_blocked(game, row) -> tuple[set[tuple[int, int]], set[tuple[int, int]]]:
    unlocked = _as_set(row.unlocked_cells)
    locked = {tuple(cell) for cell in _pairs(game.locked_cells)} - unlocked
    return locked, _as_set(row.blocked_cells)


def _score_for(elapsed_seconds: float) -> int:
    """The deterministic display score settled at completion (higher wins).

    Completed couples always outrank incomplete ones (which score 0); among
    completed, less time means more score, monotonically. Ranking itself
    compares the elapsed time, so the score can never disagree with it.
    """
    return max(1, 1_000_000 - int(elapsed_seconds))


def move(db: Database, user: UserDetails, direction: str) -> TwoKeyEscapeState:
    """Advance the couple's character one cell for the assigned Navigator.

    Every valid command moves exactly one orthogonally adjacent cell.
    Diagonal, out-of-grid, wall, locked and blocked targets are refused;
    requests from the Key Operator, strangers, or after completion or
    session expiry are refused without touching the board. A refused move
    changes nothing at all, while a valid one steps, banks the movement the
    next key needs, stamps the start clock on the first step, and completes
    the game at the exit exactly once.
    """
    if direction not in _DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown direction."
        )
    with db.sessions.begin() as session:
        _require_playable(session)
        game = _latest_escape_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Two-Key Escape session yet",
            )
        couple = _my_couple_session(session, str(game.id), user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no couple session in this game.",
            )
        row = _get_row_for_update(session, str(game.id), str(couple.id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Roles are not selected yet.",
            )
        if row.navigator_user_id is None or row.key_operator_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Roles are not finalized yet.",
            )
        if str(row.navigator_user_id) != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the Navigator can move the character.",
            )
        if row.completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This game is already completed.",
            )
        grid_size = int(game.grid_size)
        drow, dcol = _DIRECTIONS[direction]
        target = (int(row.player_row) + drow, int(row.player_col) + dcol)
        walls = _as_set(game.walls)
        locked, blocked = _locked_and_blocked(game, row)

        # A refused board move changes nothing at all: no position, no
        # movement gate, no metric. Raising inside the transaction is safe
        # precisely because there is nothing to roll back.
        if not (0 <= target[0] < grid_size and 0 <= target[1] < grid_size):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That move leaves the maze.")
        if target in walls:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A wall blocks that cell.")
        if target in locked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That cell is locked. The Key Operator must unlock it first.",
            )
        if target in blocked:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That cell is blocked.")
        now = _now()
        if row.first_move_at is None:
            row.first_move_at = now
        row.player_row = target[0]
        row.player_col = target[1]
        row.total_moves = int(row.total_moves) + 1
        row.moves_since_last_key = int(row.moves_since_last_key) + 1
        row.state_version = int(row.state_version) + 1
        if target == (int(game.exit_row), int(game.exit_col)) and not row.completed:
            row.completed = True
            row.completed_at = now
            start = row.first_move_at
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            elapsed = max(0.0, (now - start).total_seconds()) if start is not None else 0.0
            row.elapsed_seconds = elapsed
            row.score = _score_for(elapsed)
        session.flush()
        game_id, couple_id = str(game.id), str(couple.id)
    with db.sessions() as fresh:
        game = fresh.get(TwoKeyEscapeGameRecord, game_id)
        couple = fresh.get(CoupleGameSessionRecord, couple_id)
        row = fresh.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.two_key_escape_game_id == game_id,
                TwoKeyEscapeSessionRecord.couple_session_id == couple_id,
            )
        )
        assert game is not None and couple is not None
        return _assemble_state(fresh, game, couple, row, user)


# --- keys ----------------------------------------------------------------


def _pick_blocked_cell(
    grid_size: int,
    walls: set[tuple[int, int]],
    locked: set[tuple[int, int]],
    blocked: set[tuple[int, int]],
    start: tuple[int, int],
    exit_cell: tuple[int, int],
    player: tuple[int, int],
    rng_seed: str,
    extra_excluded: set[tuple[int, int]] = frozenset(),  # type: ignore[assignment]
) -> Optional[tuple[int, int]]:
    """The game logic's choice of which traversable cell to block.

    Never the start, the exit, the player's cell, a wall, a locked cell or
    an already blocked cell -- and never a cell whose blocking would leave
    the exit unreachable from the player, verified by breadth-first search
    over the resulting board with locked cells treated as impassable.
    ``extra_excluded`` removes further cells from candidacy (e.g. cells that
    must stay open for game reasons) without changing the reachability
    proof, which reads ``locked`` exactly as the board will hold it. None
    when no valid candidate exists.
    """
    candidates = [
        (r, c)
        for r in range(grid_size)
        for c in range(grid_size)
        if (r, c) not in walls
        and (r, c) not in locked
        and (r, c) not in extra_excluded
        and (r, c) not in blocked
        and (r, c) != start
        and (r, c) != exit_cell
        and (r, c) != player
    ]
    random.Random(rng_seed).shuffle(candidates)
    for candidate in candidates:
        if _bfs_reaches(
            grid_size, walls, blocked | {candidate} | locked, player, exit_cell
        ):
            return candidate
    return None


def use_key(db: Database, user: UserDetails, key_index: int) -> TwoKeyEscapeState:
    """Spend one key charge for the assigned Key Operator.

    Each lock opens a *different* lock, per the maze's hidden mapping -- the
    clicked lock itself never opens, so players must discover which lock
    opens the cell they need. Only one lock stays open at a time: opening
    another sends the previous one back. A different eligible traversable
    cell is blocked by the game logic, after verifying the maze -- with the
    revert applied -- stays solvable. Refusals consume nothing and reset
    nothing: only a successful activation spends the charge, stamps the
    server-side cooldown and re-arms the movement gate.
    """
    with db.sessions.begin() as session:
        _require_playable(session)
        game = _latest_escape_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No Two-Key Escape session yet",
            )
        couple = _my_couple_session(session, str(game.id), user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="You have no couple session in this game.",
            )
        row = _get_row_for_update(session, str(game.id), str(couple.id))
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Roles are not selected yet.",
            )
        if row.navigator_user_id is None or row.key_operator_user_id is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Roles are not finalized yet.",
            )
        if str(row.key_operator_user_id) != user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the Key Operator can activate keys.",
            )
        if row.completed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This game is already completed.",
            )
        locked_template = _pairs(game.locked_cells)
        if key_index < 0 or key_index >= len(locked_template):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Unknown key."
            )
        used = set(row.used_key_indexes or [])
        if key_index in used:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That key is already used.",
            )
        now = _now()
        if _cooldown_remaining(row, now) > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The keys are cooling down. Wait a moment before the next one.",
            )
        if bool(row.has_used_key) and int(row.moves_since_last_key) < 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The Navigator must move at least once before the next key.",
            )
        grid_size = int(game.grid_size)
        walls = _as_set(game.walls)
        locked, blocked = _locked_and_blocked(game, row)
        mapping = _key_mapping(game)
        # The clicked lock is the key; the mapping decides what opens.
        target = tuple(locked_template[mapping[key_index]])
        if target not in locked:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That cell is already unlocked.",
            )
        # One open lock at a time: everything currently open goes back,
        # except the cell this action is about to open.
        relock = _as_set(row.unlocked_cells) - {target}
        start = (int(game.start_row), int(game.start_col))
        exit_cell = (int(game.exit_row), int(game.exit_col))
        player = (int(row.player_row), int(row.player_col))
        if player in relock:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The Navigator is standing on the open cell. Step off it before the next key.",
            )
        # The board this action would leave behind: the new cell open, the
        # old ones shut again. Blocking is chosen against it, so the
        # solvability proof covers the revert as well.
        locked_result = (locked - {target}) | relock
        # Every ever-locked cell is ineligible for blocking -- including the
        # one this action opens -- so a key's progress is never undone by a
        # later block. The reachability proof reads ``locked_result`` exactly
        # as the board will hold it: the opened cell traversable, the
        # re-locked ones shut again.
        never_block = _as_set(game.locked_cells)
        candidate = _pick_blocked_cell(
            grid_size,
            walls,
            locked_result,
            blocked,
            start,
            exit_cell,
            player,
            f"{game.maze_seed}:{row.id}:{row.state_version}",
            extra_excluded=never_block,
        )
        if candidate is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That key cannot be used safely right now. Move first and try again.",
            )
        row.unlocked_cells = [[target[0], target[1]]]
        row.blocked_cells = [*_pairs(row.blocked_cells), [candidate[0], candidate[1]]]
        row.used_key_indexes = sorted([*used, key_index])
        row.last_key_at = now
        row.moves_since_last_key = 0
        row.has_used_key = True
        row.total_keys = int(row.total_keys) + 1
        row.state_version = int(row.state_version) + 1
        session.flush()
        game_id, couple_id = str(game.id), str(couple.id)
    with db.sessions() as fresh:
        game = fresh.get(TwoKeyEscapeGameRecord, game_id)
        couple = fresh.get(CoupleGameSessionRecord, couple_id)
        row = fresh.scalar(
            select(TwoKeyEscapeSessionRecord).where(
                TwoKeyEscapeSessionRecord.two_key_escape_game_id == game_id,
                TwoKeyEscapeSessionRecord.couple_session_id == couple_id,
            )
        )
        assert game is not None and couple is not None
        return _assemble_state(fresh, game, couple, row, user)


# --- leaderboard ---------------------------------------------------------


def leaderboard(db: Database, user: UserDetails) -> list[TwoKeyEscapeLeaderboardEntry]:
    """This period's couples in rank order: completed first, then by time.

    Among completed couples the shorter start-to-finish time wins; time away
    counts, by decision. Only couples of the current period appear, and names
    come from the stored pairing -- never from the request -- so one couple
    never sees another's board, only the standings.
    """
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions() as session:
        game = _latest_escape_game(session)
        if game is None:
            return []
        rows = session.scalars(
            select(TwoKeyEscapeSessionRecord)
            .where(TwoKeyEscapeSessionRecord.two_key_escape_game_id == game.id)
            .order_by(
                TwoKeyEscapeSessionRecord.completed.desc(),
                TwoKeyEscapeSessionRecord.elapsed_seconds.asc().nulls_last(),
            )
        ).all()
        entries: list[TwoKeyEscapeLeaderboardEntry] = []
        for rank, row in enumerate(rows, start=1):
            names: list[str] = []
            for user_id in (str(row.user_a_id), str(row.user_b_id)):
                account = session.get(UserRecord, user_id)
                names.append(account.name if account is not None else "Player")
            entries.append(
                TwoKeyEscapeLeaderboardEntry(
                    rank=rank,
                    couple_names=sorted(names),
                    completed=bool(row.completed),
                    total_moves=int(row.total_moves),
                    total_keys=int(row.total_keys),
                    elapsed_seconds=row.elapsed_seconds,
                    score=int(row.score),
                )
            )
        return entries
