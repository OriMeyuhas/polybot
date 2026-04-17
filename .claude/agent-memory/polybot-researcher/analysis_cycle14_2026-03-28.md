---
name: Cycle 14 Max Rung Price and Pair Cost Analysis
description: Live data proves 0.49 gets fills but pair_cost formula is WRONG for imbalanced positions. Real pair costs are profitable (0.46-0.89), not 1.19 as reported. Hybrid approach: max_rung=0.48 + reactive cancel.
type: project
---

## Cycle 14: Max Rung Price Optimization (2026-03-28)

### Critical Bug: pair_cost() formula is WRONG for imbalanced fills

Position.pair_cost() = `(up_cost + dn_cost) / min(up_qty, dn_qty)`.
When UP=16 and DN=40.5, it divides ALL costs by 16, inflating to 1.191.
The correct pair cost (UP VWAP + DN VWAP) is 0.689 -- very profitable.

This misleads the dashboard, the user, and the post-fill guard.

### Settlement Data Analysis (34 real settlements)

- Two-sided: 6 (18%), one-sided: 28 (82%)
- Two-sided PnL: +$38.23, one-sided PnL: +$0.40 (essentially zero)
- ALL six two-sided fills had real pair costs below 1.00 (0.46, 0.65, 0.69, 0.82, 0.89, 0.61)
- The logged pair_costs of 1.087 and 1.191 are wrong -- those are the inflated calculation

### Fee Math at Key Price Points (fee_rate=0.0156)

| max_rung | Worst pair cost | Margin/share |
|----------|----------------|-------------|
| 0.45     | 0.914          | 8.6c        |
| 0.47     | 0.955          | 4.5c        |
| 0.48     | 0.975          | 2.5c        |
| 0.49     | 0.995          | 0.5c        |

### User's live testing results

- max_rung=0.45: 0% two-sided fills (too restrictive, -$3)
- max_rung=0.48: ~20% two-sided (still too restrictive, -$10)
- max_rung=0.49: ~50% two-sided (+$24 in 10min, best)

### Recommended Approach: Hybrid

1. Set max_rung_price = 0.48 (not 0.49). Worst case: 2.5c loss, not unbounded.
2. Add reactive rung cancellation: after a fill on side A at price P, cancel side B rungs above `(0.95 - P_with_fee) / (1 + fee_rate)`
3. Fix pair_cost() to use VWAP sum, not total_cost/min_qty

**Why:** max_rung=0.49 gets the fills we need but 0.49+0.49 pair can lose money. Pure max_rung=0.47 is safe but kills fill rate (user confirmed: 0.45 got zero fills). The hybrid lets us post at 0.48 (keeping fill rate high) while reactively capping the other side after fills come in.

**How to apply:** Three changes: (1) config max_rung_price default to 0.48, (2) modify _check_pair_cost_after_fills to cancel expensive opposite-side rungs instead of entire ladder, (3) fix Position.pair_cost() formula.
