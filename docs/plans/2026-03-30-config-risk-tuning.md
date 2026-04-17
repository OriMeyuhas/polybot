# Config & Risk Tuning Implementation Plan

## Status: PARTIAL (audited 2026-04-17)

- **Task 1 (MAX_IMBALANCE_RATIO 0.60 → 0.35):** NOT DONE. `config.py:102` still `max_imbalance_ratio: float = 0.60`, env fallback also "0.60" at line 434.
- **Task 2 (Small tier threshold 500 → 400):** DONE. `config.py:346` reads `if bankroll < 400:`. Note docstring at line 327-328 still references old $500 threshold — stale comment.
- **Task 3 (Medium tier: 3 concurrent, 10% fraction):** PARTIAL. `position_fraction=0.10` is set (line 358), but `max_concurrent=4` (not 3 as plan specified).
- **Task 4 (Disable `_tighten_light_side`):** DONE (indirectly). Method `_tighten_light_side` no longer exists in `polybot/strategy/ladder_manager.py`. Rebalancing is not called. Accumulation / heavy-side-cancel guards live elsewhere (one-side cap, heavy_side_locked).
- **Task 5 (Halve 5m fraction: `base_fraction * 0.33 * 0.5`):** NOT DONE. `config.py:248` still `position_size_fraction=base_fraction * 0.33` (no 0.5 multiplier).
- **Task 6 (daily reset wiring test):** non-code; effectively moot.
- **Open tasks:** Task 1 (imbalance ratio), Task 3 (max_concurrent=3 for Medium), Task 5 (5m halving), plus stale docstring cleanup.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix broken config overrides, wire daily PnL reset correctly, tune trading rules for better fill rates, disable broken rebalancing, and halve 5m allocation.

**Architecture:** Six targeted changes across config, risk manager, ladder manager, and .env. Each change is independent except that the Medium tier threshold change affects test boundary assertions. No new files created.

**Tech Stack:** Python 3.12, pytest

---

## File Map

| File | Change |
|------|--------|
| `.env` | Remove `MAX_IMBALANCE_RATIO=0.6` line |
| `polybot/config.py:328` | Fix `MAX_IMBALANCE_RATIO` fallback from `"0.60"` to `"0.35"` |
| `polybot/config.py:267-273` | Change Medium tier threshold from 500 to 400; change max_concurrent 5->3, fraction 0.06->0.10 |
| `polybot/config.py:173-181` | Multiply 5m position fraction by 0.5 |
| `polybot/strategy/ladder_manager.py:818-914` | Disable `check_imbalance` rebalancing (early return after accumulation guard) |
| `tests/test_config_new_fields.py:101-110` | Update boundary value tests for 400 threshold |
| `tests/test_config_tuning.py` (new) | New test file for all tuning changes |

## Do Not Touch

- `polybot/risk_manager.py` -- `reset_daily()` is correct, `_run_daily_reset()` in bot.py already calls it (verified at bot.py:1150)
- `polybot/bot.py` -- daily reset task is already wired at lines 292 and 329, calling `self.risk.reset_daily()` at line 1150
- `pair_cost < max_pair_cost` guard in `ladder_manager.py` -- not affected
- `_settled_markets` set in `bot.py` -- not affected
- The one-sided accumulation guard (lines 843-860 in `check_imbalance`) -- must be PRESERVED; only the `_tighten_light_side` calls are disabled

## Invariant Notes

- **pair_cost guard**: Unchanged. `max_pair_cost` values in `BotConfig` dataclass defaults (0.93) and `get_ladder_params()` are not modified.
- **_settled_markets**: Not touched.
- **get_trading_rules()**: Modified (tier threshold + Medium params). All callers get new behavior automatically since it is the single source of truth.
- **All existing tests**: Two boundary tests in `test_config_new_fields.py` must be updated to match new 400 threshold.

---

### Task 1: Fix MAX_IMBALANCE_RATIO .env override and config fallback

**Files:**
- Modify: `.env:32` -- remove `MAX_IMBALANCE_RATIO=0.6`
- Modify: `polybot/config.py:328` -- change fallback from `"0.60"` to `"0.35"`
- Test: `tests/test_config_tuning.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_config_tuning.py`:

```python
"""Tests for config and risk tuning changes (2026-03-30)."""
from polybot.config import BotConfig, load_bot_config


def test_max_imbalance_ratio_default():
    """BotConfig default max_imbalance_ratio should be 0.35."""
    cfg = BotConfig()
    assert cfg.max_imbalance_ratio == 0.35


def test_load_bot_config_imbalance_fallback(monkeypatch):
    """load_bot_config fallback for MAX_IMBALANCE_RATIO should be 0.35."""
    # Clear any env var so fallback is used
    monkeypatch.delenv("MAX_IMBALANCE_RATIO", raising=False)
    cfg = load_bot_config()
    assert cfg.max_imbalance_ratio == 0.35
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_tuning.py::test_load_bot_config_imbalance_fallback -v`
Expected: FAIL -- `load_bot_config()` returns 0.60 because the fallback string is `"0.60"`.

Note: `test_max_imbalance_ratio_default` will PASS because the dataclass default on line 104 is already 0.35. The bug is only in `load_bot_config()` line 328.

- [ ] **Step 3: Fix config.py fallback**

In `polybot/config.py`, line 328, change:
```python
        max_imbalance_ratio=float(os.getenv("MAX_IMBALANCE_RATIO", "0.60")),
```
to:
```python
        max_imbalance_ratio=float(os.getenv("MAX_IMBALANCE_RATIO", "0.35")),
```

- [ ] **Step 4: Remove MAX_IMBALANCE_RATIO from .env**

In `.env`, delete line 32:
```
MAX_IMBALANCE_RATIO=0.6
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config_tuning.py -v`
Expected: Both tests PASS.

- [ ] **Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add .env polybot/config.py tests/test_config_tuning.py
git commit -m "fix: MAX_IMBALANCE_RATIO fallback 0.60->0.35, remove .env override"
```

---

### Task 2: Lower Medium tier threshold from $500 to $400

**Files:**
- Modify: `polybot/config.py:267` -- change `if bankroll < 500` to `if bankroll < 400`
- Modify: `tests/test_config_new_fields.py:101-110` -- update boundary assertions
- Test: `tests/test_config_tuning.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_tuning.py`:

```python
from polybot.config import get_trading_rules, effective_assets


def test_medium_tier_threshold_at_400():
    """Medium tier should start at $400, not $500."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    # $400 is in Medium tier -> 2 assets
    rules = get_trading_rules(assets, 400.0)
    assert len(rules.assets) == 2
    assert rules.timeframes == (300, 900, 3600)

    # $399.99 is still Small tier -> 1 asset
    rules_small = get_trading_rules(assets, 399.99)
    assert len(rules_small.assets) == 1
    assert rules_small.timeframes == (300, 900)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_tuning.py::test_medium_tier_threshold_at_400 -v`
Expected: FAIL -- $400 currently falls in Small tier (threshold is 500).

- [ ] **Step 3: Change threshold in config.py**

In `polybot/config.py`, line 267, change:
```python
    if bankroll < 500:  # Small
```
to:
```python
    if bankroll < 400:  # Small
```

Update the docstring on line 243 to match:
```python
      Small  ($200-400):  1 asset, 5m+15m, 3 concurrent, 10% fraction
      Medium ($400-2000): 2 assets, all TFs, 3 concurrent, 10% fraction
```

- [ ] **Step 4: Update existing boundary tests**

In `tests/test_config_new_fields.py`, update `test_effective_assets_boundary_values` (lines 101-110):

```python
def test_effective_assets_boundary_values():
    """Boundary: $400 exactly -> 2 assets, $2000 exactly -> all."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    # $400 is in the "< 2000" bracket -> 2 assets
    assert len(effective_assets(assets, 400.0)) == 2
    # $2000 is in the ">= 2000" bracket -> all
    assert len(effective_assets(assets, 2000.0)) == 4
    # Just below boundaries
    assert len(effective_assets(assets, 399.99)) == 1
    assert len(effective_assets(assets, 1999.99)) == 2
```

Also check `test_effective_assets_medium_bankroll` on line 72 -- it uses bankroll=1000.0 which is still in Medium tier, so no change needed.

And `test_effective_assets_low_bankroll` on line 66 uses bankroll=300.0 which is still in Small tier, so no change needed.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_config_tuning.py tests/test_config_new_fields.py -v`
Expected: All PASS.

- [ ] **Step 6: Commit**

```bash
git add polybot/config.py tests/test_config_new_fields.py tests/test_config_tuning.py
git commit -m "feat: lower Medium tier threshold from $500 to $400"
```

---

### Task 3: Fewer concurrent, bigger budgets for Medium tier

**Files:**
- Modify: `polybot/config.py:268-273` -- Medium tier: max_concurrent 5->3, fraction 0.06->0.10
- Test: `tests/test_config_tuning.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_tuning.py`:

```python
def test_medium_tier_concentrated_budget():
    """Medium tier: 3 concurrent markets, 10% fraction."""
    assets = ("BTC", "ETH", "SOL", "XRP")
    rules = get_trading_rules(assets, 1000.0)
    assert rules.max_concurrent == 3
    assert rules.position_fraction == 0.10
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_tuning.py::test_medium_tier_concentrated_budget -v`
Expected: FAIL -- currently max_concurrent=5, fraction=0.06.

- [ ] **Step 3: Update Medium tier in config.py**

In `polybot/config.py`, lines 268-273, change:
```python
    if bankroll < 2000:  # Medium
        return TradingRules(
            assets=tuple(sorted_assets[:2]),
            timeframes=(300, 900, 3600),
            max_concurrent=5,
            position_fraction=0.06,
        )
```
to:
```python
    if bankroll < 2000:  # Medium
        return TradingRules(
            assets=tuple(sorted_assets[:2]),
            timeframes=(300, 900, 3600),
            max_concurrent=3,
            position_fraction=0.10,
        )
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config_tuning.py -v`
Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
git add polybot/config.py tests/test_config_tuning.py
git commit -m "feat: Medium tier — 3 concurrent, 10% fraction for better fill rates"
```

---

### Task 4: Disable broken rebalancing in check_imbalance

**Files:**
- Modify: `polybot/strategy/ladder_manager.py:818-914` -- early return after accumulation guard, skip `_tighten_light_side` calls
- Test: `tests/test_config_tuning.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_tuning.py`:

```python
from unittest.mock import MagicMock, patch
from polybot.strategy.ladder_manager import LadderManager, LadderState
from polybot.strategy.position_manager import PositionManager
from polybot.types import Side
import time


def _make_ladder_manager_for_imbalance():
    """Create a LadderManager with mocked deps for imbalance testing."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    executor = MagicMock()
    executor.get_best_ask = MagicMock(return_value=0.50)
    executor.place_batch_limit_buys = MagicMock(return_value=[])
    tracker = MagicMock()
    tracker.get_resting = MagicMock(return_value=[])
    tracker.get_resting_side = MagicMock(return_value=[])
    pm = PositionManager(cfg, bankroll=1000.0)
    risk = MagicMock()
    risk.is_halted = MagicMock(return_value=False)
    return LadderManager(cfg, executor, tracker, pm, risk)


def test_check_imbalance_does_not_call_tighten():
    """check_imbalance should NOT call _tighten_light_side (disabled)."""
    lm = _make_ladder_manager_for_imbalance()
    state = LadderState(
        up_token_id="up", dn_token_id="dn", timeframe_sec=900,
        anchor_up=0.45, anchor_dn=0.45,
    )
    lm.ladders["test-market"] = state

    # Simulate moderate imbalance: UP has 10, DN has 3
    lm.tracker.filled_qty = MagicMock(side_effect=lambda mid, side: 10.0 if side == Side.UP else 3.0)

    with patch.object(lm, '_tighten_light_side') as mock_tighten:
        lm.check_imbalance(int(time.time()))
        mock_tighten.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_tuning.py::test_check_imbalance_does_not_call_tighten -v`
Expected: FAIL -- `_tighten_light_side` gets called for moderate imbalance.

- [ ] **Step 3: Modify check_imbalance**

In `polybot/strategy/ladder_manager.py`, modify the `check_imbalance` method. The accumulation guard (lines 843-860) that cancels the heavy side when ratio > 5:1 must be PRESERVED. Only the `_tighten_light_side` calls on lines 878 and 902 must be disabled.

Replace the section from line 862 (after the accumulation guard's `continue`) through end of the method with:

```python
            # DISABLED: rebalancing via _tighten_light_side has broken call sites.
            # The accumulation guard above (>5:1 ratio) still cancels heavy side.
            # Full rebalancing fix tracked separately — do not re-enable without fixing
            # _tighten_light_side's 3 broken call sites first.
            if imbalance > self.cfg.max_imbalance_ratio:
                if state.imbalance_alert_at is None:
                    state.imbalance_alert_at = now_epoch
                    # Cancel heavy side's unfilled rungs (this part works fine)
                    self._flush_uncredited_to_positions(mid)
                    cancelled = self.tracker.cancel_side(mid, heavy_side)
                    for oid in cancelled:
                        self.executor.cancel_order(oid)
                    self.tracker.confirm_cancels(cancelled)
                    logger.warning(
                        "IMBALANCE SEVERE (no rebalance): %s imb=%.0f%% — cancelled %d %s rungs",
                        mid, imbalance * 100, len(cancelled), heavy_side.value,
                    )
                    acted.append(mid)
                elif now_epoch - state.imbalance_alert_at > self.cfg.imbalance_timeout_sec:
                    logger.warning("IMBALANCE TIMEOUT: %s — accepting one-sided position", mid)
                    state.imbalance_accepted = True
                    state.imbalance_alert_at = None
                    acted.append(mid)

            elif imbalance > self.cfg.rebalance_moderate_threshold:
                logger.info(
                    "IMBALANCE MODERATE (no rebalance): %s imb=%.0f%% — logging only",
                    mid, imbalance * 100,
                )
            else:
```

Keep everything after the `else:` (the imbalance recovery logic) unchanged.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config_tuning.py::test_check_imbalance_does_not_call_tighten -v`
Expected: PASS.

- [ ] **Step 5: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass. The existing `test_ladder_manager.py` tests that reference `check_imbalance` should still pass since the accumulation guard path is preserved.

- [ ] **Step 6: Commit**

```bash
git add polybot/strategy/ladder_manager.py tests/test_config_tuning.py
git commit -m "fix: disable broken _tighten_light_side rebalancing, keep accumulation guard"
```

---

### Task 5: Halve 5m position allocation

**Files:**
- Modify: `polybot/config.py:173-181` -- multiply 5m `position_size_fraction` by 0.5
- Test: `tests/test_config_tuning.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config_tuning.py`:

```python
def test_5m_fraction_is_half_of_15m():
    """5m position fraction should be ~half of what it would be without the halving.

    Currently 5m uses base_fraction * 0.33. With halving it should be base_fraction * 0.33 * 0.5.
    For $1000 bankroll (Medium tier, fraction=0.10): 5m = 0.10 * 0.33 * 0.5 = 0.0165
    """
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    lp_5m = cfg.get_ladder_params(300, current_bankroll=1000.0)
    lp_15m = cfg.get_ladder_params(900, current_bankroll=1000.0)

    # 5m should be roughly half of (15m * 0.33)
    expected_5m = lp_15m.position_size_fraction * 0.33 * 0.5
    assert abs(lp_5m.position_size_fraction - expected_5m) < 0.001


def test_5m_fraction_smaller_than_before():
    """5m allocation at $1000: was base*0.33, now base*0.33*0.5."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    lp_5m = cfg.get_ladder_params(300, current_bankroll=1000.0)
    # Medium tier base_fraction = 0.10
    # Old: 0.10 * 0.33 = 0.033
    # New: 0.10 * 0.33 * 0.5 = 0.0165
    assert lp_5m.position_size_fraction < 0.025  # well below old 0.033
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config_tuning.py::test_5m_fraction_is_half_of_15m -v`
Expected: FAIL -- currently 5m uses `base_fraction * 0.33` without the 0.5 multiplier.

- [ ] **Step 3: Modify get_ladder_params for 5m**

In `polybot/config.py`, line 180, change:
```python
                position_size_fraction=base_fraction * 0.33,
```
to:
```python
                position_size_fraction=base_fraction * 0.33 * 0.5,  # halved: 5m has poor fill rates
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config_tuning.py -v`
Expected: All PASS.

- [ ] **Step 5: Check existing 5m tests still pass**

Run: `pytest tests/test_config_new_fields.py::test_get_ladder_params_5m_dynamic_bankroll -v`
Expected: PASS -- the test only checks that `lp_100.position_size_fraction > lp_50k.position_size_fraction` and `< 0.10`, both still true after halving.

- [ ] **Step 6: Commit**

```bash
git add polybot/config.py tests/test_config_tuning.py
git commit -m "feat: halve 5m position allocation for reduced exposure"
```

---

### Task 6: Verify daily PnL reset wiring (no code change needed)

**Files:**
- Test: `tests/test_config_tuning.py` (append)

The researcher flagged daily PnL reset as P0, but investigation shows it is already correctly wired:
- `bot.py:292` and `bot.py:329` both create `_run_daily_reset()` task
- `bot.py:1150` calls `self.risk.reset_daily()`
- `risk_manager.py:84-87` resets `daily_pnl` and `consecutive_losses`

We add a test to document this wiring and prevent regression.

- [ ] **Step 1: Write the verification test**

Append to `tests/test_config_tuning.py`:

```python
from polybot.risk_manager import RiskManager


def test_risk_manager_reset_daily():
    """reset_daily should zero out daily_pnl and consecutive_losses."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    risk = RiskManager(cfg, starting_bankroll=1000.0)

    # Simulate losses
    risk.update_pnl(-20.0)
    risk.update_pnl(-15.0)
    assert risk.daily_pnl == -35.0
    assert risk.consecutive_losses == 2

    # Reset
    risk.reset_daily()
    assert risk.daily_pnl == 0.0
    assert risk.consecutive_losses == 0
    assert risk.is_halted() is False
```

- [ ] **Step 2: Run test**

Run: `pytest tests/test_config_tuning.py::test_risk_manager_reset_daily -v`
Expected: PASS (no code change needed -- this confirms the wiring works).

- [ ] **Step 3: Commit**

```bash
git add tests/test_config_tuning.py
git commit -m "test: verify daily PnL reset wiring is correct"
```

---

### Task 7: Final validation

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`
Expected: All tests pass (existing + new).

- [ ] **Step 2: Spot-check config values**

Run a quick Python check:
```python
from polybot.config import BotConfig, get_trading_rules
cfg = BotConfig()
assert cfg.max_imbalance_ratio == 0.35
rules = get_trading_rules(("BTC", "ETH"), 500.0)
assert rules.max_concurrent == 3
assert rules.position_fraction == 0.10
lp = cfg.get_ladder_params(300, current_bankroll=500.0)
assert lp.position_size_fraction < 0.02  # halved 5m
print("All spot checks pass")
```

- [ ] **Step 3: Final commit if any cleanup needed**

```bash
git add -A
git commit -m "chore: config and risk tuning complete"
```
