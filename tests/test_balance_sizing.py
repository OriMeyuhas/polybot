import logging
from polybot.config import BotConfig
from polybot.strategy.position_manager import PositionManager


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
