import pytest
from polybot.types import Side, StrategyType, MarketWindow, Position
from polybot.signal_engine import SignalEngine
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig()


@pytest.fixture
def engine(cfg):
    return SignalEngine(cfg)


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


class TestDirectionalOpportunity:
    def test_no_signal_when_too_early(self, engine, market):
        spot_delta = 0.005
        best_asks = {"UP": 0.80, "DOWN": 0.30}
        now = 1400
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_when_move_too_small(self, engine, market):
        spot_delta = 0.001
        best_asks = {"UP": 0.80, "DOWN": 0.30}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_signal_up_when_positive_delta(self, engine, market):
        spot_delta = 0.003
        best_asks = {"UP": 0.85, "DOWN": 0.25}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is not None
        assert result.side == Side.UP
        assert result.price == 0.85
        assert result.strategy == StrategyType.DIRECTIONAL

    def test_signal_down_when_negative_delta(self, engine, market):
        spot_delta = -0.004
        best_asks = {"UP": 0.20, "DOWN": 0.88}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is not None
        assert result.side == Side.DOWN
        assert result.price == 0.88

    def test_no_signal_when_price_too_high(self, engine, market):
        spot_delta = 0.005
        best_asks = {"UP": 0.95, "DOWN": 0.10}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_when_price_too_low(self, engine, market):
        spot_delta = 0.003
        best_asks = {"UP": 0.05, "DOWN": 0.96}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_in_final_seconds(self, engine, market):
        spot_delta = 0.005
        best_asks = {"UP": 0.85, "DOWN": 0.25}
        now = 1850
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None


class TestSpreadOpportunity:
    def test_spread_detected(self, engine, market):
        best_asks = {"UP": 0.48, "DOWN": 0.49}
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is not None
        assert result.strategy == StrategyType.SPREAD
        assert result.up_price == 0.48
        assert result.dn_price == 0.49
        assert result.edge == pytest.approx(0.03)

    def test_no_spread_when_sum_too_high(self, engine, market):
        best_asks = {"UP": 0.51, "DOWN": 0.50}
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is None

    def test_no_spread_when_edge_below_minimum(self, engine, market):
        best_asks = {"UP": 0.49, "DOWN": 0.50}
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is None

    def test_no_spread_in_final_seconds(self, engine, market):
        best_asks = {"UP": 0.45, "DOWN": 0.45}
        result = engine.check_spread(market, best_asks, now_epoch=1860)
        assert result is None

    def test_no_spread_before_min_elapsed(self, engine, market):
        """Spread blocked before spread_min_elapsed_pct of window (10% of 900s = 90s)."""
        best_asks = {"UP": 0.45, "DOWN": 0.45}
        # 50s into 900s = 5.6% < 10%
        result = engine.check_spread(market, best_asks, now_epoch=1050)
        assert result is None

    def test_spread_allowed_after_min_elapsed(self, engine, market):
        """Spread allowed after 10% of window elapsed."""
        best_asks = {"UP": 0.45, "DOWN": 0.45}
        # 100s into 900s = 11.1% > 10%
        result = engine.check_spread(market, best_asks, now_epoch=1100)
        assert result is not None

    def test_spread_on_5m_window(self, engine):
        """Spread works on 5-minute windows with early entry."""
        market_5m = MarketWindow(
            market_id="btc-updown-5m-100",
            condition_id="0xabc",
            asset="BTC",
            timeframe_sec=300,
            up_token_id="tok_up",
            dn_token_id="tok_dn",
            open_epoch=1000,
            close_epoch=1300,
        )
        best_asks = {"UP": 0.45, "DOWN": 0.45}
        # 40s into 300s = 13.3% > 10%
        result = engine.check_spread(market_5m, best_asks, now_epoch=1040)
        assert result is not None
        assert result.edge == pytest.approx(0.10)


class TestEarlyExit:
    def test_exit_when_up_appreciated(self, engine, market):
        """Should signal exit UP when UP price rose 50%+ above cost."""
        pos = Position(
            market_id=market.market_id,
            up_qty=100.0, up_cost=40.0,   # avg 0.40
            dn_qty=100.0, dn_cost=45.0,   # avg 0.45
        )
        # UP ask is now 0.65 -> gain = (0.65 - 0.40)/0.40 = 62.5%
        best_asks = {"UP": 0.65, "DOWN": 0.35}
        result = engine.check_early_exit(market, pos, best_asks, now_epoch=1200)
        assert result == Side.UP

    def test_exit_when_dn_appreciated(self, engine, market):
        """Should signal exit DOWN when DOWN price rose 50%+ above cost."""
        pos = Position(
            market_id=market.market_id,
            up_qty=100.0, up_cost=45.0,   # avg 0.45
            dn_qty=100.0, dn_cost=30.0,   # avg 0.30
        )
        # DN ask is now 0.50 -> gain = (0.50 - 0.30)/0.30 = 66.7%
        best_asks = {"UP": 0.40, "DOWN": 0.50}
        result = engine.check_early_exit(market, pos, best_asks, now_epoch=1200)
        assert result == Side.DOWN

    def test_no_exit_when_gain_too_small(self, engine, market):
        """No exit when neither side appreciated enough."""
        pos = Position(
            market_id=market.market_id,
            up_qty=100.0, up_cost=45.0,   # avg 0.45
            dn_qty=100.0, dn_cost=45.0,   # avg 0.45
        )
        # UP ask is 0.50 -> gain = (0.50-0.45)/0.45 = 11% < 50%
        best_asks = {"UP": 0.50, "DOWN": 0.50}
        result = engine.check_early_exit(market, pos, best_asks, now_epoch=1200)
        assert result is None

    def test_no_exit_for_directional_position(self, engine, market):
        """Early exit only applies to spread positions (both sides)."""
        pos = Position(
            market_id=market.market_id,
            up_qty=100.0, up_cost=40.0,
            dn_qty=0.0, dn_cost=0.0,
        )
        best_asks = {"UP": 0.80, "DOWN": 0.20}
        result = engine.check_early_exit(market, pos, best_asks, now_epoch=1200)
        assert result is None
