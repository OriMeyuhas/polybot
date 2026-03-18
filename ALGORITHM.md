# PolyBot Trading Algorithm

Simple overview of how the bot makes money. Keep this file updated when the algorithm changes.

---

## What Does the Bot Trade?

The bot trades on **Polymarket crypto prediction markets** — short windows (5 min, 15 min, 1 hour) where you bet whether a coin (BTC, ETH, SOL, XRP) goes UP or DOWN. Each window has two tokens you can buy: an UP token and a DOWN token. The winning token pays $1, the losing one pays $0.

## Core Strategy: Passive Limit Order Ladders

Instead of hitting the market with single orders, the bot acts as a **market maker**. It posts resting limit orders at multiple price levels on both sides of every market and waits for other traders to fill them.

### How the Ladder Works

When a new market window opens, the bot posts **16 buy orders per side** (32 total), spread $0.01 apart:

```
DOWN ladder:     UP ladder:
$0.33 × 4 qty    $0.37 × 4 qty   ← cheapest, smallest, rarely fill
$0.34 × 4 qty    $0.38 × 4 qty
$0.35 × 5 qty    $0.39 × 5 qty
  ...               ...
$0.47 × 8 qty    $0.51 × 8 qty
$0.48 × 9 qty    $0.52 × 9 qty   ← most expensive, largest, fill first
```

**Why this works:** Expensive rungs (near market) fill first and often. Cheap rungs fill later and rarely. The volume-weighted average cost ends up well below $1 combined, locking in spread profit.

### Example

- DOWN fills average at $0.42, UP fills average at $0.50
- Combined VWAP: $0.92
- **Guaranteed profit: $0.08 per share** (one side always pays $1)

## Ladder Lifecycle

```
Window opens (5m / 15m / 1h)
    |
    v
Wait for 10% of window elapsed
    |
    v
Post ladder: 16 UP rungs + 16 DN rungs ($0.01 spacing)
    |
    v
Every 500ms:
    |
    +-- Check fills (orders that disappeared from book)
    |       --> Update positions with actual filled qty/price
    |
    +-- Reprice if book moved > $0.02
    |       --> Cancel unfilled rungs, rebuild at new levels
    |
    +-- Imbalance guard:
    |       - < 30%: normal
    |       - 30-60%: monitor
    |       - > 60%: cancel heavy side, wait 30s for other side
    |
    +-- Early exit: if one side up 50%+ from entry, sell it
    |
    v
60 seconds before expiry: cancel all unfilled rungs
    |
    v
Window expires --> settle filled positions, update bankroll
```

## Imbalance Guard

The main risk: filling one side but not the other.

| Imbalance | Action |
|-----------|--------|
| < 30% | Normal — both sides filling |
| 30-60% | Monitor |
| > 60% | Cancel the heavy side's unfilled orders. Wait 30s. If lagging side doesn't catch up, accept as directional — stop-loss manages it |

## Early Exit

If one side of a spread appreciates 50%+ above our entry price, sell it and book profit early. Don't wait for settlement.

## Risk Controls

| Control | Rule | Default |
|---------|------|---------|
| **Daily drawdown halt** | Stop all trading if daily loss > 5% of starting bankroll | 5% |
| **Max positions** | No more than 8 open positions at once | 8 |
| **Pair cost guard** | Don't post ladder if combined VWAP would exceed $0.985 | $0.985 |
| **Stop-loss** | Exit one-sided position if spot reverses 0.1% | 0.1% |
| **Early exit** | Sell appreciated side when gain > 50% of entry | 50% |
| **No-trade zone** | Cancel unfilled rungs 60s before window close | 60s |
| **Reprice threshold** | Only reprice when book moves > $0.02 | $0.02 |
| **Dry run** | Default ON — no real orders placed | true |

## Configuration

| Param | Default | Purpose |
|-------|---------|---------|
| `ladder_rungs` | 16 | Orders per side |
| `ladder_spacing` | $0.01 | Gap between rungs |
| `ladder_width` | $0.15 | Distance from best ask to cheapest rung |
| `ladder_size_skew` | 2.0 | Expensive rung gets 2x the size of cheapest |
| `position_size_fraction` | 10% | Bankroll fraction per ladder |

## How the Bot Runs

Four async tasks in parallel:

1. **Binance WebSocket** — real-time spot prices
2. **Market Discovery** — scans Polymarket every 60s for active windows (5m, 15m, 1h)
3. **Trading Loop** — manages ladders: post → fill detection → reprice → imbalance → early exit → settle
4. **Dashboard** — Rich terminal display at 1 Hz

Logging goes to `polybot.log`. Dashboard shows ladder status (rungs resting/filled, VWAP, imbalance).

---

*Last updated: 2026-03-18 — replaced signal-based single-order approach with passive limit order ladder system based on 0x8dxd whale analysis*
