import logging
from unittest.mock import MagicMock
from polybot.config import BotConfig
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.ladder_manager import LadderManager, MIN_ORDER_SIZE
from polybot.types import MarketWindow


def test_update_bankroll_logs_change(caplog):
    """update_bankroll should log old -> new when values differ."""
    cfg = BotConfig(dry_run=True)
    pm = PositionManager(cfg, bankroll=1000.0)

    with caplog.at_level(logging.INFO, logger="polybot.strategy.position_manager"):
        pm.update_bankroll(1500.0)

    assert pm.bankroll == 1500.0
    assert "1000" in caplog.text or "1500" in caplog.text


def test_update_bankroll_no_log_if_same(caplog):
    """No log if bankroll hasn't changed."""
    cfg = BotConfig(dry_run=True)
    pm = PositionManager(cfg, bankroll=1000.0)

    with caplog.at_level(logging.INFO, logger="polybot.strategy.position_manager"):
        pm.update_bankroll(1000.0)

    assert "bankroll" not in caplog.text.lower() or caplog.text == ""


def _make_ladder_manager(bankroll=1000.0):
    """Create a LadderManager with mocked dependencies."""
    cfg = BotConfig(dry_run=True, bankroll=bankroll)
    executor = MagicMock()
    executor.get_best_ask = MagicMock(return_value=0.50)
    executor.place_batch_limit_buys = MagicMock(return_value=[])
    tracker = MagicMock()
    tracker.get_resting = MagicMock(return_value=[])
    pm = PositionManager(cfg, bankroll=bankroll)
    risk = MagicMock()
    risk.is_halted = MagicMock(return_value=False)
    risk.can_open_position = MagicMock(return_value=True)

    return LadderManager(cfg, executor, tracker, pm, risk)


def test_min_capital_guard_skips_when_broke():
    """post_ladder returns 0 when available capital is below minimum."""
    lm = _make_ladder_manager(bankroll=5.0)
    market = MarketWindow(
        market_id="btc-updown-5m-123", condition_id="c", asset="BTC",
        timeframe_sec=300, up_token_id="up", dn_token_id="dn",
        open_epoch=100, close_epoch=400,
    )
    count = lm.post_ladder(market)
    assert count == 0
