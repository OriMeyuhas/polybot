"""HTTP + WebSocket server for PolyBot dashboard — aiohttp implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from polybot.web.state import GuiStateHolder

logger = logging.getLogger(__name__)

WEB_AUTH_TOKEN = os.environ.get("WEB_AUTH_TOKEN", "")

STATIC_DIR = Path(__file__).parent / "static"


@web.middleware
async def auth_middleware(request: web.Request, handler):
    """Require Bearer token on mutating requests (POST/PUT/DELETE) when WEB_AUTH_TOKEN is set."""
    token = request.app.get("auth_token", "")
    if token and request.method in ("POST", "PUT", "DELETE"):
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {token}":
            return web.json_response({"error": "Unauthorized"}, status=401)
    return await handler(request)
# .env file at project root
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


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


def _read_env() -> dict[str, str]:
    """Read .env file into a dict."""
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip("\"'")
    return env


def _write_env(env: dict[str, str]) -> None:
    """Write dict back to .env file, preserving comments."""
    lines: list[str] = []
    existing_keys: set[str] = set()
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                k = stripped.partition("=")[0].strip()
                if k in env:
                    lines.append(f"{k}={env[k]}")
                    existing_keys.add(k)
                else:
                    lines.append(line)
            else:
                lines.append(line)
    for k, v in env.items():
        if k not in existing_keys:
            lines.append(f"{k}={v}")
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _env_dry_run() -> bool:
    """Return the DRY_RUN value persisted in .env (default True)."""
    env = _read_env()
    raw = env.get("DRY_RUN")
    if raw is None:
        return True
    return raw.strip().lower() in ("true", "1", "yes")


def _configured_mode() -> str:
    """Return the mode string currently persisted in .env ('dry_run' or 'live')."""
    return "dry_run" if _env_dry_run() else "live"


async def handle_get_config(request: web.Request) -> web.Response:
    """GET /api/config — return credential status (never returns actual secrets)."""
    env = _read_env()
    return web.json_response({
        "has_private_key": bool(env.get("PRIVATE_KEY", "")),
        "has_api_key": bool(env.get("API_KEY", "")),
        "has_api_secret": bool(env.get("API_SECRET", "")),
        "has_api_passphrase": bool(env.get("API_PASSPHRASE", "")),
    })


_VALIDATION_TIMEOUT_SEC = 10.0


def _validate_credentials_sync(
    private_key: str,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
) -> tuple[bool, float | None, str | None]:
    """Synchronously validate credentials against Polymarket CLOB.

    Calls get_balance_allowance — the same check the manual "Test Connection"
    button uses. Returns (ok, balance, error). Never raises. Credentials stay
    in the local scope and are never logged or returned.
    """
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import BalanceAllowanceParams

        host = "https://clob.polymarket.com"
        chain_id = 137
        client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            creds={
                "apiKey": api_key,
                "secret": api_secret,
                "passphrase": api_passphrase,
            },
        )
        result = client.get_balance_allowance(BalanceAllowanceParams())
        raw = result.get("balance") if isinstance(result, dict) else None
        if raw is None:
            return False, None, "Unexpected response — no balance field"
        balance = float(raw) / 1e6
        return True, round(balance, 2), None
    except Exception as e:
        # Redact any credential substrings that may have been echoed by the SDK.
        msg = str(e)
        for secret in (private_key, api_key, api_secret, api_passphrase):
            if secret and secret in msg:
                msg = msg.replace(secret, "<redacted>")
        return False, None, msg


async def _validate_credentials(
    private_key: str,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    timeout: float = _VALIDATION_TIMEOUT_SEC,
) -> tuple[bool, float | None, str | None]:
    """Async wrapper around _validate_credentials_sync with a network timeout."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                _validate_credentials_sync,
                private_key,
                api_key,
                api_secret,
                api_passphrase,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        return False, None, f"Validation timed out after {timeout:.0f}s"
    except Exception as e:
        return False, None, f"Validation error: {e}"


async def handle_post_config(request: web.Request) -> web.Response:
    """POST /api/config — save credentials to .env file.

    When credentials are posted AND the merged set is complete (all 4 present),
    we call _validate_credentials before persisting. If validation fails, the
    previous creds in .env are preserved (rollback). On success we persist and
    return balance so the UI can render a "Credentials valid — balance $X" toast.

    Returns restart_required=True when credentials were actually saved OR when
    .env is already flagged for live mode — both situations mean the running
    bot (still paper with no creds) is out of sync with the persisted config.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # Snapshot existing env BEFORE any write so we can roll back on validation failure.
    original_env = _read_env()
    env = dict(original_env)  # working copy we'll write
    cred_keys = {
        "private_key": "PRIVATE_KEY",
        "api_key": "API_KEY",
        "api_secret": "API_SECRET",
        "api_passphrase": "API_PASSPHRASE",
    }
    saved_any = False
    any_cred_changed = False
    for js_key, env_key in cred_keys.items():
        val = body.get(js_key, "").strip() if isinstance(body.get(js_key), str) else ""
        if val:
            if env.get(env_key, "") != val:
                any_cred_changed = True
            env[env_key] = val
            saved_any = True

    # Write optimistically — we'll roll back if validation fails.
    _write_env(env)

    # Decide whether to validate. Rules:
    #   - Skip validation if no cred was changed in this request (user may be
    #     saving non-cred config changes via the same endpoint in future).
    #   - Skip if the merged set is incomplete — nothing to validate.
    all_creds_present = all(env.get(k, "") for k in cred_keys.values())
    should_validate = any_cred_changed and all_creds_present

    validated_balance: float | None = None
    if should_validate:
        ok, balance, err = await _validate_credentials(
            env["PRIVATE_KEY"],
            env["API_KEY"],
            env["API_SECRET"],
            env["API_PASSPHRASE"],
        )
        if not ok:
            # Roll back: restore ONLY the credential fields to their prior values.
            # We keep any other keys that may have been in the working copy.
            rolled = dict(env)
            for k in cred_keys.values():
                if k in original_env:
                    rolled[k] = original_env[k]
                else:
                    rolled.pop(k, None)
            _write_env(rolled)
            return web.json_response({
                "ok": False,
                "saved": False,
                "error": f"Credentials invalid: {err}",
            })
        validated_balance = balance

    # Compare running bot mode against persisted mode. If creds were saved,
    # signal restart so the user can transition into live (after flipping
    # DRY_RUN separately).
    state: GuiStateHolder = request.app["state"]
    running_mode = state.serialize().get("mode", "dry_run")
    configured = _configured_mode()
    restart_required = saved_any or (configured != running_mode)

    payload = {
        "ok": True,
        "restart_required": bool(restart_required),
        "saved": saved_any,
    }
    if validated_balance is not None:
        payload["balance"] = validated_balance
        payload["validated"] = True
    elif saved_any and not all_creds_present:
        # Partial save — let the UI know we did NOT validate.
        payload["validated"] = False
    return web.json_response(payload)


# Settings keys that map to env vars (ladder params are auto-calculated from bankroll)
_SETTINGS_MAP: dict[str, tuple[str, type]] = {
    "bankroll": ("BANKROLL", float),
    "dry_run": ("DRY_RUN", bool),
    "trade_btc": ("TRADE_BTC", bool),
    "trade_eth": ("TRADE_ETH", bool),
    "trade_sol": ("TRADE_SOL", bool),
    "trade_xrp": ("TRADE_XRP", bool),
    "trade_5m": ("TRADE_5M", bool),
    "trade_15m": ("TRADE_15M", bool),
    "trade_1h": ("TRADE_1H", bool),
}

# Default values for settings
_SETTINGS_DEFAULTS: dict[str, Any] = {
    "bankroll": 1000.0,
    "dry_run": True,
    "trade_btc": True,
    "trade_eth": True,
    "trade_sol": True,
    "trade_xrp": True,
    "trade_5m": True,
    "trade_15m": True,
    "trade_1h": True,
}


async def handle_get_settings(request: web.Request) -> web.Response:
    """GET /api/settings — return current bot settings."""
    env = _read_env()
    settings: dict[str, Any] = {}
    for key, (env_key, typ) in _SETTINGS_MAP.items():
        raw = env.get(env_key)
        if raw is None:
            settings[key] = _SETTINGS_DEFAULTS.get(key)
        elif typ is bool:
            settings[key] = raw.lower() in ("true", "1", "yes")
        elif typ is int:
            settings[key] = int(raw)
        elif typ is float:
            settings[key] = float(raw)
        else:
            settings[key] = raw
    return web.json_response(settings)


async def handle_post_settings(request: web.Request) -> web.Response:
    """POST /api/settings — save to .env AND update the running bot.

    Most settings (bankroll, asset toggles) apply immediately via hot-update
    callbacks. DRY_RUN is different — it would require swapping clob_client,
    cancelling paper orders, and re-init of risk/position state; that's too
    invasive to hot-swap safely. Instead we persist to .env and return
    restart_required=True so the UI can prompt the user.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    env = _read_env()
    for key, (env_key, typ) in _SETTINGS_MAP.items():
        if key in body:
            val = body[key]
            if typ is bool:
                env[env_key] = "true" if val else "false"
            else:
                env[env_key] = str(val)
    _write_env(env)

    # Apply bankroll change to running bot immediately
    update_bankroll_fn = request.app.get("update_bankroll_fn")
    if update_bankroll_fn and "bankroll" in body:
        try:
            update_bankroll_fn(float(body["bankroll"]))
        except Exception:
            pass

    # Compare persisted mode to actually-running mode.
    state: GuiStateHolder = request.app["state"]
    running_mode = state.serialize().get("mode", "dry_run")
    configured = _configured_mode()
    restart_required = configured != running_mode

    return web.json_response({
        "ok": True,
        "restart_required": bool(restart_required),
    })


async def handle_test_connection(request: web.Request) -> web.Response:
    """POST /api/test-connection — validate credentials by calling get_balance_allowance.

    Delegates to the shared _validate_credentials helper that handle_post_config
    uses, so the two surfaces never diverge. Reads credentials from .env.
    """
    env = _read_env()
    private_key = env.get("PRIVATE_KEY", "")
    api_key = env.get("API_KEY", "")
    api_secret = env.get("API_SECRET", "")
    api_passphrase = env.get("API_PASSPHRASE", "")

    missing = []
    if not private_key:
        missing.append("PRIVATE_KEY")
    if not api_key:
        missing.append("API_KEY")
    if not api_secret:
        missing.append("API_SECRET")
    if not api_passphrase:
        missing.append("API_PASSPHRASE")
    if missing:
        return web.json_response({
            "ok": False,
            "error": f"Missing credentials: {', '.join(missing)}",
        })

    ok, balance, err = await _validate_credentials(
        private_key, api_key, api_secret, api_passphrase
    )
    if ok:
        return web.json_response({"ok": True, "balance": balance})
    return web.json_response({"ok": False, "error": err})


def _archive_paper_logs(data_dir: Path, now_ts: int) -> list[str]:
    """Rename paper-mode data logs to .bak. Non-fatal on missing files.

    Returns the list of archived basenames for UI confirmation. Safe to call
    while the bot is running — the bot re-opens log files on next write.
    """
    archived: list[str] = []
    for name in ("settlement_log.jsonl", "activity_log.jsonl"):
        src = data_dir / name
        if not src.exists():
            continue
        dst = data_dir / f"{name}.{now_ts}.bak"
        try:
            src.rename(dst)
            archived.append(name)
        except OSError as exc:
            logger.warning("archive_failed name=%s err=%s", name, exc)
    return archived


async def handle_restart_reset(request: web.Request) -> web.Response:
    """POST /api/restart-reset — paper-only clean-slate restart.

    Archives cumulative paper logs, reseeds .env BANKROLL from DRY_RUN_BANKROLL
    (default 10000), then delegates to the normal restart flow. Hard-refuses
    when .env says DRY_RUN=false so we never wipe live history.
    """
    if not _env_dry_run():
        return web.json_response(
            {"ok": False, "error": "refuse to wipe live history (DRY_RUN is false)"},
            status=400,
        )

    restart_fn: Callable | None = request.app.get("restart_fn")
    if restart_fn is None:
        return web.json_response(
            {"ok": False, "error": "restart_fn not wired"}, status=400
        )

    import time as _time
    now_ts = int(_time.time())

    # Reseed bankroll from DRY_RUN_BANKROLL (default 10000).
    env = _read_env()
    seed_raw = env.get("DRY_RUN_BANKROLL", "10000")
    try:
        seed = float(seed_raw)
    except ValueError:
        seed = 10000.0
    env["BANKROLL"] = str(int(seed) if seed.is_integer() else seed)
    _write_env(env)

    # Archive paper logs (settlement + activity). Resolve project root the
    # same way _ENV_FILE does — lets tests monkeypatch it.
    data_dir = _ENV_FILE.parent / "data"
    archived = _archive_paper_logs(data_dir, now_ts)

    async def _do_restart() -> None:
        await asyncio.sleep(0.3)
        try:
            result = restart_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.exception("restart_reset_failed: %s", exc)

    asyncio.create_task(_do_restart())
    return web.json_response({
        "ok": True,
        "status": "restarting",
        "archived": archived,
        "bankroll_seed": seed,
    })


async def handle_restart(request: web.Request) -> web.Response:
    """POST /api/restart — gracefully stop the bot and re-exec the process.

    Sequence:
      1. Invoke stop_fn (bot.stop cancels exchange orders, closes WS, flushes).
      2. Spawn a new `python run_bot.py` with the same argv; stdout inherits
         so the existing `polybot.log > 2>&1 &` piping keeps working.
      3. Schedule process exit on a short delay so the HTTP response flushes.

    If restart_fn is wired (by run_bot.py), delegate to it for full control.
    Otherwise fall back to a minimal stop-then-spawn-then-exit loop.
    """
    restart_fn: Callable | None = request.app.get("restart_fn")
    if restart_fn is None:
        return web.json_response(
            {"ok": False, "error": "restart_fn not wired"}, status=400
        )

    async def _do_restart() -> None:
        # Small delay so the HTTP response gets flushed to the browser.
        await asyncio.sleep(0.3)
        try:
            result = restart_fn()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:
            logger.exception("restart_failed: %s", exc)

    asyncio.create_task(_do_restart())
    return web.json_response({"ok": True, "status": "restarting"})


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
    update_bankroll_fn: Callable[[float], None] | None = None,
    restart_fn: Callable[[], Any] | None = None,
) -> web.Application:
    """Create and configure the aiohttp Application."""
    app = web.Application(middlewares=[auth_middleware])
    app["auth_token"] = WEB_AUTH_TOKEN
    app["state"] = state
    app["start_fn"] = start_fn
    app["stop_fn"] = stop_fn
    app["update_bankroll_fn"] = update_bankroll_fn
    app["restart_fn"] = restart_fn
    app["ws_clients"] = set()

    # Inject configured_mode (read from .env) into every serialized snapshot so
    # the frontend can render a mismatch banner when the running bot's mode
    # disagrees with the persisted config.
    def _augment(_snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"configured_mode": _configured_mode()}
    state.set_augmenter(_augment)

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
    app.router.add_get("/api/config", handle_get_config)
    app.router.add_post("/api/config", handle_post_config)
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings", handle_post_settings)
    app.router.add_post("/api/test-connection", handle_test_connection)
    app.router.add_post("/api/restart", handle_restart)
    app.router.add_post("/api/restart-reset", handle_restart_reset)
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
