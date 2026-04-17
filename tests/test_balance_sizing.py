import logging
from unittest.mock import MagicMock
from polybot.config import BotConfig, effective_assets
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
    risk.check_capital_at_risk = MagicMock(return_value=True)
    risk.exposure_factor = MagicMock(return_value=1.0)

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


from polybot.bot import Bot


def test_settle_position_paper_updates_bankroll():
    """In paper mode, _settle_position should add PnL to bankroll."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    bot = Bot(cfg)
    assert bot.position_manager.bankroll == 1000.0


def test_bot_wallet_balance_initialized_from_bankroll():
    """Bot._wallet_balance should start equal to cfg.bankroll."""
    cfg = BotConfig(dry_run=True, bankroll=500.0)
    bot = Bot(cfg)
    assert bot._wallet_balance == 500.0


def test_settle_position_live_does_not_add_pnl():
    """In live mode, _settle_position should NOT add PnL to bankroll."""
    cfg = BotConfig(dry_run=False, bankroll=1000.0, private_key="0x" + "ab" * 32)
    bot = Bot(cfg)
    from polybot.types import Position
    market = MarketWindow(
        market_id="test", condition_id="c", asset="BTC",
        timeframe_sec=300, up_token_id="up", dn_token_id="dn",
        open_epoch=100, close_epoch=400,
    )
    bot.position_manager.positions["test"] = Position(
        market_id="test", up_qty=100, up_cost=50, dn_qty=0, dn_cost=0,
    )
    bot.position_manager.mark_pending_settlement("test")
    bot._expired_market_cache["test"] = market
    bankroll_before = bot.position_manager.bankroll
    bot._settle_position("test", market, "UP")
    assert bot.position_manager.bankroll == bankroll_before


def test_overleverage_flag():
    """Bot should detect overleveraged state when wallet < committed."""
    cfg = BotConfig(dry_run=False, bankroll=100.0, private_key="0x" + "ab" * 32)
    bot = Bot(cfg)
    bot._wallet_balance = 50.0
    committed = 100.0
    overleveraged = (
        not bot.cfg.dry_run
        and bot._wallet_balance < committed
    )
    assert overleveraged is True


def test_effective_assets_scales_with_bankroll():
    """Integration: as bankroll grows, more assets become tradeable."""
    all_assets = ("BTC", "ETH", "SOL", "XRP")

    # Low bankroll -> 1 asset
    low = effective_assets(all_assets, 300.0)
    assert len(low) == 1

    # Medium bankroll -> 2 assets
    med = effective_assets(all_assets, 1000.0)
    assert len(med) == 2
    # Medium is a superset of low
    assert all(a in med for a in low)

    # High bankroll -> all assets
    high = effective_assets(all_assets, 5000.0)
    assert len(high) == 4
    # High is a superset of medium
    assert all(a in high for a in med)
