"""Tests for the web dashboard state serializer."""
import time
from collections import deque
from unittest.mock import MagicMock

from polybot.config import BotConfig
from polybot.types import ActivityEvent, Position, MarketWindow, Side
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.order_tracker import OrderTracker
from polybot.web.server import build_state_snapshot


def _make_bot(cfg=None, bankroll=10000.0):
    """Create a minimal mock bot with real managers."""
    cfg = cfg or BotConfig()
    bot = MagicMock()
    bot.cfg = cfg
    bot._start_time = time.time() - 60
    bot._cancel_only_mode = False
    bot._trade_count = 5
    bot.spot_prices = {"BTC": 70000.0, "ETH": 2100.0}
    bot.window_open_prices = {"BTC": 69900.0, "ETH": 2095.0}
    bot.compute_spot_delta = lambda asset: (
        (bot.spot_prices.get(asset, 0) - bot.window_open_prices.get(asset, 0))
        / bot.window_open_prices.get(asset, 1)
    )
    bot.position_manager = PositionManager(cfg, bankroll=bankroll)
    bot.risk_manager = RiskManager(cfg, starting_bankroll=bankroll)
    bot.order_tracker = OrderTracker()
    bot.ladder_manager = MagicMock()
    bot.ladder_manager.ladders = {}
    bot.ladder_manager.total_committed.return_value = 0.0
    bot.heartbeat = MagicMock()
    bot.heartbeat.is_healthy.return_value = True
    bot.active_markets = []
    bot._activity_log = deque(maxlen=20)
    bot._wallet_balance = None
    bot._expired_market_cache = {}
    return bot


def test_snapshot_basic_fields():
    bot = _make_bot()
    snap = build_state_snapshot(bot)
    assert snap["mode"] == "dry_run"
    assert snap["bankroll"] == 10000.0
    assert snap["daily_pnl"] == 0.0
    assert snap["heartbeat_healthy"] is True
    assert snap["cancel_only_mode"] is False
    assert snap["risk_halted"] is False
    assert isinstance(snap["uptime_sec"], (int, float))
    assert snap["uptime_sec"] >= 60


def test_snapshot_spot_prices():
    bot = _make_bot()
    snap = build_state_snapshot(bot)
    assert "BTC" in snap["spots"]
    assert snap["spots"]["BTC"]["price"] == 70000.0
    assert abs(snap["spots"]["BTC"]["delta"] - 0.00143) < 0.001


def test_snapshot_positions():
    bot = _make_bot()
    bot.position_manager.update_position("m1", Side.UP, 50.0, 20.0)
    bot.position_manager.update_position("m1", Side.DOWN, 50.0, 22.0)
    bot.active_markets = [
        MarketWindow("m1", "0xcond", "BTC", 900, "up_tok", "dn_tok", 0, int(time.time()) + 300)
    ]
    snap = build_state_snapshot(bot)
    assert len(snap["positions"]) == 1
    pos = snap["positions"][0]
    assert pos["market_id"] == "m1"
    assert pos["asset"] == "BTC"
    assert pos["up_qty"] == 50.0
    assert pos["pnl_if_up"] is not None
    assert pos["pnl_if_down"] is not None
    assert pos["pnl_worst_case"] == min(pos["pnl_if_up"], pos["pnl_if_down"])


def test_snapshot_activity():
    bot = _make_bot()
    bot._activity_log.append(ActivityEvent(
        timestamp=time.time(), event_type="FILL",
        asset="BTC", detail="test fill", pnl=10.5,
    ))
    snap = build_state_snapshot(bot)
    assert len(snap["activity"]) == 1
    assert snap["activity"][0]["type"] == "FILL"
    assert snap["activity"][0]["pnl"] == 10.5


def test_snapshot_ladders_with_time_left():
    bot = _make_bot()
    now = int(time.time())
    market = MarketWindow("m1", "0xcond", "BTC", 900, "up", "dn", now - 300, now + 600)
    bot.active_markets = [market]
    bot.ladder_manager.ladders = {"m1": MagicMock(asset="BTC")}
    bot.ladder_manager.get_ladder_stats.return_value = {
        "up_resting": 5, "dn_resting": 6,
        "up_filled": 20.0, "dn_filled": 18.0,
        "up_vwap": 0.40, "dn_vwap": 0.45,
        "combined_vwap": 0.85, "imbalance": 0.10,
    }
    snap = build_state_snapshot(bot)
    assert len(snap["ladders"]) == 1
    lad = snap["ladders"][0]
    assert lad["asset"] == "BTC"
    assert lad["pair_cost"] == 0.85
    assert lad["time_left_sec"] > 500
