# Cycle 29 — Reprice-Path Gate Persistence

**Date**: 2026-04-18
**Priority**: Critical — cycle 28 root-cause fix
**Scope**: Single file: `polybot/strategy/ladder_manager.py` + tests

## Problem

The book-mid gate in `post_ladder()` sets local `budget_up` / `budget_dn` variables such that
only one side (the gate winner) gets allocated. This works for the initial ladder post.

However, `reprice_if_needed()` recomputes budgets from scratch using
`inventory_skew_enabled` logic (line 1487ff) and reposts BOTH sides whenever `up_moved` or
`dn_moved` crosses `reprice_threshold`. The gate's one-sided decision is lost on the first
reprice (~10s after open on a 15m market), nullifying the gate entirely.

Evidence: market `btc-updown-15m-1776474900`, gate fired "post DN only $18"; by settlement
`up_qty=198.3, dn_qty=8.9` — inverted.

## Fix — persist gate decision on `LadderState`

### Change 1: Extend `LadderState` (line 26)

Add three fields:

```python
gate_fired: bool = False
gate_winner_side: Side | None = None  # the side to post when gate fires
gate_budget_cap: float = 0.0  # $ cap from book-mid gate
```

### Change 2: Set fields on `LadderState` when gate fires in `post_ladder()`

After the gate decision (~line 1058 where `book_mid_gate_fired = True` is set), once the
`LadderState` is created/stored (look for the `LadderState(...)` construction or
`self.ladders[mid] = state` assignment), also persist:

- `state.gate_fired = True`
- `state.gate_winner_side = Side.UP` if `budget_dn == 0.0` else `Side.DOWN`
- `state.gate_budget_cap = _capped_bmg`

Additionally, set `state.is_directional = True` if not already.

**Also handle the `skip_on_gate_miss` path (line 1108)**: when the gate misses and
`skip_on_gate_miss` is true, the ladder must NOT be posted at all — but if a LadderState
already exists from a prior attempt, we need to be defensive. Preferred: the current code
returns early without creating state, so reprice will simply not find the ladder. Confirm
this by reading the skip-on-gate-miss branch.

### Change 3: Honor persisted gate decision in `reprice_if_needed()`

Near the top of the per-market loop in `reprice_if_needed()` (after fetching `state`, before
computing `budget_up_side`/`budget_dn_side`, around line 1487):

```python
# Gate persistence: if the book-mid gate fired when this ladder was posted,
# reprice must honor the one-sided decision. Only repost the winner side.
if state.gate_fired and state.gate_winner_side is not None:
    if state.gate_winner_side == Side.UP:
        budget_up_side = min(half_budget, state.gate_budget_cap)
        budget_dn_side = 0.0
    else:
        budget_up_side = 0.0
        budget_dn_side = min(half_budget, state.gate_budget_cap)
else:
    # ... existing inventory-skew logic ...
```

Wrap the existing `if self.cfg.inventory_skew_enabled ...` block in an `else` branch so
gate-fired ladders bypass inventory skew entirely.

The existing `side_budget = max(0, budget_up_side - up_filled_cost)` logic below will then
naturally zero out the loser side (because `budget_up_side = 0` → `side_budget = 0` →
`side_budget >= 1.0` is false → no orders placed). We should also ensure the loser-side
`cancel_side` still runs if `up_moved`/`dn_moved` is true so stale loser-side orders (if
any exist from a prior bug) get cancelled. The existing code already cancels unconditionally
inside the `if up_moved:` / `if dn_moved:` branches, so this is fine.

### Change 4: Logging

In `reprice_if_needed()`, when gate-persistence is active, add a debug log:

```python
logger.debug("REPRICE gate-persist: %s winner=%s cap=$%.2f",
             mid, state.gate_winner_side.value, state.gate_budget_cap)
```

## Tests — add to `tests/test_book_mid_gate.py` (or new file if that doesn't exist)

Find existing gate tests with `grep`. Add:

### Test 1: `test_reprice_honors_gate_fired_one_sided`
- Set up a market, fire the gate with UP winner
- Trigger reprice (simulate price move > threshold on the DN side)
- Assert: `executor.place_batch_limit_buys` called only for `Side.UP` tokens; DN order list is empty

### Test 2: `test_reprice_honors_gate_fired_budget_cap`
- Gate fires with `gate_budget_cap=$10`
- On reprice, assert total UP budget allocated ≤ $10 (not the default half_budget)

### Test 3: `test_reprice_skip_on_gate_miss_true`
- Configure `skip_on_gate_miss=true`, gate misses (crossed book)
- Assert no LadderState is created, so reprice finds nothing — no orders placed

### Test 4: `test_reprice_no_gate_keeps_existing_behavior`
- Gate disabled / not fired
- Reprice behaves as before (bilateral inventory-skew logic)
- Assert both UP and DN orders are placed

### Test 5: `test_reprice_gate_fired_dn_winner`
- Mirror of test 1 but with DN as winner; confirms both directions work

## Deployment

1. Commit message: `fix(strategy): persist gate decision across reprice (cycle 29)`
2. Restart bot, verify via log `REPRICE gate-persist` lines appear on gate-fired markets
3. Monitor first 5 gate-fired settlements: `up_qty` should be ~0 on DN-winner markets, and vice versa

## Rollback

If after 20 settlements, rolling PnL is more negative than pre-ship baseline, revert the
commit and redeploy. The fix is additive/well-scoped so rollback is trivial.
