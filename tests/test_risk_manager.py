import pytest
from polybot.risk_manager import RiskManager
from polybot.config import BotConfig
from polybot.types import MarketWindow


@pytest.fixture
def cfg():
    return BotConfig(
        max_concurrent_positions=8,
        max_daily_drawdown_pct=0.05,
        no_trade_final_sec=60,
    )


@pytest.fixture
def rm(cfg):
    return RiskManager(cfg, starting_bankroll=10_000.0)


@pytest.fixture
def market():
    return MarketWindow(
        market_id="m1", condition_id="0x", asset="BTC",
        timeframe_sec=900, up_token_id="u", dn_token_id="d",
        open_epoch=1000, close_epoch=1900,
    )


class TestPositionLimits:
    def test_allows_when_below_limit(self, rm):
        assert rm.can_open_position(current_count=7) is True

    def test_blocks_when_at_limit(self, rm):
        assert rm.can_open_position(current_count=8) is False


class TestDrawdownCircuitBreaker:
    def test_not_halted_initially(self, rm):
        assert rm.is_halted() is False

    def test_halted_after_drawdown(self, rm):
        rm.update_pnl(-600.0)
        assert rm.is_halted() is True

    def test_not_halted_with_small_loss(self, rm):
        rm.update_pnl(-400.0)
        assert rm.is_halted() is False

    def test_pnl_accumulates(self, rm):
        rm.update_pnl(-300.0)
        rm.update_pnl(-300.0)
        assert rm.is_halted() is True


class TestWindowTiming:
    def test_allows_trade_in_valid_window(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=1600) is True

    def test_blocks_trade_in_final_seconds(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=1860) is False

    def test_blocks_trade_after_close(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=2000) is False


class TestDailyReset:
    def test_reset_clears_daily_pnl(self, rm):
        rm.update_pnl(-600.0)
        assert rm.is_halted() is True
        rm.reset_daily()
        assert rm.is_halted() is False
        assert rm.daily_pnl == 0.0
