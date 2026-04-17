"""Integration tests for registry pruning in the Bot orchestrator."""

import time

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.types import MarketWindow


def _make_market_window(market_id: str, asset: str = "BTC") -> MarketWindow:
    """Create a minimal MarketWindow for testing."""
    now = int(time.time())
    return MarketWindow(
        market_id=market_id,
        condition_id=f"cond_{market_id}",
        asset=asset,
        up_token_id=f"up_{market_id}",
        dn_token_id=f"dn_{market_id}",
        open_epoch=now - 300,
        close_epoch=now + 300,
        timeframe_sec=600,
    )


def test_expired_market_cache_pruned_without_position():
    """Expired market cache entries with no position/pending settlement are removed."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)

    mw = _make_market_window("mkt_old")
    bot._expired_market_cache["mkt_old"] = mw

    # No position and not pending settlement — should be pruned
    bot._cleanup_expired_windows(int(time.time()))

    assert "mkt_old" not in bot._expired_market_cache


def test_expired_market_cache_preserved_with_pending_settlement():
    """Expired market cache entries with pending settlement are preserved."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)

    mw = _make_market_window("mkt_pending")
    bot._expired_market_cache["mkt_pending"] = mw
    bot.position_manager.mark_pending_settlement("mkt_pending")

    bot._cleanup_expired_windows(int(time.time()))

    assert "mkt_pending" in bot._expired_market_cache
