# PolyBot Trading Algorithm

Simple overview of how the bot makes money. Keep this file updated when the algorithm changes.

---

## What Does the Bot Trade?

The bot trades on **Polymarket crypto prediction markets** — short windows (5 min, 15 min, 1 hour) where you bet whether a coin (BTC, ETH, SOL, XRP) goes UP or DOWN. Each window has two tokens you can buy: an UP token and a DOWN token. The winning token pays $1, the losing one pays $0.

## Three Strategies (in priority order)

### 0. Early Exit (active position management)

**Idea:** Don't wait for settlement. If one side of a spread appreciated 50%+ above our entry cost, sell it early and lock in profit.

**Example:**
- Bought UP at $0.40, now trading at $0.65 → gain = 62.5% > 50% threshold
- Sell the UP side early, book $25 profit on 100 shares
- Don't wait for the window to expire

**When it fires:**
- We hold a spread position (both UP and DN)
- One side's current price is 50%+ above our average entry
- Checked every tick before looking for new trades

### 1. Spread Capture (Primary — 91% of whale's trades)

**Idea:** Sometimes UP + DOWN tokens are priced below $1 total. Buy both, guarantee profit.

**Example:**
- UP token: $0.46, DOWN token: $0.48 → total = $0.94
- Buy both → one will pay $1 → profit = $1 - $0.94 = **$0.06 per share**

**When it fires:**
- Combined price is at least 2.5% below $1.00
- No existing position on that market
- At least 10% of window elapsed (30s on 5m, 90s on 15m, 6m on 1h)
- At least 60 seconds left in the window

### 2. Directional (Momentum — secondary)

**Idea:** If the real coin price has moved since the window opened, bet on that direction continuing.

**Example:**
- BTC up 0.3% since window opened → buy the UP token at $0.46
- If UP wins → payout $1, profit = $1 - $0.46 = **$0.54 per share**

**When it fires:**
- Spot price moved at least 0.2% from window open
- At least 8 minutes into the window (let the price drift settle)
- At least 60 seconds left before close
- Token price between $0.07 and $0.93 (avoid extremes)
- No directional position already open on that market
- Only fires if spread didn't fire first

## Multi-Timeframe Trading

The bot trades across all available timeframes simultaneously:

| Timeframe | Spread Entry After | Notes |
|-----------|-------------------|-------|
| **5 min** | 30 seconds | Fastest cycle, most opportunities |
| **15 min** | 90 seconds | Medium cycle |
| **1 hour** | 6 minutes | Larger sizes per trade |

Market discovery scans Polymarket every 60 seconds and picks up all active windows across all timeframes.

## Fees

Polymarket charges: `2% x min(price, 1-price)`. The closer to $0.50, the higher the fee. The bot only enters if profit > fee.

## Position Sizing

- Each trade uses up to **10% of bankroll**
- For spreads: split 50/50 between UP and DOWN
- For directional: capped at 50% of available book depth (don't eat too much liquidity)

## Settlement

When a window expires (and no early exit happened):
1. Check which direction the coin actually moved
2. Calculate profit/loss based on held tokens
3. Update bankroll
4. Remove the position

**Profit math:**
- Holding UP and UP wins: `up_qty x (1 - avg_price) - down_cost`
- Holding DOWN and DOWN wins: `dn_qty x (1 - avg_price) - up_cost`

## Risk Controls

| Control | Rule | Default |
|---------|------|---------|
| **Daily drawdown halt** | Stop all trading if daily loss > 5% of starting bankroll | 5% |
| **Max positions** | No more than 8 open positions at once | 8 |
| **Stop-loss** | Exit directional if spot reverses 0.1% against you | 0.1% |
| **Early exit** | Sell appreciated spread side when gain > 50% of entry | 50% |
| **No-trade zone** | Don't trade in the final 60 seconds of a window | 60s |
| **Spread min elapsed** | Don't enter spreads until 10% of window has passed | 10% |
| **Directional min elapsed** | Don't take directional trades until 8 min into window | 480s |
| **Dry run** | Default ON — no real orders placed | true |

## How the Bot Runs

Four async tasks run in parallel:

1. **Binance WebSocket** — streams real-time spot prices
2. **Market Discovery** — scans Polymarket every 60s for active windows (5m, 15m, 1h)
3. **Trading Loop** — evaluates all markets every 500ms: early exit → spread → directional → settlement → stop-loss
4. **Dashboard** — Rich terminal display refreshing at 1 Hz

All logging goes to `polybot.log` so the dashboard stays clean.

## Decision Flow

```
Window opens (5m / 15m / 1h)
    |
    v
Every 500ms:
    |
    +-- Risk checks pass? (not halted, positions < 8, time OK)
    |       |
    |       v
    |   Check EARLY EXIT first (existing spread positions):
    |       - One side up 50%+ from entry?
    |       --> YES: sell that side, book profit, done
    |       |
    |       v
    |   Check SPREAD (priority 1):
    |       - 10% of window elapsed?
    |       - UP + DOWN < $0.975?
    |       - Edge > fee?
    |       --> YES: buy both sides, done for this market
    |       |
    |       v
    |   Check DIRECTIONAL (priority 2):
    |       - 8+ min elapsed?
    |       - Spot moved > 0.2%?
    |       - Price in [0.07, 0.93]?
    |       - Edge > fee?
    |       --> YES: place order
    |
    v
Window expires --> settle remaining positions, update bankroll
```

---

*Last updated: 2026-03-17 — multi-timeframe (5m/15m/1h), spread-first priority, early exit logic, based on 0x8dxd whale data analysis*
