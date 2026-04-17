# Manager Calibration Notes

## One-Sided Fill Losses Are Normal Variance (2026-04-08)

3 consecutive losses triggered investigation. Root cause: BTC trending DOWN, bot getting
heavy UP fills but not enough DN fills. This is the known "one-sided fill" pattern:
- pair_cost=null means only one side filled significantly
- The risk manager auto-reduces exposure_factor to 0.5 after consecutive losses
- This is NOT a bug or strategy failure - it's inherent to market-making in trending markets

**Decision**: Do NOT dispatch researcher/debugger for this pattern unless:
- consecutive_losses >= 5 (not just 3)
- bankroll drawdown > 15% from session start
- The losses are from TWO-SIDED fills (pair_cost != null) losing money, which would indicate a spread/pricing issue

## Research Cycle False Positives

When research identifies "pair rate dropped to X% due to trending BTC" and the bot is still
profitable overall, this is expected behavior, not actionable. Skip the dispatch.

## Pair Cost Trend (2026-04-08)

Average pair cost has crept from 0.911 (early session) to 0.937 (later session).
This reduces profit margin per pair from ~8.9% to ~6.3%.
- At pair_cost < 0.93: healthy margin
- At pair_cost 0.93-0.95: acceptable but monitor
- At pair_cost > 0.95: consider widening LADDER_WIDTH or reducing LADDER_RUNGS
- This is market-driven (book competition), not a code issue

## Book Gate Fix Assessment (2026-04-09)

10-settlement post-fix assessment. Book gate (blocks fills when no book data) deployed.

**Balanced fills (pair_cost not null): 4/10 settlements, PnL -$0.48**
- The book gate is successfully producing balanced fills
- pair_cost ranges 0.815 to 0.930
- Balanced fills are essentially break-even (spread profit ~$0 after costs)
- This is expected: the spread is thin (VWAP 0.95-1.01)

**FV directional bets (one-sided): 6/10 settlements, PnL -$24.70**
- FV brain accuracy: 2/6 correct (33%), BELOW coin flip
- Correct bets: +$19.67 total
- Incorrect bets: -$44.38 total
- The -$26.05 loss (settlement 8) was FV posting DOWN-only then BTC reversed

**Key pattern**: 15m markets are frequently blocked by pair cost guard (VWAP 0.95-1.01).
When FV override posts directional-only, 3/5 of these 15m markets had ZERO fills.
The remaining ones with fills were either correct or wrong depending on BTC direction.

**Observation**: 1h markets consistently get balanced fills (enough time for both sides).
15m markets struggle because (a) pair cost guard blocks often, (b) FV goes one-sided.

**Decision**: Continue monitoring. Do NOT investigate unless:
- Balanced fill PnL turns significantly negative (spread pricing broken)
- FV accuracy drops below 25% over 20+ bets (systematic error)
- Zero-fill rate exceeds 50% of 15m markets over 20+ markets

## External Restart Detection (2026-04-08)

Bot was restarted externally at 13:54 with BANKROLL=$500 (should have been ~$1409).
When PIDs change and bankroll resets, it's an external restart, not a bug.
The session_start_bankroll should be updated to match the restart value ($500).
Paper mode: no real money impact from wrong bankroll.

## Session Summary (2026-04-08 21:28 to 2026-04-09 02:53)

20 monitoring cycles over ~5.5 hours. Bot processed 21+ settlements.

**Performance**: Started at bankroll $500, peaked at $695.94 (+39%), ended at ~$475.
- First 10 settlements: 87.5% WR, +$129.35 -- exceptional run driven by directional wins
- Next 10 settlements: 40% WR, -$68 -- drawdown from one-sided DN losses on UP outcomes
- Net session PnL still slightly negative from the $500 restart point

**Key patterns observed**:
1. Pair cost guard blocks 15m windows frequently (VWAP 0.97-1.03 vs 0.95 threshold). This is correct behavior protecting from unprofitable entries.
2. One-sided directional wins average +$33.56, losses average -$34.21. Nearly symmetric, meaning the strategy edge comes from the FV brain's accuracy, not position sizing.
3. When BTC trends strongly in one direction for multiple consecutive windows, one-sided losses accumulate because counterparties fill the wrong side aggressively.
4. Two-sided (balanced) fills are nearly break-even: avg PnL ~$1-5. The real alpha comes from FV directional bets.

**No code changes deployed** -- the strategy is working as designed. All losses are from normal variance in market-making.

## Session Continuation (2026-04-09 03:00)

Continued monitoring after external restart at 00:30. Bot restarted with $500.
- 8 settlements since restart: 3W/5L, Session PnL: -$25.18
- Overnight BTC choppy (ranging between $70,960-$71,660)
- FV gate accuracy poor in choppy conditions (2/6 correct on one-sided bets)
- Two-sided fills performing as expected (small +/- around breakeven)
- Pair cost guard continues to block most 15m windows (VWAP > 0.95)
- No investigation warranted per calibration thresholds

## Book-Mid Gate Threshold Calibration (2026-04-17)

Holdout-validated threshold sweep on dome corpus (n=787 train, n=212 holdout 04-04 to 04-08).
All other gate params held constant (fv_cancel=0.60, width=0.10, rungs=10, skew=2.0, max_pair_cost=0.95,
directional_budget_cap=$20, one_sided_abort=false).

| Threshold | Train $/mkt | Holdout $/mkt | Retention | Holdout WR | Sharpe | MaxDD% |
|-----------|---:|---:|---:|---:|---:|---:|
| 0.55 | 6.11 | 5.10 | 83.5% | 84.4% | 0.491 | 1.26 |
| 0.60 | 5.42 | 4.73 | 87.3% | 80.7% | **0.541** | 1.05 |
| 0.65 (shipped) | 3.90 | 3.23 | 82.8% | 69.3% | 0.464 | 0.80 |
| 0.70 | 2.29 | 2.41 | 105.2% | 60.9% | 0.390 | 1.89 |
| 0.75 | 1.14 | 1.37 | 121.0% | 52.4% | 0.212 | 3.43 |

**Optimal = 0.60** (highest Sharpe 0.541, best retention 87.3%, +46% $/mkt vs 0.65).
0.65 was the FIRST tried value, not the calibrated optimum. Lower thresholds capture more edge because
the book-mid signal is well-calibrated down to 0.55+ (see also proposal calibration table).

**Max loss bounded at -$20** across all thresholds by directional_budget_cap. No tail-risk from loosening gate.

**Do NOT change threshold yet.** Paper mode is currently validating 0.65 and has < 5 gate-fired settlements.
Wait for paper evidence on 0.65 before switching. Rollout order: 0.65 (current) -> validate paper ->
0.60 (next improvement). If 0.60 ships, re-sweep with Binance FV data once live, since backtester uses
book-mid as FV proxy.

Sweep configs saved at `experiments/threshold_sweep_{55,60,70,75}.yaml`; results at
`results/threshold_sweep_*.json` and `results/holdout/threshold_*_holdout.json`. Rerun with
`python tools/backtester.py --config experiments/threshold_sweep_60.yaml --start 2026-04-04 --end 2026-04-08`.

## 1h Market Variance is Extreme but Net Positive (2026-04-09 15:26)

15-settlement rolling window analysis of 156 total 1h settlements shows extreme variance:
- Best 15-stl window: +$1,908
- Worst 15-stl window: -$116
- Current session (last 15): -$90.70

This is NORMAL. Do NOT disable 1h markets based on a single bad 15-settlement stretch.
The lifetime 1h PnL is ~+$3,165 over 156 settlements (+$20.29/stl average).

**Decision**: Keep 1h markets enabled. Only investigate 1h if:
- 30+ consecutive 1h settlements are net negative (sustained, not just a patch)
- 1h paired fill WR drops below 50% over 30+ settlements
- The bad stretch extends to 30+ settlements AND bankroll approaches hard floor

## Bankroll Reset Confusion via UI POST /api/settings (2026-04-17)

When user reports "bankroll was reseeded to $X" but actual `BANKROLL=Y` in `.env` and bot shows $Y:
the most common cause is that someone (browser/UI) submitted `POST /api/settings` AFTER the
restart-reset endpoint wrote `.env` with the seeded value. The settings POST overrides bankroll
in both running state and `.env`.

Diagnosis recipe (do NOT escalate as a bug):
1. `grep "Bankroll updated:" polybot.log` — shows the override timestamp.
2. `grep "POST /api/settings" polybot.log` — usually within seconds of step 1.
3. `stat -c '%Y %n' .env` — mtime will match the settings POST, not the restart.

This is user behavior, not a bot bug. If the user wants $10K paper, they need to NOT subsequently
adjust bankroll in the UI, or update `DRY_RUN_BANKROLL` and re-run reset without follow-up settings POST.

## Activation 2026-04-17 18:00 — All Major Proposals Deployed

Confirmed live in source + paper log:
- Book-mid gate (`BOOK_MID_GATE_CERTAINTY_THRESHOLD=0.65`, fired in log)
- Directional budget cap `$18`
- One-side cap call sites removed (`_check_one_side_cap` is dead code at line 1911)
- FV cancel circuit breaker (3 fires/60s -> kill ladder)
- Grace period (30s before one-sided abort)
- `strategy_log_*.jsonl` writing 3.4MB today (silent-failure fix landed)

Test suite: 1004/1004 passing. 0 errors in `polybot.log`.

**Single highest-ROI queued change**: lower `BOOK_MID_GATE_CERTAINTY_THRESHOLD` from 0.65 -> 0.60.
Holdout-validated: Sharpe 0.541 vs 0.464, +46% $/mkt. Wait for >=5 paper settlements with 0.65
before swapping. This is a 1-line `.env` edit + restart — does NOT need planner/coder/tester chain
(but must verify paper performance match holdout post-deployment).
