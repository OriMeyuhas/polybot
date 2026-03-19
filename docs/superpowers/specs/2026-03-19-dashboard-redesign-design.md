# PolyBot Dashboard Redesign — Design Spec

## Overview

Full rebuild of the PolyBot web dashboard. The current dashboard has broken data display (no live Polymarket prices, incorrect deployed/available calculations, cumulative fill logs), no bot controls, and an outdated visual design. This spec covers the new UI, backend data changes, controls API, and settlement display fix.

## Goals

1. Show **live Polymarket UP/DN token prices** per active market
2. Accurate capital tracking — bankroll, on-orders, in-positions, available
3. Bot controls — start/stop button, bot starts paused (configurable)
4. Fill progress shown as rung counts (18/36), not raw quantities
5. Settlement activity shows both winning and losing sides
6. Futuristic visual redesign — Glass + Purple Gradient theme
7. Space Grotesk (labels) + Fira Code (data) font pairing

## Non-Goals

- Historical analytics / charting (future work)
- Parameter adjustment UI (future work — just start/stop for now)
- Mobile responsiveness (desktop-first, trading monitor use case)

---

## 1. Visual Design System

### Theme: Glass + Purple Gradient

- **Background:** `linear-gradient(160deg, #0c0c1d 0%, #1a1033 50%, #0c1a2e 100%)`
- **Panel background:** `rgba(255,255,255,0.04)` with `border: 1px solid rgba(255,255,255,0.08)`, `border-radius: 12px`
- **Text primary:** `#e2e8f0`
- **Text secondary:** `#94a3b8`
- **Text muted:** `#64748b`
- **Accent (labels, headers):** `#a78bfa` (purple)
- **Accent secondary:** `#c4b5fd` (light purple, for market names)
- **Positive / profit:** `#34d399` (green)
- **Negative / loss:** `#fb7185` (red/pink)
- **Warning:** `#fbbf24` (yellow)
- **Logo gradient:** `linear-gradient(90deg, #a78bfa, #60a5fa)`

### Fonts

- **Labels / headers:** Space Grotesk (400, 500, 700) — loaded from Google Fonts
- **Data / numbers:** Fira Code (400, 500) — loaded from Google Fonts
- **Label style:** uppercase, letter-spacing 1-2px, font-size 8-10px, color `#a78bfa`

### Component Patterns

- **Glass card:** `background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; padding: 12px;`
- **Badge:** `background: rgba(167,139,250,0.15); border: 1px solid rgba(167,139,250,0.3); color: #a78bfa; padding: 2px 8px; border-radius: 12px; font-size: 9px;`
- **Status dot:** `width: 7px; height: 7px; border-radius: 50%; box-shadow: 0 0 6px [color];`
- **Table rows:** No visible row borders; use alternating subtle background or hover highlight with `rgba(167,139,250,0.05)`

---

## 2. Dashboard Layout

### Top Bar

A single glass panel containing:

| Element | Position | Description |
|---------|----------|-------------|
| Logo "POLYBOT" | Left | Gradient text, 16px, font-weight 700 |
| Mode badge | Left, after logo | "DRY RUN" (purple) or "LIVE" (red) pill |
| Uptime | Left, after badge | "↑ 1h 20m" in muted text |
| Heartbeat dot | Right | Green pulsing dot if healthy, red if not |
| Status button | Right | Green "● RUNNING" or red "■ STOPPED" toggle |
| Stop/Start button | Right | Inverse action button |

### KPI Row

5 equal-width glass cards in a grid:

| Card | Primary Value | Secondary |
|------|--------------|-----------|
| BANKROLL | `$10,000` | — |
| DAILY PnL | `+$412.50` (green/red) | `+4.13%` |
| DEPLOYED | `$3,200` | `32% of bankroll` |
| AVAILABLE | `$6,800` | — |
| TRADES | `142` | `6 active positions` |

### Spot Prices Row

Flexible grid using `repeat(auto-fill, minmax(150px, 1fr))` — one card per configured asset (adapts if assets are added/removed):

| Left | Right |
|------|-------|
| Asset name (muted) | Price in Fira Code |
| | Delta % (green/red) |

### Main Grid (3:2 ratio)

**Left panel — Active Ladders table:**

| Column | Source | Description |
|--------|--------|-------------|
| MARKET | `{asset} {market_id_suffix}` | e.g., "BTC 1971044" |
| TF | `timeframe_sec` | "15m", "5m", "1h" |
| ASK ↑ | **NEW** `ask_up` | Live best ask for UP token from Polymarket |
| ASK ↓ | **NEW** `ask_dn` | Live best ask for DN token from Polymarket |
| FILL ↑ | `up_filled_count/up_total_rungs` | e.g., "18/36" (rung counts, not raw qty) |
| FILL ↓ | `dn_filled_count/dn_total_rungs` | e.g., "14/36" |
| PAIR$ | `pair_cost` | Color-coded: green < 0.92, yellow < 0.95, red ≥ 0.95 |
| IMBAL | `imbalance` | Color-coded: green < 30%, yellow < 60%, red ≥ 60% |
| TIME | `time_left_sec` | MM:SS countdown |

**Right panel — stacked:**

**Positions table:**

| Column | Source |
|--------|--------|
| MARKET | `{asset} {timeframe_label}` |
| QTY ↑ | `up_qty` |
| QTY ↓ | `dn_qty` |
| IF ↑ | `pnl_if_up` (green/red) |
| IF ↓ | `pnl_if_down` (green/red) |
| WORST | `pnl_worst_case` (green/red) |

**Wallet card:**

| Row | Value |
|-----|-------|
| Address | "DRY RUN" or truncated 0x address |
| Balance | `usdc_balance` — in dry run mode, clicking the value opens an inline edit field to change the demo bankroll |
| On Orders | **NEW** resting order cost |
| In Positions | **NEW** filled position cost |
| Available | `balance - on_orders - in_positions` |

**Demo balance edit (dry run only):** The Balance value in the Wallet card is clickable when in dry run mode. Clicking it replaces the number with a text input pre-filled with the current value. Press Enter or blur to submit. This calls `POST /api/set-bankroll` with `{"bankroll": 5000}`. In live mode, the value is not editable (wallet balance comes from on-chain).

### Bottom — Activity Feed

Full-width glass panel. Each row:
```
{timestamp}  {type_badge}  {asset}  {detail}  {pnl}
```

Settlement entries show both sides:
```
7:04:15  SETTLE  ETH  UP won → ↑ +$42.40 ↓ -$24.00 = net +$18.40
```

### Empty States

When sections have no data, show a centered muted message inside the panel:

| Section | Empty Message |
|---------|--------------|
| Active Ladders | "No active ladders" |
| Positions | "No open positions" |
| Activity Feed | "No activity yet" |
| Spot Prices | Cards show `$--` and `--` for delta |

---

## 3. Backend Changes

### 3.1 Live Book Prices

**Problem:** Dashboard never shows current Polymarket UP/DN token prices. The bot fetches them during post/reprice but discards them.

**Solution:** Cache latest ask prices in `LadderState` and update them opportunistically wherever best asks are already fetched (avoiding extra API calls).

**Changes to `ladder_manager.py`:**

- Add `current_ask_up: float = 0.0` and `current_ask_dn: float = 0.0` to `LadderState`
- Add `up_token_id: str = ""` and `dn_token_id: str = ""` to `LadderState` (needed to fetch prices later)
- In `post_ladder()`, store token IDs and initial best asks on `LadderState` when the ladder is created (asks are already fetched here, just cache them)
- In `reprice_if_needed()`, update `current_ask_up` / `current_ask_dn` on the LadderState (asks are already fetched here too — zero extra API calls)
- In `get_ladder_stats()`, include `ask_up` and `ask_dn` from the cached values
- Add `up_filled_count`, `dn_filled_count`, `up_total_rungs`, `dn_total_rungs` to stats

**Changes to `order_tracker.py`:**

- Add `filled_count(market_id, side)` method — count of orders where `status == "filled"` (fully filled rungs only; partial fills do not count)
- Add `total_count(market_id, side)` method — count of orders where `status in ("resting", "partial", "filled")` (active rungs, excluding cancelled orders from reprices)

### 3.2 Wallet Breakdown

**Problem:** "Deployed" showed $0 because it only counted resting orders, not filled positions.

**Note:** Bug 1 & 2 fixes from the prior session already addressed the core issue (`total_committed()` now includes position costs). The frontend change is to split the display.

**Changes to `web/server.py` (`build_state_snapshot`):**

- Compute `in_positions` = `position_manager.total_position_cost()`
- Compute `on_orders` = `ladder_manager.total_committed() - in_positions` (total_committed already includes both; subtract to get just resting cost)
- Send both separately in the wallet object instead of a single "deployed" value

### 3.3 Controls API

**Problem:** No way to start/stop the bot from the dashboard.

**Changes to `config.py`:**

- Add `start_paused: bool = True` config field (default True for dashboard use, overridable via `START_PAUSED` env var). Tests can set this to `False`.

**Changes to `bot.py`:**

- Initialize `_cancel_only_mode` from `cfg.start_paused` instead of hardcoded `False`
- Track previous `_cancel_only_mode` state to detect transitions: when `_cancel_only_mode` goes from `True` to `False` (resume), call `ladder_manager.clear_cancelled_ladders()` so fresh ladders can be posted
- Fix `_on_connection_lost()`: remove the line `self._cancel_only_mode = False` — connection loss should NOT override the user's stop intent. If the user stopped the bot via the dashboard, it stays stopped even after reconnection.

**Changes to `web/server.py`:**

- Add `POST /api/start` endpoint — sets `bot._cancel_only_mode = False`
- Add `POST /api/stop` endpoint — sets `bot._cancel_only_mode = True` and sets a `_pending_cancel_all` flag on the bot
- Add `POST /api/set-bankroll` endpoint — accepts `{"bankroll": float}`, only works in dry run mode. Updates `bot.position_manager.bankroll` and `bot.risk_manager.starting_bankroll`. Returns 403 in live mode.
- The trading loop (in `bot.py`) checks `_pending_cancel_all` at the top of each iteration and cancels all ladders if set, then clears the flag. This avoids thread-safety issues — the HTTP handler only sets a flag, the trading loop performs the actual cancellation on its own thread.

**Changes to `ladder_manager.py`:**

- Add `cancel_all_ladders()` method — iterates all ladders, calls `cancel_ladder()` for each. Does NOT remove ladder entries from `self.ladders` (they remain visible on the dashboard with 0 resting rungs).
- Add `clear_cancelled_ladders()` method — removes ladder entries where all rungs are cancelled or filled (0 resting). Called by the trading loop when `_cancel_only_mode` transitions from `True` to `False` (bot resume), so fresh ladders can be posted.

**State snapshot:**

- `cancel_only_mode` is already in the snapshot. The frontend derives the running/stopped display from `not cancel_only_mode`. No redundant `bot_running` field needed.

### 3.4 Settlement Activity Detail

**Problem:** Settlement log shows only net PnL, not the winning/losing side breakdown.

**Changes to `bot.py` `_settle_position()`:**

Compute winning proceeds and losing cost separately using these formulas:

- If outcome is UP:
  - `winning_proceeds = pos.up_qty - pos.up_cost` (UP tokens pay $1 each, minus what we paid)
  - `losing_cost = pos.dn_cost` (DN tokens are worthless, total loss)
  - `net_pnl = winning_proceeds - losing_cost`
  - Guard: if `pos.up_qty == 0`, `winning_proceeds = 0.0`
- If outcome is DOWN:
  - `winning_proceeds = pos.dn_qty - pos.dn_cost` (DN tokens pay $1 each, minus what we paid)
  - `losing_cost = pos.up_cost` (UP tokens are worthless, total loss)
  - `net_pnl = winning_proceeds - losing_cost`
  - Guard: if `pos.dn_qty == 0`, `winning_proceeds = 0.0`

**One-sided positions:** If `losing_cost == 0` (no position on the losing side), omit the losing part from the detail string.

Format activity detail as:
- Both sides: `"UP won → ↑ +$42.40 ↓ -$24.00 = net +$18.40"`
- One-sided: `"UP won → ↑ +$42.40 = net +$42.40"`

### 3.5 Trade Count

**Problem:** `bot._trade_count` exists but never sent to frontend.

**Changes to `web/server.py`:**

- Add `trade_count` field to state snapshot

### 3.6 Positions Schema Update

**Problem:** Positions table wants to show `{asset} {timeframe_label}` but the current schema has no timeframe info.

**Changes to `web/server.py` (`build_state_snapshot`):**

- Add `timeframe_sec` to positions entries (sourced from `MarketWindow.timeframe_sec` via the market_map lookup that already exists)

### 3.7 State Snapshot Schema (Updated)

New/changed fields marked with ★:

```json
{
  "mode": "dry_run | live",
  "uptime_sec": 4800.0,
  "bankroll": 10000.0,
  "daily_pnl": 412.50,
  "daily_pnl_pct": 4.13,
  "heartbeat_healthy": true,
  "cancel_only_mode": false,
  "risk_halted": false,
  "trade_count": 142,                           // ★ NEW

  "wallet": {
    "address": "DRY RUN",
    "usdc_balance": 10000.00,
    "on_orders": 1840.00,                       // ★ NEW (was "deployed")
    "in_positions": 1360.00,                    // ★ NEW
    "available": 6800.00
  },

  "spots": { "BTC": { "price": 69211.0, "delta": -0.0017 }, ... },

  "ladders": [
    {
      "market_id": "0xbtc_15m_1971044",
      "asset": "BTC",
      "timeframe_sec": 900,
      "ask_up": 0.434,                          // ★ NEW — live Polymarket best ask
      "ask_dn": 0.482,                          // ★ NEW — live Polymarket best ask
      "up_resting": 18,
      "dn_resting": 22,
      "up_filled": 289.4,
      "dn_filled": 218.7,
      "up_filled_count": 18,                    // ★ NEW — fully filled rung count
      "dn_filled_count": 14,                    // ★ NEW — fully filled rung count
      "up_total_rungs": 36,                     // ★ NEW — active rungs (excl. cancelled)
      "dn_total_rungs": 36,                     // ★ NEW — active rungs (excl. cancelled)
      "up_vwap": 0.434,
      "dn_vwap": 0.482,
      "pair_cost": 0.868,
      "imbalance": 0.12,
      "time_left_sec": 346
    }
  ],

  "positions": [
    {
      "market_id": "0xbtc_15m_1971044",
      "asset": "BTC",
      "timeframe_sec": 900,                     // ★ NEW
      "up_qty": 289.4,
      "up_cost": 125.60,
      "dn_qty": 218.7,
      "dn_cost": 105.40,
      "pnl_if_up": 52.00,
      "pnl_if_down": 38.00,
      "pnl_worst_case": 38.00
    }
  ],

  "pending_settlements": [ ... ],
  "failed_settlements": [ ... ],
  "activity": [ ... ]                           // SETTLE detail format changes
}
```

---

## 4. Frontend Implementation

### File Structure

All frontend remains in `polybot/web/static/`:

| File | Change |
|------|--------|
| `index.html` | Full rewrite — new layout structure |
| `style.css` | Full rewrite — Glass + Purple Gradient theme |
| `dashboard.js` | Full rewrite — new data bindings, controls, updated columns |

### Font Loading

```html
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
```

### WebSocket Connection

- Connect to `ws://{host}/ws`
- On disconnect: show a fixed banner "DISCONNECTED — reconnecting..." at top of page
- Auto-reconnect every 3 seconds
- On page load: also fetch `GET /api/state` as initial data (in case WebSocket takes a moment to connect)

### Controls Integration

- Start button sends `POST /api/start` via `fetch()`
- Stop button sends `POST /api/stop` via `fetch()`
- Button state derived from `cancel_only_mode` in WebSocket state (running = `!cancel_only_mode`)
- On page load, buttons reflect current state

### Data Binding Changes

| Old | New |
|-----|-----|
| `l.up_resting + "/" + l.dn_resting` | `l.up_resting + "/" + l.dn_resting` (unchanged) |
| `l.up_filled + "/" + l.dn_filled` (raw qty) | `l.up_filled_count + "/" + l.up_total_rungs` (rung progress) |
| No book prices | `l.ask_up` and `l.ask_dn` columns |
| `w.deployed` (single value) | `w.on_orders` + `w.in_positions` (split) |
| Positions MARKET: `{asset} {id}` | `{asset} {tfLabel(p.timeframe_sec)}` |
| `fills > 0` triggers ALL markets logged | Only newly filled orders logged (already fixed) |

---

## 5. Testing

- Existing 183 tests must continue to pass
- Tests that construct `Bot` or call trading loop methods should use `start_paused=False` in their config to preserve existing behavior
- New tests for:
  - `filled_count()` and `total_count()` methods on OrderTracker
  - `cancel_all_ladders()` on LadderManager
  - `/api/start`, `/api/stop`, and `/api/set-bankroll` endpoints (including 403 for set-bankroll in live mode)
  - Updated `build_state_snapshot()` fields (new wallet split, trade_count, ladder ask prices, rung counts, position timeframe_sec)
  - Settlement detail format string (both two-sided and one-sided cases)

---

## 6. Files Changed

| File | Type of Change |
|------|---------------|
| `polybot/web/static/index.html` | Full rewrite |
| `polybot/web/static/style.css` | Full rewrite |
| `polybot/web/static/dashboard.js` | Full rewrite |
| `polybot/web/server.py` | Add start/stop endpoints, update snapshot schema |
| `polybot/ladder_manager.py` | Add ask caching on LadderState, rung counts in stats, cancel_all_ladders |
| `polybot/order_tracker.py` | Add filled_count, total_count methods |
| `polybot/bot.py` | Start paused from config, _pending_cancel_all flag, settlement detail format |
| `polybot/config.py` | Add start_paused config field |
| `tests/test_order_tracker.py` | New tests for filled_count, total_count |
| `tests/test_ladder_manager.py` | New test for cancel_all_ladders |
| `tests/test_web_server.py` | Updated snapshot tests, new endpoint tests |
