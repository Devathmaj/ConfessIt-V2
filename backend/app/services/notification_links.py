"""Frontend URL generators for notification email actions."""

from __future__ import annotations

from urllib.parse import quote

from ..config import public_frontend_origin

# Target IDs are quoted to prevent URL manipulation.
_PARTICIPANT_TARGET_PATHS: dict[str, str] = {
    "conversation": "/inbox?match={target}",
    "love_note": "/love-notes?tab=inbox&note={target}",
}

_STAFF_CATEGORY_PATHS: dict[str, str] = {
    "report": "/admin/reports",
    "love_note": "/admin/love-notes",
    "game": "/admin/games",
}

# Specific admin management paths per game console.
_STAFF_GAME_PATHS: dict[str, str] = {
    "featured": "/admin/games/manage/featured",
    "wordle": "/admin/games/manage/wordle",
    "anagram": "/admin/games/manage/anagram",
    "connections": "/admin/games/manage/connections",
    "trivia-drop": "/admin/games/manage/trivia-drop",
}


def _absolute(path: str) -> str:
    return f"{public_frontend_origin()}{path}"


def participant_notification_link(
    target_kind: str | None, target_id: str | None
) -> str | None:
    """The in-app page a participant notification points at, or None."""
    if not target_kind or not target_id:
        return None
    template = _PARTICIPANT_TARGET_PATHS.get(target_kind)
    if template is None:
        return None
    target = quote(str(target_id), safe="")
    return _absolute(template.format(target=target))


def staff_notification_link(
    category: str, *, game_id: str | None = None
) -> str | None:
    """Return destination action URL for a staff notification category."""
    if category == "game" and game_id:
        path = _STAFF_GAME_PATHS.get(game_id)
        if path is None:
            path = f"/admin/games/manage/{quote(str(game_id), safe='')}"
    else:
        path = _STAFF_CATEGORY_PATHS.get(category)
    if path is None:
        return None
    return _absolute(path)
