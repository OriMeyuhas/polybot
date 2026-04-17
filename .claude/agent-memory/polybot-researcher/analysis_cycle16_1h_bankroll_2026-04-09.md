---
name: Cycle 16 1h Bankroll Crisis
description: 1h losses are bankroll-driven not structural. 2x sizing at low bankroll amplifies bleed. Top 5 wins = 96% of 1h all-time profit.
type: project
---

## Cycle 16 Analysis: Should We Disable 1h Markets?

**Date:** 2026-04-09
**Trigger:** Bankroll at $426, 1h -$87 current session vs 15m +$20

### Root Cause: BANKROLL, NOT TIMEFRAME

The 1h degradation is entirely explained by bankroll level, not by any strategy or code change:

| Bankroll Range | 15m EV/stl | 1h EV/stl | 15m WR | 1h WR |
|---|---|---|---|---|
| <$550 | -$0.69 | -$5.09 | 42.4% | 35.8% |
| $550-700 | +$11.38 | +$16.31 | 65.7% | 68.0% |
| $700-1000 | +$19.18 | +$9.70 | 68.4% | 54.5% |
| >$1000 | +$18.77 | +$212.84 | 75.9% | 71.4% |

**Both timeframes are negative at low bankroll.** The one-sided rate jumps to 62-64% (from 25-40% at high bank) because small position sizes don't attract fills on both sides.

### Why 1h Bleeds 7x Worse at Low Bankroll

1. **2x sizing multiplier** in `config.py:239`: `position_size_fraction = base_fraction * 2.0` for 1h
2. At $426, this doubles the damage per losing one-sided settlement
3. Simulated impact: 1x sizing would halve the 1h low-bank loss ($-242 to $-484 over 95 settlements)
4. 1h one-sided losses average $10.55 vs 15m $9.46 at low bank — not huge gap, but 2x sizing makes them more frequent

### 1h Profit Concentration (Critical Insight)

Top 5 all-time 1h winners = **$2,984 of $3,117 total (95.7%)**:
- 04/07 10:00: +$1,940 (one-sided at bank $2955)
- 04/03 00:00: +$546 (paired at bank $1453)
- 04/02 13:00: +$278 (paired at bank $1051)
- All occurred at HIGH bankroll levels (>$700)

**The remaining 151 settlements contribute only $133.** 1h is a "lottery ticket" strategy that only works when bankroll supports proper position sizing.

### Statistical Significance

- Last 11 1h settlements: -$86.64 (4.8th percentile — unusual but not unprecedented)
- Last 25 1h settlements: -$23.99 (36.4th percentile — normal variance)
- Not a structural break — consistent with low-bankroll regime behavior

### Recommendation

**YES, temporarily disable 1h at current bankroll.** But the PERMANENT fix is bankroll-adaptive 1h sizing:

| Bankroll | 1h Sizing | Rationale |
|---|---|---|
| <$450 | DISABLED | Both TFs negative, capital preservation mode |
| $450-600 | 1x (not 2x) | Halves bleed, preserves lottery ticket |
| >$600 | 2x (current) | Proven profitable at this level |

**Why:** At $426, 1h contributes -$5.09/stl vs 15m -$0.69/stl. Disabling 1h removes the biggest bleeder. Freed capital may improve 15m fill rates.

**Risk:** Missing a rare 1h big winner. But big winners only occur at high bankroll anyway (all top 5 were at bank >$700).

**Priority:** HIGH — implement immediately via TRADE_1H=false, then implement adaptive sizing as permanent fix.
