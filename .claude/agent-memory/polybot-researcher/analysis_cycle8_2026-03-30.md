---
name: Cycle 8 Live Paper Session Analysis
description: 2-hour paper run at $500 bankroll — actual PnL -$3.17, 12W/14L, 46.2% WR. 100% of markets hit imbalance timeout. Posted VWAP underestimates realized pair cost by $0.147. 1h windows lose money due to 0.50x budget multiplier.
type: project
---

## Cycle 8: Live Paper Session Analysis (2026-03-30)

### Session Stats (polybot.log, 20:54-23:21 UTC+2)
- 26 settlements, 12W/14L (46.2% WR)
- Final PnL: -$3.17 (bankroll $496.86 from $500.00)
- 122 total fills across 30 markets
- 24/30 markets got two-sided fills (80%)
- Avg realized pair cost: $0.892 (two-sided markets only)

### Finding 1: 100% Imbalance Timeout Rate
Every single market (30/30) hit IMBALANCE TIMEOUT within 30 seconds. The cascade:
1. Paper fill engine fills one side quickly (high prob for near-market orders)
2. ONE-SIDE CAP fires at 5+ fills with 0 on other side (46 triggers total)
3. IMBALANCE guard detects >60% imbalance, locks heavy side
4. Light side can't get repriced (656 REPRICE SKIPs, 26% of attempts)
5. After 30s timeout, bot accepts one-sided position

**Why:** The imbalance_timeout_sec=30 is far too short for the paper fill engine's tick rate. The one-fill-per-token-per-tick restriction means the light side needs multiple ticks to catch up, but the 30s timeout expires first.

**How to apply:** Either increase timeout to 90-120s OR (better) implement active rebalancing from Cycle 6 design.

### Finding 2: VWAP Guard Is Misleading (Cycle 4 Confirmed)
- Posted VWAP pair cost averages $0.745
- Realized pair cost averages $0.892
- Gap: +$0.147 (posted understates by 15 cents)
- Worst case: posted $0.659, realized $0.900 (+$0.241 gap)

The pair cost guard checks full-ladder VWAP but only top rungs fill. At $50 budget with 9 rungs per side, the bottom rungs at $0.01-$0.05 pull the VWAP down but never fill.

### Finding 3: 1h Budget Is Half of 15m (Should Be Equal or Higher)
- 15m: 10% fraction = $50/window, 10W/10L (50% WR), PnL=+$8.48
- 1h: 5% fraction = $25/window, 2W/4L (33% WR), PnL=-$11.65
- The 0.50x multiplier for 1h in get_ladder_params() is wrong
- Whale data: 1h is most profitable per market ($31.71 avg)
- 1h needs MORE budget (more rungs, more fill surface area)

### Finding 4: BTC >> ETH in This Session
- BTC: 13s, 8W/5L (62% WR), PnL=+$19.73
- ETH: 13s, 4W/9L (31% WR), PnL=-$22.90
- BTC 15m specifically: 10s, 7W/3L (70%), PnL=+$25.32
- ETH 15m specifically: 10s, 3W/7L (30%), PnL=-$16.84
- Likely noise at small sample, but ETH's lower volatility may produce tighter markets

### Finding 5: NoneType Bug on Startup
Line 170 of ladder_manager.py: `best_ask_up > 0 and best_ask_dn > 0` crashes when get_best_ask returns None (before order book is seeded). Causes the first batch of ladders to partially fail.

### Finding 6: One-Sided Initial Ladders
4 of the first 8 markets got one-sided ladders (all UP, 0 DN) because order book data was missing for DN tokens at startup. This led to 4 one-sided positions that settled as 2W/2L.

**Why:** The REPRICE SKIP cascade is the central issue. It converts what should be a temporary imbalance (paper engine fills one side faster) into a permanent one-sided position. The 30s timeout codifies this failure.

**How to apply:** The highest-impact single change is increasing imbalance_timeout_sec from 30 to 120 AND adding light-side tightening (Cycle 6 design). Second highest: fix 1h budget multiplier from 0.50 to 1.0.
