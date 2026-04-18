import asyncio
import time
from decimal import Decimal

import pytest
from unittest.mock import MagicMock
from polybot.bot import Bot
from polybot.config import BotConfig
from polybot.types import MarketWindow, Side, Position


@pytest.fixture
def cfg():
    return BotConfig(
        dry_run=True,
        poll_interval_ms=100,
        ladder_rungs=4,
        ladder_spacing=0.02,
        ladder_width=0.06,
        ladder_size_skew=0.7,
        start_paused=False,
        bankroll=10_000.0,
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-5m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1300,
    )


@pytest.fixture
def mock_clob():
    clob = MagicMock()
    clob.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.44", size="5000")],
        asks=[MagicMock(price="0.46", size="5000")],
    )
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []
    return clob


def _make_bot(cfg, mock_clob=None):
    """Helper to create a Bot and optionally inject a mock CLOB client."""
    bot = Bot(cfg)
    if mock_clob is not None:
        bot.clob_client = mock_clob
        bot.order_executor.client = mock_clob
    return bot


class TestBotInitialization:
    def test_bot_has_ladder_manager(self, cfg, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        assert bot.ladder_manager is not None
        assert bot.order_tracker is not None

    def test_bot_start_time(self, cfg, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        # Start time is 0 until start() is called
        assert bot._start_time == 0.0


class TestWindowOpenPriceSnapshot:
    def _active_market(self):
        """Create a market window that is currently active (open_epoch in past, close_epoch in future)."""
        now = int(time.time())
        return MarketWindow(
            market_id="btc-5m-active",
            condition_id="0xabc",
            asset="BTC",
            timeframe_sec=300,
            up_token_id="tok_up",
            dn_token_id="tok_dn",
            open_epoch=now - 60,
            close_epoch=now + 240,
        )

    def test_snapshot_captures_spot_price(self, cfg, mock_clob):
        import asyncio
        active = self._active_market()
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {active.market_id: active}
        bot.price_feed._update_price("BTC", Decimal("84500"))
        bot.spot_prices["BTC"] = 84500.0

        asyncio.run(bot._snapshot_window_open_prices())

        # Candle open or spot fallback should be captured
        assert active.market_id in bot.window_open_prices
        assert active.market_id in bot._snapped_windows

    def test_snapshot_not_overwritten_on_second_call(self, cfg, mock_clob):
        import asyncio
        active = self._active_market()
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {active.market_id: active}
        bot.price_feed._update_price("BTC", Decimal("84500"))
        bot.spot_prices["BTC"] = 84500.0

        asyncio.run(bot._snapshot_window_open_prices())
        first_price = bot.window_open_prices[active.market_id]
        bot.price_feed._update_price("BTC", Decimal("85000"))
        bot.spot_prices["BTC"] = 85000.0
        asyncio.run(bot._snapshot_window_open_prices())

        assert bot.window_open_prices[active.market_id] == first_price

    def test_snapshot_skips_pre_open_window(self, cfg, mock_clob):
        """Pre-open windows should NOT get their open price snapped early."""
        import asyncio
        now = int(time.time())
        pre_open = MarketWindow(
            market_id="btc-5m-preopen",
            condition_id="0xdef",
            asset="BTC",
            timeframe_sec=300,
            up_token_id="tok_up2",
            dn_token_id="tok_dn2",
            open_epoch=now + 60,  # opens in the future
            close_epoch=now + 360,
        )
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {pre_open.market_id: pre_open}
        bot.spot_prices["BTC"] = 84500.0

        asyncio.run(bot._snapshot_window_open_prices())

        assert pre_open.market_id not in bot.window_open_prices
        assert pre_open.market_id not in bot._snapped_windows


class TestSettlement:
    def test_settlement_marks_pending(self, cfg, market, mock_clob):
        """Settlement marks expired windows for async settlement instead of computing PnL."""
        bot = _make_bot(cfg, mock_clob)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.position_manager.update_position(
            market.market_id, Side.DOWN, qty=100.0, cost=49.0,
        )
        bot._active_markets = {market.market_id: market}
        bot._snapped_windows.add(market.market_id)

        # Call settlement after window close
        asyncio.run(bot._settle_expired_windows(now_epoch=1400))

        # Position is NOT removed — it is marked pending
        assert market.market_id in bot.position_manager.positions
        assert market.market_id in bot.position_manager.get_pending_settlements()
        # Bankroll unchanged
        assert bot.position_manager.bankroll == pytest.approx(10_000.0)
        # Snapped window cleaned up
        assert market.market_id not in bot._snapped_windows

    def test_settlement_skips_already_pending(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot._active_markets = {market.market_id: market}

        # Pre-mark as pending
        bot.position_manager.mark_pending_settlement(market.market_id)

        # Should not error or double-mark
        asyncio.run(bot._settle_expired_windows(now_epoch=1400))
        assert bot.position_manager.get_pending_settlements().count(market.market_id) == 1

    def test_settlement_skips_no_position(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {market.market_id: market}
        # No position — should be a no-op
        asyncio.run(bot._settle_expired_windows(now_epoch=1400))
        assert market.market_id not in bot.position_manager.get_pending_settlements()

    def test_expired_unfilled_logged(self, cfg, market, mock_clob, caplog):
        """Expired windows with no position emit EXPIRED_UNFILLED observability log."""
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {market.market_id: market}
        # No position — should log observability line
        with caplog.at_level("INFO", logger="polybot.bot"):
            asyncio.run(bot._settle_expired_windows(now_epoch=1400))
        assert any(
            "EXPIRED_UNFILLED" in rec.message and market.market_id in rec.message
            for rec in caplog.records
        )


class TestConnectionLost:
    def test_on_connection_lost_preserves_state(self, cfg, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        # Set up some state
        bot._cancel_only_mode = False
        # Add a mock ladder
        bot.ladder_manager.ladders["m1"] = MagicMock()
        bot.order_tracker.mark_all_unknown = MagicMock()

        bot._on_connection_lost()

        # Ladder should NOT be cleaned up — preserved for recovery
        assert "m1" in bot.ladder_manager.ladders
        bot.order_tracker.mark_all_unknown.assert_called_once()
        # cancel_only_mode should be True after connection loss
        assert bot._cancel_only_mode is True
        assert bot._cancel_only_reason == "connection_loss"


class TestFindMarket:
    def test_find_active_market(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {market.market_id: market}
        found = bot._find_market(market.market_id)
        assert found is market

    def test_find_cached_expired_market(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {}
        bot._expired_market_cache[market.market_id] = market
        found = bot._find_market(market.market_id)
        assert found is market

    def test_find_market_returns_none(self, cfg, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot._active_markets = {}
        assert bot._find_market("nonexistent") is None


class TestSettlementPollerTimeout:
    def test_settlement_timeout_marks_failed(self, cfg, market, mock_clob):
        """When close_epoch is far in the past, the poller should mark failed.

        We simulate the timeout logic inline (same as run_settlement_poller)
        rather than running the full async poller.
        """
        import time as _time

        bot = _make_bot(cfg, mock_clob)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot._active_markets = {market.market_id: market}
        bot._snapped_windows.add(market.market_id)

        # Settle the window to mark pending and cache it
        asyncio.run(bot._settle_expired_windows(now_epoch=1400))
        assert market.market_id in bot.position_manager.get_pending_settlements()
        assert market.market_id in bot._expired_market_cache

        # Use a config with instant timeout (0 seconds)
        cfg2 = BotConfig(
            dry_run=True,
            poll_interval_ms=100,
            ladder_rungs=4,
            ladder_spacing=0.02,
            ladder_width=0.06,
            ladder_size_skew=0.7,
            bot_settlement_give_up_sec=0.0,  # instant timeout
        )
        bot.cfg = cfg2

        # Simulate the timeout check from run_settlement_poller
        for mid in list(bot.position_manager.get_pending_settlements()):
            mkt = bot._find_market(mid)
            if mkt is None:
                continue
            now = _time.time()
            elapsed = now - mkt.close_epoch
            if elapsed > bot.cfg.bot_settlement_give_up_sec:
                bot.position_manager.mark_failed_settlement(mid)

        assert market.market_id not in bot.position_manager.get_pending_settlements()
        assert market.market_id in bot.position_manager.get_failed_settlements()


class TestSettlementCachesMarket:
    def test_settle_caches_expired_market(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot._active_markets = {market.market_id: market}
        bot._snapped_windows.add(market.market_id)

        asyncio.run(bot._settle_expired_windows(now_epoch=1400))

        assert market.market_id in bot._expired_market_cache
        assert bot._expired_market_cache[market.market_id] is market


class TestPositionLimit:
    def test_no_ladder_when_risk_halted(self, cfg, market, mock_clob):
        """RiskStub never halts, but if risk.is_halted() returns True, no ladder is posted."""
        bot = _make_bot(cfg, mock_clob)
        for i in range(8):
            bot.position_manager.update_position(f"m{i}", Side.UP, 100.0, 50.0)

        # Override risk stub to simulate halted state
        bot.risk.is_halted = lambda: True
        bot.ladder_manager.risk.is_halted = lambda: True

        count = bot.ladder_manager.post_ladder(market)
        assert count == 0


class TestSettlementDetail:
    def test_settle_two_sided_detail(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        bot.position_manager.update_position(market.market_id, Side.UP, qty=100.0, cost=43.0)
        bot.position_manager.update_position(market.market_id, Side.DOWN, qty=100.0, cost=48.0)
        bot._expired_market_cache[market.market_id] = market
        bot.position_manager.mark_pending_settlement(market.market_id)

        bot._settle_position(market.market_id, market, "UP")

        assert len(bot._activity_log) == 1
        detail = bot._activity_log[0].detail
        assert "UP won" in detail
        assert "\u2191" in detail  # up arrow
        assert "\u2193" in detail  # down arrow (losing side)
        assert "net" in detail

    def test_settle_one_sided_detail(self, cfg, market, mock_clob):
        bot = _make_bot(cfg, mock_clob)
        bot.redeemer = MagicMock()
        bot.position_manager.update_position(market.market_id, Side.UP, qty=100.0, cost=43.0)
        # No DOWN side
        bot._expired_market_cache[market.market_id] = market
        bot.position_manager.mark_pending_settlement(market.market_id)

        bot._settle_position(market.market_id, market, "UP")

        detail = bot._activity_log[0].detail
        assert "UP won" in detail
        assert "\u2193" not in detail  # no down arrow when no losing side


class TestConcurrentWindowOpenPrices:
    def test_concurrent_windows_independent_open_prices(self, cfg, mock_clob):
        """Two concurrent BTC windows must each keep their own open price."""
        import asyncio
        now = int(time.time())
        window_1h = MarketWindow(
            market_id="btc-1h-100",
            condition_id="0xaaa",
            asset="BTC",
            timeframe_sec=3600,
            up_token_id="tok_up_1h",
            dn_token_id="tok_dn_1h",
            open_epoch=now - 3000,
            close_epoch=now + 600,
        )
        window_5m = MarketWindow(
            market_id="btc-5m-200",
            condition_id="0xbbb",
            asset="BTC",
            timeframe_sec=300,
            up_token_id="tok_up_5m",
            dn_token_id="tok_dn_5m",
            open_epoch=now - 60,
            close_epoch=now + 240,
        )
        bot = _make_bot(cfg, mock_clob)

        # 1h window opens first at $87,000
        bot._active_markets = {window_1h.market_id: window_1h}
        bot.price_feed._update_price("BTC", Decimal("87000"))
        bot.spot_prices["BTC"] = 87000.0
        asyncio.run(bot._snapshot_window_open_prices())
        assert window_1h.market_id in bot.window_open_prices
        first_price = bot.window_open_prices[window_1h.market_id]

        # 5m window opens later — must NOT overwrite 1h
        bot._active_markets[window_5m.market_id] = window_5m
        bot.price_feed._update_price("BTC", Decimal("87500"))
        bot.spot_prices["BTC"] = 87500.0
        asyncio.run(bot._snapshot_window_open_prices())
        assert window_5m.market_id in bot.window_open_prices
        assert bot.window_open_prices[window_1h.market_id] == first_price  # PRESERVED

        # Deltas are independent — different open prices for each window
        delta_1h = bot.compute_spot_delta("BTC", window_1h.market_id)
        delta_5m = bot.compute_spot_delta("BTC", window_5m.market_id)
        assert delta_1h is not None
        assert delta_5m is not None


class TestDryRunResolve:
    def test_dry_run_resolve_uses_per_window_open_price(self, cfg, mock_clob):
        """_dry_run_resolve must use the window's own open price, not another window's."""
        now = int(time.time())
        window_1h = MarketWindow(
            market_id="btc-1h-100", condition_id="0xaaa", asset="BTC",
            timeframe_sec=3600, up_token_id="t1", dn_token_id="t2",
            open_epoch=now - 3600, close_epoch=now,
        )
        bot = _make_bot(cfg, mock_clob)
        bot.price_feed._update_price("BTC", Decimal("87500"))
        bot.window_open_prices[window_1h.market_id] = 87000.0
        bot.spot_prices["BTC"] = 87500.0  # price went UP from 87000

        outcome = bot._dry_run_resolve(window_1h)
        assert outcome == "UP"

        # Now if a different window had snapped at 88000, resolve should be DOWN
        window_5m = MarketWindow(
            market_id="btc-5m-200", condition_id="0xbbb", asset="BTC",
            timeframe_sec=300, up_token_id="t3", dn_token_id="t4",
            open_epoch=now - 300, close_epoch=now,
        )
        bot.window_open_prices[window_5m.market_id] = 88000.0
        # spot is 87500, open was 88000 => DOWN
        outcome_5m = bot._dry_run_resolve(window_5m)
        assert outcome_5m == "DOWN"
