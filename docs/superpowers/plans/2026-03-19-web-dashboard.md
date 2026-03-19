# Web Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Rich terminal dashboard with a FastAPI + WebSocket browser-based live monitoring UI.

**Architecture:** FastAPI runs as an async task inside `Bot.run()`, sharing memory directly. A WebSocket endpoint pushes a JSON snapshot of all bot state at 1Hz. Vanilla JS frontend connects and updates the DOM.

**Tech Stack:** FastAPI, uvicorn, WebSocket, vanilla JS + CSS

**Spec:** `docs/superpowers/specs/2026-03-19-web-dashboard-design.md`

---

## File Map

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `polybot/web/__init__.py` | Package marker |
| Create | `polybot/web/server.py` | FastAPI app, state serializer, WebSocket broadcast |
| Create | `polybot/web/static/index.html` | Dashboard page |
| Create | `polybot/web/static/dashboard.js` | WebSocket client, DOM updater |
| Create | `polybot/web/static/style.css` | Dashboard styling |
| Create | `tests/test_web_server.py` | Server + serializer tests |
| Modify | `polybot/types.py` | Add `ActivityEvent` dataclass (moved from display.py) |
| Modify | `polybot/config.py:118` | Add `web_port: int = 8080` field |
| Modify | `polybot/config.py:183` | Add `web_port` to `load_bot_config()` |
| Modify | `polybot/ladder_manager.py:94` | Rename `_total_committed` → `total_committed` |
| Modify | `polybot/bot.py:133` | Update `ActivityEvent` import to `polybot.types` |
| Modify | `polybot/bot.py:305-318` | Replace `run_display()` with `run_web_server()` |
| Modify | `polybot/bot.py:415-432` | Wire web server task, add graceful shutdown |
| Modify | `requirements.txt` | Add `fastapi`, `uvicorn[standard]` |
| Delete | `polybot/display.py` | Replaced by web UI |

---

### Task 1: Prep — Migrate ActivityEvent, Config, Public API

**Files:**
- Modify: `polybot/types.py`
- Modify: `polybot/config.py:118,183`
- Modify: `polybot/ladder_manager.py:94`
- Modify: `polybot/bot.py:133`
- Modify: `requirements.txt`

- [ ] **Step 1: Move `ActivityEvent` to `polybot/types.py`**

Add at the bottom of `polybot/types.py`:

```python
@dataclass
class ActivityEvent:
    timestamp: float
    event_type: str  # LADDER, FILL, SETTLE, CANCEL, HEARTBEAT_LOST
    asset: str
    detail: str
    pnl: float | None = None
```

- [ ] **Step 2: Update import in `polybot/bot.py`**

The import is a local import inside `_record_activity()` (line ~138), not a top-level import. Change:
```python
from polybot.display import ActivityEvent
```
To:
```python
from polybot.types import ActivityEvent
```

Note: there is a second import of `display` inside `run_display()` which will be removed entirely when that method is replaced in Task 4.

- [ ] **Step 3: Add `web_port` to BotConfig**

In `polybot/config.py`, add after `mock_base_fill_rate` (line ~121):
```python
    # Web dashboard
    web_port: int = 8080
```

In `load_bot_config()` (line ~183), add before the closing paren:
```python
        web_port=int(os.getenv("WEB_PORT", "8080")),
```

- [ ] **Step 4: Make `_total_committed` public in `polybot/ladder_manager.py`**

Rename `_total_committed` to `total_committed` (the method definition at line 94 plus the 2 call sites: `post_ladder` at line ~114 and `reprice_if_needed` at line ~285).

- [ ] **Step 5: Add dependencies to `requirements.txt`**

Append:
```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 6: Run tests to verify nothing broke**

Run: `pytest tests/ -x -q`
Expected: All 175 tests pass

- [ ] **Step 7: Commit**

```bash
git add polybot/types.py polybot/config.py polybot/ladder_manager.py polybot/bot.py requirements.txt
git commit -m "prep: migrate ActivityEvent, add web_port config, public total_committed"
```

---

### Task 2: State Serializer

The core function that reads bot state and produces the JSON snapshot. Tested independently, no FastAPI dependency.

**Files:**
- Create: `polybot/web/__init__.py`
- Create: `polybot/web/server.py` (serializer portion only)
- Create: `tests/test_web_server.py`

- [ ] **Step 1: Create package**

Create empty `polybot/web/__init__.py`.

- [ ] **Step 2: Write tests for `build_state_snapshot`**

Create `tests/test_web_server.py`:

```python
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
    bot._start_time = time.time() - 60  # 1 min uptime
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

    # Minimal ladder manager mock
    bot.ladder_manager = MagicMock()
    bot.ladder_manager.ladders = {}
    bot.ladder_manager.total_committed.return_value = 0.0

    bot.heartbeat = MagicMock()
    bot.heartbeat.is_healthy.return_value = True

    bot.active_markets = []
    bot._activity_log = deque(maxlen=20)
    bot._wallet_balance = None  # cached balance
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
    # Need a market for asset lookup
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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/test_web_server.py -v`
Expected: ImportError — `build_state_snapshot` does not exist yet

- [ ] **Step 4: Implement `build_state_snapshot` in `polybot/web/server.py`**

```python
"""Web dashboard server: FastAPI app, state serializer, WebSocket broadcast."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polybot.bot import Bot


def build_state_snapshot(bot: Bot) -> dict:
    """Build a JSON-serializable snapshot of all bot state.

    Thread safety: uses .copy() on mutable dicts to avoid RuntimeError
    from concurrent to_thread() mutations in the trading loop.
    """
    cfg = bot.cfg
    now = time.time()
    now_epoch = int(now)

    # Header
    daily_pnl = bot.risk_manager.daily_pnl
    starting = bot.risk_manager.starting_bankroll
    pnl_pct = (daily_pnl / starting * 100) if starting > 0 else 0.0

    # Spot prices — copy to avoid mutation
    spots = {}
    for asset in cfg.assets:
        price = bot.spot_prices.get(asset, 0.0)
        delta = bot.compute_spot_delta(asset) if price > 0 else 0.0
        spots[asset] = {"price": price, "delta": round(delta, 6)}

    # Active markets lookup (for asset + time_left)
    market_map = {m.market_id: m for m in list(bot.active_markets)}

    # Ladders — snapshot keys then iterate
    ladders = []
    for mid in list(bot.ladder_manager.ladders):
        stats = bot.ladder_manager.get_ladder_stats(mid)
        market = market_map.get(mid)
        asset = market.asset if market else ""
        tf = market.timeframe_sec if market else 0
        time_left = market.remaining(now_epoch) if market else 0
        ladders.append({
            "market_id": mid,
            "asset": asset,
            "timeframe_sec": tf,
            "up_resting": stats["up_resting"],
            "dn_resting": stats["dn_resting"],
            "up_filled": stats["up_filled"],
            "dn_filled": stats["dn_filled"],
            "up_vwap": round(stats["up_vwap"], 4),
            "dn_vwap": round(stats["dn_vwap"], 4),
            "pair_cost": round(stats["combined_vwap"], 4),
            "imbalance": round(stats["imbalance"], 4),
            "time_left_sec": time_left,
        })

    # Positions — snapshot keys then iterate
    positions = []
    for mid, pos in list(bot.position_manager.positions.items()):
        market = market_map.get(mid) or bot._expired_market_cache.get(mid)
        asset = market.asset if market else ""
        pnl_up = pos.profit_if_up()
        pnl_dn = pos.profit_if_down()
        positions.append({
            "market_id": mid,
            "asset": asset,
            "up_qty": round(pos.up_qty, 2),
            "up_cost": round(pos.up_cost, 2),
            "dn_qty": round(pos.dn_qty, 2),
            "dn_cost": round(pos.dn_cost, 2),
            "pnl_if_up": round(pnl_up, 2),
            "pnl_if_down": round(pnl_dn, 2),
            "pnl_worst_case": round(min(pnl_up, pnl_dn), 2),
        })

    # Activity — copy deque
    activity = []
    for ev in list(bot._activity_log):
        activity.append({
            "ts": ev.timestamp,
            "type": ev.event_type,
            "asset": ev.asset,
            "detail": ev.detail,
            "pnl": ev.pnl,
        })

    # Wallet
    deployed = bot.ladder_manager.total_committed()
    balance = getattr(bot, "_wallet_balance", None)
    if balance is None:
        balance = bot.position_manager.bankroll if cfg.dry_run else 0.0

    # Derive public address — NEVER expose private key
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
        "deployed": round(deployed, 2),
        "available": round(balance - deployed, 2),
    }

    return {
        "mode": "dry_run" if cfg.dry_run else "live",
        "uptime_sec": round(now - bot._start_time, 1),
        "bankroll": bot.position_manager.bankroll,
        "daily_pnl": round(daily_pnl, 2),
        "daily_pnl_pct": round(pnl_pct, 2),
        "heartbeat_healthy": bot.heartbeat.is_healthy() if bot.heartbeat else True,
        "cancel_only_mode": bot._cancel_only_mode,
        "risk_halted": bot.risk_manager.is_halted(),
        "wallet": wallet,
        "spots": spots,
        "ladders": ladders,
        "positions": positions,
        "pending_settlements": list(bot.position_manager.get_pending_settlements()),
        "failed_settlements": list(bot.position_manager.get_failed_settlements()),
        "activity": activity,
    }
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_web_server.py -v`
Expected: All 5 pass

- [ ] **Step 6: Commit**

```bash
git add polybot/web/ tests/test_web_server.py
git commit -m "feat: add web dashboard state serializer with tests"
```

---

### Task 3: FastAPI App — REST + WebSocket Endpoints

**Files:**
- Modify: `polybot/web/server.py`
- Modify: `tests/test_web_server.py`

- [ ] **Step 1: Add tests for the FastAPI endpoints**

Append to `tests/test_web_server.py`:

```python
import pytest
from httpx import AsyncClient, ASGITransport
from polybot.web.server import create_app


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
        assert "deployed" in data


@pytest.mark.asyncio
async def test_index_serves_html():
    bot = _make_bot()
    app = create_app(bot)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
```

- [ ] **Step 2: Install test deps and run to verify failures**

Run: `pip install fastapi pytest-asyncio` (httpx already in requirements.txt)
Run: `pytest tests/test_web_server.py -v`
Expected: ImportError — `create_app` does not exist yet

- [ ] **Step 3: Implement `create_app` and endpoints in `polybot/web/server.py`**

Add to the top of `server.py`:

```python
import asyncio
import json
import logging
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
```

Add the `create_app` function and WebSocket broadcast:

```python
def create_app(bot: Bot) -> FastAPI:
    app = FastAPI(title="PolyBot Dashboard")

    # Mount static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

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

    # WebSocket broadcast
    connected: list[WebSocket] = []

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        connected.append(websocket)
        try:
            while True:
                await websocket.receive_text()  # keep alive, ignore input
        except WebSocketDisconnect:
            pass
        finally:
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

    app._broadcast_loop = broadcast_loop  # stored for bot.py to launch as task

    return app
```

- [ ] **Step 4: Create placeholder static files**

Create `polybot/web/static/index.html`:
```html
<!DOCTYPE html>
<html><head><title>PolyBot</title></head>
<body><h1>PolyBot Dashboard</h1><p>Loading...</p></body>
</html>
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_web_server.py -v`
Expected: All 8 pass

- [ ] **Step 6: Commit**

```bash
git add polybot/web/server.py polybot/web/static/index.html tests/test_web_server.py
git commit -m "feat: add FastAPI app with REST and WebSocket endpoints"
```

---

### Task 4: Wire Web Server Into Bot

**Files:**
- Modify: `polybot/bot.py`
- Delete: `polybot/display.py`

- [ ] **Step 1: Replace `run_display()` with `run_web_server()` in `polybot/bot.py`**

Replace the `run_display` method (lines ~305-318) with:

```python
    async def run_web_server(self):
        """Start the FastAPI web dashboard."""
        import uvicorn
        from polybot.web.server import create_app

        app = create_app(self)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=self.cfg.web_port,
            log_level="warning",
        )
        self._uvicorn_server = uvicorn.Server(config)
        self._uvicorn_server.install_signal_handlers = lambda: None

        # Launch broadcast and balance polling as sibling tasks
        broadcast_task = asyncio.create_task(app._broadcast_loop())
        balance_task = asyncio.create_task(self._poll_wallet_balance())
        try:
            logger.info("Web dashboard at http://127.0.0.1:%d", self.cfg.web_port)
            await self._uvicorn_server.serve()
        except Exception as e:
            logger.warning("Web server stopped: %s", e)
        finally:
            broadcast_task.cancel()
            balance_task.cancel()
```

Also add the balance polling method:

```python
    async def _poll_wallet_balance(self):
        """Poll wallet USDC balance every 60s. Stores in _wallet_balance."""
        while True:
            try:
                if not self.cfg.dry_run:
                    result = await asyncio.to_thread(
                        self.clob_client.get_balance_allowance
                    )
                    self._wallet_balance = float(result.get("balance", 0)) / 1e6
                else:
                    self._wallet_balance = self.position_manager.bankroll
            except Exception as e:
                logger.debug("Balance poll failed: %s", e)
            await asyncio.sleep(60)
```

- [ ] **Step 2: Update task list in `run()` method**

Replace `asyncio.create_task(self.run_display())` with `asyncio.create_task(self.run_web_server())`.

- [ ] **Step 3: Add graceful shutdown for uvicorn**

In the `finally` block of `run()`, add before `self.order_executor.cancel_all()`:

```python
            if hasattr(self, '_uvicorn_server'):
                self._uvicorn_server.should_exit = True
```

- [ ] **Step 4: Remove Rich import from bot.py**

Remove the import line `from polybot.display import ActivityEvent` (already changed in Task 1). Verify no other Rich imports remain in `bot.py`.

- [ ] **Step 5: Delete `polybot/display.py`**

```bash
git rm polybot/display.py
```

- [ ] **Step 6: Run all tests**

Run: `pytest tests/ -x -q`
Expected: All tests pass. If any test imported `display.py`, update the import.

- [ ] **Step 7: Quick smoke test**

```bash
python run_bot.py > /dev/null 2>&1 &
sleep 5
curl http://localhost:8080/api/state 2>/dev/null | python -m json.tool | head -20
```

Expected: JSON with `mode`, `bankroll`, `spots`, etc.

- [ ] **Step 8: Commit**

```bash
git add polybot/bot.py polybot/display.py
git commit -m "feat: wire web server into bot, remove Rich display"
```

Note: `git rm` in Step 5 already staged the display.py deletion, but including it here makes the commit explicit.

---

### Task 5: Frontend — Dashboard HTML + CSS

**Files:**
- Create: `polybot/web/static/index.html` (overwrite placeholder)
- Create: `polybot/web/static/style.css`

- [ ] **Step 1: Write `style.css`**

Create `polybot/web/static/style.css` — dark theme trading dashboard:

```css
* { margin: 0; padding: 0; box-sizing: border-box; }
:root {
    --bg: #0d1117; --surface: #161b22; --border: #30363d;
    --text: #c9d1d9; --muted: #8b949e; --green: #3fb950;
    --red: #f85149; --yellow: #d29922; --blue: #58a6ff;
}
body { background: var(--bg); color: var(--text); font-family: 'SF Mono', 'Cascadia Code', monospace; font-size: 13px; padding: 16px; }
h1 { font-size: 16px; color: var(--blue); }

.header { display: flex; align-items: center; gap: 16px; padding: 12px 16px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.badge { padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 11px; text-transform: uppercase; }
.badge-dry { background: var(--yellow); color: #000; }
.badge-live { background: var(--red); color: #fff; }
.badge-cancel { background: var(--yellow); color: #000; }
.badge-halted { background: var(--red); color: #fff; }
.stat { text-align: center; }
.stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; }
.stat-value { font-size: 16px; font-weight: bold; }
.dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
.dot-green { background: var(--green); }
.dot-red { background: var(--red); }

.spots { display: flex; gap: 12px; margin-bottom: 12px; }
.spot-card { flex: 1; padding: 12px; background: var(--surface); border: 1px solid var(--border); border-radius: 8px; text-align: center; }
.spot-asset { font-size: 11px; color: var(--muted); }
.spot-price { font-size: 18px; font-weight: bold; }
.spot-delta { font-size: 12px; }

.panel { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-bottom: 12px; }
.panel-title { font-size: 11px; color: var(--muted); text-transform: uppercase; margin-bottom: 8px; letter-spacing: 1px; }

table { width: 100%; border-collapse: collapse; }
th { text-align: left; font-size: 10px; color: var(--muted); text-transform: uppercase; padding: 4px 8px; border-bottom: 1px solid var(--border); }
td { padding: 6px 8px; border-bottom: 1px solid var(--border); font-size: 12px; }
tr:hover { background: rgba(88, 166, 255, 0.05); }

.text-green { color: var(--green); }
.text-red { color: var(--red); }
.text-yellow { color: var(--yellow); }
.text-muted { color: var(--muted); }

.activity-row { display: flex; gap: 8px; padding: 4px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.activity-time { color: var(--muted); width: 60px; flex-shrink: 0; }
.activity-type { width: 80px; flex-shrink: 0; font-weight: bold; }
.activity-asset { width: 40px; flex-shrink: 0; }
.activity-detail { flex: 1; }
.activity-pnl { width: 80px; text-align: right; flex-shrink: 0; }

.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }

#disconnect-banner { display: none; position: fixed; top: 0; left: 0; right: 0; background: var(--red); color: #fff; text-align: center; padding: 8px; font-weight: bold; z-index: 999; }
```

- [ ] **Step 2: Write `index.html`**

Overwrite `polybot/web/static/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PolyBot Dashboard</title>
    <link rel="stylesheet" href="/static/style.css">
</head>
<body>
    <div id="disconnect-banner">DISCONNECTED — reconnecting...</div>

    <!-- Header -->
    <div class="header" id="header">
        <h1>POLYBOT</h1>
        <span class="badge" id="mode-badge">--</span>
        <span class="badge badge-cancel" id="cancel-badge" style="display:none">CANCEL ONLY</span>
        <span class="badge badge-halted" id="halted-badge" style="display:none">HALTED</span>
        <div class="stat"><div class="stat-label">Uptime</div><div class="stat-value" id="uptime">--</div></div>
        <div class="stat"><div class="stat-label">Bankroll</div><div class="stat-value" id="bankroll">--</div></div>
        <div class="stat"><div class="stat-label">Daily PnL</div><div class="stat-value" id="pnl">--</div></div>
        <div class="stat"><div class="stat-label">Heartbeat</div><div class="stat-value"><span class="dot" id="hb-dot"></span></div></div>
        <div class="stat"><div class="stat-label">Wallet</div><div class="stat-value" id="wallet-balance">--</div></div>
    </div>

    <!-- Spot Prices -->
    <div class="spots" id="spots"></div>

    <div class="grid-2">
        <!-- Ladders -->
        <div class="panel">
            <div class="panel-title">Active Ladders</div>
            <table>
                <thead>
                    <tr>
                        <th>Market</th><th>TF</th><th>Resting</th><th>Filled</th>
                        <th>VWAP UP</th><th>VWAP DN</th><th>Pair Cost</th><th>Imbal</th><th>Time</th>
                    </tr>
                </thead>
                <tbody id="ladders-body"></tbody>
            </table>
        </div>

        <!-- Positions + Wallet -->
        <div>
            <div class="panel">
                <div class="panel-title">Positions</div>
                <table>
                    <thead>
                        <tr>
                            <th>Market</th><th>UP Qty</th><th>DN Qty</th>
                            <th>PnL if UP</th><th>PnL if DN</th><th>Worst</th>
                        </tr>
                    </thead>
                    <tbody id="positions-body"></tbody>
                </table>
            </div>
            <div class="panel">
                <div class="panel-title">Wallet</div>
                <div id="wallet-detail" class="text-muted">--</div>
            </div>
        </div>
    </div>

    <!-- Activity Feed -->
    <div class="panel">
        <div class="panel-title">Activity</div>
        <div id="activity-feed"></div>
    </div>

    <script src="/static/dashboard.js"></script>
</body>
</html>
```

- [ ] **Step 3: Commit**

```bash
git add polybot/web/static/
git commit -m "feat: add dashboard HTML and CSS"
```

---

### Task 6: Frontend — JavaScript WebSocket Client

**Files:**
- Create: `polybot/web/static/dashboard.js`

- [ ] **Step 1: Write `dashboard.js`**

```javascript
(function() {
    const WS_URL = `ws://${location.host}/ws`;
    let ws = null;
    let reconnectTimer = null;

    function connect() {
        ws = new WebSocket(WS_URL);
        ws.onopen = () => {
            document.getElementById('disconnect-banner').style.display = 'none';
            if (reconnectTimer) { clearInterval(reconnectTimer); reconnectTimer = null; }
        };
        ws.onmessage = (e) => update(JSON.parse(e.data));
        ws.onclose = () => {
            document.getElementById('disconnect-banner').style.display = 'block';
            if (!reconnectTimer) reconnectTimer = setInterval(connect, 3000);
        };
        ws.onerror = () => ws.close();
    }

    function fmt(n, d=2) { return n != null ? n.toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d}) : '--'; }
    function fmtPct(ratio) { return (ratio * 100).toFixed(2) + '%'; }
    function fmtTime(sec) {
        if (sec <= 0) return '--';
        const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
        return `${m}:${s.toString().padStart(2,'0')}`;
    }
    function fmtUptime(sec) {
        const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60);
        return h > 0 ? `${h}h ${m}m` : `${m}m`;
    }
    function pnlClass(v) { return v >= 0 ? 'text-green' : 'text-red'; }
    function pairClass(v) { return v < 0.92 ? 'text-green' : v < 0.95 ? 'text-yellow' : 'text-red'; }
    function imbalClass(v) { return v < 0.30 ? 'text-green' : v < 0.60 ? 'text-yellow' : 'text-red'; }
    function tfLabel(sec) { return sec >= 3600 ? (sec/3600)+'h' : (sec/60)+'m'; }

    function update(d) {
        // Header
        const badge = document.getElementById('mode-badge');
        badge.textContent = d.mode === 'dry_run' ? 'DRY RUN' : 'LIVE';
        badge.className = 'badge ' + (d.mode === 'dry_run' ? 'badge-dry' : 'badge-live');

        document.getElementById('cancel-badge').style.display = d.cancel_only_mode ? '' : 'none';
        document.getElementById('halted-badge').style.display = d.risk_halted ? '' : 'none';
        document.getElementById('uptime').textContent = fmtUptime(d.uptime_sec);
        document.getElementById('bankroll').textContent = '$' + fmt(d.bankroll);

        const pnlEl = document.getElementById('pnl');
        pnlEl.textContent = `$${d.daily_pnl >= 0 ? '+' : ''}${fmt(d.daily_pnl)} (${d.daily_pnl_pct >= 0 ? '+' : ''}${fmt(d.daily_pnl_pct)}%)`;
        pnlEl.className = 'stat-value ' + pnlClass(d.daily_pnl);

        const dot = document.getElementById('hb-dot');
        dot.className = 'dot ' + (d.heartbeat_healthy ? 'dot-green' : 'dot-red');

        document.getElementById('wallet-balance').textContent = '$' + fmt(d.wallet.usdc_balance);

        // Spots
        const spotsEl = document.getElementById('spots');
        spotsEl.innerHTML = '';
        for (const [asset, info] of Object.entries(d.spots)) {
            if (info.price <= 0) continue;
            const deltaStr = (info.delta >= 0 ? '+' : '') + fmtPct(info.delta);
            spotsEl.innerHTML += `<div class="spot-card">
                <div class="spot-asset">${asset}</div>
                <div class="spot-price">$${fmt(info.price)}</div>
                <div class="spot-delta ${pnlClass(info.delta)}">${deltaStr}</div>
            </div>`;
        }

        // Ladders
        const lb = document.getElementById('ladders-body');
        lb.innerHTML = '';
        for (const l of d.ladders) {
            lb.innerHTML += `<tr>
                <td>${l.market_id.split('_').pop()}</td>
                <td>${tfLabel(l.timeframe_sec)}</td>
                <td>${l.up_resting}/${l.dn_resting}</td>
                <td>${fmt(l.up_filled,0)}/${fmt(l.dn_filled,0)}</td>
                <td>$${fmt(l.up_vwap,3)}</td>
                <td>$${fmt(l.dn_vwap,3)}</td>
                <td class="${pairClass(l.pair_cost)}">${fmt(l.pair_cost,3)}</td>
                <td class="${imbalClass(l.imbalance)}">${fmtPct(l.imbalance)}</td>
                <td>${fmtTime(l.time_left_sec)}</td>
            </tr>`;
        }
        if (!d.ladders.length) lb.innerHTML = '<tr><td colspan="9" class="text-muted">No active ladders</td></tr>';

        // Positions
        const pb = document.getElementById('positions-body');
        pb.innerHTML = '';
        for (const p of d.positions) {
            pb.innerHTML += `<tr>
                <td>${p.asset} ${p.market_id.split('_').pop()}</td>
                <td>${fmt(p.up_qty,1)}</td>
                <td>${fmt(p.dn_qty,1)}</td>
                <td class="${pnlClass(p.pnl_if_up)}">$${fmt(p.pnl_if_up)}</td>
                <td class="${pnlClass(p.pnl_if_down)}">$${fmt(p.pnl_if_down)}</td>
                <td class="${pnlClass(p.pnl_worst_case)}">$${fmt(p.pnl_worst_case)}</td>
            </tr>`;
        }
        if (!d.positions.length) pb.innerHTML = '<tr><td colspan="6" class="text-muted">No open positions</td></tr>';

        // Wallet detail
        const w = d.wallet;
        document.getElementById('wallet-detail').innerHTML =
            `<b>${w.address}</b> &nbsp; Balance: $${fmt(w.usdc_balance)} &nbsp; Deployed: $${fmt(w.deployed)} &nbsp; Available: $${fmt(w.available)}`;

        // Activity
        const af = document.getElementById('activity-feed');
        af.innerHTML = '';
        for (const a of d.activity.slice().reverse()) {
            const ts = new Date(a.ts * 1000).toLocaleTimeString();
            const pnlStr = a.pnl != null ? `<span class="${pnlClass(a.pnl)}">$${a.pnl >= 0 ? '+' : ''}${fmt(a.pnl)}</span>` : '';
            af.innerHTML += `<div class="activity-row">
                <span class="activity-time">${ts}</span>
                <span class="activity-type">${a.type}</span>
                <span class="activity-asset">${a.asset}</span>
                <span class="activity-detail">${a.detail}</span>
                <span class="activity-pnl">${pnlStr}</span>
            </div>`;
        }
        if (!d.activity.length) af.innerHTML = '<div class="text-muted">No activity yet</div>';
    }

    // Initial load via REST, then switch to WebSocket
    fetch('/api/state').then(r => r.json()).then(update).catch(() => {});
    connect();
})();
```

- [ ] **Step 2: Commit**

```bash
git add polybot/web/static/dashboard.js
git commit -m "feat: add dashboard JavaScript with WebSocket client"
```

---

### Task 7: Integration Test + Cleanup

**Files:**
- Modify: `tests/test_web_server.py`
- Modify: any broken test imports

- [ ] **Step 1: Check for broken imports referencing `display.py`**

Run: `grep -r "from polybot.display" tests/`

If any test imports `ActivityEvent` from `display`, update to `from polybot.types import ActivityEvent`.

- [ ] **Step 2: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 3: Install new deps and full smoke test**

```bash
pip install fastapi "uvicorn[standard]"
python run_bot.py &
sleep 10
curl -s http://localhost:8080/api/state | python -m json.tool | head -30
# Open http://localhost:8080 in browser — should see live dashboard
```

Kill bot and verify it exits cleanly (no hanging uvicorn).

- [ ] **Step 4: Final commit**

```bash
git add -A
git commit -m "feat: complete web dashboard — replaces Rich terminal UI"
```
