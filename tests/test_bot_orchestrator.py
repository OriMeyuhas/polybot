"""Tests for the rebuilt Bot orchestrator (polybot.bot.Bot)."""

import asyncio
import time
from unittest.mock import patch

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.types import MarketWindow, Position

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
    # Wallet should be set (address or placeholder) and must not contain the private key
    assert state["wallet"] is not None
    assert _FAKE_LIVE_KEY not in str(state["wallet"])


def test_state_snapshot_has_all_24_fields():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    expected_keys = {
        "mode", "running", "connected", "heartbeat_healthy", "cancel_only_mode",
        "total_pnl", "realized_pnl", "unrealized_pnl",
        "trade_count", "position_count", "pairs_completed",
        "avg_pair_cost", "best_pair_cost", "imbalance_ratio", "runtime_sec",
        "markets_active", "win_rate", "settled_wins", "settled_losses",
        "consecutive_losses", "exposure_factor", "daily_pnl", "is_halted", "capital_at_risk_pct",
        "prices", "binance_prices", "spots", "binance_spot_values",
        "active_markets", "activity_feed", "trades",
        "pending_settlements", "wallet",
        "usdc_balance", "price_feed_stale",
        "per_asset_pnl", "per_asset_pairs",
        "settlement_history", "pnl_series",
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
    bot.window_open_prices["btc-5m-100"] = 50000.0
    bot.spot_prices["BTC"] = 51000.0
    delta = bot.compute_spot_delta("BTC", "btc-5m-100")
    assert abs(delta - 0.02) < 1e-9

    # No open price => 0
    assert bot.compute_spot_delta("ETH", "eth-5m-100") == 0.0


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


def test_live_wallet_does_not_leak_key():
    cfg = BotConfig(dry_run=False, private_key=_FAKE_LIVE_KEY)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    # Wallet should be set and must NOT contain the private key
    wallet = state["wallet"]
    assert wallet is not None, "Wallet should be derived when private_key is set"
    assert _FAKE_LIVE_KEY not in str(wallet), "Private key leaked in wallet field"


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


def test_snapshot_market_has_resting_and_budget_fields():
    """Each market in active_markets has resting_up, resting_dn (int) and budget (float)."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    bot = Bot(cfg)
    now = time.time()
    mkt = MarketWindow(
        market_id="btc-5m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=int(now) - 200,
        close_epoch=int(now) + 100,
    )
    bot._active_markets = {mkt.market_id: mkt}
    state = bot.build_state_snapshot()
    assert len(state["active_markets"]) == 1
    entry = state["active_markets"][0]
    assert "resting_up" in entry
    assert "resting_dn" in entry
    assert "budget" in entry
    assert isinstance(entry["resting_up"], int)
    assert isinstance(entry["resting_dn"], int)
    assert isinstance(entry["budget"], float)


def test_snapshot_position_has_profit_projections():
    """Position dict includes profit_if_up and profit_if_down matching dataclass methods."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    bot = Bot(cfg)
    now = time.time()
    mkt = MarketWindow(
        market_id="btc-15m-200",
        condition_id="0xdef",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up2",
        dn_token_id="tok_dn2",
        open_epoch=int(now) - 500,
        close_epoch=int(now) + 400,
    )
    bot._active_markets = {mkt.market_id: mkt}
    pos = Position(
        market_id=mkt.market_id,
        up_qty=10,
        up_cost=4.5,
        dn_qty=10,
        dn_cost=4.0,
    )
    bot.position_manager.positions[mkt.market_id] = pos
    state = bot.build_state_snapshot()
    assert len(state["active_markets"]) == 1
    entry = state["active_markets"][0]
    assert entry["position"] is not None
    p = entry["position"]
    assert "profit_if_up" in p
    assert "profit_if_down" in p
    assert abs(p["profit_if_up"] - pos.profit_if_up()) < 1e-9
    assert abs(p["profit_if_down"] - pos.profit_if_down()) < 1e-9
