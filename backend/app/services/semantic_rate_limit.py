"""Per-bucket fixed-window budgets for hybrid-search embedding calls."""

import threading
import time

_buckets: dict[tuple[str, str], tuple[int, float]] = {}
_lock = threading.Lock()

# Traffic classes for user and admin embedding budgets.
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
    """Check and consume one embedding call budget for the given bucket and key."""
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
    """Clear all bucket counters for test isolation."""
    with _lock:
        _buckets.clear()
