---
name: Cycle 8 Edge Cases and Regression Bugs
description: NameError crash in _tighten_light_side (scaled_rungs), reprice missing confirm_cancels causing ghost orders, validate_live_config missing 5 new fields, rebalance events invisible to dashboard
type: project
---

## Cycle 8: Edge Cases, Regressions, and Missing Guards (2026-03-28)

### BUG 1 (CRITICAL): _tighten_light_side NameError — `scaled_rungs` undefined
- Lines 761-764: reference `scaled_rungs` but local variable is `rungs`
- Cycle 7 identified 3 call-site bugs in this function; they were fixed, but this REGRESSION was introduced
- Effect: active rebalancing silently crashes every time. Orders on light side get cancelled and new ones posted, but anchor update crashes. Next tick, rebalance retries (since last_rebalance_at never set), cancels the just-posted orders, posts new ones, crashes again. **Order churn loop.**
- The main loop catches Exception so bot doesn't die, but rebalancing is effectively dead
- Fix: change `scaled_rungs` to `rungs` on lines 761-764

### BUG 2 (HIGH): Reprice path missing `confirm_cancels()`
- `reprice_if_needed()` lines 577-578 and 611-612: calls `tracker.cancel_side()` then `executor.cancel_batch()` but never calls `tracker.confirm_cancels()`
- Orders stay in 'cancelling' status permanently — not in get_resting(), not in filled, not cleaned up
- They accumulate in the tracker's `orders` dict as ghost entries, one set per reprice cycle
- For comparison: `_tighten_light_side` (693) and `check_imbalance` (806) both correctly call `confirm_cancels`
- Fix: add `self.tracker.confirm_cancels(cancelled)` after each cancel_batch in reprice

### BUG 3 (MEDIUM): validate_live_config missing 5 new config fields
- `consecutive_loss_halt` — no validation. If set to 0, bot halts immediately on ANY loss. If negative, never halts.
- `max_capital_at_risk_pct` — no validation. If > 1.0, guard is useless. If <= 0, blocks all trading.
- `rebalance_moderate_threshold` — no validation. Must be < max_imbalance_ratio.
- `rebalance_cooldown_sec` — no validation. If 0, rebalances on every tick.
- `max_imbalance_ratio` — no validation. Must be > rebalance_moderate_threshold.

### BUG 4 (LOW-MEDIUM): Rebalance events not visible on dashboard
- `check_imbalance()` logs to logger (WARNING/INFO) but never calls `_record_activity()`
- `bot.py` line 671 calls `check_imbalance` but doesn't capture return value or log results
- Operator has no dashboard visibility into rebalancing — can't see if imbalance was detected, what action was taken, or if rebalancing failed

### Verified OK:
- PnL calculation: `profit_if_up/down` algebra is correct with fee-inclusive costs
- Position tracking: `_flush_uncredited_to_positions` correctly uses delta-based crediting, no double-counting
- Settlement: outcome validation, paper dry_run_resolve using spot delta, stale price deferral all correct
- Capital-at-risk wired into _post_ladder_core correctly
- exposure_factor wired into budget calculation correctly

**Why:** Bug 1 makes the entire rebalancing system (designed in cycles 5-7) dead code. Bug 2 causes memory leak proportional to number of reprices. Bug 3 is a live-mode safety gap.

**How to apply:** Fix bugs 1-2 immediately (one-line fixes each). Add config validation for bug 3. Wire rebalance activity events for bug 4. All fixes should be validated with targeted unit tests.
