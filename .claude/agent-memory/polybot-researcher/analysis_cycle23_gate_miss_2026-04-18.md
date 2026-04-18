---
name: Cycle 23 Gate-Miss Audit 2026-04-18
description: Audit of certainty_resweep CSV uncovered that gate-miss paired fallback loses -$4.04/mkt (-$2354 on 583 markets) while gate-fires earn +$4.36/mkt (+$901 on 207); Dome overall -$1452 at t=0.55 driven entirely by paired-ladder fallback on gate-miss.
type: project
---

# Cycle 23 research audit — gate-miss fallback is a net drag

**Why:** Cycle 23 was dispatched for new-hypothesis generation after all known
tuning axes (width, max_pair_cost, fv_cancel, max_spread, certainty thresholds
0.45-0.60) CI-straddled zero. While auditing the existing
`results/sweep/certainty_resweep_per_market.csv`, the gate-miss decomposition
surfaced a much larger lever than threshold sweeps could find.

**How to apply:** Before proposing threshold or ladder tweaks in future
cycles, always decompose corpus PnL by gate-fired vs gate-missed. The
fired subset has been profitable across 4 thresholds; the missed subset
has been catastrophic at all 4. Tuning the threshold just re-partitions
the same two subsets.

## Headline numbers (14-day Dome corpus, t=0.55, 790 markets)

| Subset | N | Sum PnL | $/mkt | Win rate |
|---|---:|---:|---:|---:|
| Gate fires (directional) | 207 | $+901.61 | $+4.36 | 202W / 3L (97.6%) |
| Gate misses (paired-ladder fallback) | 583 | $-2354.24 | $-4.04 | 121W / 462L (20.8%) |
| Overall | 790 | $-1452.63 | $-1.84 | — |

Unfired $/mkt by threshold: -$3.40 / -$3.46 / -$4.04 / -$4.55 at t=0.45 /
0.50 / 0.55 / 0.60. Sign is stable across all tested thresholds.

## Live fall-through location

`polybot/strategy/ladder_manager.py` lines ~1065-1097 are the three
gate-miss skip branches (certainty_too_low / spread_too_wide /
missing_bid_ask). All three only log and fall through to the default
paired split at lines ~1167-1169 (`budget_up = budget / 2; budget_dn =
budget / 2`). No live skip behavior.

## Open question

Does the live bot's rescue stack (fv_cancel@0.60, fv_exit@0.30,
one_sided_abort_*, imbalance throttle) already neutralize the Dome -$4.04
drag on gate-miss markets? Phase C of H0 in the cycle 23 plan
(`docs/plans/new-hypotheses-2026-04-18.md`) is the cross-check. Result
from 39 live settlements will determine whether H0 is a ship candidate
or a Dome-artifact parking decision.

## References
- Plan: `docs/plans/new-hypotheses-2026-04-18.md` (Priority 0 section)
- Data: `results/sweep/certainty_resweep_per_market.csv` (3160 rows, 4
  thresholds × 790 markets)
- Prior plan: `docs/plans/certainty-threshold-resweep-2026-04-18.md` (the
  sweep that produced the CSV; its conclusion was "hold 0.55" based on
  fired-subset analysis — this audit examines the unfired subset it did
  not report)
