---
name: Cycle 13 Root Cause — max_rung_price kills two-sided fills
description: max_rung_price=0.45 with midpoints at 0.50 means rungs can never fill near market. Only the declining side fills (the loser). Monte Carlo shows raising to 0.49 gives 96% two-sided fills vs 12%. Top-rung guard must be removed (VWAP guard is sufficient).
type: project
---

## Cycle 13: Two-Sided Fill Root Cause (2026-03-28)

### Root Cause Chain
1. max_rung_price=0.45 caps all rung prices at $0.45
2. CLOB midpoints for binary markets sit at ~$0.50 each side
3. Fill condition: `market_price <= order_price` — midpoint (0.50) > max rung (0.45) => NO FILL
4. Only the side whose midpoint DROPS below 0.45 gets fills (the losing side)
5. Result: 87% one-sided fill rate on the losing side -> systematic losses

### Evidence
- Settlement data: Session 3 had 4/4 one-sided fills, all on DN, all losses (outcome=UP)
- Session 1 (pre-guard): 2/2 one-sided, -$27.30 total
- Monte Carlo (100 sims each):
  - mrp=0.45: 12% two-sided, net -$2.80
  - mrp=0.48: 92% two-sided, net +$15.90
  - mrp=0.49: 96% two-sided, net +$42.30 (OPTIMAL)
  - mrp=0.50: 98% two-sided, net +$9.60 (pair cost too high)

### Blocking Issue: Top-Rung Guard
- With mrp=0.49, top_pair_cost = 0.995 > max_pair_cost (0.93) -> BLOCKED
- VWAP pair cost = 0.678 -> easily passes VWAP guard at 0.93
- Top-rung guard must be removed or converted to a log-only warning
- VWAP guard is the correct profitability gate

### One-Sided Guard Assessment
- Threshold of 3.0 is appropriate: reduces avg loss from $13.65 to $1.16 (91% reduction)
- Not the root cause of poor performance — that's max_rung_price

### Proposed Changes
1. Raise max_rung_price from 0.45 to 0.49 in LadderParams
2. Remove top-rung pair cost guard (lines 307-315 in ladder_manager.py)
3. Keep VWAP pair cost guard as the sole profitability gate
4. Consider raising max_pair_cost to 0.95 to accommodate higher fill VWAPs

**Why:** max_rung_price=0.45 makes it mathematically impossible to get fills on the side with midpoint > 0.45, which is ALWAYS the winning side in a binary market. This is the #1 blocker for profitability.

**How to apply:** Change LadderParams default max_rung_price, remove top-rung guard block, keep VWAP guard.
