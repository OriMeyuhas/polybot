# Web Dashboard Design

**Date:** 2026-03-19
**Status:** Approved

## Purpose

Replace the Rich terminal dashboard with a browser-based live monitoring UI. The dashboard shows the same data (positions, ladders, PnL, spot prices) but in a more accessible format, with wallet balance integration for live trading.

## Architecture

```
┌─────────────┐     WebSocket (1Hz)     ┌──────────────┐
│   Bot.run()  │ ──────────────────────→ │  Browser Tab  │
│  (existing)  │                         │  (dashboard)  │
└──────┬───────┘                         └──────┬───────┘
       │                                        │
       │  shares memory                         │ HTTP GET
       ▼                                        ▼
┌─────────────┐     serves static +     ┌──────────────┐
│  FastAPI app │ ←───── REST/WS ──────→ │  /static/*.js │
│  (new task)  │                         │  /static/*.css│
└─────────────┘                         │  index.html   │
                                         └──────────────┘
```

- FastAPI runs as an async task inside `Bot.run()`, alongside heartbeat, settlement poller, etc.
- Reads bot state directly from memory — `bot.spot_prices`, `bot.ladder_manager`, `bot.position_manager`, `bot.risk_manager`.
- WebSocket endpoint pushes a JSON snapshot every 1 second.
- Static files served by FastAPI from `polybot/web/static/`.
- Uvicorn runs inside the existing asyncio event loop via `uvicorn.Server(...).serve()`.

## Tech Stack

- **Backend:** FastAPI + uvicorn (async, fits existing asyncio architecture)
- **Transport:** WebSocket for live updates at 1Hz
- **Frontend:** Vanilla JS + CSS (no framework, no build step)
- **No database** — all data read from bot memory

## API Endpoints

### WebSocket

`ws://localhost:8080/ws` — pushes full dashboard state JSON every 1 second.

### REST

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serves dashboard HTML |
| `/api/state` | GET | Full state JSON (for initial load before WS connects) |
| `/api/balance` | GET | Wallet balance + deployed capital (cached 60s) |

### WebSocket JSON Payload

```json
{
  "mode": "dry_run",
  "uptime_sec": 3600,
  "bankroll": 10000.0,
  "daily_pnl": 740.70,
  "daily_pnl_pct": 7.41,
  "heartbeat_healthy": true,
  "cancel_only_mode": false,
  "risk_halted": false,
  "wallet": {
    "address": "0x63ce...9a6a",
    "usdc_balance": 5000.0,
    "deployed": 3200.0,
    "available": 1800.0
  },
  "spots": {
    "BTC": { "price": 70500.0, "delta": 0.0008 },
    "ETH": { "price": 2185.0, "delta": -0.0003 }
  },
  "ladders": [
    {
      "market_id": "0xbtc_15m_...",
      "asset": "BTC",
      "timeframe_sec": 900,
      "up_resting": 12,
      "dn_resting": 14,
      "up_filled": 50.0,
      "dn_filled": 48.0,
      "up_vwap": 0.42,
      "dn_vwap": 0.45,
      "pair_cost": 0.87,
      "imbalance": 0.04,
      "time_left_sec": 320
    }
  ],
  "positions": [
    {
      "market_id": "0xbtc_15m_...",
      "asset": "BTC",
      "up_qty": 50.0,
      "up_cost": 21.0,
      "dn_qty": 48.0,
      "dn_cost": 21.6,
      "pnl_if_up": 29.0,
      "pnl_if_down": -21.0,
      "pnl_worst_case": -21.0
    }
  ],
  "pending_settlements": ["0xbtc_5m_..."],
  "failed_settlements": [],
  "activity": [
    {
      "ts": 1773920000,
      "type": "FILL",
      "asset": "BTC",
      "detail": "UP:50@$0.42 DN:48@$0.45 combined=$0.87",
      "pnl": null
    }
  ]
}
```

**Field conventions:**
- `delta` fields are ratios (0.0008 = +0.08%), formatted as percentages by the frontend
- Ladder field names match `get_ladder_stats()` keys: `up_resting`, `dn_resting`, `up_filled`, `dn_filled`, `up_vwap`, `dn_vwap`
- Position PnL shows both outcomes (`pnl_if_up`, `pnl_if_down`) plus `pnl_worst_case = min(pnl_if_up, pnl_if_down)`
- `time_left_sec` computed by cross-referencing `bot.active_markets` via market_id; if market expired, shows 0

## Dashboard Layout

Five panels on a single page:

### 1. Header Bar
- Mode badge (DRY RUN / LIVE)
- Cancel-only mode indicator (yellow badge when active)
- Risk halted indicator (red badge when circuit breaker tripped)
- Wallet address (truncated) + Polygonscan link
- Live USDC balance (polled every 60s; in dry-run shows initial bankroll with "simulated" label)
- Bankroll vs free balance
- Uptime, heartbeat status (green/red dot)
- Daily PnL ($ and %)

### 2. Spot Prices Row
- BTC, ETH, SOL, XRP
- Live price + window delta %
- Color-coded green (up) / red (down)

### 3. Active Ladders Table

| Market | TF | Resting UP/DN | Filled UP/DN | VWAP UP | VWAP DN | Pair Cost | Imbalance | Time Left |
|--------|----|---------------|--------------|---------|---------|-----------|-----------|-----------|

- Pair cost: green < 0.92, yellow 0.92-0.95, red > 0.95
- Imbalance: green < 30%, yellow 30-60%, red > 60%
- Time left as countdown

### 4. Positions & Wallet Panel
- Per-market: UP qty/cost, DN qty/cost, PnL if UP, PnL if DOWN, worst-case PnL
- Pending settlements with status (waiting / failed)
- Total unrealized worst-case PnL
- USDC.e balance on Polygon
- Capital deployed via `LadderManager.total_committed()` (made public)
- Capital available (balance - deployed)
- Pending redemptions count

### 5. Activity Feed
- Scrolling log of recent events (LADDER, FILL, SETTLE, CANCEL, HEARTBEAT_LOST)
- Timestamp, type, asset, detail, PnL (if applicable)
- Last 20 events (matches `bot._activity_log` deque maxlen)

## File Structure

```
polybot/
  types.py             # ActivityEvent moved here from display.py
  web/
    __init__.py
    server.py          # FastAPI app, WebSocket handler, state serializer
    static/
      index.html       # Single-page dashboard
      dashboard.js     # WebSocket client, DOM updates
      style.css        # Dashboard styling
  bot.py               # Add run_web_server() task, remove run_display()
  display.py           # DELETE (replaced by web UI)
```

**Migration:** `ActivityEvent` dataclass currently lives in `polybot/display.py`. It must be moved to `polybot/types.py` before `display.py` is deleted. Update the import in `bot.py`.

## Integration

### Uvicorn Setup
```python
config = uvicorn.Config(app, host="127.0.0.1", port=cfg.web_port, log_level="warning")
server = uvicorn.Server(config)
server.install_signal_handlers = lambda: None  # Disable — let outer loop handle Ctrl+C
await server.serve()
```

Disabling uvicorn's signal handlers is required because the bot's `asyncio.run()` already owns signal handling. Without this, Ctrl+C fails to shut down cleanly.

### Graceful Shutdown
In the `finally` block of `Bot.run()`, set `uvicorn_server.should_exit = True` before cancelling tasks. This allows uvicorn to close WebSocket connections cleanly rather than hanging.

### Thread Safety
The trading loop dispatches `ladder_manager` and `order_tracker` calls via `asyncio.to_thread()`, creating real OS threads that mutate shared state. The WebSocket serializer must not iterate those dicts concurrently.

**Solution:** The state serializer takes a snapshot using `.copy()` on all mutable dicts before iterating. This is cheap (shallow copy of dict keys) and prevents `RuntimeError: dictionary changed size during iteration`. For nested data (like `TrackedOrder` objects), the serializer reads only immutable attributes.

### Wallet Data Sources
- **Address:** In live mode, derived from `cfg.private_key` via the CLOB client. In dry-run, show "DRY RUN" placeholder.
- **USDC balance:** Polled via `clob_client.get_balance_allowance()` every 60s in `server.py`. Raw value divided by 1e6 (USDC has 6 decimals). In dry-run, shows the configured bankroll.
- **Deployed capital:** Read from `LadderManager.total_committed()` (rename `_total_committed` to public).

### Config Addition
Add `web_port: int = 8080` to `BotConfig`, loaded from `WEB_PORT` env var. Follows existing pattern for all config fields.

## Dependencies

**Added:**
- `fastapi` — web framework
- `uvicorn[standard]` — ASGI server

**Kept:**
- `rich` — still used by `polybot/tracker/dashboard.py` (separate entry point from the bot)

**Removed from bot path:**
- `polybot/display.py` deleted
- `run_display()` removed from `bot.py`
- Bot no longer imports Rich

## Error Handling

- **WebSocket disconnect:** JS shows red "DISCONNECTED" banner, auto-reconnect every 3 seconds
- **Browser before bot:** `/api/state` returns empty state, dashboard shows "Waiting for bot..."
- **Multiple tabs:** Each gets its own WebSocket connection. JSON snapshot serialized once, same bytes sent to all connections.
- **Port conflict:** Log warning with error. Configurable via `WEB_PORT` env var.
- **Uvicorn shutdown:** `server.should_exit = True` in finally block, then task cancellation.

## Security

- Localhost only for v1 (desktop browser, same machine)
- No authentication required
- No CORS configuration needed (HTML served by same origin)
- If remote access added later, auth will be added then

## Future (Out of Scope)

- Control panel (start/stop, parameter tuning) — stretch goal for later
- Remote/mobile access with authentication
- Modern frontend framework (React/Vue) — can swap in later, WebSocket API stays the same
- Historical analytics / tracker data visualization
