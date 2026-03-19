# PolyBot Infrastructure Rebuild Design

**Date:** 2026-03-19
**Status:** Approved
**Approach:** Rebuild infrastructure, keep ladder market maker strategy

## Problem

PolyBot is non-functional due to:
1. Dry-run MockClobClient used by default — orders are fake, simulated locally with 3% random fill rate
2. Silent error swallowing throughout — market discovery, book fetches, and order placement catch exceptions and continue silently
3. Polymarket API breaking changes (WS price_change format Sept 2025, HeartBeats API Jan 2026, py-clob-client auth bugs)
4. No real-time price feeds — no Binance WS, no CLOB midpoint polling
5. On-chain redemption is a TODO (returns $0)

## Solution

Rebuild the infrastructure layer using proven patterns from the polytrader project while keeping PolyBot's tested ladder market maker strategy untouched.

## Architecture

```
polybot/
├── data/                       # NEW: Real-time data (polytrader patterns)
│   ├── price_feed.py           # Binance WS multi-asset + CoinGecko fallback
│   ├── market_ws.py            # Per-market WS order book subscriptions
│   ├── book.py                 # Order book state machine
│   ├── book_manager.py         # Multi-market book tracking
│   ├── clob_midpoints.py       # CLOB REST midpoint polling (2s interval)
│   └── gamma.py                # Gamma API market discovery
├── strategy/                   # KEPT: Ladder MM (existing code, moved)
│   ├── ladder_manager.py       # Ladder logic (unchanged)
│   ├── position_manager.py     # Position accounting (unchanged)
│   ├── order_tracker.py        # Order state tracking (unchanged)
│   └── risk_stub.py            # No-op RiskManager stub (always allows trading)
├── oms/                        # REBUILT: Order management
│   ├── order_executor.py       # Order placement (fixed for real API)
│   ├── heartbeat.py            # CLOB heartbeat with health tracking
│   └── clob_client.py          # Client wrapper (paper vs live mode)
├── web/                        # REBUILT: Web UI (from polytrader)
│   ├── server.py               # aiohttp + WebSocket broadcast
│   ├── state.py                # Shared state holder
│   └── static/
│       ├── index.html          # Full dashboard adapted for ladder MM
│       ├── app.js              # Frontend logic + WS reconnect
│       └── styles.css          # Dark-mode design
├── bot.py                      # REBUILT: Core orchestrator
├── config.py                   # UPDATED: Configuration
├── types.py                    # KEPT: Data structures
├── tick_size_cache.py          # KEPT: Tick size management
├── settlement.py               # KEPT: Resolution helpers (CLOB + Gamma)
├── redeemer.py                 # KEPT: Redemption stub (TODO for on-chain)
├── errors.py                   # UPDATED: Proper error propagation
└── utils/
    └── time_utils.py           # KEPT: Time utilities
```

## Data Layer

### Price Feeds (`data/price_feed.py`)
- `MultiAssetPriceFeed` streams BTC, ETH, SOL, XRP via Binance combined WS (`wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade`)
- CoinGecko fallback: if no Binance tick for >10s, polls CoinGecko every 2s (note: polytrader's `MultiAssetPriceFeed` does not have this — the fallback pattern must be ported from the single-asset `BinanceWSPriceFeed._fallback_poll_loop` and extended to multi-asset)
- Bootstrap: fetch initial price from Binance REST, fallback to CoinGecko
- Exponential backoff reconnect (1s to 60s) on WS disconnect
- Exposes `get_price(asset) -> Decimal` and optional `on_tick` callback

### Market WebSocket (`data/market_ws.py`, `data/book.py`, `data/book_manager.py`)
- One WS connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribes to token IDs for all active markets
- Handles message types: `book` (full snapshot), `price_change` (deltas), `best_bid_ask`, `last_trade_price`
- 10s ping/pong keepalive
- Exponential backoff reconnect
- Stale detection: flags books not updated in 30s
- `BookManager` routes messages to per-token `OrderBook` objects

### CLOB Midpoint Polling (`data/clob_midpoints.py`)
- REST poll `POST /midpoints` every 2s with active token IDs
- Canonical midpoints for up/down binary option pricing
- Dynamic token registration as markets are discovered/expire

### Market Discovery (`data/gamma.py`)
- Polls `https://gamma-api.polymarket.com/events` for crypto up/down markets
- Extracts `condition_id`, token IDs, `price_to_beat`, timing metadata
- Errors propagate up instead of being silently swallowed
- **MarketInfo to MarketWindow mapping:** The Gamma API returns `MarketInfo` objects (polytrader format) which must be mapped to `MarketWindow` objects (PolyBot strategy format):
  - `MarketWindow.up_token_id` / `dn_token_id` ← from `MarketInfo.clob_token_ids` matched by outcome labels
  - `MarketWindow.condition_id` ← from `MarketInfo.condition_id`
  - `MarketWindow.open_epoch` / `close_epoch` ← parsed from `MarketInfo.event_start_iso` and end time
  - `MarketWindow.timeframe_sec` ← computed from `close_epoch - open_epoch`
  - `MarketWindow.market_id` ← from Gamma event slug (note: `MarketWindow` has no `slug` field; the slug maps to `market_id`)
  - This mapping lives in `data/gamma.py` as a `to_market_window()` function

### Data Flow
```
Binance WS ──> MultiAssetPriceFeed._prices[asset] (spot prices)
Polymarket WS ──> BookManager._books[token_id] (bid/ask/mid)
CLOB REST poll ──> ClobMidpointPoller._midpoints[token_id]
Gamma API poll ──> active MarketInfo list
         |
         v
    bot.py combines all -> pushes to GuiStateHolder -> broadcasts to web UI
```

## Order Management System

### Client Wrapper (`oms/clob_client.py`)
- **Paper mode:** Uses real py-clob-client for all read operations (order books, tick sizes, market data) but intercepts write operations (post_order, cancel) and simulates them locally. Paper mode uses real market data.
- **Live mode:** Passes everything through to real py-clob-client with proper L1/L2 authentication.
- Both modes expose the same interface so the strategy layer is mode-agnostic.
- Typed exceptions for CLOB API errors: 429 (rate limit), 503 (cancel-only), 401 (auth failure). Never silently swallowed.

### Order Executor (`oms/order_executor.py`)
- Same interface the ladder strategy calls: `place_limit_buy`, `place_batch_limit_buys`
- Errors bubble up to bot orchestrator instead of returning empty lists
- Batch orders use CLOB batch endpoint (up to 15 per request)
- Tick size validation before submission

### Heartbeat (`oms/heartbeat.py`)
- Posts heartbeat to CLOB every 5s
- After 2 consecutive failures: `healthy = False`
- Bot checks health before placing orders
- Auto-recovers when heartbeat succeeds again

### Paper Mode Fill Simulation
- Resting orders tracked in memory inside the paper client wrapper (`oms/clob_client.py`)
- Paper client holds a reference to `BookManager` (injected at construction)
- On each main loop iteration (500ms), the bot calls `paper_client.tick()` which:
  - For each resting buy order: checks if `BookManager.get_book(token_id).best_ask <= order.price` — if so, fills
  - For each resting sell order: checks if `BookManager.get_book(token_id).best_bid >= order.price` — if so, fills
  - Fills use available depth from the real book: fill size = min(order.remaining, depth at price level)
  - Partial fills are supported — order remains resting with reduced remaining size
- More realistic than the current 3% random fill rate

### Sync/Async Interface Contract
The strategy layer (`LadderManager`, `PositionManager`, `OrderTracker`) is entirely synchronous. The rebuilt `OrderExecutor` maintains **synchronous public methods** (`place_limit_buy`, `place_batch_limit_buys`, `get_best_ask`, etc.). The bot orchestrator dispatches strategy calls via `asyncio.to_thread()` as the current code already does. This preserves the "unchanged strategy code" guarantee.

### Risk Stub (`strategy/risk_stub.py`)
The `LadderManager` constructor requires a `RiskManager` parameter and calls `self.risk.is_halted()` and `self.risk.can_open_position()`. Since risk management is out of scope for now, a no-op stub is provided:
- `is_halted() -> False` (always allows trading)
- `can_open_position(current_count: int) -> True` (ignores parameter, always allows new positions)
This is injected into `LadderManager` in place of the real `RiskManager`. When risk management is added later, the stub is swapped for the real implementation.

## Web UI

### Backend (`web/server.py`, `web/state.py`)
- Switching from current FastAPI/uvicorn to aiohttp (matching polytrader's pattern) for simpler WebSocket integration
- aiohttp server on port 8080
- `GET /api/state` for REST fallback
- `WebSocket /ws` for live state push
- `POST /api/start` — start trading loop
- `POST /api/stop` — stop trading loop
- `POST /api/set-bankroll` — update bankroll (paper mode)
- `GET /api/balance` — current balance info
- `GuiStateHolder` triggers broadcast on state update
- 20s ping/pong keepalive
- Decimal to float serialization

### Frontend — Kept from Polytrader
- Dark-mode design with CSS variables and animated gradients
- Hero metrics: Total PnL, trade count, position count, runtime, markets active, win rate
- PnL sparkline chart (last 60 values)
- Price strips: CLOB midpoints + Binance spot for BTC/ETH/SOL/XRP
- Full market grid with cards per market:
  - Market name + timeframe badge (5m/15m/1h)
  - Up/Down prices from CLOB midpoints + book data
  - Countdown timer to resolution
  - Position info (quantity, cost, unrealized PnL)
  - State visual (scanning/active/expiring)
- Activity feed with recent events
- WebSocket auto-reconnect (3s delay), fallback to 2s polling

### Frontend — Adapted for Ladder MM
- Market cards show ladder info instead of certainty badges:
  - Active rungs count (e.g., "24/36 rungs filled")
  - Spread width and current ladder position
  - Imbalance indicator
- Trades table: rung price, side (Up/Down), fill size, pair cost
- Hero metrics: total pairs completed, average pair cost, imbalance ratio

### Frontend — Removed
- Risk management controls (kill switch, circuit breaker, daily loss limit)
- Trade option controls (no manual buy/sell)
- Config panel sliders for strategy parameters
- Auto-optimizer section

### Control Bar
- Mode indicator (Paper / Live)
- Start / Stop buttons
- Connection status indicator

### Web API Contract
REST endpoints backing the UI controls:
- `GET /api/state` — full state snapshot (JSON), used as polling fallback
- `POST /api/start` — start the trading loop
- `POST /api/stop` — stop the trading loop (cancels resting orders in live mode)
- `POST /api/set-bankroll` — update bankroll amount (paper mode only)
- `GET /api/balance` — current balance info
- `WebSocket /ws` — live state push (JSON), same payload as `/api/state` but pushed on every state change

The `/api/state` and WS payloads include:
- `mode`: "dry_run" or "live" (matches current convention)
- `running`: boolean (trading loop active)
- `heartbeat_healthy`: boolean (CLOB connection healthy)
- `cancel_only_mode`: boolean (CLOB in cancel-only state)
- `total_pnl`, `realized_pnl`, `unrealized_pnl`: floats
- `trade_count`, `position_count`, `pairs_completed`: ints
- `avg_pair_cost`, `imbalance_ratio`: floats
- `runtime_sec`: int
- `markets_active`: int
- `win_rate`: float
- `prices`: dict of asset -> CLOB midpoint prices
- `binance_prices`: dict of asset -> Binance spot prices
- `spots`: dict of asset -> latest spot prices (Binance primary, CoinGecko fallback)
- `active_markets`: list of market objects (market_id, label, timeframe, up/down prices, ladder state, position info, timer)
- `activity_feed`: list of recent events
- `trades`: list of recent trade records
- `pending_settlements`: list of markets awaiting resolution
- `wallet`: wallet address (live mode) or null (paper mode)

Note: static files are renamed from current `dashboard.js`/`style.css` to `app.js`/`styles.css` to match polytrader convention.

## Settlement

### Resolution (`settlement.py` — kept as-is)
- When a market window expires, the bot marks it as `pending_settlement`
- Settlement poller runs in the bot orchestrator, queries resolution every 30s
- Resolution checks CLOB API first (`GET /markets/{condition_id}`), then Gamma API as fallback
- On resolution: computes PnL, updates bankroll, queues redemption
- If not resolved after 4 hours: marked as failed settlement
- In paper mode: auto-resolves based on spot price delta (UP if current > open, DOWN otherwise)

### Redemption (`redeemer.py` — kept as-is, remains TODO)
- Redemption stub exists with retry logic (exponential backoff, max 10 attempts)
- Currently returns $0 (on-chain call not implemented)
- This is explicitly out of scope for now

### Daily PnL Reset
- Runs in the bot orchestrator, resets daily counters at midnight
- Kept from current bot.py logic

## File Reorganization and Import Paths

Strategy files move from `polybot/` to `polybot/strategy/` and OMS files move to `polybot/oms/`. This changes import paths. To maintain compatibility:
- `polybot/__init__.py` re-exports key classes so old import paths continue working:
  - `from polybot.ladder_manager import LadderManager` still works (re-exported from `polybot.strategy.ladder_manager`)
  - `from polybot.order_executor import OrderExecutor` still works (re-exported from `polybot.oms.order_executor`)
- Strategy file internals (e.g., `from polybot.config import BotConfig`) remain valid since config stays at `polybot/config.py`
- New code uses the full paths (`from polybot.strategy.ladder_manager import ...`)

### Dropped Subpackages
- `polybot/tracker/` — data collection/analytics pipeline. Not part of this rebuild. Files remain in place but are not used by the rebuilt bot. Can be removed in a future cleanup pass.

## Bot Orchestrator

### Startup Sequence
1. Load config (env vars + defaults)
2. Initialize CLOB client wrapper (paper or live)
3. Start data layer concurrently: `MultiAssetPriceFeed`, `ClobMidpointPoller`, `BookManager` + `MarketWSClient`
4. Start `Heartbeat` (live mode only)
5. Start web UI server
6. Run market discovery
7. Begin main trading loop

### Main Trading Loop (500ms)
1. Check heartbeat health (skip trading if unhealthy)
2. Run market discovery if interval elapsed (60s)
3. For each active market:
   - Get book state from `BookManager`
   - Get spot price from `MultiAssetPriceFeed`
   - Pass to `LadderManager` for ladder decisions
   - Execute orders via `OrderExecutor`
4. Check for expired markets, handle settlement
5. Update `GuiStateHolder` -> WS broadcast

### Error Handling
- Data layer errors: logged + surfaced to UI as warnings, trading pauses for affected markets only
- Order errors (429, 503, auth): typed exceptions, bot reacts (back off, cancel-only, stop)
- Market discovery errors: logged + UI warning, continues with existing list but user sees staleness
- No silent swallowing. Every error handled with recovery action or surfaced to user.

### Graceful Shutdown
- Cancel all resting orders (live mode)
- Close WS connections
- Stop web server
- Log final state

## Configuration

### Kept (Ladder Strategy Params)
- `ladder_rungs`, `ladder_spacing`, `ladder_width`, `ladder_size_skew`
- `max_pair_cost`, `position_size_fraction`, `batch_order_size`
- `reprice_threshold`, `poll_interval_ms`
- `max_concurrent_markets`, `max_concurrent_positions`, `bankroll`
- `max_imbalance_ratio`, `imbalance_timeout_sec`
- `no_trade_final_sec`, `max_daily_drawdown_pct`

### Added (Data Layer)
- `binance_ws_url` — default `wss://stream.binance.com:9443/ws` (env overridable)
- `binance_fallback_interval_sec` — CoinGecko poll interval (default 2)
- `clob_midpoint_poll_sec` — CLOB midpoint REST poll (default 2)
- `market_ws_ping_sec` — Polymarket WS keepalive (default 10)
- `book_stale_sec` — stale book threshold (default 30)
- `market_discovery_interval_sec` — Gamma API poll (default 60)

### Added (Web UI)
- `web_port` — default 8080

### Mode Switching
- `dry_run: bool` — paper mode if True or if no PRIVATE_KEY set
- Credentials from env: `PRIVATE_KEY`, `API_KEY`, `API_SECRET`, `API_PASSPHRASE`
- All settings overridable via environment with sensible defaults

## Out of Scope
- Risk management (kill switch, circuit breaker, daily loss limits)
- Trade option controls in UI
- Whale arb strategy, fair value pricing, volatility estimator
- On-chain redemption (remains TODO for now)
- Auto-optimizer
