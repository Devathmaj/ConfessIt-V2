"""Short-lived cache for shared leaderboard reads.

Leaderboards aggregate the persisted award ledger across every participant.
That is the one read in this app that is both expensive and identical for all
viewers, so it is the one read that earns a Redis cache. Personal reads
(``/leaderboards/me``, a category's ``/me``) never pass through here: their
answer depends on the caller.

Daily, weekly and monthly boards are calendar windows, not rolling windows.
The frontend opens daily boards on the latest completed UTC day because a
day's awards are written at closure; weekly is Monday-Sunday and monthly is
the calendar month. A board only changes when an award is finalized or
reprocessed, and both paths call :func:`invalidate`.

Invalidation is a version counter, not a key scan. Every key embeds the
current version, so a finalize/reprocess bumps one integer and every stale
entry becomes unreachable. The old entries expire on their own TTL; the TTL
is a safety net for a missed invalidation, not the correctness mechanism.
Redis being down is not an error: the loader runs and the response is simply
uncached.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from ..logger import get_logger
from .league_scoring import PERIOD_ALL_TIME, PERIOD_VALUES
from .redis_service import redis_service

logger = get_logger(__name__)

# Correctness comes from invalidate(); the TTL only bounds leftover keys and
# stale data if a version bump could not reach Redis.
LEADERBOARD_CACHE_TTL_SECONDS = 300
_VERSION_KEY = "cache:leaderboards:v1:version"
_PREFIX = "cache:leaderboards:v1"


def _version() -> int:
    raw = redis_service.client.get(_VERSION_KEY)
    return int(raw) if raw is not None else 0


def window_key(period: str, anchor: Optional[date]) -> str:
    """Canonical cache-key component for the effective UTC anchor.

    An omitted anchor resolves to the current UTC day instead of a permanent
    ``None`` key, so a cached board cannot cross a day/week/month boundary.
    The raw anchor is kept when supplied because category responses echo it;
    anchors inside the same week/month therefore get separate entries.
    Invalid periods fall back to the raw values and are refused later by the
    loader.
    """
    if period == PERIOD_ALL_TIME:
        return "all_time"
    if period not in PERIOD_VALUES:
        return f"{period}:{anchor or '-'}"
    return (anchor or datetime.now(timezone.utc).date()).isoformat()


def _key(parts: tuple[Any, ...], version: int) -> str:
    encoded = ":".join(str(part) if part is not None else "-" for part in parts)
    return f"{_PREFIX}:{version}:{encoded}"


def _read(parts: tuple[Any, ...], version: int) -> Optional[dict]:
    try:
        raw = redis_service.client.get(_key(parts, version))
        if raw is None:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except Exception as error:  # pragma: no cover - outage path, logged
        logger.warning("Leaderboard cache read failed, serving fresh: %s", error)
        return None


def _write(parts: tuple[Any, ...], value: dict, version: int) -> None:
    try:
        redis_service.client.set(
            _key(parts, version),
            json.dumps(value, default=str),
            ex=LEADERBOARD_CACHE_TTL_SECONDS,
        )
    except Exception as error:  # pragma: no cover - outage path, logged
        logger.warning("Leaderboard cache write failed, continuing uncached: %s", error)


def get(parts: tuple[Any, ...]) -> Optional[dict]:
    """Return a cached board, or None on a miss or any Redis failure."""
    try:
        version = _version()
    except Exception as error:  # pragma: no cover - outage path, logged
        logger.warning("Leaderboard cache version read failed, serving fresh: %s", error)
        return None
    return _read(parts, version)


def put(parts: tuple[Any, ...], value: dict) -> None:
    """Store a board for the TTL under the current version."""
    try:
        version = _version()
    except Exception as error:  # pragma: no cover - outage path, logged
        logger.warning("Leaderboard cache version read failed, continuing uncached: %s", error)
        return
    _write(parts, value, version)


def cached_board(parts: tuple[Any, ...], loader: Callable[[], dict]) -> dict:
    """Read through the cache; the loader owns the database work.

    The version is captured before loading and reused for the write. If a
    finalizer bumps the version while the loader is running, that write lands
    under the old, unreachable version instead of pinning pre-finalization
    data under the new one.
    """
    try:
        version = _version()
    except Exception as error:  # pragma: no cover - outage path, logged
        logger.warning("Leaderboard cache version read failed, serving fresh: %s", error)
        return loader()
    hit = _read(parts, version)
    if hit is not None:
        return hit
    value = loader()
    _write(parts, value, version)
    return value


def invalidate() -> None:
    """Make every existing leaderboard entry unreachable after a ledger write."""
    try:
        redis_service.client.incr(_VERSION_KEY)
    except Exception as error:  # pragma: no cover - outage path, logged
        # Without Redis there is nothing cached to invalidate.
        logger.warning("Leaderboard cache invalidation skipped: %s", error)
