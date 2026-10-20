"""Absolute frontend links attached to notification emails.

The in-app inbox already decides where a notification opens from its
``target_kind`` / ``target_id`` pair. This module mirrors that mapping for
participant notifications so the email lands on the same page, and provides the
category/game destinations used by staff notices.

``_PARTICIPANT_TARGET_PATHS`` must stay aligned with
``frontend/src/lib/notificationTargets.ts``. A kind this module does not know
returns no link rather than a guessed one, so an email is never clickable when
the app cannot open the item.
"""

from __future__ import annotations

from urllib.parse import quote

from ..config import public_frontend_origin

# A target id is inserted as one path/query value, never as raw URL syntax.
# The values are UUIDs or match ids today, but quoting keeps a malformed id from
# changing the destination.
_PARTICIPANT_TARGET_PATHS: dict[str, str] = {
    "conversation": "/inbox?match={target}",
    "love_note": "/love-notes?tab=inbox&note={target}",
}

_STAFF_CATEGORY_PATHS: dict[str, str] = {
    "report": "/admin/reports",
    "love_note": "/admin/love-notes",
    "game": "/admin/games",
}

# The game consoles that have their own screen. A pending Wordle, Anagram or
# Connections session opens the editor directly; featured and Trivia Drop do
# the same. An unrecognised game id still gets a quoted manage path, which the
# frontend's catch-all route sends back to the games dashboard if it is wrong.
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
    """The review queue or game console a staff notice points at.

    Reports and love notes open the queue that owns them. Games open the exact
    manage screen when the notice names one, otherwise the games dashboard.
    """
    if category == "game" and game_id:
        path = _STAFF_GAME_PATHS.get(game_id)
        if path is None:
            path = f"/admin/games/manage/{quote(str(game_id), safe='')}"
    else:
        path = _STAFF_CATEGORY_PATHS.get(category)
    if path is None:
        return None
    return _absolute(path)
