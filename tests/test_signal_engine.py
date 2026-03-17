import pytest
from polybot.types import Side, StrategyType, MarketWindow
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
