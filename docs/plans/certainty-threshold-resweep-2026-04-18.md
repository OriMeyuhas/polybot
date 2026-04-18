# Certainty-Threshold Re-Sweep (Book-Mid Gate)

- **Date:** 2026-04-18
- **Label:** plan-only — cycle 22
- **Author:** polybot research
- **Artifacts:**
  - Script: `C:/Users/pc/Desktop/Bots/PolyBot/tools/certainty_resweep.py`
  - Per-market CSV: `C:/Users/pc/Desktop/Bots/PolyBot/results/sweep/certainty_resweep_per_market.csv`
  - Summary JSON: `C:/Users/pc/Desktop/Bots/PolyBot/results/sweep/certainty_resweep_summary.json`

## Purpose

Re-run the `book_mid_gate` certainty-threshold sweep on the full Dome corpus with improved statistics (per-market records, bootstrap confidence intervals on Δ$/mkt vs baseline 0.55, regime quartile breakdown on `abs_outcome_return`, max drawdown on the fired-PnL curve) and decide whether to hold the current threshold or queue a change for cycle 23/24.

## Methodology

### Dataset

- Corpus directory: `C:/Users/pc/Desktop/Bots/PolyBot/data/dome_snapshots/`
- Loader: `tools.backtester.load_dome_snapshot` for every `btc-updown-15m-*.jsonl`.
- Filter: `has_orderbook == True` AND `outcome is not None`.
- After filtering: **790 markets** across **11 days**, 2026-03-29 .. 2026-04-16.
- Date coverage is contiguous; no explicit train/holdout split — this is a full-corpus re-sweep because the purpose is a powered re-estimate of the Δ, not an OOS validation (already performed in cycles 17–19).

### Gate and simulator

- Gate: `tools.book_mid_gate_sweep.apply_book_mid_gate` — mirrors `polybot/strategy/ladder_manager.py` (lines ~987–1029). `book_mid_up = up_mid / (up_mid + dn_mid)`; `cert = 2 * |book_mid_up - 0.5|`; fires iff `cert >= threshold` AND both sides have spread `<= max_spread`.
- Dispatch: `tools.book_mid_gate_sweep.simulate_market_with_book_gate` — when the gate fires, it rewires the backtester to post directional-only on the gate's winning side via the existing `fv_gate_enabled=True, fv_gate_certainty_threshold=0.0` path; when the gate does not fire, the baseline paired-ladder sim runs.
- `max_spread = 0.05` (matches `BOOK_MID_GATE_MAX_SPREAD` default).
- Thresholds swept: 0.45, 0.50, 0.55, 0.60. Baseline for deltas: **0.55**.

### Base config

Replicates `tools/book_mid_gate_sweep.py::main` base_cfg:

| Param | Value |
|---|---|
| bankroll | 500.0 |
| position_size_fraction | 0.1 |
| directional_budget_cap | 18.0 |
| rungs | 10 |
| spacing | 0.01 |
| width | 0.10 |
| size_skew | 2.0 |
| max_pair_cost | 0.98 |
| one_sided_abort_enabled | True |
| one_sided_abort_cost_pct | 0.01 |
| one_sided_abort_ratio | 3.0 |
| fv_gate_enabled | False (native FV gate disabled; book-mid gate is the only gate) |

### Per-market metrics captured

For every (threshold, market) pair: `market_id`, `window_start_epoch`, `fired`, `pnl`, `outcome_correct`, `paired`, `outcome`, `abs_outcome_return = |binance_at_close - ptb| / ptb` (None when either price is missing or `ptb <= 0`).

### Threshold-level metrics

- `markets_total`, `markets_fired`, `fire_rate = markets_fired / markets_total`
- `fired_pnl = sum(pnl | fired)`, `fired_pnl_per_market = fired_pnl / markets_fired`
- `fired_correct_rate = sum(outcome_correct is True | fired) / markets_fired`
- `max_drawdown`: worst peak-to-trough of the cumulative PnL curve over fired markets ordered by `window_start` (negative number; 0 when there are no fires).

### Bootstrap Δ vs 0.55

Difference-of-means bootstrap on the **fired-market PnL lists** at each non-baseline threshold vs the fired-market PnL list at 0.55.

- Iters: 1000, seed: 42 (`random.Random(42)`).
- Per iteration: resample-with-replacement from each threshold's fired-PnL list at its own N, then record `mean(sample_t) - mean(sample_0.55)`.
- CI: 2.5% / 97.5% quantiles of the resample distribution.
- CI "excludes 0" iff both bounds share the same sign.

### Regime quartiles

- Quartile cutoffs computed from `abs_outcome_return` on **all corpus markets with non-None data** (n = 790 — every market has ptb and binance_at_close). Linear-interpolation percentiles.
- Q1 = smallest (ranging), Q4 = largest (trending).
- Per (threshold × quartile): count of fired markets, $/mkt over fired markets in that quartile.

## Results

### Comparison table (all four thresholds)

| threshold | markets_total | markets_fired | fire_rate | $/mkt (fired) | Δ$/mkt vs 0.55 | 95% CI (lo, hi) | fired_correct_rate | max_dd |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.45 | 790 | 285 | 36.08% | $+4.67 | $+0.31 | ($-0.34, $+0.99) | 0.9684 | $-18.03 |
| 0.50 | 790 | 244 | 30.89% | $+4.67 | $+0.31 | ($-0.30, $+0.91) | 0.9836 | $-18.03 |
| 0.55 | 790 | 207 | 26.20% | $+4.36 | baseline | (baseline) | 0.9855 | $-18.03 |
| 0.60 | 790 | 149 | 18.86% | $+4.09 | $-0.27 | ($-0.72, $+0.30) | 1.0000 | $+0.00 |

### CI evidence (95% bounds on Δ$/mkt vs 0.55)

- 0.45 vs 0.55: point $+0.3107, 95% CI ($-0.3422, $+0.9855) — **CI straddles 0 (does not exclude).**
- 0.50 vs 0.55: point $+0.3111, 95% CI ($-0.2951, $+0.9054) — **CI straddles 0 (does not exclude).**
- 0.60 vs 0.55: point $-0.2672, 95% CI ($-0.7178, $+0.3026) — **CI straddles 0 (does not exclude).**

No non-baseline threshold produces a Δ$/mkt whose 95% CI excludes 0. The sign of each point estimate is consistent with the expected monotonic tradeoff (looser gate → more fires but slightly lower per-fire quality; stricter gate → fewer fires with higher quality and smaller absolute drawdown), but the bootstrap cannot distinguish any of them from 0.55 at the 95% level on this 790-market corpus.

### Per-regime breakdown (rows = threshold, cols = quartiles of abs_outcome_return)

Quartile cutoffs (n = 790 with data): **q25 = 0.000982**, **q50 = 0.002364**, **q75 = 0.004807** (fractional return).

Each cell is `fired_n / $/mkt on fired` in that quartile.

| threshold | Q1 (ranging, ≤0.000982) | Q2 (≤0.002364) | Q3 (≤0.004807) | Q4 (trending, >0.004807) |
|---:|---:|---:|---:|---:|
| 0.45 | 59 / $+4.98 | 76 / $+4.08 | 71 / $+5.07 | 79 / $+4.63 |
| 0.50 | 49 / $+4.41 | 63 / $+4.53 | 62 / $+5.09 | 70 / $+4.60 |
| 0.55 | 44 / $+4.09 | 55 / $+4.62 | 50 / $+4.62 | 58 / $+4.09 |
| 0.60 | 32 / $+3.96 | 41 / $+4.03 | 33 / $+4.47 | 43 / $+3.94 |

No threshold × quartile cell is negative. No cell on any non-baseline threshold is more than $5 below the same-quartile 0.55 value; the largest within-quartile gap vs 0.55 is ($-0.15) at 0.60 × Q3 (3.96 - 4.09 = -0.13 at Q1; all within ±$1). No catastrophic regime degradation.

## Recommendation

**(a) Hold 0.55 — insufficient evidence to move.**

### Rationale

The decision criterion is: a move is justified only if the 95% CI on Δ$/mkt **excludes 0** AND the quality invariant (`fired_correct_rate ≥ 96%`, using this as the mapped quality invariant because the book-mid gate's natural fire_rate is ~20–36%, so the nominal "fire_rate ≥ 96%" criterion from the task doesn't apply to a selective gate — this mapping is noted explicitly) holds AND no regime quartile shows catastrophic degradation (>$5/mkt below 0.55 in that quartile).

- **CI gate: fails for every candidate.** All three non-baseline 95% CIs straddle 0. This is the dominant blocker.
- **Quality invariant: ambiguous.** 0.50 (0.9836), 0.55 (0.9855), and 0.60 (1.0000) clear the 96% bar; 0.45 (0.9684) does not. 0.60 has perfect side-correctness on fired markets, which is suggestive but does not overcome the CI gap.
- **Regime gate: passes everywhere.** No catastrophic per-quartile degradation at any threshold.
- **Drawdown color:** 0.60 is the only threshold with 0 max drawdown (149 fires, all profitable cumulatively). 0.45/0.50/0.55 all share the same worst fired-curve drawdown of $-18.03, indicating a shared loss event inside the fired set.

A more permissive (0.45/0.50) or stricter (0.60) move would be plausible as a point-estimate optimization but is **not statistically supported** at the 95% level on 790 markets. Recommended path: keep 0.55 in production, collect more data over the next 1–2 weeks, and re-run this sweep when the corpus roughly doubles (which should tighten CIs by ~sqrt(2)). Cycle 23/24 may elect to re-examine 0.60 specifically — it has the strongest quality story (correct_rate = 1.0, max_dd = 0) and its point estimate against 0.55 is only $-0.27 — if and only if the CI then excludes 0 on the down side.

---

This document is plan-only. Cycle 23/24 may execute based on this analysis. No strategy changes are proposed this cycle.
