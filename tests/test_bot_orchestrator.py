"""Tests for the rebuilt Bot orchestrator (polybot.bot.Bot)."""

import asyncio
from unittest.mock import patch

from polybot.config import BotConfig
from polybot.bot import Bot

# A valid 32-byte hex private key for live mode tests (not a real key).
_FAKE_LIVE_KEY = "0x" + "ab" * 32


def test_bot_creation():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    assert bot.running is False
    assert bot.mode == "dry_run"


def test_bot_state_snapshot():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    assert state["mode"] == "dry_run"
    assert state["running"] is False
    assert "total_pnl" in state
    assert "active_markets" in state
    assert "prices" in state
    assert "binance_prices" in state
    assert "spots" in state
    assert "activity_feed" in state
    assert "pending_settlements" in state
    assert "wallet" in state


def test_bot_live_mode():
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY)
    bot = Bot(cfg)
    assert bot.mode == "live"
    state = bot.build_state_snapshot()
    assert state["mode"] == "live"
    assert state["wallet"] is not None


def test_state_snapshot_has_all_23_fields():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    expected_keys = {
        "mode", "running", "heartbeat_healthy", "cancel_only_mode",
        "total_pnl", "realized_pnl", "unrealized_pnl",
        "trade_count", "position_count", "pairs_completed",
        "avg_pair_cost", "imbalance_ratio", "runtime_sec",
        "markets_active", "win_rate",
        "prices", "binance_prices", "spots",
        "active_markets", "activity_feed", "trades",
        "pending_settlements", "wallet",
    }
    assert expected_keys == set(state.keys()), f"Missing: {expected_keys - set(state.keys())}, Extra: {set(state.keys()) - expected_keys}"


def test_bot_cancel_only_mode_default():
    cfg = BotConfig(dry_run=True, start_paused=True)
    bot = Bot(cfg)
    assert bot._cancel_only_mode is True

    cfg2 = BotConfig(dry_run=True, start_paused=False)
    bot2 = Bot(cfg2)
    assert bot2._cancel_only_mode is False


def test_ui_start_stop():
    cfg = BotConfig(dry_run=True, start_paused=True)
    bot = Bot(cfg)
    assert bot._cancel_only_mode is True

    asyncio.run(bot.ui_start())
    assert bot._cancel_only_mode is False

    asyncio.run(bot.ui_stop())
    assert bot._cancel_only_mode is True
    assert bot._pending_cancel_all is True


def test_compute_spot_delta():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    bot.window_open_prices["BTC"] = 50000.0
    bot.spot_prices["BTC"] = 51000.0
    delta = bot.compute_spot_delta("BTC")
    assert abs(delta - 0.02) < 1e-9

    # No open price => 0
    assert bot.compute_spot_delta("ETH") == 0.0


def test_record_activity():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    bot._record_activity("FILL", "BTC", "test fill", pnl=1.5)
    assert len(bot._activity_log) == 1
    event = bot._activity_log[0]
    assert event.event_type == "FILL"
    assert event.asset == "BTC"
    assert event.pnl == 1.5


def test_dry_run_wallet_is_none():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    assert state["wallet"] is None


def test_live_wallet_truncated():
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    # First 10 chars of the key + "..."
    assert state["wallet"] == _FAKE_LIVE_KEY[:10] + "..."


def test_subsystems_created():
    """Verify all subsystems are instantiated in the constructor."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    assert bot.price_feed is not None
    assert bot.book_manager is not None
    assert bot.market_ws is not None
    assert bot.midpoint_poller is not None
    assert bot.clob_client is not None
    assert bot.order_executor is not None
    assert bot.heartbeat is not None
    assert bot.order_tracker is not None
    assert bot.position_manager is not None
    assert bot.risk is not None
    assert bot.tick_cache is not None
    assert bot.ladder_manager is not None
    assert bot.gui_state is not None
    assert bot.redeemer is not None
