# Holdout Invalidation: FV Gate holdout measures a different code path than live flip

**Cycle**: 17
**Date**: 2026-04-18
**Decision**: NO-OP — do not flip `FV_GATE_ENABLED=true` based on the cycle-15/17
holdout comparison. The holdout measures a different gating mechanism than the
live `.env` flag actually enables.

## Background

Cycle 16 identified that `.env` has `FV_GATE_ENABLED=false` but every cycle-15
holdout "baseline" used `fv_gate_enabled=true @ 0.55`. The $5.761/mkt figure
those sweeps produced was therefore for a config the bot was not running.
Cycle 17's mandate was to rebuild the holdout twice on the same markets, once
with the flag off and once with it on, to confirm whether flipping the live
flag would actually capture the projected edge.

## Experiment

Two Dome-mode backtester runs on the 2026-04-07..2026-04-11 holdout window
(n=106 markets, all other knobs identical to `cycle15_live_baseline.yaml`):

| Config | $/mkt | Sharpe | WR | paired_rate | max_loss | n |
|---|---:|---:|---:|---:|---:|---:|
| `live_actual` (fv_gate off) | -3.745 | -0.434 | 17.0% | 100.0% | -11.72 | 106 |
| `fv_gate_on` @ 0.55 | +5.761 | +0.717 | 87.7% | 14.1% | -18.04 | 106 |
| DELTA | +9.506 | +1.151 | +70.7pt | -85.9pt | -6.32 |  |

Raw JSON:
- `results/cycle17/live_actual_holdout.json`
- `results/cycle17/fv_gate_on_holdout.json`

On the surface, this is a clear win for the gate (+$9.50/mkt). The mandate's
ship threshold was "+$2/mkt with max_loss not blowing up by >$5". Delta pnl
beats that by 5x; max_loss worsens by $6.32, slightly over the $5 bound but
bounded by `directional_budget_cap=$18` which is already deployed.

## Why we do not ship

The gain comes from a code path that **is not what `FV_GATE_ENABLED=true` turns
on live**. Three converging facts:

1. **Dome-mode FV at entry is book-mid-derived, not Binance-derived.**
   `tools/backtester.py` lines 1728-1733:
   ```
   up_mid = (dome.up_best_bid + dome.up_best_ask) / 2.0
   fv_up_entry = max(0.01, min(0.99, up_mid))
   cert_entry = _fv_certainty(fv_up_entry)
   ```
   So the "FV gate" fires based on how lopsided the book is at window open,
   not on Binance-vs-Polymarket price divergence. This is semantically the
   **book-mid gate**, not the Binance-FV gate.

2. **Live threshold is hardcoded at 0.80, not 0.55.**
   `polybot/strategy/ladder_manager.py` line 1045:
   ```python
   elif self.cfg.fv_gate_enabled and cert >= 0.80:
   ```
   `self.cfg.fv_gate_certainty_threshold` is never read in the live gate
   branch. Config comment at `polybot/config.py` line 221 acknowledges:
   > NOTE: `fv_gate_certainty_threshold` (hardcoded 0.80 in ladder_manager.py)
   > is inert when `fv_gate_enabled=False` — the threshold check is never
   > reached.
   Flipping `FV_GATE_ENABLED=true` would activate the **Binance-FV** path at
   **cert >= 0.80**, not the book-mid path at 0.55 the holdout measured.

3. **Live already has `BOOK_MID_GATE_ENABLED=true` at 0.55** — the signal the
   holdout is actually measuring. But it never fires: cycle-16 audit found zero
   `BOOK MID GATE` events in this session despite book-mid certainty being
   >=0.55 in many windows. Root cause (per cycle-16 memo): spread guard
   `book_mid_gate_max_spread=0.05` is tighter than the typical live spread,
   so the gate's precondition is not met. This is the actual latent edge; the
   FV gate flag is a red herring.

## What the finding invalidates

Every cycle where a "live_baseline" holdout $/mkt was cited as "what we should
expect live" used the fv_gate_enabled=true @ 0.55 config, which in Dome mode
is a book-mid gate, not what any `.env` flag controls directly. Specifically:

- `calibration.md` "Activation 2026-04-17" entry (corrected to note fv_gate
  was never deployed live).
- `calibration.md` "Cycle 15 Single-Knob Holdout Sweep" entry — all four
  configs shared the same phantom baseline, so the delta conclusions
  (pair_cost no-op, fv_cancel no-op, narrow_width falsified) hold, but the
  absolute $/mkt numbers measure a config the bot never ran.
- `feedback_holdout_live_fv_gate_mismatch.md` — consistent with this
  finding; this plan is the formal follow-up.

## Recommended follow-up (NOT this cycle — mandate forbids it)

The real signal the holdout identifies is **book-mid-at-0.55 with a permissive
spread guard**. Next cycle's single highest-ROI axis is likely:

1. Relax `BOOK_MID_GATE_MAX_SPREAD` from 0.05 to ~0.10 (or remove). Re-run
   the Dome holdout sweep. If the $5.76/mkt uplift survives on the looser
   spread guard, ship it as a `BOOK_MID_GATE_MAX_SPREAD=0.10` env flip.
2. Alternative: patch `ladder_manager.py` line 1045 to read
   `self.cfg.fv_gate_certainty_threshold` instead of hardcoding 0.80,
   then run a proper Binance-FV sweep. But this is a multi-line source change
   and requires Binance-aware backtest harness (cycle-15 memo already deferred
   FV-cancel validation for the same reason).

## Acceptance / rollback criteria

- No code or `.env` changes this cycle. Bot continues on current config.
- Tests: 1004/1004 passing (verified pre-decision).
- Settlement log continues uninterrupted.
- Calibration.md updated to mark fv_gate@0.55 references as invalid baselines.
- `feedback_holdout_live_fv_gate_mismatch.md` stands as-is; this plan amplifies it.

## Queued next

**Cycle 18 candidate**: Relax `BOOK_MID_GATE_MAX_SPREAD` 0.05 -> 0.10.
Expected effect: activate the book-mid gate which currently fires 0 times
in-session despite being the mechanism behind the +$5.76/mkt holdout number.
Requires a holdout re-sweep with the looser spread guard before flipping .env.
