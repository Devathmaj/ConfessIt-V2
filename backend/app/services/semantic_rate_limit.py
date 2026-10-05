"""Per-bucket fixed-window budgets for hybrid-search embedding calls.

Staff and public traffic get separate buckets (``"admin"`` vs ``"user"``) so one
cannot starve the other. Same fixed-window shape as the global flood brake
(``GlobalRateLimitMiddleware``: one counter per key per minute), but in-process
rather than Redis-shared: search must stay fast and dependency-free, and the
window is short enough that per-worker counting is an acceptable brake rather
than a precise quota. A limit of zero disables the limiter (fail-open).

Thread-safe; call :func:`reset_buckets` in tests for determinism.
"""

import threading
import time

_buckets: dict[tuple[str, str], tuple[int, float]] = {}
_lock = threading.Lock()

# The two traffic classes. Anything else is a caller bug, refused loudly rather
# than silently sharing a bucket.
USER_BUCKET = "user"
ADMIN_BUCKET = "admin"


def _limit_for(bucket: str) -> int:
    from ..config import settings as _settings

    if bucket == ADMIN_BUCKET:
        return int(_settings.EMBEDDING_ADMIN_SEARCH_RATE_LIMIT_PER_MINUTE)
    if bucket == USER_BUCKET:
        return int(_settings.EMBEDDING_SEARCH_RATE_LIMIT_PER_MINUTE)
    raise ValueError(f"Unknown semantic rate-limit bucket {bucket!r}.")


def check_budget(bucket: str, key: str) -> tuple[bool, int]:
    """Consume one embedding call from ``bucket`` for ``key``.

    Returns ``(allowed, retry_after_seconds)``. ``key`` is typically the client
    IP, or the authenticated user/regno id. Expired windows are reset lazily on
    the next call, so idle keys cost one dict entry until they are touched
    again (bounded below by opportunistic eviction).
    """
    limit = _limit_for(bucket)
    if limit <= 0:
        return True, 0
    now = time.time()
    window = int(now // 60)
    slot = (bucket, f"{key}:{window}")
    with _lock:
        count, _ = _buckets.get(slot, (0, now))
        if count >= limit:
            retry_after = max(1, 60 - int(now % 60))
            return False, retry_after
        _buckets[slot] = (count + 1, now)
        if len(_buckets) > 4096:
            current_prefix = f":{window}"
            stale = [
                slot_key
                for slot_key in _buckets
                if not slot_key[1].endswith(current_prefix)
            ]
            for slot_key in stale[:2048]:
                _buckets.pop(slot_key, None)
        return True, 0


def reset_buckets() -> None:
    """Empty all counters. Tests only; never called in production paths."""
    with _lock:
        _buckets.clear()
