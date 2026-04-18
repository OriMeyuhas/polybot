---
name: Cycle 23 gate-miss audit — root cause of Dome PnL drag
description: Book-mid gate fires select profitable markets (+$4.36/mkt, 97.6% WR); paired-ladder fallback on gate-miss bleeds (-$4.04/mkt, 20.8% WR); threshold sweeps only re-partition the same two subsets
type: project
---

Cycle 23 researcher audit of `results/sweep/certainty_resweep_per_market.csv` at threshold 0.55:

| Subset | N | Sum PnL | $/mkt | Win rate |
|---|---:|---:|---:|---:|
| Gate fires (directional) | 207 | +$901.61 | +$4.36 | 202W/3L (97.6%) |
| Gate misses (paired fallback) | 583 | -$2354.24 | -$4.04 | 121W/462L (20.8%) |
| Total Dome t=0.55 | 790 | -$1452.63 | -$1.84 | — |

Pattern stable across all 4 thresholds (0.45/0.50/0.55/0.60) — gate-miss subset is -$3.40 to -$4.55/mkt universally. Thresholds re-partition, they do not fix.

**Why:** live bot (`polybot/strategy/ladder_manager.py` ~L1065-1097) logs the three skip reasons (certainty_too_low / spread_too_wide / missing_bid_ask) but falls through at L1167-1169 to symmetric `budget_up = budget/2; budget_dn = budget/2` paired split. On gate-miss markets, that fallback is negative-EV in 14-day Dome corpus.

**How to apply:**
1. Always decompose corpus PnL by `gate_fired` vs `gate_missed` before tuning thresholds. Threshold sweeps on pooled data are meaningless when the two subsets have opposite signs.
2. H0 (skip_on_gate_miss config flag) is the highest-ROI queued change. Phase A + Phase C (Dome bootstrap + live cross-check of 39 settlements) before shipping.
3. Live cross-check is load-bearing: live has fv_cancel + one_sided_abort guards that Dome doesn't simulate — they may already rescue gate-miss markets. If live gate-miss $/mkt ∈ [-$0.5, +$0.5] → PARK H0.
4. H0 risk: cuts trading volume to ~26% of markets. Fewer fills = thinner future signal + fewer rebate dollars. Mitigate via H1 (delayed entry) which may rescue some gate-miss markets by re-gating at t=60-180s.

Full plan: `docs/plans/new-hypotheses-2026-04-18.md`.
