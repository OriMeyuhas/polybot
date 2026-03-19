"""Tests for the web dashboard state serializer."""
import time
from collections import deque
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient, ASGITransport

from polybot.config import BotConfig
from polybot.types import ActivityEvent, Position, MarketWindow, Side
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.order_tracker import OrderTracker
from polybot.web.server import build_state_snapshot, create_app


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
        "pair_cost": 0.85, "imbalance": 0.10,
        "ask_up": 0.43, "ask_dn": 0.48,
        "up_filled_count": 3, "dn_filled_count": 2,
        "up_total_rungs": 8, "dn_total_rungs": 8,
    }
    snap = build_state_snapshot(bot)
    assert len(snap["ladders"]) == 1
    lad = snap["ladders"][0]
    assert lad["asset"] == "BTC"
    assert lad["pair_cost"] == 0.85
    assert lad["time_left_sec"] > 500


@pytest.mark.asyncio
async def test_api_state_endpoint():
    bot = _make_bot()
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/state")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "dry_run"
        assert "spots" in data
        assert "ladders" in data


@pytest.mark.asyncio
async def test_api_balance_endpoint():
    bot = _make_bot()
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/balance")
        assert resp.status_code == 200
        data = resp.json()
        assert "usdc_balance" in data
        assert "on_orders" in data


@pytest.mark.asyncio
async def test_index_serves_html():
    bot = _make_bot()
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]


def test_snapshot_wallet_split():
    bot = _make_bot()
    bot.position_manager.update_position("m1", Side.UP, 50.0, 20.0)
    bot.ladder_manager.total_committed.return_value = 30.0
    snap = build_state_snapshot(bot)
    w = snap["wallet"]
    assert "on_orders" in w
    assert "in_positions" in w
    assert w["in_positions"] == 20.0
    assert w["on_orders"] == 10.0
    assert "deployed" not in w


def test_snapshot_trade_count():
    bot = _make_bot()
    bot._trade_count = 42
    snap = build_state_snapshot(bot)
    assert snap["trade_count"] == 42


def test_snapshot_position_has_timeframe():
    bot = _make_bot()
    bot.position_manager.update_position("m1", Side.UP, 50.0, 20.0)
    now = int(time.time())
    bot.active_markets = [
        MarketWindow("m1", "0xcond", "BTC", 900, "up", "dn", now - 300, now + 600)
    ]
    snap = build_state_snapshot(bot)
    assert snap["positions"][0]["timeframe_sec"] == 900


@pytest.mark.asyncio
async def test_api_start_endpoint():
    bot = _make_bot()
    bot._cancel_only_mode = True
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/start")
        assert resp.status_code == 200
        assert bot._cancel_only_mode is False


@pytest.mark.asyncio
async def test_api_stop_endpoint():
    bot = _make_bot()
    bot._cancel_only_mode = False
    bot._pending_cancel_all = False
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/stop")
        assert resp.status_code == 200
        assert bot._cancel_only_mode is True
        assert bot._pending_cancel_all is True


@pytest.mark.asyncio
async def test_api_set_bankroll_dry_run():
    bot = _make_bot()
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/set-bankroll", json={"bankroll": 5000.0})
        assert resp.status_code == 200
        assert bot.position_manager.bankroll == 5000.0
        assert bot.risk_manager.starting_bankroll == 5000.0


@pytest.mark.asyncio
async def test_api_set_bankroll_live_rejected():
    bot = _make_bot()
    bot.cfg = BotConfig(dry_run=False, private_key="0xfake")
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/set-bankroll", json={"bankroll": 5000.0})
        assert resp.status_code == 403
