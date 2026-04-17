---
name: Cycle 5 Alpha Generation Analysis
description: Whale data proves low imbalance is #1 PnL predictor. Active rebalancing and imbalance-driven tightening are the highest-impact changes.
type: project
---

## Cycle 5: Alpha Generation Deep-Dive (2026-03-28)

### Top Findings

1. **Imbalance is the strongest PnL predictor**, more than pair cost:
   - imb < 0.1: 83% WR, +$218/market
   - imb 0.1-0.3: 60% WR, +$124/market  
   - imb 0.3-0.5: 38% WR, +$20/market (break-even)
   - imb > 0.5: 32% WR, -$157/market

2. **Pair cost threshold is 0.95, not 0.90**:
   - pair < 0.85: 70% WR, +$209-370/market
   - pair 0.85-0.90: 62% WR, +$92/market
   - pair 0.90-0.95: 47% WR, +$88/market (still profitable!)
   - pair > 0.95: 34% WR, -$105/market (loss zone)

3. **Whale fills are 100% passive** (0.58 depth below ask on average)

4. **Whale trades throughout the window** (not front-loaded):
   - 0-25%: 38.4% of trades, avg price $0.47
   - 25-50%: 26.4%, avg $0.41
   - 50-75%: 22.3%, avg $0.35
   - 75-100%: 12.9%, avg $0.35
   Late fills are CHEAPER — more profitable

5. **Whale win rate is 47.6% but profitable** ($21.5K over 853 markets)
   - Edge is NOT prediction, it's payoff asymmetry
   - Avg win: $371, Avg loss: $312

6. **1h markets are most profitable per market** ($31.71 avg PnL vs $26.01 for 5m)

7. **Current bot issues**:
   - max_imbalance_ratio = 0.60 is WAY too loose (data says > 0.30 is break-even)
   - Imbalance handler only cancels heavy side, never tightens light side
   - No mid-window adjustment to attract fills on unfilled side
   - Budget skew (cheaper side gets more) is WRONG — whale doesn't do this (33.4%)

**Why:** The bot currently treats imbalance as a risk to manage (cancel at 60%), but the data shows balanced fills are the primary alpha source. The bot should actively seek balanced fills, not passively wait.

**How to apply:** Three changes: (1) tighten max_imbalance to 0.30, (2) add active rebalancing that tightens unfilled side when imbalance detected, (3) remove budget skew toward cheaper side (it's not what the whale does and not what the math supports).
