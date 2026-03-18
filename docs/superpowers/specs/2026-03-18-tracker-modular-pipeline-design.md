# Tracker Modular Pipeline — Design Spec

**Date:** 2026-03-18
**Status:** Approved
**Goal:** Upgrade tracker.py from a single-file activity monitor into a modular data pipeline that captures everything needed to reverse-engineer, calibrate, and continuously monitor the 0x8dxd whale's Polymarket strategy.

---

## Problem Statement

The current `tracker.py` collects whale trade fills with basic enrichment (spot price snapshot, best bid/ask, strategy guess). This is insufficient for comprehensive offline analysis because:

1. **No settlement outcomes** — can't compute win rate, PnL, or ROI per trade
2. **No spot price trajectory** — only a single snapshot at poll time, not the price movement over the window
3. **No order book depth** — only best bid/ask, not the full depth or how it evolves
4. **Coarse polling (5s)** — misses rapid trade sequences
5. **`spot_delta_pct` is always 0.0** — never actually computed

## Solution

A modular pipeline under `polybot/tracker/` with 4 concurrent async collectors writing to separate CSV files, joined by `session_id` and ISO timestamps for offline analysis in pandas.

---

## Architecture

### Module Structure

```
polybot/tracker/
├── __init__.py
├── runner.py              # Main entry — launches all collectors as asyncio tasks
├── trade_poller.py        # Polls Polymarket activity API for whale fills
├── settlement_tracker.py  # Polls market outcomes after window closes
├── spot_recorder.py       # Records Binance spot prices at regular intervals
├── book_recorder.py       # Periodic REST polling — captures order book depth snapshots
├── csv_writer.py          # Shared CSV writer with session IDs and consistent timestamps
├── parsing.py             # Migrated slug parsing (parse_slug, parse_title_fallback)
├── strategy.py            # Migrated strategy classification (classify_strategy)
├── state.py               # TrackerState dataclass — shared runtime state
└── dashboard.py           # Minimal Rich status display (optional)

tracker.py                 # Thin entry point: from polybot.tracker.runner import main
```

### Data Flow

```
Binance WS → spot_recorder → SpotBuffer (in-memory ring buffer)
                                  ↓ (read by trade_poller for delta computation)
Polymarket REST → trade_poller → trades.csv
                      ↓ (populates active_markets + whale_trades in TrackerState)
              settlement_tracker → settlements.csv (after window closes)

CLOB REST → book_recorder → book_snapshots.csv (every 5s per active market)
                 ↑ (reads active_markets from TrackerState)

spot_recorder → spot_prices.csv (every 2s)
```

### Output Files

```
data/tracker/
├── trades_{YYYYMMDD}.csv
├── settlements_{YYYYMMDD}.csv
├── spot_prices_{YYYYMMDD}.csv
└── book_snapshots_{YYYYMMDD}.csv
```

---

## Data Schemas

### trades.csv

| Column | Type | Source |
|--------|------|--------|
| session_id | str | runner |
| timestamp | ISO 8601 | trade data |
| tx_hash | str | trade data |
| asset | str | slug parsing |
| timeframe | str | slug parsing |
| market_slug | str | trade data |
| side | str (UP/DOWN/EXIT_UP/EXIT_DOWN) | computed from trade side + outcome |
| outcome | str (Up/Down) | raw trade data |
| price | float | trade data |
| size_usd | float | trade data |
| size_shares | float | trade data |
| spot_price_at_fill | float | spot_recorder buffer |
| spot_1m_ago | float | spot_recorder buffer |
| spot_3m_ago | float | spot_recorder buffer |
| spot_delta_1m_pct | float | computed |
| spot_delta_3m_pct | float | computed |
| window_start_epoch | int | slug parsing |
| window_end_epoch | int | computed (start + timeframe_seconds) |
| window_elapsed_sec | int | computed |
| window_total_sec | int | TIMEFRAME_SECONDS lookup |
| window_pct_elapsed | float | computed |
| book_best_bid | float | CLOB book query |
| book_best_ask | float | CLOB book query |
| book_spread_pct | float | computed |
| strategy_guess | str | classify_strategy |

Note: Book depth at fill time can be reconstructed by joining with `book_snapshots.csv` on nearest timestamp. This avoids an extra REST call per trade in the polling hot path.

### settlements.csv

| Column | Type | Source |
|--------|------|--------|
| session_id | str | runner |
| timestamp | ISO 8601 | settlement time |
| market_slug | str | market data |
| asset | str | slug parsing |
| timeframe | str | slug parsing |
| window_start_epoch | int | slug parsing |
| window_end_epoch | int | computed |
| settled_outcome | str (UP/DOWN) | Polymarket API |
| settlement_price | float | Polymarket API |
| spot_at_open | float | TrackerState.spot_at_discovery (spot price when whale first traded this market) |
| spot_at_close | float | SpotBuffer current price at settlement time |
| spot_change_pct | float | computed |
| whale_had_position | bool | accumulated trades |
| whale_side | str | dominant side from trades |
| whale_avg_price | float | weighted average |
| whale_total_usd | float | sum of trade sizes |
| whale_pnl_usd | float | computed: qty * (1.0 - avg_price) for winners, -cost for losers |
| whale_roi_pct | float | pnl / cost |

### spot_prices.csv

| Column | Type | Source |
|--------|------|--------|
| session_id | str | runner |
| timestamp | ISO 8601 | record time |
| asset | str | Binance stream |
| price | float | Binance ticker |
| price_1m_ago | float | ring buffer |
| delta_1m_pct | float | computed |

Recorded every 2 seconds per asset.

### book_snapshots.csv

| Column | Type | Source |
|--------|------|--------|
| session_id | str | runner |
| timestamp | ISO 8601 | snapshot time |
| market_slug | str | market data |
| token_id | str | market data |
| side | str (UP/DOWN) | market data |
| best_bid | float | CLOB book |
| best_ask | float | CLOB book |
| spread_pct | float | computed |
| mid_price | float | computed |
| depth_1c_bid | float | total $ within 1c of best bid |
| depth_1c_ask | float | total $ within 1c of best ask |
| depth_5c_bid | float | total $ within 5c of best bid |
| depth_5c_ask | float | total $ within 5c of best ask |
| depth_10c_bid | float | total $ within 10c of best bid |
| depth_10c_ask | float | total $ within 10c of best ask |
| num_bid_levels | int | CLOB book |
| num_ask_levels | int | CLOB book |

Captured every 5 seconds per active market.

---

## Shared State

### TrackerState dataclass (`state.py`)

```python
@dataclass
class TrackerState:
    session_id: str
    spot_buffer: SpotBuffer
    active_markets: dict[str, dict]   # market_slug → market info
    whale_trades: dict[str, list]     # market_slug → list of trade records
    spot_at_discovery: dict[str, float]  # market_slug → spot price when market first seen
    seen_trade_keys: deque[str]       # bounded dedup buffer (maxlen=200)
```

- **SpotBuffer:** Per-asset `deque(maxlen=300)` storing `(timestamp, price)` tuples — 5 minutes of history at ~1 update/sec. Exposes `record(asset, price)` and `get_price_at(asset, seconds_ago)`.
- **active_markets:** Written by trade_poller when it sees a new market slug. Read by book_recorder (to know what to track) and settlement_tracker (to know what to watch).
- **whale_trades:** Accumulated by trade_poller. Read by settlement_tracker to compute PnL at settlement time.
- **spot_at_discovery:** Records the spot price when a market first enters `active_markets`. Used by settlement_tracker for `spot_at_open` (this is "spot at first whale trade", not necessarily window open — close enough and avoids the SpotBuffer lookback problem for long-window markets).
- **seen_trade_keys:** Bounded `deque(maxlen=200)` for dedup. The activity API returns at most 50 trades, so 200 keys is more than sufficient. Older keys are automatically evicted.
- All mutations happen in the single asyncio event loop — no locks needed.

---

## Module Details

### trade_poller.py

- Poll interval: **2 seconds** (configurable)
- Queries `https://data-api.polymarket.com/activity?user={wallet}&limit=50`
- Deduplicates by `tx_hash:outcome`
- On each new trade:
  - Parses slug via `parsing.py`
  - Looks up spot buffer for `spot_price_at_fill`, `spot_1m_ago`, `spot_3m_ago`
  - Queries CLOB REST for best bid/ask/spread (lightweight, no depth — depth comes from book_recorder)
  - Classifies strategy via `strategy.py`
  - Writes to `trades.csv`
  - Updates `active_markets` and `whale_trades` in TrackerState

### settlement_tracker.py

- Maintains a watchlist from `TrackerState.active_markets`
- After a market's `window_end_epoch` passes, polls for settlement outcome via:
  - `GET https://clob.polymarket.com/markets/{condition_id}` — check `resolved` field and `winning_token_id`
  - Fallback: `GET https://data-api.polymarket.com/events?slug={slug}` — check `outcome` field
- Retries up to 5 times with exponential backoff (settlements can lag a few seconds)
- Joins with `TrackerState.whale_trades[market_slug]` to compute:
  - `whale_avg_price` (weighted average of fill prices)
  - `whale_pnl_usd` (qty * ($1.00 - avg_price) for winning side, -cost for losing side)
  - `whale_roi_pct` (pnl / total_cost)
- `spot_at_open` from `TrackerState.spot_at_discovery[market_slug]`
- `spot_at_close` from current SpotBuffer at settlement time
- Writes to `settlements.csv`
- Removes market from `active_markets` after settlement is recorded

### spot_recorder.py

- Connects to Binance WebSocket (same pattern as current `binance_spot_ws`)
- On every ticker update: writes to `SpotBuffer.record(asset, price)`
- Every 2 seconds: writes a row per asset to `spot_prices.csv`
- `SpotBuffer.get_price_at(asset, seconds_ago)` does linear scan of deque — fast enough for <300 entries

### book_recorder.py

- Polls CLOB REST API: `GET https://clob.polymarket.com/book?token_id={token_id}` per active token
- Every 5 seconds per active market (both UP and DOWN tokens)
- Computes depth at 1c/5c/10c levels by summing order sizes within price bands from the full book response
- Stops tracking a market when it's removed from `active_markets` by settlement_tracker
- Note: REST polling chosen over CLOB WebSocket because (a) 5-second snapshot interval doesn't need real-time deltas, (b) the CLOB WS protocol requires subscription management, delta application, and sequence-based resync — unnecessary complexity for a data collection tool

### csv_writer.py

- `TrackerCSVWriter` class initialized with output directory, session_id, and date
- Methods: `write_trade(row)`, `write_settlement(row)`, `write_spot(row)`, `write_book(row)`
- Each method opens its file lazily, writes header on first row, flushes after every write
- All files are date-partitioned: `{type}_{YYYYMMDD}.csv`

### runner.py

- Generates `session_id = uuid4().hex[:12]`
- Loads `TrackerConfig` from config.py
- Creates `TrackerState` and `TrackerCSVWriter`
- Launches 4 async tasks: trade_poller, settlement_tracker, spot_recorder, book_recorder
- Optionally launches dashboard (default on, `--no-display` to skip)
- Handles SIGINT/SIGTERM: cancels all tasks, closes CSV files, prints summary

### dashboard.py

- Minimal Rich status panel showing:
  - Session ID, uptime, trades captured count
  - Active markets count
  - Spot prices (current)
  - Last 5 trades (abbreviated)
- No stats aggregation — that's for offline analysis

---

## Migration from Current tracker.py

### Code that moves:

| Current location | New location |
|------------------|--------------|
| `parse_slug()`, `parse_title_fallback()`, `_ASSET_NORMALIZE`, `TIMEFRAME_SECONDS` | `polybot/tracker/parsing.py` |
| `classify_strategy()`, `market_sides` | `polybot/tracker/strategy.py` |
| `binance_spot_ws()` | `polybot/tracker/spot_recorder.py` (enhanced with SpotBuffer) |
| `CSVLogger` | `polybot/tracker/csv_writer.py` (multi-file) |
| `poll_trades()`, `process_trade()` | `polybot/tracker/trade_poller.py` (enhanced) |
| `build_display()` | `polybot/tracker/dashboard.py` (stripped down) |
| `fetch_orderbook()` | `polybot/tracker/trade_poller.py` (enhanced with depth) |

### Code that is dropped:

- Stats aggregation (`update_stats`, `stats` dict) — done in offline analysis instead
- Full trade table in dashboard — replaced with minimal status view
- `update_stats`, `recent_trades` globals — replaced by TrackerState

### What stays unchanged:

- `polybot/config.py` — `TrackerConfig` extended with new fields; `load_config()` updated with `os.getenv()` calls for all new fields
- All bot-side code (`bot.py`, `ladder_manager.py`, etc.) — completely untouched

### Entry point:

`tracker.py` becomes:
```python
from polybot.tracker.runner import main
import asyncio
asyncio.run(main())
```

---

## Config Extensions (TrackerConfig)

New fields added to `TrackerConfig`:

```python
trade_poll_interval_sec: int = 2          # down from 5
spot_record_interval_sec: int = 2
book_snapshot_interval_sec: int = 5
settlement_retry_max: int = 5
settlement_retry_backoff_sec: float = 2.0
clob_book_poll_url: str = "https://clob.polymarket.com/book"  # REST polling, not WebSocket
```

---

## Error Handling & Supervision

Each collector runs in a `while True` loop. Error handling follows the existing tracker.py pattern:

- **Transient errors** (network timeouts, API 5xx, WebSocket disconnects): catch, log warning, sleep with backoff, retry.
- **Permanent errors** (API 4xx, malformed responses): catch, log error, skip the current item, continue loop.
- **Unexpected task exit:** `runner.py` monitors all tasks via `asyncio.Task.done()`. If any collector exits unexpectedly, runner logs a CRITICAL error and restarts the failed task (up to 3 restarts). After 3 restarts of the same collector, runner logs an error and continues without it — the other collectors keep running.
- **Graceful shutdown:** SIGINT/SIGTERM cancels all tasks, closes CSV files, prints session summary.

### Disk space note

`spot_prices.csv` generates ~172,800 rows/day (4 assets x 2s interval x 86,400s). At ~80 bytes/row that's ~14 MB/day — negligible.

---

## Implementation Priority

1. **parsing.py + strategy.py** — migrate pure functions from tracker.py (easy wins, no dependencies)
2. **state.py + csv_writer.py + runner.py skeleton** — shared infrastructure all modules depend on
3. **trade_poller.py** — core polling loop with dedup, slug parsing, strategy classification
4. **spot_recorder.py + SpotBuffer** — Binance WS + ring buffer + CSV recording
5. **settlement_tracker.py** — watches for outcomes, computes PnL (depends on all prior modules)
6. **book_recorder.py** — REST-based book depth snapshots
7. **Dashboard** — minimal status view (last priority)

---

## Testing Strategy

- Unit tests for `parsing.py` (already exist, migrate them)
- Unit tests for `strategy.py` (classify_strategy with spread detection across multiple trades in same market)
- Unit tests for `SpotBuffer` (record, get_price_at with various lookback windows, deque eviction)
- Unit tests for `settlement_tracker` PnL computation (winning side, losing side, spread pairs)
- Unit tests for `csv_writer` (header writing, flush behavior, multi-file)
- Unit tests for `book_recorder` depth computation (summing within price bands)
- Integration test: mock Polymarket API + Binance WS, run full pipeline for one market cycle, verify all 4 CSVs have correct data

### Field mapping note

`size_shares` in trades.csv maps to the raw `size` field from the Polymarket activity API response (number of outcome tokens). `size_usd` maps to `usdcSize` (or `price * size` as fallback).
