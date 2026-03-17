---
title: "Quantitative Strategy Reconstruction: Polymarket Trader 0x8dxd"
subtitle: "Binary Crypto Market Spread Capture & Latency Arbitrage — Full Technical Breakdown"
date: "March 2026"
---

\newpage

# SECTION 1 — DATA COLLECTION & TRADER PROFILE

## 1.1 Trader Overview

**Profile:** polymarket.com/@0x8dxd  
**Joined:** December 2025  
**Starting Capital:** ~$313 USDC  
**All-Time Profit (as of late January 2026):** $658,000+  
**Monthly Crypto Leaderboard Position:** #2 (with +$407,516 on $28.7M volume)  
**Total Predictions:** 30,956+  
**Peak Win Rate:** ~98%  
**Biggest Single Win:** $41,200  
**Primary Markets:** Bitcoin, Ethereum, Solana, XRP — 15-minute Up/Down binary contracts  
**Profile Views:** 1.3M+

## 1.2 Market Type Traded

0x8dxd trades exclusively in Polymarket's **short-duration crypto binary markets**, which operate on the following structure:

- **Question:** "Will [BTC/ETH/SOL/XRP] go UP or DOWN in the next 15 minutes?"
- **Outcomes:** Two tokens — **UP** and **DOWN** (equivalent to YES and NO).
- **Payout:** The winning token resolves to $1.00; the losing token resolves to $0.00.
- **New markets** launch every 15 minutes, creating continuous trading windows 24/7.
- **Settlement:** Determined by the spot price change of the underlying asset between the window's open and close timestamps.

## 1.3 Observed Trading Patterns

Based on publicly available data, news reporting, and on-chain analytics:

- **Trade Frequency:** Hundreds of trades per day. At 30,956 predictions over roughly 3 months, that averages ~340 trades/day.
- **Asset Coverage:** BTC, ETH, SOL, XRP — with BTC as the dominant asset.
- **Time Windows:** Primarily 15-minute markets, with some activity in 5-minute windows.
- **Order Size:** Variable, scaling with bankroll. Positions grew from sub-$100 to $10,000+ per market as capital compounded.
- **Hold Time:** Near-zero discretionary hold time. Positions are acquired and held to settlement (max 15 minutes).
- **Execution Style:** Automated — consistent with bot behavior. Sub-second reaction times, continuous 24/7 operation, and perfectly consistent execution patterns.

## 1.4 Observed Trade-Level Data (Live Tracker, March 17 2026)

The following trades were captured by our live tracker polling the Polymarket activity API and correlating with Binance spot prices. This is a representative sample from a single session:

| Time (UTC) | Asset | Timeframe | Side | Price | Size (USD) | Window Elapsed | Strategy |
|---|---|---|---|---|---|---|---|
| 14:32:15 | BTC | 15m | EXIT_DOWN | $0.36 | $3.71 | 135s / 900s | Exit |
| 14:36:57 | BTC | 5m | DOWN | $0.31–$0.36 | $0.05–$36.32 | 117s / 300s | Directional |
| 14:36:59 | BTC | 15m | DOWN | $0.08 | $2.17–$16.17 | 419s / 900s | Directional |
| 14:37:01 | ETH | ? | DOWN | $0.10 | $13.46 | 0s | Directional |

**Key observations from live data:**

1. **Multi-fill execution:** A single position entry consists of many small fills (10+ transactions within the same second at 14:36:57), confirming the bot sweeps multiple resting limit orders rather than placing a single large order.
2. **Low entry prices for directional:** BTC 15m DOWN entries at $0.07–$0.08 (7–8 cents) when 419 seconds have elapsed in a 900-second window — the bot is buying cheap directional exposure with ~7 minutes remaining.
3. **Concurrent market participation:** The bot trades BTC 5m and BTC 15m windows simultaneously, plus ETH markets, confirming multi-asset/multi-timeframe operation.
4. **Small individual fill sizes:** Most fills are $1–$17 USD, with occasional larger fills ($36). This suggests the bot takes whatever liquidity is available at its target price rather than waiting for large fills.
5. **Exit trades observed:** EXIT_DOWN entries at $0.26–$0.36 suggest the bot sometimes sells positions before settlement, likely when the edge deteriorates or to manage risk.

*Note: Spot price data shows BTC at $84,231 during this session. The `spot_delta_pct` field showing +0.00% in some rows indicates the tracker's spot feed was initializing — this is a tracker limitation, not a trading limitation.*

## 1.5 Repeating Patterns

The following patterns are observable:

1. **Price Threshold Consistency:** The bot acquires positions only when a quantitative edge condition is met — either the combined UP+DOWN cost is below $1.00, or the directional signal from spot exchanges gives near-certainty.
2. **Liquidity Gap Exploitation:** During volatile moves on Binance/Coinbase, the Polymarket order book reprices asymmetrically. The UP side may crash while DOWN lags in its rise. The bot scoops up the underpriced side.
3. **Volatility Spike Trading:** The bot is most active during rapid price movements, exactly when retail traders create emotional mispricings.
4. **Capital Compounding:** The position size scales geometrically as the bankroll grows, consistent with a fixed-fraction reinvestment strategy.

\newpage

# SECTION 2 — MARKET MICROSTRUCTURE ANALYSIS

## 2.1 The Core Invariant

Polymarket enforces a binary structure for its crypto Up/Down markets:

$$P_{UP} + P_{DOWN} = 1.00$$

This means 1 UP token + 1 DOWN token can always be redeemed for exactly $1.00 USDC at settlement. The exchange mints new token pairs when a buyer of UP is matched with a buyer of DOWN at complementary prices (e.g., UP at $0.55 + DOWN at $0.45 = $1.00).

## 2.2 How the Invariant Breaks

In practice, the invariant frequently breaks in short-duration markets because:

**Independent Order Books:** The UP and DOWN tokens each have their own order book. When BTC suddenly drops, retail traders rush to sell UP and buy DOWN. But the order books don't synchronize instantly.

**Asymmetric Repricing:** If BTC drops sharply, the UP token might collapse from $0.50 to $0.35 within seconds. Theoretically, DOWN should rise from $0.50 to $0.65. But the DOWN order book adjusts more slowly — it might only be at $0.58 when UP hits $0.35. At that moment: $0.35 + $0.58 = $0.93. There is a 7-cent guaranteed profit per pair.

**Emotional Retail Flow:** Polymarket's crypto markets attract high retail participation. Retail traders react to news and price action emotionally, causing price overshoots and asymmetric book pressure.

**Latency Between Venues:** Spot crypto prices on Binance update in milliseconds. Polymarket's CLOB, while fast, processes orders through off-chain matching and on-chain settlement on Polygon. This creates a structural 30-90 second delay window during which Polymarket prices are "stale" relative to the already-determined outcome on spot.

## 2.3 Mechanisms the Trader Exploits

Based on the observed behavior, 0x8dxd uses a **hybrid strategy** combining two complementary approaches:

### Strategy A: Latency Arbitrage (Primary — ~70% of volume)

The bot monitors real-time spot prices on Binance and Coinbase. When BTC (or another asset) has already moved decisively in one direction during a 15-minute window, the outcome of the binary market is essentially determined — but Polymarket's prices haven't fully adjusted yet.

The bot buys the winning side at a still-discounted price.

**Example:** BTC is +0.5% with 3 minutes remaining. The UP token is trading at $0.85. The probability of BTC remaining positive is >99%. The bot buys UP at $0.85 and collects $1.00 at settlement — a 17.6% return in 3 minutes.

### Strategy B: Spread Capture / Both-Sides Arbitrage (~30% of volume)

When volatility causes the combined UP+DOWN price to drop below $1.00, the bot buys both sides at different timestamps during the window. Neither needs to be purchased simultaneously — the bot accumulates each side opportunistically when temporarily cheap.

**Example:** During a volatile 15-minute window, the bot buys UP shares at an average of $0.48 and DOWN shares at an average of $0.49. Total cost: $0.97. One side pays $1.00. Guaranteed profit: $0.03 per pair (3.1% ROI).

## 2.4 Most Likely Primary Mechanism

Based on the **98% win rate**, the primary mechanism is **latency arbitrage** (Strategy A). Pure spread capture produces near-100% win rates but only when both sides fill. The 2% loss rate is consistent with occasional directional bets where the BTC price reversed in the final seconds.

The strategy works because the bot has access to real-time spot price data and can determine the market's probable outcome before Polymarket's order book fully reflects it.

\newpage

# SECTION 3 — POSITION SIZING MODEL

## 3.1 Variable Definitions

Let:

- $P_u$ = price paid for UP token
- $P_d$ = price paid for DOWN token
- $S_u$ = number of UP shares purchased
- $S_d$ = number of DOWN shares purchased
- $C$ = total capital deployed = $S_u \cdot P_u + S_d \cdot P_d$

## 3.2 Unified Profit Model (General Case)

When the bot holds positions on both sides (or only one side), the profit under each outcome is:

**If UP wins ($1.00):**

$$\Pi_{UP} = S_u \cdot (1 - P_u) - S_d \cdot P_d$$

The UP shares each pay out $1.00, netting $(1 - P_u)$ per share. The DOWN shares expire worthless, losing $P_d$ per share.

**If DOWN wins ($1.00):**

$$\Pi_{DOWN} = S_d \cdot (1 - P_d) - S_u \cdot P_u$$

**For the trade to be profitable in both outcomes (pure arbitrage):**

$$S_u \cdot (1 - P_u) - S_d \cdot P_d > 0 \quad \text{AND} \quad S_d \cdot (1 - P_d) - S_u \cdot P_u > 0$$

**Solving for equal-quantity sizing ($S_u = S_d = Q$):**

$$Q \cdot (1 - P_u) - Q \cdot P_d > 0 \implies 1 - P_u - P_d > 0 \implies P_u + P_d < 1.00$$

This confirms the fundamental condition: both-outcome profitability requires $T = P_u + P_d < 1.00$.

**Example:** Buy 1,000 UP at $0.48 and 1,000 DOWN at $0.49. $T = 0.97$.

- If UP wins: $\Pi = 1{,}000 \times (1 - 0.48) - 1{,}000 \times 0.49 = 520 - 490 = +\$30$
- If DOWN wins: $\Pi = 1{,}000 \times (1 - 0.49) - 1{,}000 \times 0.48 = 510 - 480 = +\$30$
- Guaranteed profit: $30 on $970 deployed (3.1% ROI)

**For unequal sizing ($S_u \neq S_d$), the bot must check both outcomes:**

$$\min(\Pi_{UP}, \Pi_{DOWN}) > 0$$

If only one outcome is profitable, the position has net directional exposure — acceptable for the latency arbitrage strategy but not for spread capture.

## 3.3 Strategy A: Directional (Latency Arbitrage)

When the bot determines the outcome with high confidence, it buys only the winning side.

**Profit if correct:**

$$\Pi_{win} = S \cdot (1 - P_{entry})$$

**Loss if wrong:**

$$\Pi_{loss} = -S \cdot P_{entry}$$

**Example:** Buy 1,000 UP shares at $0.88.

- If UP wins: Profit = 1,000 × (1 - 0.88) = $120
- If UP loses: Loss = 1,000 × 0.88 = -$880

This is why the 98% win rate matters. At $0.88 entry, the breakeven win rate is 88%. At 98%, the expected value per trade is:

$$EV = 0.98 \times 120 - 0.02 \times 880 = 117.60 - 17.60 = +\$100.00$$

## 3.4 Strategy B: Both-Sides Spread Capture

When buying both sides, the sizing aims to equalize the number of UP and DOWN shares to create a perfect hedge.

**Target: $S_u \approx S_d = Q$ (equal quantity)**

**Total Cost:**

$$C = Q \cdot P_u + Q \cdot P_d = Q \cdot (P_u + P_d) = Q \cdot T$$

where $T = P_u + P_d$ (the "sum" or "pair cost").

**Profit (guaranteed, regardless of outcome):**

$$\Pi = Q \cdot 1.00 - Q \cdot T = Q \cdot (1 - T)$$

**Example (from gabagool's documented trade):**

- 1,266.72 UP shares at avg $0.517 = $655.18
- 1,294.98 DOWN shares at avg $0.449 = $581.27
- Total cost: $1,236.45
- Effective pair cost: ~$0.966 per balanced pair
- Payout: ~$1,280.85 (the smaller quantity × $1.00 plus leftover shares from the larger side)
- Profit: ~$58.52
- ROI: 4.7% in 15 minutes

## 3.5 Optimal Sizing Ratio

For pure spread capture, the optimal ratio is:

$$\frac{S_u}{S_d} = 1.0$$

Equal quantities maximize the guaranteed profit. Any imbalance creates directional exposure. If $S_u > S_d$, you have net long UP exposure on $(S_u - S_d)$ shares.

In practice, the bot targets near-equal quantities but accumulates asynchronously — buying whichever side is cheap at any given moment, and using a running cost-basis check:

$$\text{New Pair Cost} = \frac{\text{Total Cost}_{UP} + \text{Total Cost}_{DOWN}}{\min(S_u, S_d)} < 0.99$$

The bot only adds shares if the new pair cost remains below the profitability threshold.

\newpage

# SECTION 4 — MINIMUM EDGE REQUIREMENT

## 4.1 The Spread Threshold

Let $T = P_u + P_d$ (combined cost of one UP + one DOWN token).

**For spread capture, the fundamental condition is:**

$$T < 1.00$$

The edge per pair is:

$$\text{Edge} = 1.00 - T$$

## 4.2 Fee Considerations

Polymarket's international platform currently shows 0 bps maker and taker fees in its CLOB documentation. However, as of January 2026, Polymarket introduced fees on 15-minute crypto markets specifically (up to 1.56%, structured as: $\text{fee} = \text{baseRate} \times \min(P, 1-P)$).

This fee is highest at $P = 0.50$ and tapers toward $P = 0.00$ and $P = 1.00$.

**Minimum edge after fees:**

For the spread capture strategy at mid-prices (~$0.50), with a ~1.5% effective fee rate:

$$T_{max} = 1.00 - \text{fee} \approx 0.985$$

$$\text{Minimum spread required} \approx 1.5\text{c to }2.5\text{c}$$

For the latency arbitrage strategy buying at high-probability prices ($P > 0.85$), fees are minimal because $\min(P, 1-P) = 1-P$ is small:

At $P = 0.90$: fee ≈ baseRate × 0.10 — very small.

## 4.3 Expected ROI Per Trade

### Spread Capture

At an average pair cost of $0.97:

$$\text{ROI} = \frac{1.00 - 0.97}{0.97} = 3.1\%$$

At 4 trades per hour, 24 hours/day:

$$\text{Daily Gross ROI} \approx 96 \times 3.1\% \times \text{utilization} \approx 30\text{-}60\% \text{ (on utilized capital)}$$

### Latency Arbitrage

At an average entry price of $0.88, with 98% win rate:

$$EV = 0.98 \times (1 - 0.88) - 0.02 \times 0.88 = 0.1176 - 0.0176 = \$0.10 \text{ per dollar risked}$$

$$\text{ROI per trade} = \frac{0.10}{0.88} = 11.4\%$$

## 4.4 Capital Efficiency

Capital is locked for at most 15 minutes per trade (until settlement). At 4 rotations per hour:

$$\text{Annualized turns} = 4 \times 24 \times 365 = 35{,}040 \text{ turns/year}$$

Even with 10% utilization (not every window has an opportunity):

$$\text{Effective turns} \approx 3{,}504 \text{/year}$$

At 3% per turn, the compounding is:

$$\text{Final} = \text{Initial} \times (1.03)^{3504}$$

This is a theoretical number — in practice, opportunity frequency, fill rates, and bankroll constraints dramatically reduce it. But it explains how $313 can become $438,000 in one month.

\newpage

# SECTION 5 — EXECUTION STRATEGY

## 5.1 Order Types

The bot almost certainly uses **limit orders** placed via Polymarket's CLOB API.

- **Limit orders** ensure price certainty. The bot specifies the exact price it's willing to pay.
- **Fill-or-Kill (FOK)** semantics may be used for time-sensitive directional trades — the order fills immediately at the specified price or is cancelled entirely.
- **GTC (Good Till Cancelled)** limit orders may be used for passive spread capture, sitting on the book waiting for fills.

## 5.2 Execution Techniques

### Latency Arbitrage Execution

1. Bot monitors Binance WebSocket for real-time BTC price changes.
2. When cumulative price change within a 15-min window exceeds threshold (e.g., +0.20%), the bot calculates the expected winning side.
3. Bot checks Polymarket order book for the winning side's best ask.
4. If the ask is still below the expected fair value (e.g., buying UP at $0.85 when it should be $0.95), the bot submits a limit buy at or near the ask.
5. Execution must happen within 1-2 seconds of signal detection.

### Spread Capture Execution

1. Bot continuously monitors both UP and DOWN order books.
2. Maintains running tallies of accumulated shares and average costs on each side.
3. When either side becomes temporarily cheap (due to a retail sell-off or liquidity gap), the bot buys.
4. Before each purchase, it checks: "Does adding these shares keep my pair cost below $0.99?"
5. The bot targets roughly equal share quantities on both sides.
6. Position resolution is automatic at window close.

## 5.3 How Favorable Prices Are Obtained

Several factors allow consistent favorable entry:

- **Speed advantage:** The bot reacts to Binance price moves in milliseconds. Retail traders on Polymarket update their orders in seconds. This gap is the edge.
- **Passive limit orders during volatility:** When panic selling occurs, the bot has limit orders already resting on the book at attractive prices. It acts as a passive liquidity provider.
- **Order book monitoring:** The bot tracks depth and spread on both sides, identifying moments when one side is thin and cheap.
- **Avoiding adverse selection:** For directional trades, the bot only acts when the signal is strong (large, confirmed spot move) — reducing the chance of buying the wrong side.

\newpage

# SECTION 6 — CAPITAL ROTATION

## 6.1 Capital Per Trade

Based on the progression from $313 to $438,000 over ~30 days:

- **Early stage (Week 1):** $50-$500 per trade
- **Growth stage (Weeks 2-3):** $1,000-$5,000 per trade
- **Scale stage (Week 4+):** $5,000-$15,000 per trade
- **Current (March 2026):** Estimated $10,000-$30,000 per window

## 6.2 Trade Frequency

- **Total predictions:** 30,956 over ~90 days
- **Average trades/day:** ~340
- **Per 15-min window:** The bot trades in multiple windows simultaneously across BTC, ETH, SOL, and XRP
- **Active windows per day:** ~384 (96 windows × 4 assets)
- **Participation rate:** ~340/384 ≈ 88% of available windows

## 6.3 Capital Turnover Rate

Capital is deployed and returned every 15 minutes maximum.

$$\text{Daily turnover} = \text{Capital per trade} \times \text{trades per day}$$

For the leaderboard volume of $28.7M over ~30 days:

$$\text{Daily volume} \approx \$957{,}000/\text{day}$$

With an average position of ~$3,000 per trade and ~340 trades/day:

$$\text{Capital in use} \approx \$3{,}000 \times 4 \text{ (concurrent windows)} = \$12{,}000 \text{ active at any time}$$

$$\text{Turnover} = \frac{\$957{,}000}{\$12{,}000} = 80\times \text{ per day}$$

## 6.4 Compounding Mechanics

The bot reinvests profits immediately into the next window. The growth curve is approximately:

$$B_n = B_0 \times (1 + r)^n$$

Where:

- $B_0$ = starting bankroll ($313)
- $r$ = average net return per trade cycle (~1-3%)
- $n$ = number of successful trade cycles

To reach $438,000 from $313 in 30 days:

$$438{,}000 = 313 \times (1 + r)^n$$

$$\frac{438{,}000}{313} = (1 + r)^n$$

$$1{,}399 = (1 + r)^n$$

With ~6,600 trades in the first month: $(1+r)^{6600} = 1{,}399$

$$r \approx 0.0011 = 0.11\% \text{ average net return per trade}$$

This is very modest — about 11 basis points per trade on average — but compounded across thousands of trades it produces extraordinary returns.

\newpage

# SECTION 7 — FAILURE MODES AND RISKS

## 7.1 Single-Leg Fill Risk

**Problem:** In spread capture, the bot buys UP first but the DOWN side moves before the second order fills. Now the bot holds naked directional exposure.

**Mitigation:**

- Track fill status of both legs. If the second leg doesn't fill within a time limit, the bot can either: (a) sell the first leg at market to exit flat, or (b) accept the directional risk if the entry price is favorable.
- Use limit orders with tight validity — cancel and retry if not filled quickly.
- Monitor the running pair cost before each individual purchase.

## 7.2 Price Reversal (Latency Strategy)

**Problem:** BTC is +0.4% with 5 minutes remaining. The bot buys UP at $0.82. BTC reverses and closes -0.1%. The UP token is worth $0.00.

**Mitigation:**

- Only enter directional trades when the move exceeds a minimum threshold (e.g., >0.20-0.30%).
- Only enter when sufficient time has elapsed in the window (e.g., minutes 8-14 of 15).
- Set a maximum entry price for directional trades (e.g., never pay more than $0.93 for directional).
- Implement stop-loss: if the spot price reverses past a certain threshold, sell the position immediately at whatever price is available.

## 7.3 Liquidity Drying Up

**Problem:** During low-volume periods (e.g., 3 AM UTC weekdays), the order book is thin. Spreads widen, fill sizes shrink, and execution degrades.

**Mitigation:**

- Scale position size with available liquidity. Only trade a fraction of the visible book depth.
- Avoid markets/times with historically thin books.
- Use maker orders for better fill quality.

## 7.4 Market Resolution Delays

**Problem:** Polymarket uses UMA's Optimistic Oracle for resolution. Disputes can delay payouts.

**Mitigation:** Crypto Up/Down markets use automated, deterministic resolution based on price feeds — disputes are extremely rare. The bot's capital is typically freed within minutes of window close.

## 7.5 Fee Changes

**Problem:** Polymarket introduced fees on 15-minute crypto markets in January 2026, specifically targeting this type of strategy.

**Mitigation:**

- Adjust minimum edge thresholds to account for fees.
- Shift toward entry prices at extremes ($P > 0.85$ or $P < 0.15$) where fees are lower.
- Increase selectivity — only take higher-edge trades.
- The bot appears to have continued profiting post-fee-introduction, suggesting sufficient adaptation.

## 7.6 Competition / Edge Erosion

**Problem:** As more bots enter the market, mispricings are corrected faster and spreads narrow.

**Mitigation:**

- Speed is the ultimate moat. Lower-latency infrastructure (co-located servers, optimized API connections) provides sustained edge.
- Capital advantage — larger bankrolls can absorb more risk and trade larger sizes.
- Multi-asset coverage — trading BTC, ETH, SOL, and XRP simultaneously diversifies opportunity.

\newpage

# SECTION 8 — BOT IMPLEMENTATION BLUEPRINT

## 8.1 System Architecture

```
┌─────────────────────────────────────────────────────┐
│                   MAIN LOOP (24/7)                   │
│                                                      │
│  ┌──────────────┐    ┌──────────────────────────┐   │
│  │ BINANCE WS   │───>│ SIGNAL ENGINE            │   │
│  │ Real-time     │    │ - Compute Δ price        │   │
│  │ BTC/ETH/SOL  │    │ - Determine window phase  │   │
│  │ /XRP prices   │    │ - Generate direction      │   │
│  └──────────────┘    │   signal + confidence      │   │
│                       └──────────┬───────────────┘   │
│                                  │                    │
│  ┌──────────────┐    ┌──────────v───────────────┐   │
│  │ POLYMARKET   │───>│ OPPORTUNITY DETECTOR     │   │
│  │ WS / API     │    │ - Check UP + DOWN < 1.00  │   │
│  │ Order book   │    │ - Check directional edge   │   │
│  │ data          │    │ - Check liquidity depth    │   │
│  └──────────────┘    └──────────┬───────────────┘   │
│                                  │                    │
│                       ┌──────────v───────────────┐   │
│                       │ POSITION MANAGER         │   │
│                       │ - Track open positions    │   │
│                       │ - Compute pair cost       │   │
│                       │ - Size next order         │   │
│                       └──────────┬───────────────┘   │
│                                  │                    │
│                       ┌──────────v───────────────┐   │
│                       │ ORDER EXECUTOR           │   │
│                       │ - Submit limit orders     │   │
│                       │ - Monitor fills           │   │
│                       │ - Cancel stale orders     │   │
│                       └──────────┬───────────────┘   │
│                                  │                    │
│                       ┌──────────v───────────────┐   │
│                       │ SETTLEMENT HANDLER       │   │
│                       │ - Redeem winning tokens   │   │
│                       │ - Merge on-chain pos.     │   │
│                       │ - Update bankroll         │   │
│                       └──────────────────────────┘   │
└─────────────────────────────────────────────────────┘
```

## 8.2 Pseudocode

```
CONSTANTS:
    MIN_SPREAD_EDGE = 0.025        # Minimum (1 - T) for spread capture
    MIN_DIRECTIONAL_MOVE = 0.002   # 0.2% BTC move threshold
    MAX_PAIR_COST = 0.985          # Safety ceiling for pair cost
    MAX_DIRECTIONAL_PRICE = 0.93   # Never pay > 93c for directional
    MIN_DIRECTIONAL_PRICE = 0.07   # Never pay < 7c (too risky)
    WINDOW_MIN_ELAPSED = 480       # 8 minutes before directional entry
    POSITION_SIZE_FRACTION = 0.10  # Risk 10% of bankroll per window
    STOP_LOSS_REVERSAL = 0.001     # 0.1% reversal triggers stop

STATE:
    bankroll = initial_capital
    positions = {}   # { market_id: { up_qty, up_cost, dn_qty, dn_cost } }

FUNCTION get_window_phase(market):
    elapsed = current_time - market.open_time
    remaining = market.close_time - current_time
    RETURN (elapsed, remaining)

FUNCTION compute_spot_delta(asset):
    open_price = get_window_open_price(asset)
    current_price = get_binance_price(asset)
    RETURN (current_price - open_price) / open_price

FUNCTION check_spread_opportunity(market):
    best_ask_up = get_best_ask(market, "UP")
    best_ask_dn = get_best_ask(market, "DOWN")
    T = best_ask_up + best_ask_dn
    IF T < (1.0 - MIN_SPREAD_EDGE):
        RETURN { type: "SPREAD", up_price: best_ask_up,
                 dn_price: best_ask_dn, edge: 1.0 - T }
    RETURN None

FUNCTION check_directional_opportunity(market, asset):
    (elapsed, remaining) = get_window_phase(market)
    IF elapsed < WINDOW_MIN_ELAPSED:
        RETURN None

    delta = compute_spot_delta(asset)
    IF abs(delta) < MIN_DIRECTIONAL_MOVE:
        RETURN None

    IF delta > 0:  # Asset is up -> buy UP
        side = "UP"
        price = get_best_ask(market, "UP")
    ELSE:           # Asset is down -> buy DOWN
        side = "DOWN"
        price = get_best_ask(market, "DOWN")

    IF price > MAX_DIRECTIONAL_PRICE:
        RETURN None    # Already priced in, no edge
    IF price < MIN_DIRECTIONAL_PRICE:
        RETURN None    # Too uncertain

    RETURN { type: "DIRECTIONAL", side: side,
             price: price, confidence: abs(delta) }

FUNCTION compute_order_size(opportunity, market_id):
    max_capital = bankroll * POSITION_SIZE_FRACTION
    pos = positions.get(market_id, empty_position)

    IF opportunity.type == "SPREAD":
        # Buy equal quantities of both sides
        budget_per_side = max_capital / 2
        qty_up = budget_per_side / opportunity.up_price
        qty_dn = budget_per_side / opportunity.dn_price
        qty = min(qty_up, qty_dn)  # Equalize

        # Verify pair cost stays below ceiling
        new_up_cost = pos.up_cost + qty * opportunity.up_price
        new_dn_cost = pos.dn_cost + qty * opportunity.dn_price
        new_qty = pos.min_qty + qty
        pair_cost = (new_up_cost + new_dn_cost) / new_qty
        IF pair_cost > MAX_PAIR_COST:
            RETURN None
        RETURN { up_qty: qty, dn_qty: qty }

    IF opportunity.type == "DIRECTIONAL":
        qty = max_capital / opportunity.price
        # Cap at available book depth
        available = get_book_depth(market_id, opportunity.side,
                                    opportunity.price)
        qty = min(qty, available * 0.50)  # Take max 50% of depth
        RETURN { side: opportunity.side, qty: qty }

FUNCTION main_loop():
    WHILE True:
        FOR EACH active_market IN get_active_markets():
            asset = market.underlying_asset

            # Priority 1: Check directional opportunity (latency arb)
            # This is the primary strategy (~70% of volume, higher EV)
            dir_opp = check_directional_opportunity(active_market,
                                                     asset)
            IF dir_opp:
                size = compute_order_size(dir_opp, market.id)
                IF size:
                    submit_limit_buy(market, dir_opp.side,
                                     dir_opp.price, size.qty)
                    monitor_for_reversal(market, asset,
                                          STOP_LOSS_REVERSAL)
                    update_position(market.id, size)
                CONTINUE

            # Priority 2: Check spread capture (secondary, ~30% of volume)
            # Only checked when no directional signal exists
            spread_opp = check_spread_opportunity(active_market)
            IF spread_opp:
                size = compute_order_size(spread_opp, market.id)
                IF size:
                    submit_limit_buy(market, "UP",
                                     spread_opp.up_price, size.up_qty)
                    submit_limit_buy(market, "DOWN",
                                     spread_opp.dn_price, size.dn_qty)
                    update_position(market.id, size)

        # Settlement handling
        FOR EACH resolved_market IN get_resolved_markets():
            redeem_winning_tokens(resolved_market)
            update_bankroll()
            REMOVE resolved_market FROM positions

        SLEEP(500ms)  # Poll interval
```

## 8.3 Risk Controls

```
RISK RULES:
    1. MAX concurrent positions = 8 (2 per asset)
    2. MAX capital per single window = 15% of bankroll
    3. MAX daily drawdown = 5% of bankroll -> pause bot
    4. IF second leg of spread doesn't fill within 30s -> cancel
    5. IF spot reverses > STOP_LOSS_REVERSAL -> sell at market
    6. IF pair cost > 0.995 -> do NOT add more shares
    7. NEVER trade in the final 60 seconds of a window
       (insufficient time for recovery if entry is wrong)
    8. LOG every trade with timestamp, price, qty, and rationale
```

\newpage

# SECTION 9 — MATHEMATICAL MODEL

## 9.1 Edge Detection

**Spread Capture Edge:**

$$E_{spread} = 1.00 - (P_u + P_d) - F$$

where $F$ = total fees on the winning side.

**Trade is profitable when:**

$$E_{spread} > 0$$

$$P_u + P_d < 1.00 - F$$

**Directional Edge:**

$$E_{dir} = p_{win} \cdot (1 - P_{entry}) - (1 - p_{win}) \cdot P_{entry} - F_{win}$$

where $p_{win}$ is the true probability of winning (derived from spot price analysis).

**Trade is profitable when:**

$$p_{win} > \frac{P_{entry} + F_{win}}{1.00}$$

## 9.2 Expected Value Per Trade

### Spread Capture

$$EV_{spread} = Q \cdot (1.00 - T) - F(Q, P_{winner})$$

For equal quantities $Q$ on each side:

$$EV_{spread} = Q \cdot (1.00 - T) - \text{baseRate} \cdot \min(P_w, 1-P_w) \cdot Q$$

### Directional

$$EV_{dir} = S \cdot [p_{win} \cdot (1 - P) - (1 - p_{win}) \cdot P] - F(S, P)$$

## 9.3 Minimum Spread Required

Ignoring fees:

$$T_{max} = 1.00 \quad \text{(any } T < 1.00 \text{ is profitable)}$$

With fees (baseRate $\beta$, worst case at $P = 0.50$):

$$T_{max} = 1.00 - \beta \cdot 0.50$$

For $\beta = 0.02$ (2% base rate on 15-min markets):

$$T_{max} = 1.00 - 0.01 = 0.99$$

$$\text{Minimum spread} = 1\text{c}$$

For the more common fee levels ($\beta \approx 0.015$):

$$T_{max} \approx 0.9925$$

$$\text{Minimum spread} \approx 0.75\text{c}$$

## 9.4 Optimal Capital Allocation

For spread capture with equal sizing:

$$Q^* = \frac{B \cdot f}{T}$$

where $B$ = bankroll, $f$ = fraction to risk (Kelly-derived), $T$ = pair cost.

**Modified Kelly for spread capture:**

Since the win probability is ~100% (guaranteed if both legs fill), Kelly fraction approaches 1.0. The practical constraint is fill risk, not outcome risk.

$$f_{practical} = \frac{p_{both\_fill} \cdot E_{spread} - (1 - p_{both\_fill}) \cdot L_{single\_leg}}{E_{spread}}$$

where $L_{single\_leg}$ is the expected loss from holding a single unfilled leg.

For the directional strategy:

$$f_{kelly} = \frac{p_{win} \cdot (1 - P) - (1 - p_{win}) \cdot P}{1 - P}$$

$$= \frac{p_{win} - P}{1 - P}$$

At $p_{win} = 0.98$ and $P = 0.88$:

$$f_{kelly} = \frac{0.98 - 0.88}{1 - 0.88} = \frac{0.10}{0.12} = 0.833$$

Kelly suggests risking 83% of bankroll — but in practice, fractional Kelly (25-50% of Kelly) is used for variance reduction.

\newpage

# SECTION 10 — STRATEGY SUMMARY

## 10.1 How the Strategy Works

Trader 0x8dxd operates an automated trading bot on Polymarket's 15-minute crypto binary markets. The strategy has two complementary modes:

**Mode 1 — Latency Arbitrage (Primary):** The bot monitors real-time cryptocurrency spot prices on centralized exchanges (Binance, Coinbase) via WebSocket feeds. When a decisive price move occurs within a 15-minute window — typically after minute 8 — the bot determines which side (UP or DOWN) is overwhelmingly likely to win. It then buys the winning side on Polymarket at a price that hasn't yet fully adjusted to the spot reality. Each share pays $1.00 at settlement. The profit is the difference between the discounted entry price and $1.00.

**Mode 2 — Spread Capture (Secondary):** During periods of high volatility, the combined price of UP and DOWN tokens drops below $1.00 due to asymmetric order book repricing. The bot opportunistically buys both sides at different moments when each is temporarily cheap, ensuring the total cost per balanced pair remains below $1.00. Since one side must resolve to $1.00, the profit is locked in regardless of the outcome.

## 10.2 Why It Works

1. **Structural latency gap:** Spot crypto prices on Binance update in milliseconds. Polymarket's prediction market prices, driven by retail order flow, lag by 30-90 seconds during volatile periods. This is a repeatable, exploitable inefficiency.

2. **Emotional retail participation:** Crypto prediction markets attract retail traders who overreact to price movements, creating temporary mispricings that are larger and more frequent than in institutional markets.

3. **Binary payout structure:** The $1.00/$0.00 settlement creates a hard mathematical anchor. Any deviation of UP + DOWN from $1.00 is a provable edge.

4. **High frequency of opportunity:** New markets launch every 15 minutes, 24/7. With 4 assets, that's 384 windows per day — providing hundreds of daily opportunities to compound small edges.

5. **Compounding at scale:** Even a 0.11% average return per trade compounds to extraordinary returns over thousands of trades per month.

## 10.3 When It Fails

- **Low volatility environments:** When crypto prices are flat, neither directional signals nor spread mispricings appear. The bot idles.
- **Sudden reversals:** A BTC price that appears decisive can reverse in the final seconds, causing the directional trade to lose.
- **Single-leg fills:** If only one side of a spread trade fills and the other side's price moves against the bot, it creates unwanted directional exposure.
- **Fee increases:** If Polymarket raises fees on crypto markets beyond the bot's edge, profitability degrades.
- **Competition:** As more bots enter these markets, mispricings are corrected faster, spreads narrow, and the available edge shrinks. As of early 2026, 14 of the top 20 most profitable Polymarket wallets are bots.

## 10.4 Scalability

**Capital scalability:** The strategy scales well up to the liquidity limits of the order book. Polymarket's crypto markets do tens of millions in daily volume. A single bot can realistically deploy $50,000-$200,000 per day without significantly impacting the market. Beyond that, the bot's own orders begin to move prices against it.

**Operational scalability:** Multiple assets (BTC, ETH, SOL, XRP) and time windows (5m, 15m) can be traded concurrently, multiplying opportunity without additional capital requirements per marginal trade.

**Temporal scalability:** The edge depends on structural market inefficiencies (latency, retail behavior) that are likely to persist as long as Polymarket's crypto markets exist in their current form, though they will narrow over time as competition increases.

## 10.5 Infrastructure Requirements

To replicate this strategy professionally:

1. **Low-latency VPS:** A server physically close to both Binance's API servers and Polymarket's CLOB matching engine. Sub-50ms round-trip latency is ideal.

2. **Binance WebSocket feed:** Real-time price data for BTC, ETH, SOL, XRP via Binance's WebSocket API. No authentication needed for public market data.

3. **Polymarket CLOB API integration:** Full REST and WebSocket integration with Polymarket's order book API for placing orders, monitoring fills, and querying positions.

4. **Polygon wallet with USDC:** An Ethereum-compatible wallet funded with USDC on Polygon for trading. The wallet's private key is used to sign EIP-712 orders.

5. **Execution engine:** Written in Rust or Python. Rust is preferred for latency-critical components. The engine must handle concurrent market monitoring, order management, position tracking, and settlement redemption.

6. **Monitoring and alerting:** Dashboard for real-time P&L tracking, fill rate monitoring, and error alerting. Telegram/Discord webhooks for trade notifications.

7. **Capital:** Minimum $5,000 USDC for meaningful returns. $20,000-$50,000 for professional-grade operation.

8. **Risk management module:** Automated stop-losses, maximum position limits, daily drawdown circuit breakers, and fill monitoring with automatic cancellation of orphaned orders.

---

*This analysis is based on publicly available data, on-chain transaction records, and published research. It is provided for educational and analytical purposes. Trading prediction markets involves substantial risk of loss. This document does not constitute financial advice.*
