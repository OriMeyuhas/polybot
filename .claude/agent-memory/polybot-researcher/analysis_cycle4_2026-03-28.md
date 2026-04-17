---
name: Cycle 4 Profitability Analysis
description: Root cause of one-sided fills and negative PnL — top-rung pair cost exceeds 1.0 when only expensive rungs fill. Whale data shows pair cost < 0.85 is sweet spot.
type: project
---

## Cycle 4: Algorithm & Profitability Deep-Dive (2026-03-28)

### Root Cause: Top-Rung Pair Cost > 1.0

The pair cost guard checks ladder VWAP (average across ALL rungs), but only the top 1-2 rungs (nearest to market = most expensive) actually fill in paper mode or real markets. Example from run30m_v3.log:
- Ladder VWAP: $0.882 (passes guard)  
- Top rung fill: UP=$0.50 + DN=$0.51 = $1.01 (LOSS per share)
- 4 out of 5 settled markets had realized pair cost > $1.00

At $100 bankroll (5m, 15% fraction = $3.30 budget):
- Only 3 rungs per side fit
- Top rung pair cost: $0.954 at UP=0.50/DN=0.52
- Bottom rung pair cost: $0.626 (never fills — midpoint won't move 20c in 5m)

At $1000 bankroll: 22 rungs per side, top rung pair cost $0.853 — profitable.

### Whale Data Findings (4 days, 111K trades, 921 markets)
- 95.4% of markets traded both sides
- Pair cost distribution: mean=0.918, p50=0.926
- PnL by pair cost bucket:
  - <0.80: 64% win rate, avg +$232/market
  - 0.80-0.85: 65% win rate, avg +$166/market
  - 0.85-0.90: 60% win rate, avg +$75/market
  - 0.90-0.95: 47% win rate, avg +$60/market (break-even zone)
  - 0.95-1.00: 35% win rate, avg -$98/market (loss zone)
  - >1.00: 32% win rate, avg -$134/market
- Whale fills 96 trades/market (p50), gets fills across full price range
- Our bot: 2-4 fills/market, only top rungs

### Key Insight
The problem is NOT the fill simulation being unrealistic. The problem is structural:
small budgets produce few rungs, and only the most expensive rungs (nearest market) fill.
The pair cost guard gives false confidence by averaging in cheap rungs that never fill.

**Why:** The VWAP-based pair cost guard was designed for large portfolios where many rungs fill. At micro/small bankroll, it's misleading.

**How to apply:** Add a top-rung pair cost check. Consider `best_ask_up + best_ask_dn` as the "worst-case pair cost" and gate on that.
