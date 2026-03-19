"""HTTP + WebSocket server for PolyBot dashboard — aiohttp implementation."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from polybot.web.state import GuiStateHolder

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


async def handle_index(request: web.Request) -> web.Response:
    """GET / — serve index.html from static directory."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise web.HTTPNotFound(reason="index.html not found")
    return web.FileResponse(index_path)


async def handle_state(request: web.Request) -> web.Response:
    """GET /api/state — return serialized state as JSON."""
    state: GuiStateHolder = request.app["state"]
    return web.json_response(state.serialize())


async def handle_start(request: web.Request) -> web.Response:
    """POST /api/start — invoke start_fn if wired."""
    start_fn: Callable | None = request.app.get("start_fn")
    if start_fn:
        coro = start_fn()
        if asyncio.iscoroutine(coro):
            asyncio.create_task(coro)
        return web.json_response({"ok": True, "status": "running"})
    return web.json_response({"ok": False, "error": "start_fn not wired"}, status=400)


async def handle_stop(request: web.Request) -> web.Response:
    """POST /api/stop — invoke stop_fn if wired."""
    stop_fn: Callable | None = request.app.get("stop_fn")
    if stop_fn:
        coro = stop_fn()
        if asyncio.iscoroutine(coro):
            asyncio.create_task(coro)
        return web.json_response({"ok": True, "status": "stopped"})
    return web.json_response({"ok": False, "error": "stop_fn not wired"}, status=400)


async def handle_set_bankroll(request: web.Request) -> web.Response:
    """POST /api/set-bankroll — update bankroll in state."""
    state: GuiStateHolder = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    bankroll = body.get("bankroll")
    if bankroll is None:
        return web.json_response({"error": "Missing 'bankroll' field"}, status=400)
    try:
        bankroll = float(bankroll)
    except (TypeError, ValueError):
        return web.json_response({"error": "Bankroll must be a number"}, status=400)
    if bankroll <= 0:
        return web.json_response({"error": "Bankroll must be positive"}, status=400)

    state.update(bankroll=bankroll)
    return web.json_response({"ok": True, "bankroll": bankroll})


async def handle_balance(request: web.Request) -> web.Response:
    """GET /api/balance — return wallet/balance info from state."""
    state: GuiStateHolder = request.app["state"]
    data = state.serialize()
    wallet = data.get("wallet") or {}
    return web.json_response(wallet)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """GET /ws — WebSocket handler; sends state on connect and on broadcast."""
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    clients: set = request.app["ws_clients"]
    clients.add(ws)
    try:
        state: GuiStateHolder = request.app["state"]
        # Send initial state immediately on connect
        await ws.send_str(json.dumps(state.serialize()))
        # Keep connection alive; handle pings/pongs
        async for _ in ws:
            pass
    except Exception as exc:
        logger.debug("ws_client_error: %s", exc)
    finally:
        clients.discard(ws)
    return ws


def create_app(
    state: GuiStateHolder,
    start_fn: Callable[[], Any] | None,
    stop_fn: Callable[[], Any] | None,
) -> web.Application:
    """Create and configure the aiohttp Application."""
    app = web.Application()
    app["state"] = state
    app["start_fn"] = start_fn
    app["stop_fn"] = stop_fn
    app["ws_clients"] = set()

    async def broadcast() -> None:
        """Push serialized state to all connected WebSocket clients."""
        msg = json.dumps(state.serialize())
        dead: set = set()
        for ws in list(app["ws_clients"]):
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        app["ws_clients"] -= dead

    state.set_broadcast(broadcast)

    # Routes
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_post("/api/set-bankroll", handle_set_bankroll)
    app.router.add_get("/api/balance", handle_balance)
    app.router.add_get("/ws", handle_ws)

    # Static files
    if STATIC_DIR.exists():
        app.router.add_static("/static", path=str(STATIC_DIR), name="static")

    return app


async def start_gui_server(
    app: web.Application,
    port: int = 8765,
    host: str = "127.0.0.1",
) -> web.AppRunner:
    """Start the aiohttp server; return the AppRunner for later cleanup."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("gui_server_started on %s:%s", host, port)
    return runner
