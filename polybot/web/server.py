"""Web dashboard server: FastAPI app, state serializer, WebSocket broadcast."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from polybot.bot import Bot

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


def build_state_snapshot(bot: Bot) -> dict:
    """Build a JSON-serializable snapshot of all bot state."""
    cfg = bot.cfg
    now = time.time()
    now_epoch = int(now)

    daily_pnl = bot.risk_manager.daily_pnl
    starting = bot.risk_manager.starting_bankroll
    pnl_pct = (daily_pnl / starting * 100) if starting > 0 else 0.0

    spots = {}
    for asset in cfg.assets:
        price = bot.spot_prices.get(asset, 0.0)
        delta = bot.compute_spot_delta(asset) if price > 0 else 0.0
        spots[asset] = {"price": price, "delta": round(delta, 6)}

    market_map = {m.market_id: m for m in list(bot.active_markets)}

    ladders = []
    for mid in list(bot.ladder_manager.ladders):
        stats = bot.ladder_manager.get_ladder_stats(mid)
        market = market_map.get(mid)
        asset = market.asset if market else ""
        tf = market.timeframe_sec if market else 0
        time_left = market.remaining(now_epoch) if market else 0
        condition_id = market.condition_id if market else ""
        ladders.append({
            "market_id": mid, "asset": asset, "timeframe_sec": tf,
            "condition_id": condition_id,
            "up_resting": stats["up_resting"], "dn_resting": stats["dn_resting"],
            "up_filled": stats["up_filled"], "dn_filled": stats["dn_filled"],
            "up_vwap": round(stats["up_vwap"], 4), "dn_vwap": round(stats["dn_vwap"], 4),
            "pair_cost": round(stats["pair_cost"], 4),
            "imbalance": round(stats["imbalance"], 4),
            "time_left_sec": time_left,
            "ask_up": round(stats["ask_up"], 4),
            "ask_dn": round(stats["ask_dn"], 4),
            "up_filled_count": stats["up_filled_count"],
            "dn_filled_count": stats["dn_filled_count"],
            "up_total_rungs": stats["up_total_rungs"],
            "dn_total_rungs": stats["dn_total_rungs"],
        })

    positions = []
    for mid, pos in list(bot.position_manager.positions.items()):
        market = market_map.get(mid) or bot._expired_market_cache.get(mid)
        asset = market.asset if market else ""
        pnl_up = pos.profit_if_up()
        pnl_dn = pos.profit_if_down()
        positions.append({
            "market_id": mid, "asset": asset,
            "timeframe_sec": market.timeframe_sec if market else 0,
            "up_qty": round(pos.up_qty, 2), "up_cost": round(pos.up_cost, 2),
            "dn_qty": round(pos.dn_qty, 2), "dn_cost": round(pos.dn_cost, 2),
            "pnl_if_up": round(pnl_up, 2), "pnl_if_down": round(pnl_dn, 2),
            "pnl_worst_case": round(min(pnl_up, pnl_dn), 2),
        })

    activity = []
    for ev in list(bot._activity_log):
        activity.append({"ts": ev.timestamp, "type": ev.event_type, "asset": ev.asset, "detail": ev.detail, "pnl": ev.pnl})

    deployed = bot.ladder_manager.total_committed()
    in_positions = bot.position_manager.total_position_cost()
    on_orders = max(0.0, deployed - in_positions)
    balance = getattr(bot, "_wallet_balance", None)
    if balance is None:
        balance = bot.position_manager.bankroll if cfg.dry_run else 0.0

    address = "DRY RUN"
    if not cfg.dry_run and cfg.private_key:
        try:
            from eth_account import Account
            address = Account.from_key(cfg.private_key).address
            address = address[:6] + "..." + address[-4:]
        except Exception:
            address = "unknown"

    wallet = {
        "address": address,
        "usdc_balance": round(balance, 2),
        "on_orders": round(on_orders, 2),
        "in_positions": round(in_positions, 2),
        "available": round(balance - deployed, 2),
    }

    return {
        "mode": "dry_run" if cfg.dry_run else "live",
        "uptime_sec": round(now - bot._start_time, 1),
        "bankroll": bot.position_manager.bankroll,
        "daily_pnl": round(daily_pnl, 2), "daily_pnl_pct": round(pnl_pct, 2),
        "heartbeat_healthy": bot.heartbeat.is_healthy() if bot.heartbeat else True,
        "cancel_only_mode": bot._cancel_only_mode,
        "risk_halted": bot.risk_manager.is_halted(),
        "trade_count": bot._trade_count,
        "wallet": wallet, "spots": spots, "ladders": ladders, "positions": positions,
        "pending_settlements": list(bot.position_manager.get_pending_settlements()),
        "failed_settlements": list(bot.position_manager.get_failed_settlements()),
        "activity": activity,
    }


def create_app(bot: Bot) -> FastAPI:
    """Create the FastAPI application with REST and WebSocket endpoints."""
    app = FastAPI(title="PolyBot Dashboard")

    @app.get("/")
    async def index():
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/api/state")
    async def api_state():
        return JSONResponse(build_state_snapshot(bot))

    @app.get("/api/balance")
    async def api_balance():
        snapshot = build_state_snapshot(bot)
        return JSONResponse(snapshot["wallet"])

    @app.post("/api/start")
    async def api_start():
        bot._cancel_only_mode = False
        return JSONResponse({"status": "running"})

    @app.post("/api/stop")
    async def api_stop():
        bot._cancel_only_mode = True
        bot._pending_cancel_all = True
        return JSONResponse({"status": "stopped"})

    @app.post("/api/set-bankroll")
    async def api_set_bankroll(request: Request):
        if not bot.cfg.dry_run:
            return JSONResponse({"error": "Cannot set bankroll in live mode"}, status_code=403)
        body = await request.json()
        bankroll = float(body.get("bankroll", 0))
        if bankroll <= 0:
            return JSONResponse({"error": "Bankroll must be positive"}, status_code=400)
        bot.position_manager.bankroll = bankroll
        bot.risk_manager.starting_bankroll = bankroll
        bot._wallet_balance = bankroll
        return JSONResponse({"status": "ok", "bankroll": bankroll})

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    connected: list[WebSocket] = []

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        connected.append(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            if websocket in connected:
                connected.remove(websocket)

    async def broadcast_loop():
        """Push state to all connected WebSocket clients at 1Hz."""
        while True:
            if connected:
                snapshot = build_state_snapshot(bot)
                payload = json.dumps(snapshot)
                dead = []
                for ws in list(connected):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.append(ws)
                for ws in dead:
                    try:
                        connected.remove(ws)
                    except ValueError:
                        pass
            await asyncio.sleep(1.0)

    app._broadcast_loop = broadcast_loop  # type: ignore[attr-defined]
    return app
