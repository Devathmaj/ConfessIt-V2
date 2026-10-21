"""Per-game availability, content mode, and reset cadence settings."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select, text

from ..db_models import GameConfigRecord
from ..limits import GAME_CONTENT_MODES, GAME_IDS, RESET_CADENCE_VALUES
from ..postgres import Database

# Shared per-game locks serialize scheduler transitions and gameplay writes.
# Acquire couple and escape pair locks after the game lock to prevent deadlocks.
_GAME_LOCK_KEYS: dict[str, int] = {
    "wordle": 7_260_001,
    "anagram": 7_260_002,
    "couple-questionnaire": 7_260_003,
    "reaction-time": 7_260_004,
    "connections": 7_260_005,
    "trivia-drop": 7_260_006,
    "two-key-escape": 7_260_007,
    "plot-twist": 7_260_008,
    "case-closed": 7_260_009,
}

_DEFAULT_CONTENT_MODE: dict[str, Optional[str]] = {
    "wordle": "random",
    "anagram": "random",
    "couple-questionnaire": None,
    "reaction-time": None,
    "connections": "manual",
    "trivia-drop": None,
    "two-key-escape": None,
    "plot-twist": None,
    "case-closed": None,
}

# The maze edge length a fresh Two-Key Escape row starts with: 12x12 until an
# administrator picks otherwise. No other game reads this column.
_DEFAULT_GRID_SIZE: dict[str, Optional[int]] = {
    "two-key-escape": 12,
}


def validate_game_id(game_id: str) -> str:
    """Refuse anything outside the closed game vocabulary."""
    if game_id not in GAME_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown game {game_id!r}. Expected one of {', '.join(GAME_IDS)}.",
        )
    return game_id


def game_lock_key(game_id: str) -> int:
    """The advisory-lock key serialising ``game_id``'s lifecycle transitions."""
    return _GAME_LOCK_KEYS[validate_game_id(game_id)]


def lock_game_for_play(session, game_id: str) -> None:
    """Hold ``game_id``'s lifecycle lock for a gameplay write transaction (ENG-001).

    Call as the first statement inside any transaction that creates or mutates
    gameplay state keyed to a game session -- guesses, submissions, answers,
    moves, pair sessions -- so its liveness checks (``status == "active"``,
    ``current.id == game.id``, ``is_reset_due``) cannot go stale between the
    read and the commit while the scheduler rolls the period. Transaction-scoped
    (``pg_advisory_xact_lock``): released automatically at commit/rollback, and
    re-acquiring the same key later in the same transaction is a no-op, so
    helpers that each lock are safe to nest.
    """
    session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": game_lock_key(game_id)},
    )


def default_content_mode(game_id: str) -> Optional[str]:
    """The content mode a fresh game row starts with (None for the questionnaire)."""
    return _DEFAULT_CONTENT_MODE[validate_game_id(game_id)]


def get_config(session, game_id: str) -> GameConfigRecord:
    """Read ``game_id``'s row, creating it with defaults when absent.

    Called inside the caller's transaction: the row is born enabled, visible
    and on the game's default mode/cadence, so a game works before any admin
    has ever opened its settings. Insert-or-nothing on the primary key, so two
    requests racing the first read of a game serialize on the row instead of
    one of them failing with a duplicate key.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    validate_game_id(game_id)
    row = session.scalar(
        select(GameConfigRecord).where(GameConfigRecord.game_id == game_id)
    )
    if row is not None:
        return row
    session.execute(
        pg_insert(GameConfigRecord)
        .values(
            game_id=game_id,
            content_mode=default_content_mode(game_id),
            grid_size=_DEFAULT_GRID_SIZE.get(game_id),
        )
        .on_conflict_do_nothing(index_elements=["game_id"])
    )
    row = session.scalar(
        select(GameConfigRecord).where(GameConfigRecord.game_id == game_id)
    )
    assert row is not None, "the insert-or-nothing must leave the game's row"
    # A legacy row predating the column reads the default rather than NULL,
    # so the game is playable before any admin opens its settings.
    if game_id == "two-key-escape" and row.grid_size is None:
        from ..limits import TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE

        row.grid_size = TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE
    return row


def _as_dict(row: GameConfigRecord) -> dict:
    return {
        "game_id": row.game_id,
        "enabled": bool(row.enabled),
        "is_hidden": bool(row.is_hidden),
        "content_mode": row.content_mode,
        "reset_cadence": row.reset_cadence,
        "grid_size": row.grid_size,
        "difficulty": row.difficulty,
    }


def list_configs(db: Database) -> list[dict]:
    """Every game's configuration, creating missing rows with defaults."""
    with db.sessions.begin() as session:
        return [_as_dict(get_config(session, game_id)) for game_id in GAME_IDS]


def default_grid_size(game_id: str) -> Optional[int]:
    """The maze edge length a fresh Two-Key Escape row starts with (12x12)."""
    from ..limits import TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE

    validate_game_id(game_id)
    if game_id != "two-key-escape":
        return None
    return _DEFAULT_GRID_SIZE.get(game_id, TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE)


def update_config(
    db: Database,
    game_id: str,
    *,
    enabled: Optional[bool] = None,
    is_hidden: Optional[bool] = None,
    content_mode: Optional[str] = None,
    reset_cadence: Optional[str] = None,
    grid_size: Optional[int] = None,
    difficulty: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Patch one game's configuration, refusing values outside the vocabularies.

    Switching a scheduled word or puzzle game (Wordle, Anagram, Connections)
    to a 2/3/5-day cadence additionally re-anchors its live session: the
    current session ends and a fresh window opens under the new cadence,
    consuming the earliest scheduled entry inside it and silently discarding
    the rest (the old rhythm no longer exists, so old dates can never align
    again). See :func:`game_scheduler.reanchor_for_cadence_change`. All other
    cadence changes simply take effect at the next boundary.

    ``difficulty`` belongs to the two AI games alone (Plot Twist and Case
    Closed) and is read by the automated generation pipeline when it enqueues
    a cycle; a change therefore applies from the next session, never to
    content already being generated.
    """
    validate_game_id(game_id)
    if content_mode is not None and content_mode not in GAME_CONTENT_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown content mode {content_mode!r}. "
            f"Expected one of {', '.join(GAME_CONTENT_MODES)}.",
        )
    if reset_cadence is not None and reset_cadence not in RESET_CADENCE_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown reset cadence {reset_cadence!r}. "
            f"Expected one of {', '.join(RESET_CADENCE_VALUES)}.",
        )
    if game_id == "couple-questionnaire" and content_mode is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The Couple Questionnaire has no content mode.",
        )
    if game_id == "reaction-time" and content_mode is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Reaction Time has no content mode.",
        )
    if game_id == "plot-twist" and content_mode is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Plot Twist has no content mode.",
        )
    if game_id == "case-closed" and content_mode is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Case Closed has no content mode.",
        )
    if game_id == "connections" and content_mode is not None and content_mode != "manual":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Connections has no random-content mode; every puzzle is configured manually.",
        )
    if game_id == "trivia-drop" and content_mode is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trivia Drop has no content mode.",
        )
    if game_id == "trivia-drop" and reset_cadence is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Trivia Drop has no reset cadence; questions release individually.",
        )
    if grid_size is not None:
        from ..limits import TWO_KEY_ESCAPE_GRID_SIZES

        if game_id != "two-key-escape":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only Two-Key Escape has a grid size.",
            )
        if grid_size not in TWO_KEY_ESCAPE_GRID_SIZES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown grid size {grid_size!r}. "
                f"Expected one of {', '.join(str(size) for size in TWO_KEY_ESCAPE_GRID_SIZES)}.",
            )
    if difficulty is not None:
        from ..limits import AI_GAME_IDS, PLOT_TWIST_DIFFICULTIES

        if game_id not in AI_GAME_IDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only Plot Twist and Case Closed have a difficulty.",
            )
        if difficulty not in PLOT_TWIST_DIFFICULTIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown difficulty {difficulty!r}. "
                f"Expected one of {', '.join(PLOT_TWIST_DIFFICULTIES)}.",
            )
    with db.sessions.begin() as session:
        row = get_config(session, game_id)
        previous_cadence = row.reset_cadence
        if enabled is not None:
            row.enabled = bool(enabled)
        if is_hidden is not None:
            row.is_hidden = bool(is_hidden)
        if content_mode is not None:
            row.content_mode = content_mode
        if reset_cadence is not None:
            row.reset_cadence = reset_cadence
        if grid_size is not None:
            row.grid_size = grid_size
        if difficulty is not None:
            row.difficulty = difficulty
        # A legacy row predating the column reads the default rather than
        # NULL, so the game is playable before any admin opens its settings.
        if game_id == "two-key-escape" and row.grid_size is None:
            from ..limits import TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE

            row.grid_size = TWO_KEY_ESCAPE_DEFAULT_GRID_SIZE
        session.flush()
        result = _as_dict(row)
    # Re-anchor multi-day sessions after the new cadence commits; the game lock
    # serializes this with any concurrent scheduler tick.
    if (
        game_id in ("wordle", "anagram", "connections")
        and reset_cadence is not None
        and reset_cadence != previous_cadence
        and reset_cadence in ("every_2_days", "every_3_days", "every_5_days")
    ):
        from .game_scheduler import reanchor_for_cadence_change

        reanchor_for_cadence_change(
            db, game_id, now=now or datetime.now(timezone.utc)
        )
    return result


class GameUnavailable(Exception):
    """A server-side availability refusal carrying a client-branchable code.

    ``detail`` stays a plain sentence (like ``MatchmakingRefusal``) so toasts
    keep working unchanged; ``code`` travels beside it for the screen to branch
    on (``game_disabled`` vs ``game_hidden`` vs ``game_not_configured``).
    """

    def __init__(self, detail: str, code: str, status_code: int = 403) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code
        self.status_code = status_code


def check_playable(session, game_id: str) -> GameConfigRecord:
    """Enforce availability server-side: hidden and disabled games refuse play.

    Hidden is checked first so a hidden game never leaks whether it is also
    disabled. Both refuse with 403 and a machine-readable ``code`` the screens
    branch on; the human sentence stays in ``detail``.
    """
    row = get_config(session, game_id)
    if row.is_hidden:
        raise GameUnavailable("This game is not available.", code="game_hidden")
    if not row.enabled:
        raise GameUnavailable(
            "This game is temporarily unavailable. Please try again later.",
            code="game_disabled",
        )
    return row


def playable_error_code(detail: str) -> str:
    """Map an availability refusal sentence to the code the client branches on."""
    if detail == "This game is not available.":
        return "game_hidden"
    return "game_disabled"
