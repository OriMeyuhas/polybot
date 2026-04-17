"""Integration tests — paper mode startup and state verification."""

import asyncio

import pytest
from aiohttp import ClientSession

from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.web.server import create_app, start_gui_server


@pytest.mark.asyncio
async def test_paper_mode_startup():
    """Verify bot starts in paper mode and web UI is accessible."""
    cfg = BotConfig(dry_run=True, web_port=18080, start_paused=True)
    bot = Bot(cfg)
    app = create_app(state=bot.gui_state, start_fn=bot.ui_start, stop_fn=bot.ui_stop)

    runner = await start_gui_server(app, 18080)

    try:
        async with ClientSession() as session:
            async with session.get("http://127.0.0.1:18080/api/state") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["mode"] == "dry_run"
                assert data["running"] is False
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bot_state_snapshot_has_required_fields():
    """Verify state snapshot matches spec payload."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()

    required_fields = [
        "mode", "running", "heartbeat_healthy", "cancel_only_mode",
        "total_pnl", "realized_pnl", "unrealized_pnl",
        "trade_count", "position_count", "pairs_completed",
        "avg_pair_cost", "imbalance_ratio", "runtime_sec",
        "markets_active", "win_rate",
        "prices", "binance_prices", "spots",
        "active_markets", "activity_feed", "trades",
        "pending_settlements", "wallet",
    ]
    for field in required_fields:
        assert field in state, f"Missing field: {field}"


@pytest.mark.asyncio
async def test_web_ui_start_stop_endpoints():
    """Verify /api/start and /api/stop toggle cancel_only_mode."""
    cfg = BotConfig(dry_run=True, web_port=18081, start_paused=True)
    bot = Bot(cfg)
    app = create_app(state=bot.gui_state, start_fn=bot.ui_start, stop_fn=bot.ui_stop)

    runner = await start_gui_server(app, 18081)

    try:
        async with ClientSession() as session:
            # Start
            async with session.post("http://127.0.0.1:18081/api/start") as resp:
                assert resp.status == 200

            # Stop
            async with session.post("http://127.0.0.1:18081/api/stop") as resp:
                assert resp.status == 200
    finally:
        await runner.cleanup()
