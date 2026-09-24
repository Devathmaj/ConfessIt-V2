"""Per-game configuration: availability, content mode and reset cadence.

One row per game in ``game_configs``. The games never share a setting:
Wordle can reset daily while Anagram resets every five days and the Couple
Questionnaire resets weekly, and each game is enabled/hidden independently.
Reaction Time owns a row for the same availability switches; its sessions are
featured dates created by an administrator rather than rolled by the
scheduler, so its reset cadence is stored but never read.

``enabled`` and ``is_hidden`` are separate facts with separate consequences:

* disabled + visible: shown in selection but marked temporarily unavailable,
  direct play refused, session lifecycle continues;
* hidden (regardless of ``enabled``): absent from selection, direct play
  refused, and the scheduler creates no new sessions.
"""

from typing import Optional

from fastapi import HTTPException, status
from sqlalchemy import select

from ..db_models import GameConfigRecord
from ..limits import GAME_CONTENT_MODES, GAME_IDS, RESET_CADENCE_VALUES
from ..postgres import Database

# Advisory-lock keys serialising one game's lifecycle transitions (scheduler run
# vs admin Update). Arbitrary but fixed; must not be reused elsewhere.
_GAME_LOCK_KEYS: dict[str, int] = {
    "wordle": 7_260_001,
    "anagram": 7_260_002,
    "couple-questionnaire": 7_260_003,
    "reaction-time": 7_260_004,
}

_DEFAULT_CONTENT_MODE: dict[str, Optional[str]] = {
    "wordle": "random",
    "anagram": "random",
    "couple-questionnaire": None,
    "reaction-time": None,
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


def default_content_mode(game_id: str) -> Optional[str]:
    """The content mode a fresh game row starts with (None for the questionnaire)."""
    return _DEFAULT_CONTENT_MODE[validate_game_id(game_id)]


def get_config(session, game_id: str) -> GameConfigRecord:
    """Read ``game_id``'s row, creating it with defaults when absent.

    Called inside the caller's transaction: the row is born enabled, visible,
    and on the game's default mode/cadence, so a game works before any admin
    has ever opened its settings. The insert is insert-or-nothing on the
    primary key, so two requests racing the first read of a game serialize on
    the row instead of one of them failing with a duplicate key.
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
        .values(game_id=game_id, content_mode=default_content_mode(game_id))
        .on_conflict_do_nothing(index_elements=["game_id"])
    )
    row = session.scalar(
        select(GameConfigRecord).where(GameConfigRecord.game_id == game_id)
    )
    assert row is not None, "the insert-or-nothing must leave the game's row"
    return row


def _as_dict(row: GameConfigRecord) -> dict:
    return {
        "game_id": row.game_id,
        "enabled": bool(row.enabled),
        "is_hidden": bool(row.is_hidden),
        "content_mode": row.content_mode,
        "reset_cadence": row.reset_cadence,
    }


def list_configs(db: Database) -> list[dict]:
    """Every game's configuration, creating missing rows with defaults."""
    with db.sessions.begin() as session:
        return [_as_dict(get_config(session, game_id)) for game_id in GAME_IDS]


def update_config(
    db: Database,
    game_id: str,
    *,
    enabled: Optional[bool] = None,
    is_hidden: Optional[bool] = None,
    content_mode: Optional[str] = None,
    reset_cadence: Optional[str] = None,
) -> dict:
    """Patch one game's configuration, refusing values outside the vocabularies."""
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
    with db.sessions.begin() as session:
        row = get_config(session, game_id)
        if enabled is not None:
            row.enabled = bool(enabled)
        if is_hidden is not None:
            row.is_hidden = bool(is_hidden)
        if content_mode is not None:
            row.content_mode = content_mode
        if reset_cadence is not None:
            row.reset_cadence = reset_cadence
        session.flush()
        return _as_dict(row)


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
