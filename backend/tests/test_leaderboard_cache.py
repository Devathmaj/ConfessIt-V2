"""Focused tests for the versioned shared-leaderboard cache.

No database and no live Redis: the fake only implements the commands the cache
uses, so these tests exercise key resolution, TTL, invalidation, and the
version race between a loader and a finalizer.
"""

from datetime import date, datetime, timezone

import pytest

from app.services import leaderboard_cache


class FakeRedisClient:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        self.ttls[key] = ex

    def incr(self, key):
        current = int(self.values.get(key, 0)) + 1
        self.values[key] = str(current)
        return current


class FakeRedisService:
    def __init__(self):
        self.client = FakeRedisClient()


@pytest.fixture
def cache(monkeypatch):
    fake = FakeRedisService()
    monkeypatch.setattr(leaderboard_cache, "redis_service", fake)
    return fake


def test_window_key_keeps_the_explicit_anchor():
    assert leaderboard_cache.window_key("weekly", date(2026, 10, 5)) == "2026-10-05"
    assert leaderboard_cache.window_key("weekly", date(2026, 10, 11)) == "2026-10-11"
    assert leaderboard_cache.window_key("monthly", date(2026, 10, 15)) == "2026-10-15"
    assert leaderboard_cache.window_key("daily", date(2026, 10, 8)) == "2026-10-08"


def test_window_key_resolves_missing_anchor_and_ignores_all_time_anchor():
    today = datetime.now(timezone.utc).date()

    assert leaderboard_cache.window_key("weekly", None) == (
        leaderboard_cache.window_key("weekly", today)
    )
    assert leaderboard_cache.window_key("all_time", None) == "all_time"
    assert leaderboard_cache.window_key("all_time", date(2026, 10, 8)) == "all_time"


def test_window_key_leaves_invalid_period_for_loader_to_refuse():
    assert leaderboard_cache.window_key("bogus", None) == "bogus:-"


def test_put_then_get_uses_ttl(cache):
    parts = ("individual", "-", "weekly", "2026-10-05", 0, 50)

    leaderboard_cache.put(parts, {"total": 1, "entries": []})

    assert leaderboard_cache.get(parts) == {"total": 1, "entries": []}
    assert list(cache.client.ttls.values()) == [
        leaderboard_cache.LEADERBOARD_CACHE_TTL_SECONDS
    ]


def test_invalidate_makes_old_version_unreachable(cache):
    parts = ("individual", "-", "weekly", "2026-10-05", 0, 50)
    leaderboard_cache.put(parts, {"total": 1, "entries": []})

    leaderboard_cache.invalidate()

    assert leaderboard_cache.get(parts) is None
    assert cache.client.values[leaderboard_cache._VERSION_KEY] == "1"


def test_invalidation_during_load_does_not_pin_stale_data(cache):
    parts = ("individual", "-", "weekly", "2026-10-05", 0, 50)
    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        if calls == 1:
            leaderboard_cache.invalidate()
        return {"total": calls, "entries": []}

    assert leaderboard_cache.cached_board(parts, loader) == {"total": 1, "entries": []}

    # The write landed under the pre-invalidation version, so the new version
    # cannot serve it even though the loader returned during the race.
    assert leaderboard_cache.get(parts) is None

    assert leaderboard_cache.cached_board(
        parts, lambda: {"total": 2, "entries": []}
    ) == {"total": 2, "entries": []}


def test_cache_hit_skips_loader(cache):
    parts = ("individual", "-", "weekly", "2026-10-05", 0, 50)
    leaderboard_cache.put(parts, {"total": 1, "entries": []})

    def loader():
        raise AssertionError("cache hit must not call the loader")

    assert leaderboard_cache.cached_board(parts, loader) == {"total": 1, "entries": []}
