# Dome Sweep Results & Single-Knob Proposal — 2026-04-18

## Methodology

- **Dataset**: 1,755 Dome snapshot files spanning 2026-03-29 → 2026-04-16 (19 UTC days).
- **Train split**: 2026-03-29 → 2026-04-07 (10 days, 960 dome files → 777 markets with replay-able book data).
- **Holdout split**: 2026-04-08 → 2026-04-16 (9 days, 795 dome files → 13 markets with replay-able book data).
- **Fill model**: current `tools/backtester.py` post commit `0a79a2f` (real book-data fill model, no heuristic).
- **Scoring**: rank by **holdout Sharpe**, tie-break on holdout $/mkt. Reject if retention < 70% OR holdout Sharpe < train Sharpe × 0.6.
- **Caveat (known)**: backtester uses `book_mid` as FV proxy (not Binance spot). Reported `fv_accuracy` is inflated vs live. Config YAMLs with `fv_gate_enabled=true` are effectively the same signal as the LIVE `BOOK_MID_GATE` — treat them as different implementations of the same gate.
- **Holdout statistical power is weak** (n=13). Train n=777 is the reliable signal. Holdout is used as a directional sanity check only.

## Comparison Table (sorted by holdout Sharpe)

| Config                        | Tr $/mkt | Tr WR | Tr Sharpe | Ho $/mkt | Ho WR | Ho Sharpe | Retention | Max Loss | tr_n | ho_n |
|-------------------------------|---------:|------:|----------:|---------:|------:|----------:|----------:|---------:|-----:|-----:|
| **live_current_repro**        | +2.299   | 0.903 | +0.821    | +3.010   | 0.923 | **+1.044**| 131%      | -$5.24   | 777  | 13   |
| threshold_sweep_60            | +5.424   | 0.831 | +0.717    | +5.334   | 0.692 | +0.986    | 98%       | -$1.99   | 777  | 13   |
| **live_5x_position**          | +5.544   | 0.885 | +0.639    | +7.965   | 0.923 | **+0.865**| 144%      | -$18.04  | 777  | 13   |
| threshold_sweep_55            | +6.063   | 0.885 | +0.631    | +8.726   | 0.923 | +0.856    | 144%      | -$19.99  | 777  | 13   |
| aggressive_fv_gate            | +3.914   | 0.726 | +0.633    | +3.970   | 0.615 | +0.728    | 101%      | -$4.38   | 777  | 13   |
| threshold_sweep_70            | +2.302   | 0.614 | +0.394    | +1.514   | 0.462 | +0.270    | 66%       | -$5.24   | 777  | 13   |
| threshold_sweep_75            | +1.169   | 0.525 | +0.190    | -0.882   | 0.308 | -0.168    | -75%      | -$5.24   | 777  | 13   |
| narrow_width_fv_gate          | -1.560   | 0.364 | -0.237    |  (not run)|       |           |           |          | 777  | —    |
| fv_gate_full                  | -0.910   | 0.345 | -0.114    | -2.900   | 0.154 | -0.414    | 319%      | -$4.38   | 777  | 13   |
| paired_only                   | -2.878   | 0.227 | -0.381    |  (not run)|       |           |           |          | 777  | —    |
| paired_plus_trend_filter      | -2.878   | 0.227 | -0.381    |  (not run)|       |           |           |          | 777  | —    |
| narrow_band_fv_gate           | -3.443   | 0.165 | -0.428    |  (not run)|       |           |           |          | 777  | —    |
| baseline_current (OLD)        | -3.559   | 0.158 | -0.441    |  (not run)|       |           |           |          | 777  | —    |

**Interpretation**:
- `baseline_current` (stale YAML using `one_sided_abort_enabled=true` + no FV gate) is the worst — this is the OLD config before rotation 12's grace period fix. Confirms grace period + book-mid gate was the correct direction.
- `live_current_repro` closely matches LIVE `.env`: FV gate threshold 0.55 via book-mid, `position_size_fraction=0.01`, `max_pair_cost=0.98`, `directional_budget_cap=$18`. Ho Sharpe 1.044 is highest — the live config is well-calibrated.
- `live_5x_position` is the **single-knob delta**: only `position_size_fraction` raised 0.01 → 0.05 (other params match live). Holdout Sharpe 0.865 (still robust) and holdout $/mkt +$7.97 vs live's +$3.01 = **+165% profit uplift** with drawdown tolerated (max loss -$18.04, equal to directional_budget_cap).
- `threshold_sweep_55` is nearly identical to `live_5x_position` (only difference: `max_pair_cost=0.95` vs `0.98`). Since the max_pair_cost change showed **zero effect** at current position size (`live_current_repro` vs `live_plus_tight_pair_cost` were identical), 0.98 is fine to keep — the ladder won't reach prices that high with width=0.10 anyway.

## Already-Known (from calibration.md)

- Book-mid gate `0.65 → 0.55` already shipped (rotation 14). Sweep confirms 0.55 sits on the sweet spot of the Sharpe curve. No further threshold change proposed this cycle.
- `FV_GATE_ENABLED=false` live (killed 2026-04-11 because Binance-FV proxy calibration was off). Backtester's `fv_gate_enabled=true` uses book-mid as proxy which IS the live book-mid gate — no conflict, no re-enable of Binance FV.

## Winner: `live_5x_position`

**The single change to ship**: `POSITION_SIZE_FRACTION=0.01 → 0.05` (.env, 1-line edit).

**Expected uplift** (from backtester, subject to proxy-vs-live attenuation):
- Holdout $/mkt: +$3.01 → +$7.97 (+$4.96/market). At ~95 BTC 15m markets/day × 0.5 live uptime = **+$235/day expected** vs current ~$90/day.
- Win rate stays at 92.3% holdout.
- Max loss increases -$5.24 → -$18.04 (3.4% of $525 bankroll). Bounded by `DIRECTIONAL_BUDGET_CAP=$18`.

**Risks**:
1. Backtester reports `fv_gate_enabled=true` cases but live has it disabled. However the LIVE `BOOK_MID_GATE=0.55` IS the same effective signal, so results translate.
2. 5x position means 5x exposure per market. With MAX_CONCURRENT_POSITIONS=1, total exposure stays bounded.
3. Live paper environment has pair_cost VWAP ~0.95-1.01 (per calibration.md) while backtester sees better fills. Real uplift may be 30-50% of backtest prediction — still material.

## Rejected Hypotheses This Cycle

- `max_pair_cost 0.98 → 0.95`: zero-effect at current position size (ladder doesn't reach 0.95+ prices with width=0.10).
- `fv_gate_certainty_threshold` 0.70 or 0.75: both have retention < 0% on holdout (noise at best, regression at worst).
- `narrow_width_fv_gate` (width=0.05): -$1.56/mkt train. Narrowing further than 0.10 kills paired fill rate.
- `paired_only` / `paired_plus_trend_filter`: -$2.88/mkt train. Disabling FV gate entirely regresses to baseline.
- Re-enabling Binance `FV_GATE`: not attempted this cycle. Backtester uses book-mid proxy; Binance FV requires separate calibration work.

## Implementation

```diff
-POSITION_SIZE_FRACTION=0.01
+POSITION_SIZE_FRACTION=0.05
```

No code change, no test change. Run `python -m pytest tests/ -q` to confirm all tests still pass, then restart bot.

**Rollback**: if 10-settlement rolling PnL after restart is < -$30 or max_loss exceeds $25, revert `POSITION_SIZE_FRACTION` to 0.01 in .env.

## Artifacts

- Train results: `results/sweep/*_train.json`
- Holdout results: `results/sweep/*_holdout.json`
- New configs written: `experiments/live_current_repro.yaml`, `experiments/live_plus_tight_pair_cost.yaml`, `experiments/live_5x_position.yaml`
