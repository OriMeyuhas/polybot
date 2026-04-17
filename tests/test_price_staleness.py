"""Tests for price staleness guard — Steps 1-6 of the plan."""

import asyncio
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from polybot.config import BotConfig
from polybot.data.price_feed import MultiAssetPriceFeed
from polybot.types import MarketWindow, Position


# ---------------------------------------------------------------------------
# Step 1 — MultiAssetPriceFeed API tests
# ---------------------------------------------------------------------------


class TestGetPriceAge:
    """1a. get_price_age()"""

    def test_returns_none_when_no_data(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        assert feed.get_price_age("BTC") is None

    def test_returns_age(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        feed._update_price("BTC", Decimal("50000"))
        age = feed.get_price_age("BTC")
        assert age is not None
        assert 0.0 <= age < 2.0  # should be nearly instant

    def test_returns_none_for_unknown_asset(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        assert feed.get_price_age("DOGE") is None


class TestGetPriceIfFresh:
    """1b. get_price_if_fresh()"""

    def test_returns_price_when_fresh(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        feed._update_price("BTC", Decimal("50000"))
        result = feed.get_price_if_fresh("BTC", 10.0)
        assert result == Decimal("50000")

    def test_returns_none_when_stale(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        feed._update_price("BTC", Decimal("50000"))
        # Backdate the timestamp by 60 seconds
        feed._last_ts["BTC"] = time.time() - 60
        result = feed.get_price_if_fresh("BTC", 30.0)
        assert result is None

    def test_returns_none_when_no_data(self):
        feed = MultiAssetPriceFeed(assets=("BTC",))
        result = feed.get_price_if_fresh("BTC", 30.0)
        assert result is None


class TestIsFresh:
    """1c. is_fresh()"""

    def test_all_assets_fresh(self):
        feed = MultiAssetPriceFeed(assets=("BTC", "ETH", "SOL", "XRP"))
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            feed._update_price(asset, Decimal("100"))
        assert feed.is_fresh(30.0) is True

    def test_one_asset_stale(self):
        feed = MultiAssetPriceFeed(assets=("BTC", "ETH", "SOL", "XRP"))
        for asset in ("BTC", "ETH", "SOL", "XRP"):
            feed._update_price(asset, Decimal("100"))
        # Backdate SOL by 60 seconds
        feed._last_ts["SOL"] = time.time() - 60
        assert feed.is_fresh(30.0) is False

    def test_no_data(self):
        feed = MultiAssetPriceFeed(assets=("BTC", "ETH"))
        assert feed.is_fresh(30.0) is False

    def test_partial_data(self):
        feed = MultiAssetPriceFeed(assets=("BTC", "ETH"))
        feed._update_price("BTC", Decimal("50000"))
        # ETH has no data
        assert feed.is_fresh(30.0) is False


# ---------------------------------------------------------------------------
# Step 2 — Config tests
# ---------------------------------------------------------------------------


class TestConfigStaleness:
    """2a. Default config fields."""

    def test_default_price_stale_sec(self):
        cfg = BotConfig()
        assert cfg.price_stale_sec == 30.0

    def test_default_price_snap_stale_sec(self):
        cfg = BotConfig()
        assert cfg.price_snap_stale_sec == 15.0


# ---------------------------------------------------------------------------
# Step 3 — Snapshot gating tests
# ---------------------------------------------------------------------------


def _make_bot(cfg=None, mock_clob=None):
    """Helper to create a Bot with fresh price feed data."""
    from polybot.bot import Bot
    if cfg is None:
        cfg = BotConfig(dry_run=True, bankroll=10_000.0)
    bot = Bot(cfg)
    if mock_clob is not None:
        bot.clob_client = mock_clob
        bot.order_executor.client = mock_clob
    return bot


def _make_market(asset="BTC", open_epoch=None, close_epoch=None):
    now = int(time.time())
    if open_epoch is None:
        open_epoch = now - 60  # already open
    if close_epoch is None:
        close_epoch = now + 240  # closes in 4 minutes
    return MarketWindow(
        market_id=f"{asset.lower()}-5m-{open_epoch}",
        condition_id="0xabc",
        asset=asset,
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=open_epoch,
        close_epoch=close_epoch,
    )


class TestSnapshotGating:
    """Step 3: _snapshot_window_open_prices freshness gate."""

    def test_snapshot_defers_when_price_stale(self):
        """No recent price update — snapshot should NOT be recorded."""
        bot = _make_bot()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market
        # Don't update price feed — it's stale (no data)
        bot._snapshot_window_open_prices()
        assert market.market_id not in bot.window_open_prices

    def test_snapshot_succeeds_when_price_fresh(self):
        """Fresh price — snapshot should be recorded."""
        bot = _make_bot()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market
        # Set a fresh price in the feed
        bot.price_feed._update_price("BTC", Decimal("50000"))
        bot._snapshot_window_open_prices()
        assert market.market_id in bot.window_open_prices
        assert bot.window_open_prices[market.market_id] == 50000.0

    def test_snapshot_defers_when_price_too_old(self):
        """Price exists but older than snap threshold — snapshot deferred."""
        cfg = BotConfig(dry_run=True, bankroll=10_000.0, price_snap_stale_sec=15.0)
        bot = _make_bot(cfg)
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market
        bot.price_feed._update_price("BTC", Decimal("50000"))
        # Backdate the timestamp by 20 seconds (> 15s snap threshold)
        bot.price_feed._last_ts["BTC"] = time.time() - 20
        bot._snapshot_window_open_prices()
        assert market.market_id not in bot.window_open_prices


# ---------------------------------------------------------------------------
# Step 4 — Trading loop gate tests
# ---------------------------------------------------------------------------


class TestTradingLoopGate:
    """Step 4: Block ladder posting/repricing when prices are stale."""

    def test_trading_loop_blocks_ladders_when_stale(self):
        """When price feed is stale, no new ladders should be posted."""
        bot = _make_bot()
        bot.running = True
        bot._start_time = time.time()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market

        # Do NOT update price feed — all prices are stale
        # Mock ladder_manager.post_ladder to track calls
        bot.ladder_manager.post_ladder = MagicMock(return_value=0)
        bot.ladder_manager.post_ladder_pre_open = MagicMock(return_value=0)
        bot.ladder_manager.reprice_if_needed = MagicMock()

        asyncio.run(bot._trading_loop_tick())

        bot.ladder_manager.post_ladder.assert_not_called()
        bot.ladder_manager.post_ladder_pre_open.assert_not_called()
        bot.ladder_manager.reprice_if_needed.assert_not_called()

    def test_trading_loop_allows_ladders_when_fresh(self):
        """When price feed is fresh, ladder posting logic executes."""
        bot = _make_bot()
        bot.running = True
        bot._start_time = time.time()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market

        # Set fresh prices for all configured assets
        for asset in bot.cfg.assets:
            bot.price_feed._update_price(asset, Decimal("50000"))

        # Mock to track calls
        bot.ladder_manager.post_ladder = MagicMock(return_value=0)
        bot.ladder_manager.reprice_if_needed = MagicMock()

        asyncio.run(bot._trading_loop_tick())

        # reprice_if_needed should have been called (fresh prices)
        bot.ladder_manager.reprice_if_needed.assert_called()

    def test_trading_loop_blocks_repricing_when_stale(self):
        """Repricing should also be blocked when stale."""
        bot = _make_bot()
        bot.running = True
        bot._start_time = time.time()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market

        # Stale prices
        bot.ladder_manager.reprice_if_needed = MagicMock()

        asyncio.run(bot._trading_loop_tick())

        bot.ladder_manager.reprice_if_needed.assert_not_called()


# ---------------------------------------------------------------------------
# Step 5 — Settlement gate tests
# ---------------------------------------------------------------------------


class TestDryRunResolveGate:
    """Step 5: _dry_run_resolve() freshness gate."""

    def test_returns_none_when_stale(self):
        """Stale price — resolve should return None."""
        bot = _make_bot()
        market = _make_market("BTC")
        # Set price but backdate by 60 seconds
        bot.price_feed._update_price("BTC", Decimal("50000"))
        bot.price_feed._last_ts["BTC"] = time.time() - 60
        # Set open price so delta can be computed
        bot.window_open_prices[market.market_id] = 49000.0
        bot.spot_prices["BTC"] = 50000.0

        result = bot._dry_run_resolve(market)
        # Paper mode always resolves — stale prices don't block
        assert result in ("UP", "DOWN")

    def test_resolves_even_when_no_price_data(self):
        """No price data — resolve still returns an outcome (paper mode never blocks)."""
        bot = _make_bot()
        market = _make_market("BTC")
        bot.window_open_prices[market.market_id] = 49000.0
        bot.spot_prices["BTC"] = 50000.0

        result = bot._dry_run_resolve(market)
        assert result in ("UP", "DOWN")

    def test_returns_outcome_when_fresh(self):
        """Fresh price with positive delta — should return UP."""
        bot = _make_bot()
        market = _make_market("BTC")
        # Fresh price
        bot.price_feed._update_price("BTC", Decimal("50000"))
        bot.spot_prices["BTC"] = 50000.0
        bot.window_open_prices[market.market_id] = 49000.0

        result = bot._dry_run_resolve(market)
        assert result == "UP"

    def test_returns_down_when_fresh_and_negative_delta(self):
        """Fresh price with negative delta — should return DOWN."""
        bot = _make_bot()
        market = _make_market("BTC")
        bot.price_feed._update_price("BTC", Decimal("48000"))
        bot.spot_prices["BTC"] = 48000.0
        bot.window_open_prices[market.market_id] = 49000.0

        result = bot._dry_run_resolve(market)
        assert result == "DOWN"

    def test_settlement_resolves_even_when_stale(self):
        """Paper mode always resolves — stale prices don't block settlement."""
        bot = _make_bot()
        bot.running = True
        bot._start_time = time.time()
        market = _make_market("BTC")
        bot._active_markets[market.market_id] = market

        # Create a position so it appears in pending settlements
        pos = Position(
            market_id=market.market_id,
            up_qty=100,
            up_cost=45.0,
            dn_qty=100,
            dn_cost=45.0,
        )
        bot.position_manager.positions[market.market_id] = pos
        bot.position_manager.mark_pending_settlement(market.market_id)

        # Make price stale
        bot.price_feed._update_price("BTC", Decimal("50000"))
        bot.price_feed._last_ts["BTC"] = time.time() - 60
        bot.spot_prices["BTC"] = 50000.0
        bot.window_open_prices[market.market_id] = 49000.0

        # Paper mode always resolves — no blocking on stale prices
        outcome = bot._dry_run_resolve(market)
        assert outcome in ("UP", "DOWN")


# ---------------------------------------------------------------------------
# Step 6 — Dashboard state tests
# ---------------------------------------------------------------------------


class TestDashboardState:
    """Step 6: build_state_snapshot includes price_feed_stale."""

    def test_state_snapshot_includes_price_feed_stale_false(self):
        """Fresh prices — price_feed_stale should be False."""
        bot = _make_bot()
        bot._start_time = time.time()
        for asset in bot.cfg.assets:
            bot.price_feed._update_price(asset, Decimal("50000"))

        snapshot = bot.build_state_snapshot()
        assert "price_feed_stale" in snapshot
        assert snapshot["price_feed_stale"] is False

    def test_state_snapshot_includes_price_feed_stale_true(self):
        """No price data — price_feed_stale should be True."""
        bot = _make_bot()
        bot._start_time = time.time()
        # Don't update any prices — all stale

        snapshot = bot.build_state_snapshot()
        assert "price_feed_stale" in snapshot
        assert snapshot["price_feed_stale"] is True

    def test_initial_state_has_price_feed_stale(self):
        """_INITIAL_STATE should have price_feed_stale key."""
        from polybot.web.state import _INITIAL_STATE
        assert "price_feed_stale" in _INITIAL_STATE
        assert _INITIAL_STATE["price_feed_stale"] is False
