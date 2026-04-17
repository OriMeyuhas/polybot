# Live Readiness Improvement Proposal — 2026-04-17

## Summary

Top 3 improvements to ship before live, ranked by (expected PnL × confidence) / effort.
Backtest run: 787 Dome markets (2026-03-29 → 2026-04-08), 15m BTC.

| # | Title | $/day | Confidence | Effort | Score |
|---|---|---|---|---|---|
| 1 | Entry gate on CLOB book mid | +$300 | HIGH | Small | 10/10 |
| 2 | Narrow width + passive top rung | +$190 | MEDIUM | Small | 6/10 |
| 3 | Remove one-side cap + AUTO-LOCK | +$200 | HIGH | Small | 8/10 |

---

## 1. WIP Survey

58 files changed, +4777/-7067. Tracker module deleted; web UI rewritten. 28 plans in `docs/plans/`, mostly infra hardening — only 3 touch strategy PnL. Existing plans are coherent toward live-readiness infra but **underpowered on strategy**.

## 2. Backtest Results

| Config | Total PnL | $/mkt | WR | Paired | Sharpe |
|---|---|---|---|---|---|
| `baseline_current` (matches `.env`) | **-$2798** | -$3.55 | 15.8% | 99.5% | -0.44 |
| `paired_only` | -$2265 | -$2.88 | 22.6% | 99.5% | -0.38 |
| `fv_gate_full` (cert ≥ 0.80) | -$726 | -$0.92 | 34.3% | 81.1% | -0.12 |
| `narrow_width_fv_gate` (w=0.05) | -$1242 | -$1.58 | 36.0% | 34.0% | — |
| **`aggressive_fv_gate` (cert ≥ 0.65)** | **+$3072** | **+$3.90** | **72.4%** | 45.1% | **+0.63** |

**Book-mid calibration (entry):**
- [0.50–0.60] n=230, WR=60.9%
- [0.60–0.70] n=230, WR=87.0%
- [0.70–0.80] n=178, WR=94.4%
- [0.80–0.90] n=115, WR=100.0%
- [0.90–1.00] n=34,  WR=100.0%

### ⚠ CRITICAL CAVEAT
The backtester computes `fv_up_entry` from **CLOB book mid-price at window open** (`tools/backtester.py:1728–1732`), NOT from Binance-derived FV. The +$3072 result describes a **different edge** than the current bot thesis: "when CLOB consensus prices outcome at 65%+, go long winner, skip loser". This signal is readable at entry in live mode (no look-ahead), but it is not the Binance arb signal the bot currently uses.

---

## 3. Improvement #1 — Entry Gate on CLOB Book Mid (CRITICAL)

**Hypothesis:** CLOB book mid at window open is a calibrated predictor on its own. Above 60% implied, wins ~87%; above 80%, wins 100%. Bot reads the book at entry anyway — free signal.

**Proposed change:**
- In `ladder_manager.post_ladder()`, read current book mid via `executor.get_midpoint()`, compute `cert_book = 2 * abs(mid - 0.5)`, skip losing side when `cert_book ≥ 0.65`.
- Cap directional-buy budget at $20 on the winning side.
- Keep Binance-FV gate (orthogonal signal).
- Guard: require `up_ask - up_bid ≤ 0.05` spread + min resting liquidity before trusting mid.

**Expected lift:** +$7.45/market improvement → ~$715/day gross. Discount 50% for live-vs-backtest divergence → **~$300/day net at $500 bankroll**.

**Risk:** MEDIUM. Stale/thin books could trigger false positives. Bound per market at ~-$20. 1.3% tail. Needs holdout validation.

**Effort:** ~30 lines + config field + 2–3 tests.

---

## 4. Improvement #2 — Narrow Width + Passive Top Rung (HIGH)

**Hypothesis:** Width=0.10 with skew=2.0 means top rungs fill adversely at avg pair cost ~$1.10. Narrower width + truly-passive top rung brings avg pair cost < $1.00.

**Proposed change:**
- `LADDER_WIDTH` 0.10 → 0.04
- Cap top rung at `best_ask + 1 tick`
- `LADDER_RUNGS` 10 → 6
- `LADDER_SIZE_SKEW` 2.0 → 1.3
- Possibly drop `MAX_PAIR_COST` 0.98 → 0.94

**Expected lift:** +$2/market if avg pair cost drops 15¢/share → **~$190/day**.

**Risk:** MEDIUM-HIGH. Paired rate may collapse. Queue position in live degrades vs backtester optimism. Must validate together with #1.

**Effort:** Small — plan `2026-03-27-passive-top-rung.md` already scoped.

---

## 5. Improvement #3 — Remove One-Side Cap + AUTO-LOCK (HIGH)

**Hypothesis:** Two destructive mechanisms destroy winners. One-side cap cancels heavy (winning) side 49% of time; AUTO-LOCK creates asymmetric accumulation.

**Evidence:**
- Backtest: `baseline` vs `paired_only` = $533 swing from abort logic alone.
- Memory: cycle 12 showed AUTO-LOCK destroyed $80 paired profit from $53 earned.
- Plans `2026-04-07-fv-gate-paircost-onesidecap.md` and `2026-04-08-autolock-fix.md` written but not implemented.

**Proposed change:** Implement both plans verbatim. Delete AUTO-LOCK (11 lines in `reprice_if_needed`), remove two `_check_one_side_cap` call sites.

**Expected lift:** +$1.50–$2.50/market → **~$150–$250/day**. Stacks with #1.

**Risk:** LOW. Plans ready, incremental, reversible, tests included.

**Effort:** ~2 hrs coder time.

---

## Recommended Next Step

1. **Holdout-split validation of #1:** backtester on 2026-03-29 → 04-03 (tune) vs 04-04 → 04-08 (validate). If lift holds on holdout, it's real.
2. If confirmed, bundle #1 + #3 into a single plan. Defer #2 to measure #1 alone.
3. Skip #2 initially — narrowing width interacts with the destructive guards in #3; measure clean.

## Paranoid Findings

- Dome coverage: 1344 snapshots; live `book_log_*.jsonl` only 4 days (2026-04-10 to 04-13).
- Last 30 live paper settlements: +63% WR, 83% paired, +$12.55 — not statistically contradicting the -$3.55/mkt backtest baseline (small sample).
- "+72% WR" is asymmetric (small wins, capped losses). Anchor on Sharpe +0.63, not WR.
- Binance-FV logic is not tested by this backtest.

## Relevant Files

- `tools/backtester.py:1728-1732` — FV-from-mid substitution
- `tools/backtester.py:1035-1038` — FV gate logic
- `polybot/strategy/ladder_manager.py` — target for #1, #3
- `polybot/config.py` — target for #2
- `docs/plans/2026-04-07-fv-gate-paircost-onesidecap.md` — partial #3
- `docs/plans/2026-04-08-autolock-fix.md` — completes #3
- `results/2026-04-17-aggressive.json` — +$3072 backtest
- `results/2026-04-17-baseline.json` — -$2798 backtest
