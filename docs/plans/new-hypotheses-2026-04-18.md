# Cycle 23 — New Hypotheses for 15m BTC Alpha

**Generated:** 2026-04-18
**Session context:** 39 settlements, session PnL −$40.57, rolling-10 4W/5L +$17.42, rollback guard not triggered. All known single-axis tunings (width, pair-cost, fv_cancel, max_spread, certainty threshold) falsified on Dome holdout.

**Structural root-cause (re-confirmed):** of the last 38 settlements with activity, paired trades are **profitable (+$24.75 / 16 stl, avg +$1.55)** while unpaired trades **bleed (−$20.17 / 22 stl, avg −$0.92)**. Money is left on the table at the **entry-timing → fill-pairing** interface, not at threshold boundaries we've already tuned.

## Corpus constraints (hard limits for hypothesis design)

- **Dome snapshots per market** contain:
  - Full orderbook time-series (~4,250 snaps/mkt, ~4.7 Hz combined UP+DN, t=0→900s)
  - 1-min candles with yes_bid/yes_ask/open/close/high/low (t=60→900s, 16 candles)
  - **Binance + Chainlink only last 100s of window** ← HARD LIMITATION
- **Resolved markets:** 974 of 1,755 (rest are UNKNOWN). Sufficient for ~0.02-edge detection at p<0.05.
- **Per-hour sample:** ~40 resolved markets → outcome-skew CI is ±0.15 at 95%. Need hour-aggregation (e.g., 4-hour blocks of ~160 markets) for meaningful TOD tests.

---

## Ranked Hypothesis List

**Ranking key:** `score = cheap_to_test × easy_to_validate × size_of_edge`, Dome-testable first, DEFER items last.

| # | Hypothesis | Dome? | Cheap? | Validate? | Edge (est) | Score |
|---|------------|-------|--------|-----------|-----------:|------:|
| **0** | **Gate-miss skip — refuse to paired-ladder when book-mid gate does not fire** | **YES** | **VERY HIGH (CSV re-read)** | **HIGH** | **~$1-3/mkt (corpus) — largest edge surfaced** | **A+** |
| 1 | Delayed entry (60-180s post-open) | YES | HIGH | HIGH | $0.02-0.05/mkt | **A** |
| 2 | Top-rung book-depth imbalance as asymmetric-sizing signal | YES | HIGH | MED | $0.03-0.08/mkt (when signal fires) | **A** |
| 3 | Prior-window outcome as direction prior | YES | VERY HIGH | HIGH | $0.01-0.03/mkt | **B** |
| 4 | Early-candle displacement → mean-reversion vs continuation | YES | MED | MED | $0.02-0.04/mkt | **B** |
| 5 | TOD-bucketed exposure sizing | YES | VERY HIGH | LOW | $0.01-0.03/hr on edge hours | **C** |
| 6 | IV (candle-implied) vs RV (Binance-realized) straddle signal | PARTIAL | MED | LOW | $0.05+/mkt if real | DEFER |
| 7 | Full-window Binance momentum FV alternative | NO | — | — | — | DEFER |

H0 (added during 2026-04-18 audit) is the current top priority. H1-H3 remain the
secondary queue and are the priorities *if H0 is parked after live cross-check*.

---

## Priority 0: Gate-Miss Skip (surfaced 2026-04-18 by audit of `certainty_resweep_per_market.csv`)

**One-line:** The book-mid gate already selects a highly profitable subset of
markets (97.6% WR on fires). The fallback paired-ladder on gate-miss markets
is catastrophic on Dome. Refusing to trade when the gate does not fire
flips the 14-day Dome PnL sign from negative to positive.

**Audit evidence (already computed, re-read from existing sweep CSV, t=0.55):**

| Subset | N | Sum PnL | $/mkt | Win rate |
|---|---:|---:|---:|---:|
| Gate fires (directional-only) | 207 | $+901.61 | $+4.36 | 202W / 3L (97.6%) |
| Gate misses (paired-ladder fallback) | 583 | $-2354.24 | **$-4.04** | 121W / 462L (20.8%) |
| **Overall (Dome, t=0.55)** | 790 | **$-1452.63** | **$-1.84** | — |

The pattern replicates at every threshold in the existing sweep (unfired $/mkt
is -$3.40 / -$3.46 / -$4.04 / -$4.55 at t=0.45 / 0.50 / 0.55 / 0.60). The
sign is stable across thresholds, not an artifact of threshold choice.

The live bot does **not** skip on gate-miss — in
`polybot/strategy/ladder_manager.py` lines ~1065-1097 the three skip branches
(certainty_too_low / spread_too_wide / missing_bid_ask) only log and fall
through to the default paired split at lines ~1167-1169 (`budget_up = budget /
2; budget_dn = budget / 2`). In Dome simulation, that fallback is
catastrophic on the unfired subset.

**Edge thesis.** The book-mid gate's design intent was to bias budget onto
the winning side when the book is confident — but its side-effect is that it
*also* acts as a selector of the markets where a paired strategy has any
chance at all. On the markets where the book does not clear the certainty
bar, paired-ladder is not a fallback, it is a negative-expectation strategy
in this 14-day regime.

**Data source.**
- Primary: `results/sweep/certainty_resweep_per_market.csv` (790 × 4
  thresholds = 3160 rows, already on disk).
- Live cross-check: `data/settlement_log.jsonl` (39 rows) joined with
  `polybot.log` "BOOK MID GATE" lines, tagging each settlement as
  `gate_fired` / `gate_skipped(reason)`.

**Cheap test (≤ 30 min total, no new backtest required for Phase A).**

1. **Phase A — Dome point estimate bootstrap** (~5 min).
   - Re-read the CSV; bootstrap 1000 iters, seed=42, of
     `mean(pnl | fired=0)` at t=0.55. Report 95% CI.
   - Null: `mean(pnl | unfired) ≥ 0`. Expected: bootstrap 95% CI excludes 0
     on the negative side given -$4.04 point with n=583.
2. **Phase B — Dome shipping sim** (~15 min).
   - Add `skip_on_gate_miss: bool = False` to `BacktestConfig` in
     `tools/backtester.py`.
   - In `simulate_market_dome`: when `skip_on_gate_miss=True` and the
     book-mid gate does not fire, return `_empty_result(...)` with pnl=0
     (no posting, no fills). Otherwise unchanged.
   - Run `run_backtest_dome` on the 790-market corpus with `skip_on_gate_miss
     ∈ {False, True}` at t=0.55. Compare total PnL, $/mkt, and fire rate.
   - Null: Δ(skip_on_gate_miss=True vs False) ≤ 0. Expected: Δ ≈ +$2354
     (magnitude of current gate-miss sum PnL) or equivalently +$2.98/mkt.
3. **Phase C — Live cross-check** (~10 min).
   - Parse `polybot.log` for BOOK-MID-GATE lines dated alongside the 39
     `settlement_log.jsonl` rows. Tag each settlement as `gate_fired`
     (BOOK-MID-GATE fired) or `gate_skipped` (BOOK-MID-GATE SKIP logged).
   - Report per-subset $/mkt + win rate on 39 live settlements.
   - Decision:
     - If live gate-skipped $/mkt < -$0.5 → **H0 supported in live**, queue
       shipping plan (config-flagged rollout, shadow window first).
     - If live gate-skipped $/mkt ∈ [-$0.5, +$0.5] → **live guards already
       neutralize the drag** (`fv_cancel`, `fv_exit`,
       `one_sided_abort_*` are doing the work the Dome sim doesn't
       capture). Park H0; focus on H1-H3.
     - If live gate-skipped $/mkt > +$0.5 → **live fundamentally differs
       from Dome** on gate-miss markets. Open a debugger dispatch to
       understand why; block any skip plan until reconciled.

**Baseline.** Current behavior — gate-miss falls through to symmetric paired
split (`budget / 2` each side) with all existing live guards active.

**Expected $/mkt if validated.**
- Dome point estimate: +$2.98/mkt (equivalent to +$2354 over 14 days).
- Realistic live expectation: +$1 to +$2/mkt — live guards (fv_cancel,
  one_sided_abort, imbalance throttle) do rescue some gate-miss markets
  that Dome assumes get dumped. If live $/mkt lift is +$1, on 39 recent
  settlements that is +$39 — essentially the size of the current session
  loss.

**Risk / false-positive paths.**
- **Dome fill-model overstatement of paired fills.** If Dome's
  `simulate_market_dome` fills at paper-thin book liquidity that would
  never actually fill in live, the -$4.04 on gate-miss is partly
  fictional. The Phase C live cross-check is the mitigation — live PnL
  is ground truth.
- **Live guards already doing this work silently.** FV-cancel at 0.60
  certainty and one-sided-abort both act on gate-miss markets that
  become directional after they start filling. If they already neutralize
  the drag, there is nothing to ship.
- **Regime overfit.** 14-day Dome corpus is a specific BTC regime. Skip
  policy may be right now and wrong in 30 days. **Optionality is
  preserved by shipping behind a config flag** with a shadow window
  and a rollback gate.
- **Volume reduction risk.** Shipping H0 cuts trading volume from 100% to
  ~26% of markets. Fewer fills means fewer fee rebates, slower bankroll
  growth velocity, and thinner statistical signal on any future
  hypothesis test. Mitigate via H1-H3 (which may rescue gate-miss
  markets by converting some to directional fires).
- **Interaction with H1 (delayed entry).** If H1 also ships, the gate may
  fire on more markets at the delayed entry time → fewer gate-misses to
  begin with → H0's ship value shrinks. The right order is H0 first (it
  is the larger lever and simpler change), then re-estimate H1 on the
  post-H0 corpus.

**Why this is ranked above H1-H3 despite similar Dome-testability.** Size
of edge. H0's point estimate is 50-100x larger than H1/H2/H3 per-market
edge estimates. Even if live realized edge is 20% of Dome point estimate
(plausible given live guards), it dwarfs the alternatives. Cheap-to-test
is maximal — the primary evidence exists on disk today.

**What would kill H0 immediately.** Phase C live cross-check finding that
gate-skipped live settlements are profitable or break-even. That would
mean the live bot already has effective skip-equivalent behavior, and the
gap exists only in Dome simulation accuracy.

---

---

## Priority 1: Delayed Entry (wait 60-180s into window)

**One-line:** We enter at window-open when microstructure is noisiest and the informed price hasn't settled; delaying entry by 60-180s may cut unpaired rate materially without losing much fill opportunity.

**Edge thesis.** At t=0 the previous market just closed, new size is arriving, and both UP and DN ladders see a burst of taker flow. Our passive ladder sits into this noise — and the side that **does** fill tends to be the side where informed traders lifted liquidity (i.e., the losing side for us). By t=60-180s the candle has printed, price discovery has converged, and our passive orders face a cleaner flow. Cost: less time for the *other* side to fill before window close. Trade-off is quantifiable.

**Data source.**
- Dome `orderbook` snapshots (full window)
- Dome `candle` data (1-min OHLC of yes_bid/yes_ask) — for computing implied-prob evolution
- `winning_side` from header (for PnL)

**Cheap test (≤ 30 min backtester).**
1. Fork `tools/backtester.py` → add `entry_delay_sec: int` to `BacktestConfig`.
2. In `simulate_market`, instead of placing rungs at `open_ep`, place them at `open_ep + entry_delay_sec` using the book state at that timestamp (`lookup_book_state`).
3. Run a sweep across `entry_delay_sec ∈ {0, 30, 60, 90, 120, 180, 240, 300}` using `experiments/baseline_current.yaml` as the baseline config.
4. Report per-delay: paired_rate, unpaired_rate, avg pair_cost when paired, avg unpaired-side PnL, total PnL per market, and bootstrap 95% CI on mean PnL.

**Null hypothesis.** `mean_pnl(delay=D) ≤ mean_pnl(delay=0)` for all D > 0. Falsify at p < 0.05 via bootstrap over the 974 resolved markets.

**Baseline.** Existing `baseline_current.yaml` at `entry_delay_sec=0` → this is what the live bot does today.

**Expected $/mkt if validated.** If delay reduces unpaired rate from 58% (22/38 this session) to 45% and unpaired avg PnL stays at −$0.90, that's `0.13 × $0.90 ≈ $0.12/mkt` saved. Some offset from lower paired rate — net expected +$0.02 to +$0.05/mkt. At ~96 15m windows/day, ~$2-5/day. Small per-trade but consistent.

**Risk / false-positive paths.**
- If delay reduces paired_rate proportionally, net is zero — the bootstrap must separate the two effects.
- Binance catch-up arbitrage competitors may take the best fills during the delay window → in live we get adversely selected *harder* than in Dome replay. Guard: compare Dome-predicted vs live bot performance during any deployment gate.
- Dome book snapshots have ~200ms jitter on timestamps; at `delay=30s` the exact entry-book may differ from live. Use 60s as the minimum delay tested.

---

## Priority 2: Top-Rung Book-Depth Imbalance as Asymmetric-Sizing Signal

**One-line:** When the top 3-5 rungs of UP and DN books have very different total depth near midpoint, that imbalance is informed flow; size the ladder **asymmetrically** (bigger on the empty side — the side informed traders *aren't* defending).

**Edge thesis.** Market-makers pull depth on the side they think will win (so they don't get run over) and leave depth on the side they'll happily trade against. A 3:1 depth imbalance in the top-5 rungs near midpoint is a leak of their expectation. This is **orthogonal to FV certainty** (which is driven by Binance spot delta — a different information source). Combining them should dominate either alone.

**Data source.**
- Dome `orderbook` snapshots at window-open (or at `entry_delay_sec` if combined with H1)
- Header winning_side for ground truth

**Cheap test (≤ 30 min backtester).**
1. Add two signals to the simulator at entry time:
   - `depth_imbalance_top5 = (sum_up_size_top5 - sum_dn_size_top5) / (sum_up_size_top5 + sum_dn_size_top5)`, restricted to rungs at or deeper than 2 cents from midpoint.
   - (Comparison baseline) current FV certainty.
2. Run backtester with scoring only (no behavior change): for each market record `depth_imbalance`, `fv_certainty`, `winning_side`, `would-be PnL of paired`, `would-be PnL of unpaired`.
3. Analyze: bucket markets by `abs(depth_imbalance) ∈ [0, 0.2, 0.4, 0.6, 0.8, 1.0]` and compute hit-rate that the **heavier-depth side = losing side** (i.e., the "market-maker is defending winner" hypothesis).
4. If hit-rate > 55% in the `> 0.4` bucket, test a strategy: in those markets, size the thin-depth side at 1.5× and the heavy-depth side at 0.5× the current ladder. Otherwise use symmetric ladder.
5. Report per-bucket: hit-rate, mean PnL asymmetric vs baseline, bootstrap CI.

**Null hypothesis.** `P(heavy_side = losing_side | |imbalance| > 0.4) = 0.50`. Falsify at p < 0.05 (two-sided binomial).

**Baseline.** Symmetric-ladder paired strategy from `baseline_current.yaml`.

**Expected $/mkt if validated.** If signal fires on ~30% of markets (heuristic on 974 markets → ~292 events) and improves WR from 50% → 56% on those, at avg pair revenue of $0.06-$0.10 that's +$0.03-$0.06 on signal-markets, or **+$0.01-$0.02 per market across all**. If the signal is stronger (≥60% hit rate) this scales to $0.05-0.08/mkt on firing markets.

**Risk / false-positive paths.**
- Depth at the window-open moment is dominated by seed-liquidity orders (reflex $0.99 asks, etc.), not informed flow. Must filter those out — restrict analysis to **rungs between midpoint and midpoint±0.10** where real MM sits.
- Selection bias: markets where we saw big depth imbalance may also be markets with high spread → pre-filtered out already by our `spread_too_wide` guard. Cross-check by rerunning with and without guard overlay.
- Imbalance may flip within first 60s; signal could be unstable. Check stability by measuring autocorrelation from t=0 to t=60s.

---

## Priority 3: Prior-Window Outcome as Direction Prior

**One-line:** If the previous 1 or 2 windows resolved UP, the next window is not 50/50 — test both momentum (streaks continue) and mean-reversion (streaks break) on the Dome outcome sequence.

**Edge thesis.** Crypto exhibits short-horizon momentum (autocorrelation in 5-15min returns). A 15m window resolving UP means BTC ended higher than it started; if the underlying drift is positive on that horizon, the next 15m window is mildly UP-biased. Alternatively, if Polymarket pricing over-extrapolates the streak, fading the streak pays. **Both are easy to test and the result tells us which regime we're in.**

**Data source.**
- Dome headers (winning_side + window_start) sorted by time → outcome sequence
- No book or price data needed → runtime minutes, not hours

**Cheap test (≤ 5 min, no backtester needed).**
1. Load all 974 resolved markets into a sorted list by window_start.
2. For each market, compute `prior1` = outcome of previous 15m window (if adjacent), `prior2` = outcome 2 back.
3. Compute conditionals:
   - `P(next=UP | prior1=UP)` vs `P(next=UP | prior1=DOWN)`
   - `P(next=UP | prior1=UP & prior2=UP)` vs `P(next=UP | prior1=DOWN & prior2=DOWN)`
4. If conditional differs from 0.50 by >0.04 with p<0.05 (chi-square), we have a prior-based gate.
5. Then test a simple strategy on the backtester: when `prior1=prior2=X`, skew ladder size 1.3×/0.7× in direction of prediction.

**Null hypothesis.** `P(next=UP | prior1) = P(next=UP)` globally (no autocorrelation). Chi-square test on 2x2 contingency table.

**Baseline.** Symmetric-ladder baseline with no directional tilt.

**Expected $/mkt if validated.** If the prior edge is 0.04 (say 54% vs 50% conditional), on a $5 UP-biased bet vs $2.50 symmetric, expected lift is `0.04 × $0.06 ≈ $0.002/mkt` — too small to matter standalone. **Combined with H1 or H2 it's worthwhile as a tie-breaker; standalone it's a diagnostic, not alpha.** Scoring it priority-3 reflects that.

**Risk / false-positive paths.**
- Survivor bias if we drop UNKNOWN markets that cluster in specific hours.
- Market-wide regime (BTC grinding up for 3 days) creates apparent autocorrelation that isn't exploitable in live trading without regime-switching logic.
- 14-day corpus is short for stable autocorrelation estimation; split into two 7-day halves and require effect to show in both.

---

## Also-Ran (not in top 3 but worth a hit)

### #4 — Early-Candle Displacement

**Hypothesis.** If the t=60s (first 1-min close) candle prints YES price >0.60 or <0.40, the outcome follows/fades predictably.

**Test.** Bucket markets by `yes_close_at_60s ∈ {0.30-0.35, 0.35-0.40, ..., 0.60-0.65, 0.65-0.70}`. Compute P(outcome=UP | bucket). Dome-candles only, ~5min runtime.

**Why not top 3.** Very similar in spirit to H2 and more fragile — early candle is noisy and the YES price at t=60 is a 1-min mean-dollar proxy that aggregates MM noise.

---

### #5 — TOD Exposure Sizing

**Hypothesis.** Some UTC hours have systematically worse paired-rate or pair_cost due to MM behavior / low volume. Reduce exposure in those hours.

**Test.** Replay backtester bucketed by UTC hour of window_start. Per-hour mean PnL and bootstrap CI. Runtime: one backtester run, ~10 min.

**Why not top 3.** Likely to find spurious hour effects at n=40 per hour. Requires holdout-validation via a separate 7-day split. Low edge even if real.

---

## DEFER (Dome-Untestable)

### #6 — IV/RV Spread (candle-implied vs Binance-realized)

Would need full-window Binance 1s prices to compute RV; we only have last 100s. **Revisit after we've collected 14 more days of live data with full price_log_*.jsonl coverage.**

### #7 — Binance 1m-momentum FV alternative

Same Binance-coverage problem. **Revisit with live-log-derived corpus.**

---

## Recommended Execution Order (next cycles)

1. **Cycle 24, day 1 (cheap, ≤ 30 min):** Run **H0 Phase A + Phase C** —
   bootstrap CI on Dome unfired-PnL, then parse 39 live settlements against
   BOOK-MID-GATE log lines to estimate live gate-miss $/mkt.
   - If live gate-miss $/mkt < -$0.5 → go to step 2 (H0 Phase B + shipping plan).
   - If live gate-miss $/mkt ∈ [-$0.5, +$0.5] → park H0; go to step 3 (start H3).
   - If live gate-miss $/mkt > +$0.5 → dispatch debugger to reconcile Dome/live,
     defer all other H0-dependent work.
2. **Cycle 24, day 2 (1 day):** If H0 survived step 1 — H0 Phase B backtest
   sweep + shipping plan. Ship behind a single `skip_on_gate_miss` config flag
   with a shadow window (log would-have-skipped markets' live PnL for 24-48h
   before enabling the skip behavior). Define rollback gate.
3. **Cycle 25 (cheap, 1 day):** Run H3 on outcome sequences — pure arithmetic,
   no backtester. If positive, lock it in as a "directional tilt" flag
   available to later hypotheses.
4. **Cycle 26 (1-2 days):** Implement H1 (delayed entry) in backtester; run
   8-point delay sweep. Re-estimate against the **post-H0 corpus** if H0
   shipped (entry timing behaves differently on the fired-only subset). If
   any delay dominates baseline by > $0.02/mkt with p<0.05, draft a plan for
   live rollout as a **single kill-switched param**.
5. **Cycle 27 (2-3 days):** Implement H2 (depth-imbalance signal). First run
   as scoring-only analysis (no behavior change) to confirm the signal is
   real. Then, if confirmed, layer the asymmetric-sizing test on top of the
   H1-winning entry_delay.

At each gate: if null is not rejected, drop the hypothesis and move on — do **not** rescue by retuning thresholds. We've been burned by threshold-sweeps producing apparent edges that die out-of-sample.

---

## Honest Self-Assessment

- **H0** is by far the largest lever in the document and the only one surfaced
  by a concrete evidence audit (not pattern generation). Its risk profile is
  also the most asymmetric — downside is "no change, H0 parked after Phase C,"
  upside is "14-day Dome PnL sign flips." The Dome/live divergence question
  is the one real unknown; everything else is quantifiable now.
- H1 and H2 attack the confirmed root cause (unpaired-side adverse selection). Both have plausible microstructure mechanisms. They remain the right follow-on work either way — if H0 ships, they operate on the surviving fired subset; if H0 parks, they are the primary edges.
- H3 is a diagnostic that may feed into H1/H2; standalone edge is small.
- H4 / H5 are listed for completeness but I'd bet against them yielding live alpha.
- I could not generate a genuinely novel 6th/7th hypothesis (beyond what's here) that is Dome-testable AND not a restatement of an exhausted axis. I'd rather ship 3-4 clean tests than pad the list.
- Hit-rate estimate across H0 + top-3: **~40-45%** — H0 is fairly likely to
  survive Dome but has a real Dome-live divergence risk; H1-H3 remain at the
  ~33% each estimate. Plan for at most one of H1/H2/H3 surviving Dome →
  holdout → live in addition to H0.
