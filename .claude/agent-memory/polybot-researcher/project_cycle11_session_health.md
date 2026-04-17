---
name: Cycle 11 Session Health Snapshot
description: 77-settlement session $472->$972 (+$443, $29/hr). Paired excess drag is 70% adversely selected. One-sided 62% aligned but p=0.115. 15m margin tight (spread $6 - drag $5.35 = $0.65/trade net). BANKROLL env stale.
type: project
---

## Key metrics (2026-04-08 session, 77 settlements)
- 58% WR, $29/hr, max DD $118 (23.5%)
- Paired: spread $6.26/trade, excess drag $-2.00/trade, net $4.26/trade
- 15m excess drag nearly wipes out pair spread ($5.35 drag on $6.00 spread)
- 1h excess drag manageable ($4.63 on $12.36 spread) but high variance
- One-side-cap removal confirmed correct: one-sided now 62% aligned
- MAX_PAIR_COST=0.98 never triggers (all real PCs < 0.95)

**Why:** Routine proactive analysis. Session is healthy, no critical issues.
**How to apply:** No immediate changes. Update BANKROLL on next restart. The 15m excess drag deserves monitoring — if it worsens, consider tighter imbalance management.
