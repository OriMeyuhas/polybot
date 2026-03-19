import pytest
from unittest.mock import MagicMock
from polybot.bot import Bot
from polybot.config import BotConfig
from polybot.types import MarketWindow, Side, Position


@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        poll_interval_ms=100,
        ladder_rungs=4,
        ladder_spacing=0.02,
        ladder_width=0.06,
        ladder_size_skew=1.5,
        start_paused=False,
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


class TestBotInitialization:
    def test_bot_has_ladder_manager(self, cfg, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=1000.0)
        assert bot.ladder_manager is not None
        assert bot.order_tracker is not None

    def test_bot_start_time(self, cfg, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=1000.0)
        assert bot._start_time > 0


class TestWindowOpenPriceSnapshot:
    def test_snapshot_captures_spot_price(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()

        assert bot.window_open_prices["BTC"] == 84500.0
        assert market.market_id in bot._snapped_windows

    def test_snapshot_not_overwritten_on_second_call(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()
        bot.spot_prices["BTC"] = 85000.0
        bot._snapshot_window_open_prices()

        assert bot.window_open_prices["BTC"] == 84500.0


class TestSettlement:
    def test_settlement_marks_pending(self, cfg, market, mock_clob):
        """Settlement marks expired windows for async settlement instead of computing PnL."""
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.position_manager.update_position(
            market.market_id, Side.DOWN, qty=100.0, cost=49.0,
        )
        bot.active_markets = [market]
        bot._snapped_windows.add(market.market_id)

        # Call settlement after window close
        bot._settle_expired_windows(now_epoch=1400)

        # Position is NOT removed — it is marked pending
        assert market.market_id in bot.position_manager.positions
        assert market.market_id in bot.position_manager.get_pending_settlements()
        # Bankroll unchanged
        assert bot.position_manager.bankroll == pytest.approx(10_000.0)
        # Snapped window cleaned up
        assert market.market_id not in bot._snapped_windows

    def test_settlement_skips_already_pending(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.active_markets = [market]

        # Pre-mark as pending
        bot.position_manager.mark_pending_settlement(market.market_id)

        # Should not error or double-mark
        bot._settle_expired_windows(now_epoch=1400)
        assert bot.position_manager.get_pending_settlements().count(market.market_id) == 1

    def test_settlement_skips_no_position(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        # No position — should be a no-op
        bot._settle_expired_windows(now_epoch=1400)
        assert market.market_id not in bot.position_manager.get_pending_settlements()


class TestConnectionLost:
    def test_on_connection_lost_resets_state(self, cfg, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        # Set up some state
        bot._cancel_only_mode = True
        # Add a mock ladder
        bot.ladder_manager.ladders["m1"] = MagicMock()
        bot.ladder_manager.cleanup_ladder = MagicMock()
        bot.order_tracker.mark_all_unknown = MagicMock()

        bot._on_connection_lost()

        bot.ladder_manager.cleanup_ladder.assert_called_once_with("m1")
        bot.order_tracker.mark_all_unknown.assert_called_once()
        # _cancel_only_mode is preserved (user's stop intent not overridden)
        assert bot._cancel_only_mode is True


class TestFindMarket:
    def test_find_active_market(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        found = bot._find_market(market.market_id)
        assert found is market

    def test_find_cached_expired_market(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = []
        bot._expired_market_cache[market.market_id] = market
        found = bot._find_market(market.market_id)
        assert found is market

    def test_find_market_returns_none(self, cfg, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = []
        assert bot._find_market("nonexistent") is None


class TestSettlementPollerTimeout:
    def test_settlement_timeout_marks_failed(self, cfg, market, mock_clob):
        """When close_epoch is far in the past, the poller should mark failed.

        We simulate the timeout logic inline (same as run_settlement_poller)
        rather than running the full async poller.
        """
        import time as _time

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.active_markets = [market]
        bot._snapped_windows.add(market.market_id)

        # Settle the window to mark pending and cache it
        bot._settle_expired_windows(now_epoch=1400)
        assert market.market_id in bot.position_manager.get_pending_settlements()
        assert market.market_id in bot._expired_market_cache

        # Use a config with instant timeout (0 seconds)
        cfg2 = BotConfig(
            private_key="0xfake",
            api_key="key",
            api_secret="secret",
            api_passphrase="pass",
            poll_interval_ms=100,
            ladder_rungs=4,
            ladder_spacing=0.02,
            ladder_width=0.06,
            ladder_size_skew=1.5,
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
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.active_markets = [market]
        bot._snapped_windows.add(market.market_id)

        bot._settle_expired_windows(now_epoch=1400)

        assert market.market_id in bot._expired_market_cache
        assert bot._expired_market_cache[market.market_id] is market


class TestPositionLimit:
    def test_no_ladder_when_at_limit(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        for i in range(8):
            bot.position_manager.update_position(f"m{i}", Side.UP, 100.0, 50.0)

        count = bot.ladder_manager.post_ladder(market)
        assert count == 0
