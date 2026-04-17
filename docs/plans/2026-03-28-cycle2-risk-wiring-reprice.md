# Cycle 2: Risk Wiring, Reprice Tuning, Cancel Race Fix

## Status: PARTIAL (audited 2026-04-17)

- **Task 1 (reprice_threshold on LadderParams):** NOT DONE. `reprice_threshold` remains on `BotConfig` only (`config.py:101`, default 0.05). `LadderParams` has no `reprice_threshold` field. Per-timeframe thresholds (0.08/0.08/0.06) are not wired.
- **Task 2 (MIN_REPRICE_INTERVAL = 15):** NOT DONE. `ladder_manager.py:23` still `MIN_REPRICE_INTERVAL = 10.0`. Reprice path still reads `self.cfg.reprice_threshold` (the global field), not `lp.reprice_threshold`.
- **Task 3 (exposure_factor + check_capital_at_risk wiring):** DONE. `risk_manager.py` has both methods; `ladder_manager.py` calls `exposure_factor()` in budget scaling and `check_capital_at_risk` in post_ladder.
- **Task 4 (cancelling transient status):** DONE. `TrackedOrder.cancelling_at` field exists; `cancel_market`/`cancel_side` set "cancelling"; `confirm_cancelled` method exists; reconcile respects it.
- **Task 5 (integration smoke test):** would follow from 1-4 completion.
- **Open tasks:** Task 1 (LadderParams field + per-TF values), Task 2 (MIN_REPRICE_INTERVAL=15, use lp.reprice_threshold).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire dead-code risk guards into ladder budget, fix cancel-then-reprice false fill detection, and reduce excessive repricing via per-timeframe thresholds.

**Architecture:** Three independent fixes applied in order: (1) call `exposure_factor()` and `check_capital_at_risk()` in both ladder managers, (2) add a "cancelling" transient status to order tracker so reconcile skips recently-cancelled orders, (3) move `reprice_threshold` into `LadderParams` with per-timeframe values and increase the reprice cooldown.

**Tech Stack:** Python 3.11, pytest, polybot codebase

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/strategy/ladder_manager.py` | Modify | Wire risk guards, use per-TF reprice threshold, bump MIN_REPRICE_INTERVAL |
| `polybot/ladder_manager.py` | Modify | Mirror: wire risk guards, use per-TF reprice threshold |
| `polybot/strategy/order_tracker.py` | Modify | Add "cancelling" status, skip in reconcile, 30s timeout |
| `polybot/order_tracker.py` | Modify | Mirror: add "cancelling" status, skip in reconcile, 30s timeout |
| `polybot/config.py` | Modify | Add `reprice_threshold` to `LadderParams`, per-TF values |
| `polybot/risk_manager.py` | No change | Already has `exposure_factor()` and `check_capital_at_risk()` |
| `tests/test_risk_wiring_budget.py` | Create | Tests for Finding 1 |
| `tests/test_cancelling_status.py` | Create | Tests for Finding 3 |
| `tests/test_reprice_threshold.py` | Create | Tests for Finding 2 |

## Do Not Touch

- `polybot/risk_manager.py` - already correct, no changes needed
- `polybot/types.py` - no type changes
- `polybot/fees.py` - fee logic unchanged
- `polybot/bot.py` - orchestrator unchanged
- Pair cost guard logic in ladder_manager - invariant preserved (max_pair_cost < 0.90)
- `_settled_markets` set in `bot.py` - not touched
- `get_trading_rules()` in `config.py` - not touched (only `LadderParams` and `get_ladder_params()` change)

## Invariants

1. **pair_cost < max_pair_cost (0.90)** - Unchanged. Pair cost guard in ladder_manager is not modified.
2. **_settled_markets** - Not touched.
3. **get_trading_rules()** - Not touched. Only `LadderParams` NamedTuple and `get_ladder_params()` are modified.
4. **All existing tests must pass** - `LadderParams` gets a new field with a default, so existing constructors still work via the default.

---

### Task 1: Add `reprice_threshold` to `LadderParams` (Finding 2 - Config)

**Files:**
- Modify: `polybot/config.py`
- Test: `tests/test_reprice_threshold.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_reprice_threshold.py`:

```python
"""Tests for per-timeframe reprice thresholds."""

from polybot.config import BotConfig, LadderParams


def test_ladder_params_has_reprice_threshold():
    """LadderParams includes reprice_threshold field."""
    lp = LadderParams(
        rungs=10, spacing=0.01, width=0.20, size_skew=0.7,
        max_pair_cost=0.90, position_size_fraction=0.05,
        reprice_threshold=0.08,
    )
    assert lp.reprice_threshold == 0.08


def test_5m_reprice_threshold():
    """5m windows use 0.08 reprice threshold."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    lp = cfg.get_ladder_params(300, current_bankroll=1000.0)
    assert lp.reprice_threshold == 0.08


def test_15m_reprice_threshold():
    """15m windows use 0.08 reprice threshold."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    lp = cfg.get_ladder_params(900, current_bankroll=1000.0)
    assert lp.reprice_threshold == 0.08


def test_1h_reprice_threshold():
    """1h windows use 0.06 reprice threshold."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    lp = cfg.get_ladder_params(3600, current_bankroll=1000.0)
    assert lp.reprice_threshold == 0.06


def test_ladder_params_default_reprice_threshold():
    """LadderParams without explicit reprice_threshold gets the default."""
    # Existing code that constructs LadderParams with 6 positional args still works
    lp = LadderParams(
        rungs=10, spacing=0.01, width=0.20, size_skew=0.7,
        max_pair_cost=0.90, position_size_fraction=0.05,
    )
    assert lp.reprice_threshold == 0.08  # default
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reprice_threshold.py -v`
Expected: FAIL - `LadderParams` doesn't accept `reprice_threshold`

- [ ] **Step 3: Implement - modify LadderParams and get_ladder_params()**

In `polybot/config.py`, change the `LadderParams` NamedTuple (line 7-14) to add a new field with a default:

```python
class LadderParams(NamedTuple):
    """Timeframe-specific ladder parameters."""
    rungs: int
    spacing: float
    width: float
    size_skew: float
    max_pair_cost: float
    position_size_fraction: float
    reprice_threshold: float = 0.08  # default for 5m/15m
```

In `get_ladder_params()` (line 158-195), add `reprice_threshold` to each return:

For `timeframe_sec <= 300` (5m) return block around line 169-177, add:
```python
    reprice_threshold=0.08,
```

For `timeframe_sec <= 900` (15m) return block around line 179-186, add:
```python
    reprice_threshold=0.08,
```

For `1h+` return block around line 188-195, add:
```python
    reprice_threshold=0.06,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_reprice_threshold.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Run full test suite for regressions**

Run: `pytest tests/ -x -q`
Expected: All existing tests pass. The new `reprice_threshold` field has a default value so existing `LadderParams(...)` calls with 6 positional args still work.

- [ ] **Step 6: Commit**

```bash
git add polybot/config.py tests/test_reprice_threshold.py
git commit -m "feat: add per-timeframe reprice_threshold to LadderParams"
```

---

### Task 2: Use per-TF reprice threshold and bump cooldown (Finding 2 - Ladder Managers)

**Files:**
- Modify: `polybot/strategy/ladder_manager.py`
- Modify: `polybot/ladder_manager.py`
- Test: `tests/test_reprice_threshold.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_reprice_threshold.py`:

```python
import time
from unittest.mock import MagicMock

from polybot.strategy.ladder_manager import LadderManager as StrategyLadderManager
from polybot.strategy.ladder_manager import MIN_REPRICE_INTERVAL as STRATEGY_MIN_REPRICE
from polybot.strategy.order_tracker import OrderTracker as StrategyOrderTracker
from polybot.strategy.position_manager import PositionManager as StrategyPositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side


def test_strategy_min_reprice_interval_is_15():
    """MIN_REPRICE_INTERVAL in strategy/ladder_manager should be 15s."""
    assert STRATEGY_MIN_REPRICE == 15.0


def test_reprice_uses_per_tf_threshold():
    """Reprice should use the per-timeframe threshold from LadderParams, not cfg.reprice_threshold."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0, reprice_threshold=0.05)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    executor = MagicMock()
    tracker = StrategyOrderTracker()
    pos_mgr = StrategyPositionManager(cfg, bankroll=1000.0)
    lm = StrategyLadderManager(cfg, executor, tracker, pos_mgr, risk)

    now = time.time()
    market = MarketWindow(
        market_id="m1", condition_id="c1", asset="BTC",
        timeframe_sec=3600,  # 1h -> reprice_threshold=0.06
        up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=int(now) - 1800, close_epoch=int(now) + 1800,
    )

    # Set up a ladder state with anchors at 0.50
    from polybot.strategy.ladder_manager import LadderState
    lm.ladders["m1"] = LadderState(
        market_id="m1", asset="BTC",
        anchor_up=0.50, anchor_dn=0.50,
        posted_at=now - 100, last_reprice_at=now - 100,
        timeframe_sec=3600,
        up_token_id="tok_up", dn_token_id="tok_dn",
    )

    # Move ask by 0.055 - below old threshold (0.05 would trigger) but below 1h threshold (0.06)
    executor.get_best_ask.return_value = 0.555

    result = lm.reprice_if_needed({"m1": market})
    assert result == 0, "0.055 move should NOT trigger reprice with 1h threshold of 0.06"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reprice_threshold.py::test_reprice_uses_per_tf_threshold -v`
Expected: FAIL - currently uses `self.cfg.reprice_threshold` (0.05), so 0.055 move triggers reprice

- [ ] **Step 3: Implement changes in strategy/ladder_manager.py**

In `polybot/strategy/ladder_manager.py`:

**Line 37** - Change `MIN_REPRICE_INTERVAL`:
```python
MIN_REPRICE_INTERVAL = 15.0  # seconds between reprices for the same market
```

**Lines 493-494** in `reprice_if_needed()` - Replace `self.cfg.reprice_threshold` with per-TF threshold. Move the `lp = self.cfg.get_ladder_params(...)` call (currently at line 500) to BEFORE the threshold check (before line 493), and use `lp.reprice_threshold`:

```python
            # Select timeframe-specific ladder parameters (needed for threshold + rungs)
            lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)

            up_moved = abs(best_ask_up - state.anchor_up) > lp.reprice_threshold
            dn_moved = abs(best_ask_dn - state.anchor_dn) > lp.reprice_threshold
```

Remove the duplicate `lp = self.cfg.get_ladder_params(...)` call that was previously at line 500.

- [ ] **Step 4: Implement same changes in polybot/ladder_manager.py**

In `polybot/ladder_manager.py`:

**Line 37** - Change `MIN_REPRICE_INTERVAL`:
```python
MIN_REPRICE_INTERVAL = 15.0  # seconds between reprices for the same market
```

**Lines 374-375** in `reprice_if_needed()` - Same pattern: move the `lp = ...` call (currently line 381) to before the threshold check, use `lp.reprice_threshold`:

```python
            # Select timeframe-specific ladder parameters (needed for threshold + rungs)
            lp = self.cfg.get_ladder_params(market.timeframe_sec)

            up_moved = abs(best_ask_up - state.anchor_up) > lp.reprice_threshold
            dn_moved = abs(best_ask_dn - state.anchor_dn) > lp.reprice_threshold
```

Remove the duplicate `lp = ...` that was at line 381.

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_reprice_threshold.py -v`
Expected: All PASS

Run: `pytest tests/ -x -q`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add polybot/strategy/ladder_manager.py polybot/ladder_manager.py tests/test_reprice_threshold.py
git commit -m "feat: per-timeframe reprice thresholds, 15s cooldown"
```

---

### Task 3: Wire `exposure_factor()` and `check_capital_at_risk()` (Finding 1)

**Files:**
- Modify: `polybot/strategy/ladder_manager.py`
- Modify: `polybot/ladder_manager.py`
- Test: `tests/test_risk_wiring_budget.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_risk_wiring_budget.py`:

```python
"""Tests for risk guard wiring into ladder budget."""

import time
from unittest.mock import MagicMock, patch

from polybot.config import BotConfig
from polybot.strategy.ladder_manager import LadderManager as StrategyLadderManager
from polybot.strategy.order_tracker import OrderTracker as StrategyOrderTracker
from polybot.strategy.order_tracker import TrackedOrder as StrategyTrackedOrder
from polybot.strategy.position_manager import PositionManager as StrategyPositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side


def _make_market(remaining_sec=300, timeframe_sec=900):
    now = int(time.time())
    return MarketWindow(
        market_id="test-m1", condition_id="cond-1", asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up", dn_token_id="tok_dn",
        open_epoch=now - (timeframe_sec - remaining_sec),
        close_epoch=now + remaining_sec,
    )


def _make_lm(cfg=None, bankroll=1000.0, consecutive_losses=0):
    cfg = cfg or BotConfig(dry_run=True, bankroll=bankroll)
    risk = RiskManager(cfg, starting_bankroll=bankroll)
    # Simulate consecutive losses
    for _ in range(consecutive_losses):
        risk.update_pnl(-1.0)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.place_batch_limit_buys.return_value = []
    tracker = StrategyOrderTracker()
    pos_mgr = StrategyPositionManager(cfg, bankroll=bankroll)
    return StrategyLadderManager(cfg, executor, tracker, pos_mgr, risk)


def test_exposure_factor_scales_budget_after_3_losses():
    """After 3 consecutive losses, exposure_factor() returns 0.5, halving the budget."""
    lm = _make_lm(bankroll=1000.0, consecutive_losses=3)
    assert lm.risk.exposure_factor() == 0.5

    # Post a ladder - the budget should be halved compared to no-loss scenario
    market = _make_market(remaining_sec=300)
    # We can't easily check the exact budget, but we verify the method is called
    # by checking that the risk.exposure_factor is wired in
    with patch.object(lm.risk, 'exposure_factor', return_value=0.5) as mock_ef:
        lm.post_ladder(market)
        mock_ef.assert_called_once()


def test_exposure_factor_is_1_with_no_losses():
    """With 0 consecutive losses, exposure_factor() returns 1.0 (no scaling)."""
    lm = _make_lm(bankroll=1000.0, consecutive_losses=0)
    assert lm.risk.exposure_factor() == 1.0


def test_capital_at_risk_blocks_new_ladder():
    """When committed capital exceeds max_capital_at_risk_pct, post_ladder returns 0."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0, max_capital_at_risk_pct=0.40)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.place_batch_limit_buys.return_value = []
    tracker = StrategyOrderTracker()
    pos_mgr = StrategyPositionManager(cfg, bankroll=1000.0)
    lm = StrategyLadderManager(cfg, executor, tracker, pos_mgr, risk)

    # Simulate 50% of bankroll already committed via filled positions
    pos_mgr.update_position("other-market", Side.UP, 100.0, 500.0)

    market = _make_market(remaining_sec=300)
    result = lm.post_ladder(market)
    assert result == 0, "Should block new ladder when >40% capital committed"


def test_capital_at_risk_allows_when_under_limit():
    """When committed capital is under the limit, ladder posting proceeds."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0, max_capital_at_risk_pct=0.40)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.place_batch_limit_buys.return_value = []
    tracker = StrategyOrderTracker()
    pos_mgr = StrategyPositionManager(cfg, bankroll=1000.0)
    lm = StrategyLadderManager(cfg, executor, tracker, pos_mgr, risk)

    # Only 10% committed - should be allowed
    pos_mgr.update_position("other-market", Side.UP, 10.0, 100.0)

    market = _make_market(remaining_sec=300)
    # Will return 0 because mock executor returns no orders, but it should
    # get past the capital-at-risk check (not return 0 early from that guard)
    with patch.object(lm.risk, 'check_capital_at_risk', return_value=True) as mock_car:
        lm.post_ladder(market)
        mock_car.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_risk_wiring_budget.py -v`
Expected: FAIL - `exposure_factor()` and `check_capital_at_risk()` are never called

- [ ] **Step 3: Implement in strategy/ladder_manager.py**

In `polybot/strategy/ladder_manager.py`, modify `_post_ladder_core()`:

**After line 204** (after `budget = min(budget_base * lp.position_size_fraction, available)`), add:

```python
        # Risk: scale budget by exposure factor (halved after 3+ consecutive losses)
        budget *= self.risk.exposure_factor()
```

**In `post_ladder()`**, after the `can_open_position` check (after line 160) and before the `can_trade_in_window` check, add:

```python
            if not self.risk.check_capital_at_risk(self.total_committed(), self.positions.bankroll):
                logger.info("CAPITAL AT RISK: committed $%.2f > %.0f%% of bankroll $%.2f, skipping",
                            self.total_committed(), self.cfg.max_capital_at_risk_pct * 100,
                            self.positions.bankroll)
                return 0
```

**In `post_ladder_pre_open()`**, after the `can_open_position` check (after line 178), add the same capital-at-risk check:

```python
            if not self.risk.check_capital_at_risk(self.total_committed(), self.positions.bankroll):
                logger.info("CAPITAL AT RISK: committed $%.2f > %.0f%% of bankroll $%.2f, skipping",
                            self.total_committed(), self.cfg.max_capital_at_risk_pct * 100,
                            self.positions.bankroll)
                return 0
```

- [ ] **Step 4: Implement in polybot/ladder_manager.py**

In `polybot/ladder_manager.py`, modify `post_ladder()`:

**After the `can_open_position` check (line 155-156)**, add:

```python
            if not self.risk.check_capital_at_risk(self.total_committed(), self.positions.bankroll):
                logger.info("CAPITAL AT RISK: committed $%.2f > %.0f%% of bankroll $%.2f, skipping",
                            self.total_committed(), self.cfg.max_capital_at_risk_pct * 100,
                            self.positions.bankroll)
                return 0
```

**After the budget computation (after line 177** `budget = min(budget_base * lp.position_size_fraction, available)`**)**, add:

```python
            # Risk: scale budget by exposure factor (halved after 3+ consecutive losses)
            budget *= self.risk.exposure_factor()
```

- [ ] **Step 5: Run tests**

Run: `pytest tests/test_risk_wiring_budget.py -v`
Expected: All PASS

Run: `pytest tests/ -x -q`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add polybot/strategy/ladder_manager.py polybot/ladder_manager.py tests/test_risk_wiring_budget.py
git commit -m "feat: wire exposure_factor and check_capital_at_risk into ladder budget"
```

---

### Task 4: Add "cancelling" transient status to OrderTracker (Finding 3)

**Files:**
- Modify: `polybot/strategy/order_tracker.py`
- Modify: `polybot/order_tracker.py`
- Test: `tests/test_cancelling_status.py` (create)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_cancelling_status.py`:

```python
"""Tests for 'cancelling' transient status in OrderTracker."""

import time
from polybot.strategy.order_tracker import OrderTracker, TrackedOrder
from polybot.types import Side


def _make_order(oid="o1", market_id="m1", side=Side.UP, price=0.45, size=10.0):
    return TrackedOrder(
        order_id=oid, market_id=market_id, token_id="tok",
        side=side, price=price, size=size, placed_at=time.time(),
    )


def test_cancel_side_sets_cancelling_status():
    """cancel_side should set status to 'cancelling', not 'cancelled'."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1", side=Side.UP))
    tracker.add(_make_order("o2", side=Side.DOWN))

    cancelled_ids = tracker.cancel_side("m1", Side.UP)
    assert cancelled_ids == ["o1"]
    assert tracker.orders["o1"].status == "cancelling"
    # DOWN side unaffected
    assert tracker.orders["o2"].status == "resting"


def test_cancel_market_sets_cancelling_status():
    """cancel_market should set status to 'cancelling', not 'cancelled'."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1"))
    tracker.add(_make_order("o2"))

    cancelled_ids = tracker.cancel_market("m1")
    assert set(cancelled_ids) == {"o1", "o2"}
    assert tracker.orders["o1"].status == "cancelling"
    assert tracker.orders["o2"].status == "cancelling"


def test_reconcile_skips_cancelling_orders():
    """Orders in 'cancelling' status should not be treated as filled when missing from exchange."""
    tracker = OrderTracker()
    order = _make_order("o1")
    tracker.add(order)

    # Mark as cancelling (simulates cancel_side was called)
    cancelled_ids = tracker.cancel_side("m1", Side.UP)
    assert cancelled_ids == ["o1"]

    # Reconcile with empty exchange (order not found)
    result = tracker.reconcile([])
    # Should NOT appear in filled list
    assert result["filled"] == []
    # Order should now be fully cancelled
    assert tracker.orders["o1"].status == "cancelled"


def test_reconcile_reverts_cancelling_if_still_on_exchange():
    """If a 'cancelling' order is still on exchange, revert to resting."""
    tracker = OrderTracker()
    order = _make_order("o1")
    tracker.add(order)
    tracker.cancel_side("m1", Side.UP)

    # Exchange still shows the order
    result = tracker.reconcile([{"id": "o1", "size_matched": "0"}])
    assert tracker.orders["o1"].status == "resting"
    assert "o1" in result["reverted"]


def test_cancelling_timeout_marks_cancelled():
    """Orders in 'cancelling' for >30s should be marked 'cancelled' by reconcile."""
    tracker = OrderTracker()
    order = _make_order("o1")
    order.placed_at = time.time() - 100  # old order
    tracker.add(order)

    # Mark cancelling with old timestamp
    tracker.cancel_side("m1", Side.UP)
    # Backdate the cancelling_at timestamp
    tracker.orders["o1"].cancelling_at = time.time() - 35

    # Reconcile with empty exchange
    result = tracker.reconcile([])
    assert tracker.orders["o1"].status == "cancelled"
    assert result["filled"] == []


def test_get_resting_excludes_cancelling():
    """get_resting should not include 'cancelling' orders."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1"))
    tracker.add(_make_order("o2"))
    tracker.cancel_side("m1", Side.UP)

    # Both are cancelling, neither should appear as resting
    assert tracker.get_resting("m1") == []


def test_all_resting_ids_excludes_cancelling():
    """all_resting_ids should not include 'cancelling' orders."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1"))
    tracker.cancel_side("m1", Side.UP)
    assert "o1" not in tracker.all_resting_ids()


def test_confirm_cancelled_transitions_cancelling():
    """confirm_cancelled() marks 'cancelling' orders as 'cancelled'."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1"))
    tracker.cancel_side("m1", Side.UP)
    assert tracker.orders["o1"].status == "cancelling"

    tracker.confirm_cancelled(["o1"])
    assert tracker.orders["o1"].status == "cancelled"


def test_has_orders_excludes_cancelling():
    """has_orders should return False when all orders are cancelling."""
    tracker = OrderTracker()
    tracker.add(_make_order("o1"))
    tracker.cancel_side("m1", Side.UP)
    assert tracker.has_orders("m1") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_cancelling_status.py -v`
Expected: FAIL - no `cancelling` status, no `cancelling_at` field, no `confirm_cancelled` method

- [ ] **Step 3: Implement in strategy/order_tracker.py**

In `polybot/strategy/order_tracker.py`:

**TrackedOrder dataclass** (around line 11-20) - add a new field:

```python
@dataclass
class TrackedOrder:
    order_id: str
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    filled: float = 0.0
    status: str = "resting"  # resting, partial, filled, cancelling, cancelled
    placed_at: float = 0.0
    credited_to_pm: float = 0.0
    cancelling_at: float | None = None  # timestamp when cancel was initiated
```

**cancel_market** (line 49-57) - change `"cancelled"` to `"cancelling"` and set timestamp:

```python
    def cancel_market(self, market_id: str) -> list[str]:
        """Cancel all resting/partial/unknown orders for a market. Returns order IDs to cancel on exchange."""
        cancelled = []
        now = time.time()
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.status in ("resting", "partial", "unknown"):
                order.status = "cancelling"
                order.cancelling_at = now
                cancelled.append(oid)
        return cancelled
```

**cancel_side** (line 59-67) - same pattern:

```python
    def cancel_side(self, market_id: str, side: Side) -> list[str]:
        """Cancel all resting/partial/unknown orders for one side of a market."""
        cancelled = []
        now = time.time()
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.side == side and order.status in ("resting", "partial", "unknown"):
                order.status = "cancelling"
                order.cancelling_at = now
                cancelled.append(oid)
        return cancelled
```

**Add `import time`** at the top of the file (after `from __future__ import annotations`).

**Add `confirm_cancelled` method** after `cancel_side`:

```python
    def confirm_cancelled(self, order_ids: list[str]) -> None:
        """Mark orders as fully cancelled after exchange confirms cancellation."""
        for oid in order_ids:
            order = self.orders.get(oid)
            if order and order.status == "cancelling":
                order.status = "cancelled"
                order.cancelling_at = None
```

**Modify `reconcile()`** (around line 157-201) - handle "cancelling" status:

In the loop over orders, add handling for "cancelling" status. Replace the `elif order.status in ("resting", "partial"):` block:

```python
            if on_exchange:
                # Check for partial fills via size_matched field
                exch_order = exchange_by_id.get(order.order_id, {})
                size_matched = exch_order.get("size_matched")
                if size_matched is not None:
                    matched = float(size_matched)
                    new_fill = matched - order.filled
                    if new_fill > 0.001:
                        self.update_fill(order.order_id, new_fill)
                        if order.status == "filled":
                            filled.append(order)
                        else:
                            partial.append(order)

                # Revert "unknown", "cancelled", or "cancelling" if still on exchange
                if order.status in ("unknown", "cancelled", "cancelling"):
                    order.status = "resting"
                    order.cancelling_at = None
                    reverted.append(order.order_id)
            elif order.status in ("resting", "partial"):
                # Order disappeared from exchange — treat remaining as filled
                fill_qty = order.size - order.filled
                if fill_qty > 0:
                    self.update_fill(order.order_id, fill_qty)
                    filled.append(order)
            elif order.status == "cancelling":
                # Order is being cancelled — don't treat as filled
                # After 30s timeout, mark as fully cancelled
                CANCELLING_TIMEOUT = 30.0
                if order.cancelling_at and (time.time() - order.cancelling_at > CANCELLING_TIMEOUT):
                    order.status = "cancelled"
                    order.cancelling_at = None
                else:
                    # Still within timeout — mark as cancelled (exchange confirmed removal)
                    order.status = "cancelled"
                    order.cancelling_at = None
```

**Note on `mark_all_unknown`** (line 152-155) - also handle "cancelling":

```python
    def mark_all_unknown(self) -> None:
        for order in self.orders.values():
            if order.status not in ("filled", "cancelling"):
                order.status = "unknown"
```

This preserves "cancelling" status during reconnection so they don't become false fills.

- [ ] **Step 4: Implement the same changes in polybot/order_tracker.py**

Apply identical changes to `polybot/order_tracker.py`:
- Add `import time`
- Add `cancelling_at: float | None = None` to `TrackedOrder`
- Change `cancel_market` and `cancel_side` to use `"cancelling"` status
- Add `confirm_cancelled()` method
- Modify `reconcile()` to handle `"cancelling"` status
- Update `mark_all_unknown` to preserve `"cancelling"`

The code is identical to the strategy/ version.

- [ ] **Step 5: Wire confirm_cancelled in ladder managers**

In both `polybot/strategy/ladder_manager.py` and `polybot/ladder_manager.py`:

After `self.executor.cancel_batch(cancelled)` calls in `reprice_if_needed()`, add:
```python
                    self.tracker.confirm_cancelled(cancelled)
```

There are two places in each file where `cancel_batch` is called inside `reprice_if_needed()` (one for UP side, one for DN side). Add `confirm_cancelled` after each.

Similarly, in `cancel_ladder()` after the cancel loop, add:
```python
        self.tracker.confirm_cancelled(cancelled)
```

And in `check_imbalance()` after the cancel loop:
```python
                    self.tracker.confirm_cancelled(cancelled)
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/test_cancelling_status.py -v`
Expected: All PASS

Run: `pytest tests/ -x -q`
Expected: All existing tests pass. Key concern: existing tests that call `cancel_side`/`cancel_market` and then check `status == "cancelled"` will need the status to be `"cancelling"` instead. Check `tests/test_order_tracker.py` for such assertions and update them.

- [ ] **Step 7: Fix any broken existing tests**

Existing tests in `tests/test_order_tracker.py` and `tests/test_reconciliation.py` may assert `status == "cancelled"` after `cancel_side()` or `cancel_market()`. These need to be updated to assert `status == "cancelling"` instead, OR call `confirm_cancelled()` before the assertion.

Search for patterns like:
- `order.status == "cancelled"` after `cancel_side` or `cancel_market`
- Tests that call `cancel_market` then check the status

For each found assertion, change `"cancelled"` to `"cancelling"` if the test is checking immediately after cancel (before exchange confirmation).

- [ ] **Step 8: Commit**

```bash
git add polybot/strategy/order_tracker.py polybot/order_tracker.py \
       polybot/strategy/ladder_manager.py polybot/ladder_manager.py \
       tests/test_cancelling_status.py tests/test_order_tracker.py tests/test_reconciliation.py
git commit -m "fix: cancelling transient status prevents false fills during reprice"
```

---

### Task 5: Integration sanity test

**Files:**
- Test: `tests/test_risk_wiring_budget.py` (append)

- [ ] **Step 1: Write integration test**

Append to `tests/test_risk_wiring_budget.py`:

```python
def test_all_three_fixes_integrated():
    """Smoke test: exposure_factor + capital_at_risk + per-TF threshold all active together."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0, max_capital_at_risk_pct=0.40)
    risk = RiskManager(cfg, starting_bankroll=1000.0)
    # 3 consecutive losses -> exposure_factor = 0.5
    risk.update_pnl(-1.0)
    risk.update_pnl(-1.0)
    risk.update_pnl(-1.0)
    assert risk.exposure_factor() == 0.5
    assert risk.is_halted() is False  # not at 5 losses yet

    executor = MagicMock()
    executor.get_best_ask.return_value = 0.45
    executor.place_batch_limit_buys.return_value = []
    tracker = StrategyOrderTracker()
    pos_mgr = StrategyPositionManager(cfg, bankroll=1000.0)
    lm = StrategyLadderManager(cfg, executor, tracker, pos_mgr, risk)

    # Capital at risk is 0% (no positions) -> allowed
    assert risk.check_capital_at_risk(lm.total_committed(), pos_mgr.bankroll) is True

    # Per-TF threshold: 1h market should use 0.06
    lp = cfg.get_ladder_params(3600, current_bankroll=1000.0)
    assert lp.reprice_threshold == 0.06
```

- [ ] **Step 2: Run full suite**

Run: `pytest tests/ -x -q`
Expected: All tests pass

- [ ] **Step 3: Commit**

```bash
git add tests/test_risk_wiring_budget.py
git commit -m "test: integration smoke test for cycle 2 risk+reprice fixes"
```
