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
    def test_settlement_updates_bankroll(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        # Simulate a spread position: bought both sides below $1
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.position_manager.update_position(
            market.market_id, Side.DOWN, qty=100.0, cost=49.0,
        )
        # Set spot delta positive -> UP wins
        bot.spot_prices["BTC"] = 85200.0
        bot.window_open_prices["BTC"] = 85000.0

        pos = bot.position_manager.positions[market.market_id]
        pnl_up = pos.profit_if_up()  # 100*(1-0.48) - 49 = 52 - 49 = 3
        assert pnl_up == pytest.approx(3.0)

        # Manually call settlement
        bot.active_markets = [market]
        # Set now to after window close
        bot._settle_expired_windows(now_epoch=1400)

        assert bot.position_manager.bankroll == pytest.approx(10_003.0)
        assert bot.risk_manager.daily_pnl == pytest.approx(3.0)
        assert market.market_id not in bot.position_manager.positions


class TestStopLoss:
    def test_stop_loss_is_noop(self, cfg, market, mock_clob):
        """Stop-loss was removed; _check_stop_losses is now a no-op."""
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=45.0,
        )
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84800.0
        bot.window_open_prices["BTC"] = 85000.0

        bot._check_stop_losses(now_epoch=1100)

        # Position should still be there (no-op)
        assert market.market_id in bot.position_manager.positions


class TestPositionLimit:
    def test_no_ladder_when_at_limit(self, cfg, market, mock_clob):
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        for i in range(8):
            bot.position_manager.update_position(f"m{i}", Side.UP, 100.0, 50.0)

        count = bot.ladder_manager.post_ladder(market)
        assert count == 0
