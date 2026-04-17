# Position Monitor UI Design

**Date:** 2026-03-27
**Goal:** Replace the Active Markets grid and Activity Feed with Option B's Position Monitor cards + Settlement History, keeping all existing header/hero/price/config infrastructure.

---

## What Stays (unchanged)

- Header (logo, status dot, Live/Offline, LADDER MM badge, Start/Stop)
- Hero metrics row (Total PnL sparkline, Trades, Pairs Completed)
- Price strips (Polymarket + Binance live prices)
- Price stale banner
- Config tab (credentials, settings)
- Toast system, modals, WebSocket/polling infra

## What Gets Replaced

### 1. Active Markets Grid → Position Cards

**Remove:** The `market-grid` section with asset headers, market-card grid, next-card sub-cards, and market detail modal.

**Replace with:** Stacked position cards, one per active market that has fills. Markets without fills show as compact single-line "scanning" rows grouped below the position cards.

**Each position card contains:**

- **Header row:** Asset+timeframe label (e.g., "BTC 15m") + countdown timer badge
- **UP/DN side boxes** (side-by-side grid):
  - Side label (UP green / DOWN red)
  - "X resting" badge (orders still on book)
  - Filled shares count
  - Average entry price
  - Total cost
- **Projection boxes** (side-by-side):
  - "If UP wins" → computed PnL (green if positive, red if negative)
  - "If DN wins" → computed PnL
  - Uses `profit_if_up` / `profit_if_down` from Position dataclass
- **Metrics bar** (4-column grid at card bottom):
  - Pair Cost
  - Imbalance
  - Budget (from ladder params)
  - Deployed (total committed cost)

**Scanning rows** (markets without fills):
- Single line: `[BTC 5m] [0.42 / 0.58] [12 rungs posted] [4:32 remaining]`
- Compact, muted styling

### 2. Activity Feed → Settlement History

**Remove:** The raw text activity feed list and the trades table.

**Replace with:** Structured Settlement History panel.

**Settlement History:**
- Panel header: "Settlement History" + running total PnL (green/red)
- Scrollable list of SETTLE events only
- Each row: `[time] [asset timeframe] [outcome (UP/DN won)] [fill counts] [PnL]`
- Grid layout: `70px | 1fr | 70px | auto`
- Max height 280px with scroll
- PnL colored green (positive) or red (negative)

**Data source:** Filter `activity_feed` for events where `kind === "SETTLE"`. Parse the `msg` field to extract asset, outcome, and fill counts. Use `pnl` field directly.

### 3. Backend Changes

**Minimal.** The backend already emits all needed data:
- `active_markets[].position` has `up_qty`, `up_avg`, `up_cost`, `dn_qty`, `dn_avg`, `dn_cost`, `pair_cost`
- `active_markets[].rungs_filled`, `rungs_total`, `imbalance`, `remaining_sec`
- `activity_feed[].kind`, `.msg`, `.pnl`, `.ts`

**One addition needed:** Add `profit_if_up` and `profit_if_down` to the position dict in `build_state_snapshot()`. These are already computed by the `Position` dataclass methods but not currently included in the snapshot.

Also add `resting_up` and `resting_dn` counts (orders still on book per side) to each market card. Currently only `rungs_filled` and `rungs_total` are included.

## Visual Design

- Dark glassmorphism: `background: rgba(12, 16, 24, 0.78)`, `border: 1px solid rgba(255,255,255,0.06)`, `border-radius: 14px`, `backdrop-filter: blur(20px)`
- Fonts: Inter for labels, JetBrains Mono for numbers (already loaded in current CSS)
- Colors: Green `#00d68f`, Red `#ff4d6a`, Blue `#5b9cf6`, Muted `#636e7e`, Secondary `#a0aab8`
- Timer badge: blue tint `rgba(91,156,246,0.12)` with `#5b9cf6` text
- Resting badge: `rgba(255,255,255,0.06)` background, `#636e7e` text
- Side boxes: `rgba(255,255,255,0.03)` background, `1px solid rgba(255,255,255,0.05)`
- Projection boxes: green tint for UP, blue tint for DN
- Metrics bar: dark cells separated by 1px gaps

## Files to Modify

| File | Change |
|---|---|
| `polybot/web/static/index.html` | Replace market-grid + activity feed sections with position-cards container + settlement-history |
| `polybot/web/static/styles.css` | Remove market-card/grid styles, add position card + settlement styles |
| `polybot/web/static/app.js` | Replace `renderMarketGrid` + `renderActivityFeed` with `renderPositionCards` + `renderSettlementHistory` |
| `polybot/bot.py` | Add `profit_if_up`, `profit_if_down`, `resting_up`, `resting_dn` to market card in `build_state_snapshot()` |

## What Gets Removed

- `_renderMarketCard()`, `_renderNextCard()`, `renderMarketGrid()` functions in app.js
- Market detail modal (HTML + JS)
- `.market-card`, `.market-grid`, `.market-slot`, `.next-card` CSS classes
- `renderActivityFeed()`, `renderTradesTable()` functions
- Trades table HTML
