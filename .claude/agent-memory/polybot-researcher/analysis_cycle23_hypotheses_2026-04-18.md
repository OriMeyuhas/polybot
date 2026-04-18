---
name: Cycle 23 New Hypotheses 2026-04-18
description: Strategic pivot — 3 priority hypotheses attacking unpaired adverse-selection root cause, after all known threshold axes falsified
type: project
---

Context: 39 settlements session PnL −$40.57. Rolling-20 PnL eroding. All single-axis threshold tunings (width, pair-cost, fv_cancel, max_spread, certainty sweep 0.45-0.60) falsified. Structural bleed re-confirmed: paired avg +$1.55 (n=16), unpaired avg −$0.92 (n=22).

**Why:** The team keeps retuning thresholds and keeps finding null results. Need a strategic pivot to mechanism-level hypotheses (microstructure-informed) rather than parameter sweeps.

**How to apply:** Future researcher cycles: first ask "is this just another threshold-sweep variant?" before proposing tests. If yes, it's likely dead. Real alpha now requires novel signal construction or timing/sizing mechanism changes, not rethresholding.

## Three priority hypotheses (in `docs/plans/new-hypotheses-2026-04-18.md`)

1. **Delayed entry (60-180s into window)** — attacks adverse-selection at window-open. Dome-testable via orderbook time-series. Est edge $0.02-0.05/mkt.
2. **Top-rung book-depth imbalance** — orthogonal signal to FV certainty. Dome-testable. Est $0.03-0.08/mkt on firing markets (~30%).
3. **Prior-window outcome as direction prior** — cheap diagnostic (no backtester needed), standalone edge small but feeds into H1/H2.

## Hard constraint discovered

Dome snapshots contain Binance/Chainlink ONLY for the last 100s of each window. Any Binance-FV-at-entry-time or full-window RV-based hypothesis is Dome-UNTESTABLE. IV/RV spread and Binance momentum FV alternatives are DEFER until we have 14 days of live `price_log_*.jsonl` corpus.

## Null-result discipline

Plan explicitly forbids threshold rescue: "if null is not rejected, drop the hypothesis and move on — do not rescue by retuning thresholds." Expected survival rate across 3 hypotheses: ~33%.

## Execution order

Cycle 24: H3 (pure arithmetic, 1 day)
Cycle 25: H1 (backtester sweep, 1-2 days)
Cycle 26: H2 (scoring-only first, then asymmetric sizing layered on H1 winner)
