# Time-Decay Ladder Tightening

## Status: PARTIAL (audited 2026-04-17)

- **Done:** `compute_decay_factor()` function exists at `polybot/strategy/ladder_manager.py:56` (signature differs from plan — uses `floor=0.58` with a two-phase decay that holds at floor after 60% elapsed, rather than the linear `floor=0.3` the plan specified).
- **Not applied:** The function is **never called**. `_post_ladder_core()` uses vol-based `effective_width` (line 981-985) but no decay. `reprice_if_needed` does not apply decay. `effective_rungs` logic is absent — rung count is not decayed.
- **Open tasks:** Phase 2 (apply in reprice path), Phase 3 (apply in repost path), Phase 4 (tests for decay wiring).

**Goal:** Scale ladder width and rung count down as a market window approaches expiry, concentrating capital on rungs that have a realistic chance of filling instead of wasting budget on wide rungs that will never be reached.

## Problem

Ladder parameters (width, rung count) are fixed for the entire window lifetime. A reprice at 4 minutes remaining uses the same 0.41 width and 31 rungs as the initial post. As expiry approaches, the possible price range narrows sharply (less time for spot to move), so outer rungs become dead weight that locks up capital with zero fill probability.

## Design

Compute a `decay_factor` from elapsed fraction of the window, then apply it to `width` and `rungs` before passing them to `build_ladder_rungs()`. The factor decays linearly from 1.0 at window open to a floor of 0.3 at expiry:

```python
remaining = market.remaining(now)
total = market.timeframe_sec
elapsed_frac = 1.0 - (remaining / total) if total > 0 else 1.0
decay_factor = max(0.3, 1.0 - elapsed_frac * 0.7)

effective_width = lp.width * decay_factor
effective_rungs = max(4, int(lp.rungs * decay_factor))
```

| elapsed % | decay_factor | effective width (15m, base 0.41) | effective rungs (base 31) |
|---|---|---|---|
| 0% | 1.00 | 0.41 | 31 |
| 25% | 0.82 | 0.34 | 25 |
| 50% | 0.65 | 0.27 | 20 |
| 75% | 0.48 | 0.20 | 14 |
| 90% | 0.37 | 0.15 | 11 |
| 100% | 0.30 | 0.12 | 9 |

This only affects reprices (existing rungs are cancelled and reposted when the book moves). The initial post uses full parameters. The `no_trade_final_sec=60` guard in `RiskManager.can_trade_in_window()` already prevents any posting in the last 60 seconds.

## Files to Modify

- `polybot/strategy/ladder_manager.py` -- apply decay in `reprice_if_needed()` and `_post_ladder_core()`; add `_compute_decay_factor()` helper
- `polybot/types.py` -- no changes (already has `remaining()` and `timeframe_sec`)
- `tests/test_ladder_manager.py` -- add decay factor tests
- `tests/test_time_decay.py` (new) -- focused unit tests for the decay math and integration with `build_ladder_rungs()`

## Do-Not-Touch List

- `polybot/config.py` -- LadderParams and get_ladder_params() stay unchanged; decay is a runtime adjustment, not a config parameter
- `polybot/data/` -- data layer is unrelated
- `polybot/oms/` -- order execution is unrelated
- `polybot/risk_manager.py` -- no_trade_final_sec guard is orthogonal and must not be weakened
- `polybot/web/` -- dashboard reads ladder stats which will naturally reflect tighter ladders
- `polybot/fees.py` -- fee logic is independent
- `polybot/tracker/` -- whale tracker is separate

## Ordered Change List

### Phase 1: Decay helper

- [ ] **1.1** Add a static/module-level helper function `compute_decay_factor(market: MarketWindow, now: int) -> float` to `polybot/strategy/ladder_manager.py`. Logic:
  ```python
  def compute_decay_factor(market: MarketWindow, now: int, floor: float = 0.3) -> float:
      remaining = market.remaining(now)
      total = market.timeframe_sec
      if total <= 0:
          return floor
      elapsed_frac = 1.0 - (remaining / total)
      elapsed_frac = max(0.0, min(1.0, elapsed_frac))
      return max(floor, 1.0 - elapsed_frac * (1.0 - floor))
  ```

### Phase 2: Apply decay in reprice path

- [ ] **2.1** In `reprice_if_needed()`, after `lp = self.cfg.get_ladder_params(...)` (line ~469), compute the decay factor:
  ```python
  decay = compute_decay_factor(market, int(now))
  effective_width = lp.width * decay
  effective_rungs = max(4, int(lp.rungs * decay))
  ```

- [ ] **2.2** In the UP side `build_ladder_rungs()` call inside `reprice_if_needed()` (line ~516-519), replace `lp.rungs` with `effective_rungs` and `lp.width` with `effective_width`. Keep `lp.spacing` and `lp.size_skew` unchanged.

- [ ] **2.3** Same replacement for the DN side `build_ladder_rungs()` call (line ~549-552).

- [ ] **2.4** Add a log line showing the decay factor applied:
  ```python
  logger.info("REPRICE: %s (UP moved=%s, DN moved=%s) decay=%.2f rungs=%d width=%.3f",
              mid, up_moved, dn_moved, decay, effective_rungs, effective_width)
  ```

### Phase 3: Apply decay in repost path

- [ ] **3.1** In `_post_ladder_core()`, after `lp = self.cfg.get_ladder_params(...)` (line ~170), compute the decay factor:
  ```python
  now_epoch = int(time.time())
  decay = compute_decay_factor(market, now_epoch)
  effective_width = lp.width * decay
  effective_rungs = max(4, int(lp.rungs * decay))
  ```

- [ ] **3.2** Replace `lp.rungs` and `lp.width` with `effective_rungs` and `effective_width` in both `build_ladder_rungs()` calls (UP side line ~218-221 and DN side line ~223-226). Keep `lp.spacing` and `lp.size_skew` unchanged.

- [ ] **3.3** Include decay factor in the LADDER POSTED log line (line ~302):
  ```python
  logger.info(
      "LADDER POSTED: %s | %d UP + %d DN rungs | budget=$%.2f | pair_cost=%.3f | decay=%.2f",
      market.market_id, len(up_rungs), len(dn_rungs), budget, pair_cost, decay,
  )
  ```

### Phase 4: Tests

- [ ] **4.1** Create `tests/test_time_decay.py` with:
  - `test_decay_at_window_open` -- elapsed_frac=0, decay_factor=1.0
  - `test_decay_at_halfway` -- elapsed_frac=0.5, decay_factor=0.65
  - `test_decay_at_80_percent` -- elapsed_frac=0.8, decay_factor=0.44
  - `test_decay_at_expiry` -- elapsed_frac=1.0, decay_factor=0.3 (floor)
  - `test_decay_floor_never_below` -- verify factor >= 0.3 for all elapsed fractions
  - `test_decay_with_zero_timeframe` -- timeframe_sec=0 returns floor

- [ ] **4.2** `test_effective_rungs_minimum` -- verify `max(4, int(31 * 0.3))` = 9, and that even with very small lp.rungs (e.g. 5), the floor of 4 is respected.

- [ ] **4.3** `test_build_ladder_with_decayed_params` -- call `build_ladder_rungs()` with decayed width and rungs, verify output has fewer rungs and all prices are within the tighter range.

- [ ] **4.4** `test_reprice_uses_decay` -- integration test: create a LadderManager, mock a market at 75% elapsed, trigger `reprice_if_needed()`, verify the ladder is reposted with ~48% width and ~14 rungs instead of full params.

- [ ] **4.5** `test_initial_post_uses_decay` -- integration test: post a ladder when market is 50% elapsed, verify decayed params are applied.

- [ ] **4.6** Run `pytest tests/ -v` and verify all existing + new tests pass.

## Risk Notes

- The 0.3 floor prevents the ladder from collapsing to a single rung, which would make it a market order with no spread.
- The minimum 4-rung guard ensures at least some price diversity even at the floor.
- Spacing (`lp.spacing`) is deliberately NOT decayed. `build_ladder_rungs()` already auto-widens spacing when `effective_rungs < rungs` and `effective_rungs > 1` (line ~74-75), so the rungs will naturally spread across the narrower width.
- The `no_trade_final_sec=60` guard in risk_manager is the hard stop. Decay is a soft optimization that improves capital efficiency before that cutoff.
- Pair cost guard is unaffected: tighter ladders concentrate near the market, which tends to increase VWAP. The existing guard will correctly reject if the tighter ladder's pair cost exceeds the ceiling. This is the desired behavior -- near expiry, 50/50 markets converge toward pair cost ~1.0 and should be skipped.
