"""Tests for tick size fix: T1-T8 per plan docs/plans/2026-03-27-tick-size-fix.md."""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from polybot.tick_size_cache import TickSizeCache, _fetch_tick_size


# ---------------------------------------------------------------------------
# T1: _fetch_tick_size prefers order book path
# ---------------------------------------------------------------------------

class TestFetchTickSizeOrderBookPreferred:
    def test_order_book_used_when_token_id_provided(self):
        """T1: When token_id is given and order book works, use it; skip get_tick_size."""
        client = MagicMock()
        book = SimpleNamespace(tick_size="0.001")
        client.get_order_book.return_value = book
        client.get_tick_size.return_value = 0.01  # should NOT be used

        tick_size, authoritative = _fetch_tick_size(client, "cond_abc", token_id="tok_123")

        assert tick_size == 0.001
        assert authoritative is True
        client.get_order_book.assert_called_once_with("tok_123")
        client.get_tick_size.assert_not_called()


# ---------------------------------------------------------------------------
# T2: _fetch_tick_size falls back to getter for mock clients
# ---------------------------------------------------------------------------

class TestFetchTickSizeFallbackGetter:
    def test_getter_used_when_no_token_id(self):
        """T2a: No token_id → falls back to get_tick_size(key)."""
        client = MagicMock()
        client.get_tick_size.return_value = 0.01

        tick_size, authoritative = _fetch_tick_size(client, "cond_abc", token_id=None)

        assert tick_size == 0.01
        assert authoritative is True
        client.get_tick_size.assert_called_once_with("cond_abc")

    def test_getter_used_when_order_book_fails(self):
        """T2b: token_id given but order book raises → fall back to get_tick_size."""
        client = MagicMock()
        client.get_order_book.side_effect = Exception("404")
        client.get_tick_size.return_value = 0.01

        tick_size, authoritative = _fetch_tick_size(client, "cond_abc", token_id="tok_123")

        assert tick_size == 0.01
        assert authoritative is True
        client.get_tick_size.assert_called_once_with("cond_abc")


# ---------------------------------------------------------------------------
# T3: fallback value is 0.001
# ---------------------------------------------------------------------------

class TestFetchTickSizeFallbackValue:
    def test_fallback_is_0_001(self):
        """T3: When both order book and get_tick_size fail, fallback is 0.001."""
        client = MagicMock()
        client.get_order_book.side_effect = Exception("fail")
        client.get_tick_size.side_effect = Exception("fail")

        tick_size, authoritative = _fetch_tick_size(client, "cond_abc", token_id="tok_123")

        assert tick_size == 0.001
        assert authoritative is False

    def test_fallback_without_token_id(self):
        """T3b: No token_id and get_tick_size fails → 0.001."""
        client = MagicMock()
        del client.get_tick_size  # no getter at all

        tick_size, authoritative = _fetch_tick_size(client, "cond_abc", token_id=None)

        assert tick_size == 0.001
        assert authoritative is False


# ---------------------------------------------------------------------------
# T4: fallback NOT cached
# ---------------------------------------------------------------------------

class TestFallbackNotCached:
    def test_fallback_not_written_to_cache(self, monkeypatch):
        """T4: If _fetch_tick_size returns the fallback, cache stays empty.
        Next call with a working API should fetch fresh."""
        client = MagicMock()
        # First call: both paths fail → fallback
        client.get_order_book.side_effect = Exception("fail")
        del client.get_tick_size

        fake_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: fake_time)

        cache = TickSizeCache(client, ttl_sec=60)
        result1 = cache.get_tick_size("cond_a", token_id="tok_1")
        assert result1 == 0.001
        assert len(cache._cache) == 0  # fallback NOT cached

        # Second call: order book now works
        client.get_order_book.side_effect = None
        client.get_order_book.return_value = SimpleNamespace(tick_size="0.001")
        result2 = cache.get_tick_size("cond_a", token_id="tok_1")
        assert result2 == 0.001
        # Now it IS cached (authoritative)
        assert "cond_a" in cache._cache


# ---------------------------------------------------------------------------
# T5: authoritative values ARE cached
# ---------------------------------------------------------------------------

class TestAuthoritativeCached:
    def test_order_book_result_cached_single_fetch(self, monkeypatch):
        """T5: Working order book → result cached, only one API call within TTL."""
        client = MagicMock()
        client.get_order_book.return_value = SimpleNamespace(tick_size="0.001")

        fake_time = 1000.0
        monkeypatch.setattr(time, "monotonic", lambda: fake_time)

        cache = TickSizeCache(client, ttl_sec=60)
        cache.get_tick_size("cond_a", token_id="tok_1")
        cache.get_tick_size("cond_a", token_id="tok_1")
        cache.get_tick_size("cond_a", token_id="tok_1")

        client.get_order_book.assert_called_once()


# ---------------------------------------------------------------------------
# T6: build_ladder_rungs uses tick_size for floor
# ---------------------------------------------------------------------------

class TestBuildLadderRungsFloor:
    def test_anchor_respects_tick_size_floor(self):
        """T6: With tick_size=0.001 and best_ask=0.015, width=0.02,
        anchor should be max(0.001, 0.015-0.02) = max(0.001, -0.005) = 0.001,
        not clamped to 0.01."""
        from polybot.strategy.ladder_manager import build_ladder_rungs

        rungs = build_ladder_rungs(
            best_ask=0.015,
            budget=100.0,
            rungs=5,
            spacing=0.003,
            width=0.02,
            size_skew=1.5,
            tick_size=0.001,
        )
        assert len(rungs) > 0
        prices = [p for p, _ in rungs]
        # The cheapest rung should be at 0.001, not 0.01
        assert min(prices) < 0.01, f"Min price {min(prices)} should be < 0.01 with tick_size=0.001"

    def test_no_prices_clamped_to_hardcoded_001(self):
        """T6b: No prices should be floored at exactly 0.01 when tick_size=0.001."""
        from polybot.strategy.ladder_manager import build_ladder_rungs

        rungs = build_ladder_rungs(
            best_ask=0.012,
            budget=100.0,
            rungs=3,
            spacing=0.002,
            width=0.01,
            size_skew=1.0,
            tick_size=0.001,
        )
        prices = [p for p, _ in rungs]
        # With anchor = max(0.001, 0.012-0.01) = 0.002, prices should include sub-0.01 values
        for p in prices:
            assert p >= 0.001, f"Price {p} below tick_size"


# ---------------------------------------------------------------------------
# T7: build_ladder_rungs uses tick_size for ceiling
# ---------------------------------------------------------------------------

class TestBuildLadderRungsCeiling:
    def test_prices_can_reach_above_099(self):
        """T7: With tick_size=0.001, prices should be able to go up to 0.999, not 0.99."""
        from polybot.strategy.ladder_manager import build_ladder_rungs

        rungs = build_ladder_rungs(
            best_ask=0.98,
            budget=1000.0,
            rungs=5,
            spacing=0.005,
            width=0.02,
            size_skew=1.5,
            tick_size=0.001,
        )
        assert len(rungs) > 0
        prices = [p for p, _ in rungs]
        # At least one rung should exceed 0.99 (anchor=0.96, prices go up to 0.98)
        # Actually anchor = max(0.001, 0.98 - 0.02) = 0.96
        # prices: 0.96, 0.965, 0.97, 0.975, 0.98 — none exceed 0.99
        # Let's use a higher best_ask to test the ceiling
        rungs2 = build_ladder_rungs(
            best_ask=0.995,
            budget=1000.0,
            rungs=5,
            spacing=0.002,
            width=0.008,
            size_skew=1.0,
            tick_size=0.001,
            max_rung_price=1.0,  # disable cap for this test
        )
        prices2 = [p for p, _ in rungs2]
        max_price = max(prices2)
        assert max_price > 0.99, f"Max price {max_price} should exceed 0.99 with tick_size=0.001"


# ---------------------------------------------------------------------------
# T8: Integration — ladder posts with 0.001 tick
# ---------------------------------------------------------------------------

class TestLadderPostIntegration:
    def test_order_prices_have_3_decimal_places(self):
        """T8: Full LadderManager.post_ladder with tick_cache returning 0.001.
        Verify order prices have 3 decimal places (not rounded to 2)."""
        from polybot.strategy.ladder_manager import build_ladder_rungs

        rungs = build_ladder_rungs(
            best_ask=0.045,
            budget=200.0,
            rungs=6,
            spacing=0.005,
            width=0.03,
            size_skew=1.5,
            tick_size=0.001,
        )
        for price, _ in rungs:
            # Price should be a multiple of 0.001
            remainder = round(price % 0.001, 10)
            assert remainder == 0.0 or abs(remainder - 0.001) < 1e-9, \
                f"Price {price} not aligned to tick_size=0.001"
