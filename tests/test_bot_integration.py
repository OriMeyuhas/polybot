import pytest
from unittest.mock import MagicMock
from polybot.bot import Bot, compute_fee
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
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-updown-15m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )


class TestFeeComputation:
    def test_fee_at_midprice(self):
        # fee = 0.02 * min(0.50, 0.50) = 0.01
        assert compute_fee(0.50) == pytest.approx(0.01)

    def test_fee_at_high_price(self):
        # fee = 0.02 * min(0.90, 0.10) = 0.002
        assert compute_fee(0.90) == pytest.approx(0.002)

    def test_fee_at_low_price(self):
        # fee = 0.02 * min(0.10, 0.90) = 0.002
        assert compute_fee(0.10) == pytest.approx(0.002)


class TestBotEvaluateMarket:
    def test_directional_trade_executed(self, cfg, market):
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "matched"}
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.10", size="5000")],
            asks=[MagicMock(price="0.85", size="5000")],
        )

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 84800.0  # +0.24% delta

        actions = bot.evaluate_market(market, now_epoch=1600)
        # Price 0.85 -> edge = 0.15, fee = 0.02*0.15 = 0.003, net = 0.147 > 0
        assert len(actions) >= 1
        assert actions[0]["type"] == "directional"
        assert actions[0]["side"] == Side.UP

    def test_no_trade_when_halted(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.risk_manager.update_pnl(-600.0)  # trigger circuit breaker

        actions = bot.evaluate_market(market, now_epoch=1600)
        assert len(actions) == 0

    def test_spread_trade_when_no_directional(self, cfg, market):
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "matched"}

        book_up = MagicMock(
            bids=[MagicMock(price="0.44", size="1000")],
            asks=[MagicMock(price="0.46", size="1000")],
        )
        book_dn = MagicMock(
            bids=[MagicMock(price="0.46", size="1000")],
            asks=[MagicMock(price="0.48", size="1000")],
        )
        mock_clob.get_order_book.side_effect = lambda tid: (
            book_up if tid == "tok_up" else book_dn
        )

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 85000.0  # 0% delta — no directional

        actions = bot.evaluate_market(market, now_epoch=1200)
        # T = 0.46 + 0.48 = 0.94, edge = 0.06, fee worst = 0.01, net = 0.05 > 0
        assert any(a["type"] == "spread" for a in actions)

    def test_position_limit_respected(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        for i in range(8):
            bot.position_manager.update_position(f"m{i}", Side.UP, 100.0, 50.0)

        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 84800.0

        actions = bot.evaluate_market(market, now_epoch=1600)
        assert len(actions) == 0


class TestWindowOpenPriceSnapshot:
    def test_snapshot_captures_spot_price(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()

        assert bot.window_open_prices["BTC"] == 84500.0
        assert market.market_id in bot._snapped_windows

    def test_snapshot_not_overwritten_on_second_call(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()
        bot.spot_prices["BTC"] = 85000.0  # price changed
        bot._snapshot_window_open_prices()

        # Should still be the original snapshot
        assert bot.window_open_prices["BTC"] == 84500.0


class TestEarlyExitIntegration:
    def test_early_exit_books_profit(self, cfg, market):
        mock_clob = MagicMock()

        # UP price appreciated: ask was 0.40, now 0.65
        book_up = MagicMock(
            bids=[MagicMock(price="0.63", size="1000")],
            asks=[MagicMock(price="0.65", size="1000")],
        )
        book_dn = MagicMock(
            bids=[MagicMock(price="0.33", size="1000")],
            asks=[MagicMock(price="0.35", size="1000")],
        )
        mock_clob.get_order_book.side_effect = lambda tid: (
            book_up if tid == "tok_up" else book_dn
        )

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 85000.0

        # Simulate existing spread position bought at 0.40 UP + 0.45 DN
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=40.0,
        )
        bot.position_manager.update_position(
            market.market_id, Side.DOWN, qty=100.0, cost=45.0,
        )

        actions = bot.evaluate_market(market, now_epoch=1200)
        assert len(actions) == 1
        assert actions[0]["type"] == "early_exit"
        assert actions[0]["exit_side"] == Side.UP
        # PnL = 100 * 0.65 - 40 = 25
        assert actions[0]["pnl"] == pytest.approx(25.0)
        # Position should be removed
        assert market.market_id not in bot.position_manager.positions
        # Bankroll should be updated
        assert bot.position_manager.bankroll == pytest.approx(10_025.0)


class TestSettlement:
    def test_settlement_updates_bankroll(self, cfg, market):
        mock_clob = MagicMock()
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

        # After settlement, bankroll should increase
        initial = bot.position_manager.bankroll
        # Manually call the settlement logic (extracted from run_trading_loop)
        bot.active_markets = [market]
        spot_delta = bot.compute_spot_delta(market.asset)
        assert spot_delta > 0
        pnl = pos.profit_if_up()
        bot.position_manager.update_bankroll(initial + pnl)
        bot.risk_manager.update_pnl(pnl)
        bot.position_manager.remove_position(market.market_id)

        assert bot.position_manager.bankroll == pytest.approx(10_003.0)
        assert bot.risk_manager.daily_pnl == pytest.approx(3.0)
