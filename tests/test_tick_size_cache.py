"""Tests for tick size cache with TTL and invalidation."""

import time
from unittest.mock import MagicMock

import pytest

from polybot.tick_size_cache import TickSizeCache, round_to_tick


# ---------------------------------------------------------------------------
# round_to_tick tests
# ---------------------------------------------------------------------------

class TestRoundToTick:
    def test_rounds_down_to_nearest_tick(self):
        assert round_to_tick(0.52, 0.1) == 0.5

    def test_rounds_up_to_nearest_tick(self):
        assert round_to_tick(0.56, 0.1) == 0.6

    def test_exact_tick_unchanged(self):
        assert round_to_tick(0.50, 0.01) == 0.50

    def test_small_tick_size(self):
        assert round_to_tick(0.123, 0.001) == 0.123

    def test_large_tick_size(self):
        assert round_to_tick(0.03, 0.05) == 0.05

    def test_midpoint_rounds_to_nearest(self):
        # 0.25 is exactly between 0.2 and 0.3 with tick 0.1 -> rounds to 0.2 (banker's) or 0.3
        result = round_to_tick(0.25, 0.1)
        assert result in (0.2, 0.3)


# ---------------------------------------------------------------------------
# TickSizeCache tests
# ---------------------------------------------------------------------------

class TestTickSizeCache:
    def _make_client(self, tick_size=0.01):
        client = MagicMock()
        client.get_tick_size.return_value = tick_size
        return client

    def test_returns_fetched_value(self):
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=60)
        assert cache.get_tick_size("cond_a") == 0.01

    def test_caches_value_single_fetch(self):
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=60)
        cache.get_tick_size("cond_a")
        cache.get_tick_size("cond_a")
        cache.get_tick_size("cond_a")
        client.get_tick_size.assert_called_once_with("cond_a")

    def test_invalidate_forces_refetch(self):
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=60)

        cache.get_tick_size("cond_a")
        assert client.get_tick_size.call_count == 1

        cache.invalidate("cond_a")
        cache.get_tick_size("cond_a")
        assert client.get_tick_size.call_count == 2

    def test_ttl_expiry_triggers_refetch(self, monkeypatch):
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=5)

        fake_time = 1000.0

        def mock_monotonic():
            return fake_time

        monkeypatch.setattr(time, "monotonic", mock_monotonic)

        cache.get_tick_size("cond_a")
        assert client.get_tick_size.call_count == 1

        # Still within TTL
        fake_time = 1004.0
        cache.get_tick_size("cond_a")
        assert client.get_tick_size.call_count == 1

        # Past TTL
        fake_time = 1006.0
        cache.get_tick_size("cond_a")
        assert client.get_tick_size.call_count == 2

    def test_different_markets_cached_separately(self):
        client = MagicMock()
        client.get_tick_size.side_effect = lambda cid: {
            "cond_a": 0.01,
            "cond_b": 0.001,
        }[cid]

        cache = TickSizeCache(client, ttl_sec=60)

        assert cache.get_tick_size("cond_a") == 0.01
        assert cache.get_tick_size("cond_b") == 0.001
        assert client.get_tick_size.call_count == 2

        # Subsequent calls use cache
        cache.get_tick_size("cond_a")
        cache.get_tick_size("cond_b")
        assert client.get_tick_size.call_count == 2

    def test_invalidate_nonexistent_key_is_noop(self):
        client = self._make_client()
        cache = TickSizeCache(client, ttl_sec=60)
        cache.invalidate("does_not_exist")  # should not raise

    def test_evict_stale_removes_old_entries(self, monkeypatch):
        """Entries older than max_age_factor * TTL are evicted."""
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=60)

        fake_time = 1000.0

        def mock_monotonic():
            return fake_time

        monkeypatch.setattr(time, "monotonic", mock_monotonic)

        cache.get_tick_size("cond_a")
        cache.get_tick_size("cond_b")
        assert len(cache._cache) == 2

        # Advance past 10x TTL (600s)
        fake_time = 1700.0
        evicted = cache.evict_stale()

        assert evicted == 2
        assert len(cache._cache) == 0

    def test_evict_stale_keeps_recent(self, monkeypatch):
        """Entries within max_age_factor * TTL are kept."""
        client = self._make_client(0.01)
        cache = TickSizeCache(client, ttl_sec=60)

        fake_time = 1000.0

        def mock_monotonic():
            return fake_time

        monkeypatch.setattr(time, "monotonic", mock_monotonic)

        cache.get_tick_size("cond_a")
        cache.get_tick_size("cond_b")

        # Advance within 10x TTL (100s < 600s)
        fake_time = 1100.0
        evicted = cache.evict_stale()

        assert evicted == 0
        assert len(cache._cache) == 2
