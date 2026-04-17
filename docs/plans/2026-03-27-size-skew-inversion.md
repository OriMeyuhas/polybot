# Plan: Size Skew Inversion

**Date:** 2026-03-27
**Status:** Planned
**Files:** `polybot/config.py`, `polybot/strategy/ladder_manager.py`, `polybot/ladder_manager.py`

## Problem

Top rungs (nearest market price) fill instantly at worst price. The current `size_skew=1.5` puts 50% MORE size on expensive rungs. This maximizes loss when adversely selected -- two windows lost $0.60 each from single top-rung fills.

The weight formula in `build_ladder_rungs()` is:
```
weight = 1.0 + (idx / (rungs-1)) * (size_skew - 1.0)
```

Rungs are ordered cheapest-first (index 0 = farthest from market, index N-1 = nearest to market). With `size_skew=1.5`, the most expensive rung (highest index) gets weight 1.5 while the cheapest gets 1.0. This is backwards for a passive market-making strategy: the rung most likely to be adversely selected carries the most capital.

## Fix: Invert size skew from 1.5 to 0.7

Change `size_skew` default from 1.5 to 0.7 across all three timeframes. The formula stays identical -- only the parameter value changes:

- With `size_skew=0.7`: cheapest rung weight=1.0, most expensive rung weight=0.7
- Cheap rungs (safer, far from market) get ~30% more shares than expensive rungs
- Total budget unchanged -- just redistributed toward safer rungs
- Worst-case single-fill loss reduced because the nearest-market rung is now the smallest

## Steps

### A. Change defaults in `polybot/config.py`

- [ ] **A1.** Change `ladder_size_skew` default from `1.5` to `0.7` (line 80)
- [ ] **A2.** Change `ladder_size_skew_5m` default from `1.5` to `0.7` (line 88)
- [ ] **A3.** Change `ladder_size_skew_1h` default from `1.5` to `0.7` (line 96)
- [ ] **A4.** Change `load_bot_config()` env var defaults from `"1.5"` to `"0.7"` for all three `LADDER_SIZE_SKEW` variants (lines 295, 301, 307)

### B. Update comment in `polybot/strategy/ladder_manager.py`

- [ ] **B1.** Update the comment on line 73 from "Safe with flat skew (1.5x)" to "Safe with inverted skew (0.7x)" to reflect the new default. The logic and formula are unchanged.

### C. Mirror in `polybot/ladder_manager.py`

- [ ] **C1.** Same comment update as B1 if present (line ~73). This file duplicates the strategy version.

### D. Update tests that hard-code `size_skew=1.5` as the default

Tests that explicitly pass `size_skew=1.5` to `build_ladder_rungs()` or `ladder_size_skew=1.5` to `BotConfig()` are testing specific behavior with that value -- they are NOT asserting on the default. These tests pass the value explicitly and will continue to work unchanged. **No test changes needed** unless a test relies on the default config value without overriding.

Tests to audit (verify they pass explicit values and don't depend on default):

- [ ] **D1.** `tests/test_passive_top_rung.py` -- lines 22, 35, 50, 55, 73, 101-102, 110, 115 all pass `size_skew=1.5` explicitly to `build_ladder_rungs` or `BotConfig`. These test the formula behavior at that value. **No change needed.**
- [ ] **D2.** `tests/test_tick_size_fix.py` -- lines 156, 198, 237 pass `size_skew=1.5` explicitly. **No change needed.**
- [ ] **D3.** `tests/test_bot_integration.py` -- lines 20, 251 pass `ladder_size_skew=1.5` to `BotConfig`. **Change to 0.7** since these construct configs meant to represent defaults.
- [ ] **D4.** `tests/test_live_capital_accounting.py` -- lines 32, 48 pass `ladder_size_skew=1.5`. **Change to 0.7** since these represent default configs.
- [ ] **D5.** `tests/test_ladder_manager.py` -- line 22 uses `ladder_size_skew=2.0`, all `build_ladder_rungs` calls use explicit values (2.0, 3.0, 1.0). **No change needed** -- these test skew behavior at specific values, not the default.
- [ ] **D6.** `tests/test_fee_accounting.py` -- line 93 uses `ladder_size_skew=1.0`. **No change needed.**

### E. Verify

- [ ] **E1.** Run `pytest tests/ -v` -- all tests pass
- [ ] **E2.** Spot-check: construct a default `BotConfig()` and call `get_ladder_params(900)`. Verify `size_skew == 0.7`.
- [ ] **E3.** Call `build_ladder_rungs(best_ask=0.50, budget=10.0, rungs=5, spacing=0.01, width=0.04, size_skew=0.7)` and verify the cheapest rung has the largest size and the most expensive rung has the smallest.

## Test Cases

- [ ] **T1. Unit: inverted skew gives more size to cheap rungs** -- `build_ladder_rungs` with `size_skew=0.7`, 5 rungs. Assert `result[0][1] > result[-1][1]` (cheapest rung has more shares than most expensive).
- [ ] **T2. Unit: budget is fully allocated** -- Same call as T1. Assert `sum(price * size for price, size in result)` is approximately equal to budget.
- [ ] **T3. Unit: default config uses 0.7** -- `BotConfig()` without overrides. Assert `cfg.ladder_size_skew == 0.7`, `cfg.ladder_size_skew_5m == 0.7`, `cfg.ladder_size_skew_1h == 0.7`.
- [ ] **T4. Unit: env var override still works** -- Set `LADDER_SIZE_SKEW=1.5` in env, call `load_bot_config()`. Assert `cfg.ladder_size_skew == 1.5`.
- [ ] **T5. Regression: all existing tests pass** -- `pytest tests/ -v` green.

## Do-Not-Touch List

- `polybot/strategy/ladder_manager.py` `build_ladder_rungs()` formula -- the weight formula is correct, only the input value changes.
- `polybot/strategy/ladder_manager.py` `LadderManager` class -- no logic changes.
- `polybot/config.py` `get_ladder_params()` -- passes `size_skew` through, no formula to change.
- `polybot/config.py` `LadderParams` / `get_trading_rules()` / `validate_live_config()` -- unrelated.
- `polybot/bot.py` -- orchestrator, no skew logic.
- `polybot/data/*` -- data layer, no skew logic.
- `polybot/oms/*` -- order management, no skew logic.
- Tests that explicitly pass non-default skew values (test_ladder_manager.py, test_fee_accounting.py) -- these test formula behavior at specific values.

## Rollback

If inverted skew causes unforeseen issues (e.g. cheap rungs fill too much volume, budget exhaustion on safe side), revert the four default values in `config.py` from `0.7` back to `1.5`. Alternatively, set `LADDER_SIZE_SKEW=1.5` in `.env` without any code change -- the env var override path is unchanged.
