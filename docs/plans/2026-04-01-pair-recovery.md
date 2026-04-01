# Plan: Pair Recovery Strategy (D->B: Boost then Force-Buy)
Date: 2026-04-01
Source: Direct request
Status: complete

## Summary
Implement a two-phase pair recovery strategy to convert one-sided fills into profitable bilateral positions. Phase D ("Boost") reanchors the light side's ladder closer to market. Phase B ("Force-Buy") places a limit buy at the ask to complete the pair when time is running out. Also fixes two broken mechanisms: the loss-cap cancel/repost spam loop, and the imbalance trigger firing too early on the first fill.

## Affected Files
- `polybot/strategy/ladder_manager.py` -- Phase D boost, Phase B force-buy in `try_complete_pair`, loss-cap kill fix, imbalance timing fix, new `_killed_ladders` set, new `boost_light_side` method, new `_estimate_fill_cost` helper (mutating + additive)
- `polybot/order_executor.py` -- new `estimate_fill_cost(token_id, qty)` method that walks the ask side of the order book (additive)
- `polybot/config.py` -- new config params: `boost_elapsed_pct`, `force_buy_elapsed_pct`, `force_buy_max_pair_cost`, `imbalance_min_heavy_fills` (additive)
- `polybot/bot.py` -- pass `market.timeframe_sec` and `market.open_epoch` to `try_complete_pair`; check `_killed_ladders` before posting (mutating)
- `polybot/ladder_manager.py` -- mirror loss-cap kill fix and imbalance timing fix to legacy copy (mutating)
- `tests/test_pair_recovery.py` -- new test file for all pair recovery tests (additive)

## Implementation Tasks

### Task 1: Add config parameters for pair recovery
**File**: `polybot/config.py`
**What**: Add four new fields to `BotConfig` and corresponding env-var loading in `load_bot_config()`:

1. `boost_elapsed_pct: float = 0.20` -- minimum fraction of window elapsed before Phase D boost triggers. Default 20%.
2. `force_buy_elapsed_pct: float = 0.70` -- minimum fraction of window elapsed before Phase B force-buy triggers. Default 70%.
3. `force_buy_max_pair_cost: float = 0.93` -- pair cost ceiling for the forced buy. Higher than the passive `max_pair_cost` (0.90) because we are accepting slightly worse pricing to avoid a fully one-sided loss. Must still be < 1.00.
4. `imbalance_min_heavy_fills: int = 3` -- minimum number of fully filled orders on the heavy side before imbalance logic activates. Replaces the current implicit "first fill" trigger.

Add corresponding env vars: `BOOST_ELAPSED_PCT`, `FORCE_BUY_ELAPSED_PCT`, `FORCE_BUY_MAX_PAIR_COST`, `IMBALANCE_MIN_HEAVY_FILLS`.

Add validation in `validate_live_config()`:
- `force_buy_max_pair_cost` must be in (0.80, 0.99)
- `boost_elapsed_pct` must be < `force_buy_elapsed_pct`
- `force_buy_elapsed_pct` must be < 0.95 (leave room before no_trade_final_sec)

**Why**: Keep thresholds configurable rather than hardcoded. The whale data shows 0.90 is optimal for passive fills, but active pair completion can tolerate up to 0.93 and still be profitable ($1.00 - $0.93 = $0.07/pair).
**Tests to write**:
- `test_pair_recovery::test_config_defaults_exist` -- verify all four new fields exist on `BotConfig` with correct defaults
- `test_pair_recovery::test_config_env_var_loading` -- verify `load_bot_config()` reads the env vars
- `test_pair_recovery::test_config_validation_force_buy_bounds` -- verify `validate_live_config()` rejects out-of-range values

### Task 2: Add `estimate_fill_cost` to OrderExecutor
**File**: `polybot/order_executor.py`
**What**: Add a new method:

```
def estimate_fill_cost(self, token_id: str, qty: float) -> tuple[float, float] | None:
    """Walk the ask side of the order book to estimate the average fill price
    for buying `qty` shares.

    Returns (avg_price, total_cost) or None if the book is empty or
    insufficient depth exists for the requested quantity.

    Does NOT place any orders.
    """
```

Implementation:
1. Call `self.client.get_order_book(token_id)` to get the book.
2. Walk asks from lowest to highest price, accumulating quantity and cost.
3. If cumulative quantity >= `qty`, compute `avg_price = total_cost / qty_filled` and return `(avg_price, total_cost)`.
4. If book exhausted before `qty` filled, return `None` (insufficient liquidity).
5. Wrap in try/except for `ClobApiError` -- return `None` on failure.

This method is read-only (no order placement) and works identically in paper and live mode because both clients implement `get_order_book`.

**Why**: Phase B needs to estimate the cost of completing the pair BEFORE placing the order, to enforce the pair cost guard. Using `get_best_ask` alone understates the cost for larger quantities that eat through multiple price levels.
**Tests to write**:
- `test_pair_recovery::test_estimate_fill_cost_basic` -- mock book with 3 ask levels, verify correct VWAP for a qty that spans 2 levels
- `test_pair_recovery::test_estimate_fill_cost_insufficient_depth` -- mock book with less qty than requested, verify returns None
- `test_pair_recovery::test_estimate_fill_cost_empty_book` -- mock empty book, verify returns None

### Task 3: Fix loss-cap cancel/repost spam loop
**File**: `polybot/strategy/ladder_manager.py`
**What**: Three changes:

(a) Add `_killed_ladders: set[str] = set()` to `LadderManager.__init__`. This set tracks market_ids whose ladders were killed by `check_loss_cap`. These markets must not be re-posted or repriced for the remainder of the window.

(b) In `check_loss_cap`: after `self.cancel_ladder(mid)`, also do:
- `del self.ladders[mid]` -- remove the LadderState so `reprice_if_needed` won't iterate over it
- `self._killed_ladders.add(mid)` -- prevent `post_ladder` from re-creating it
- Log the kill once at WARNING level

(c) In `post_ladder`: at the top of the method, after the `is_halted()` check, add:
```python
if market.market_id in self._killed_ladders:
    return 0
```

(d) Add `is_killed(market_id: str) -> bool` public method that returns `market_id in self._killed_ladders`. This lets `bot.py` skip pair completion on killed markets.

(e) In `cleanup_ladder(market_id)`: also do `self._killed_ladders.discard(market_id)` so the kill flag is cleared when the market is fully cleaned up (post-settlement).

**Mirror to legacy**: Apply changes (a), (b), (c), (e) to `polybot/ladder_manager.py` as well. The legacy file has a `check_loss_cap` method -- if it does not, note that in the plan. (Verified: legacy file does NOT have `check_loss_cap`; skip mirroring for that method but still add `_killed_ladders` field and the `post_ladder` guard.)

**Why**: Currently `check_loss_cap` cancels orders but leaves the LadderState in `self.ladders`. On the next tick, `reprice_if_needed` finds the state, sees the book has moved, and reposts new rungs. Then `check_loss_cap` fires again. This repeats every tick, spamming cancels and placing orders that are immediately cancelled.
**Tests to write**:
- `test_pair_recovery::test_loss_cap_removes_ladder_state` -- after loss_cap fires, verify `has_ladder(mid)` returns False
- `test_pair_recovery::test_loss_cap_blocks_repost` -- after loss_cap fires, verify `post_ladder(market)` returns 0
- `test_pair_recovery::test_loss_cap_blocks_reprice` -- after loss_cap fires, verify `reprice_if_needed()` does not iterate the killed market
- `test_pair_recovery::test_loss_cap_cleanup_clears_kill` -- after `cleanup_ladder(mid)`, verify the market can be re-posted
- `test_pair_recovery::test_loss_cap_logs_once` -- verify only one WARNING log per market (use caplog fixture)

### Task 4: Fix imbalance timing (require 3+ fills, dynamic timeout)
**File**: `polybot/strategy/ladder_manager.py`
**What**: Modify `check_imbalance(now_epoch)`:

(a) Replace the current `total < 1.0` early-exit guard with a more specific check. At the top of the per-market loop, compute:
```python
heavy_count = max(
    self.tracker.filled_count(mid, Side.UP),
    self.tracker.filled_count(mid, Side.DOWN),
)
light_count = min(
    self.tracker.filled_count(mid, Side.UP),
    self.tracker.filled_count(mid, Side.DOWN),
)
```
Then add the guard:
```python
if heavy_count < self.cfg.imbalance_min_heavy_fills:
    continue  # not enough fills to judge imbalance
```
And tighten the severe-imbalance branch to also require `light_count == 0`:
```python
if imbalance > self.cfg.max_imbalance_ratio and light_count == 0:
```
This prevents the imbalance lock from firing when both sides have some fills (which is natural early-window behavior).

(b) Replace the fixed `imbalance_timeout_sec` with a dynamic timeout based on window elapsed fraction. The method currently does not receive `timeframe_sec`. Two options:

**Option chosen**: Store `timeframe_sec` on `LadderState` when the ladder is posted. Add field `timeframe_sec: int = 900` to `LadderState`. In `post_ladder`, set `state.timeframe_sec = market.timeframe_sec`. Then in `check_imbalance`, compute:
```python
dynamic_timeout = state.timeframe_sec * 0.30  # 30% of window
```
Use `dynamic_timeout` instead of `self.cfg.imbalance_timeout_sec`. The existing `imbalance_timeout_sec` config field is kept as a floor: `timeout = max(self.cfg.imbalance_timeout_sec, dynamic_timeout)`.

This produces:
- 5m (300s): timeout = max(120, 90) = 120s (unchanged)
- 15m (900s): timeout = max(120, 270) = 270s (was 120s)
- 1h (3600s): timeout = max(120, 1080) = 1080s (was 120s)

**Mirror to legacy**: Apply the same `filled_count` guard change to `polybot/ladder_manager.py::check_imbalance` if it exists there.

**Why**: The current code fires imbalance on the first fill (`total < 1.0` passes as soon as total >= 1.0). This is too early -- one fill on one side is normal. Requiring 3+ fills on the heavy side AND 0 on the light side ensures we only lock when there's a genuine directional trend. The dynamic timeout prevents premature timeout on longer windows.
**Tests to write**:
- `test_pair_recovery::test_imbalance_requires_min_fills` -- with 2 UP fills and 0 DN fills, verify `check_imbalance` does NOT set `heavy_side_locked`
- `test_pair_recovery::test_imbalance_fires_at_min_fills` -- with 3 UP fills and 0 DN fills (imbalance > threshold), verify `heavy_side_locked` IS set
- `test_pair_recovery::test_imbalance_no_lock_when_both_sides_have_fills` -- with 5 UP fills and 1 DN fill (high imbalance ratio but light_count > 0), verify lock is NOT set
- `test_pair_recovery::test_imbalance_dynamic_timeout_15m` -- for a 15m window, verify timeout is 270s not 120s
- `test_pair_recovery::test_imbalance_dynamic_timeout_1h` -- for a 1h window, verify timeout is 1080s

### Task 5: Implement Phase D -- Boost Light Side
**File**: `polybot/strategy/ladder_manager.py`
**What**: Add a new method `boost_light_side(market: MarketWindow, now: float) -> int`:

```python
def boost_light_side(self, market: MarketWindow, now: float) -> int:
    """Phase D: Reanchor the light side's ladder closer to market.

    Trigger conditions (ALL must be true):
    1. Ladder exists for this market
    2. boosted_side is None (only boost once per window)
    3. Heavy side has >= imbalance_min_heavy_fills (3) fully filled orders
    4. Light side has 0 filled orders
    5. Window is >= boost_elapsed_pct (20%) elapsed
    6. Ladder is not killed

    Action:
    - Cancel all resting orders on the light side
    - Repost with new anchor = best_ask - tick_size (right below market)
    - Use half the original width (tighter concentration)
    - Keep same budget allocation for that side
    - Set state.boosted_side = light_side

    Returns number of new orders placed (0 if no action taken).
    """
```

Implementation details:
1. Get the LadderState. Check all trigger conditions.
2. Compute `elapsed_frac = (now - market.open_epoch) / market.timeframe_sec`. If < `self.cfg.boost_elapsed_pct`, return 0.
3. Determine heavy_side and light_side using `filled_count`.
4. Cancel the light side's resting orders via `self.tracker.cancel_side` + `self.executor.cancel_batch`.
5. Get `best_ask` for the light side's token.
6. Get ladder params. Use `width = lp.width / 2.0` (half width = tighter).
7. Compute budget: `min(lp.position_size_fraction * self.positions.bankroll / 2.0, available / 2.0)` minus any existing filled cost on that side.
8. Build new rungs with `build_ladder_rungs(best_ask, budget, lp.rungs, lp.spacing, width/2, ...)`.
9. Apply pair cost guard: if heavy side has fills, trim rungs where `rung_price + heavy_vwap > lp.max_pair_cost`.
10. Place orders via `place_batch_limit_buys`.
11. Track orders in `OrderTracker`.
12. Set `state.boosted_side = light_side`.
13. Update the anchor for that side.

**DRY_RUN parity**: The method uses `self.executor.place_batch_limit_buys` which already handles DRY_RUN (see `_place_batch_sequential` in order_executor.py). No additional DRY_RUN handling needed.

**Why**: When price trends one direction, the light side's rungs (set at original post time) are too far from market to fill. Boosting re-anchors them near the current ask, giving them a chance to fill passively before resorting to an expensive force-buy.
**Tests to write**:
- `test_pair_recovery::test_boost_triggers_at_threshold` -- verify boost fires when all conditions met (3+ heavy fills, 0 light fills, 20%+ elapsed)
- `test_pair_recovery::test_boost_skips_before_elapsed_threshold` -- verify no boost at 15% elapsed
- `test_pair_recovery::test_boost_only_once_per_window` -- verify second call with boosted_side already set returns 0
- `test_pair_recovery::test_boost_cancels_light_side_rungs` -- verify the old light-side orders are cancelled before new ones are placed
- `test_pair_recovery::test_boost_uses_half_width` -- verify the new rungs have a tighter spread (compare rung prices to what build_ladder_rungs produces with width/2)
- `test_pair_recovery::test_boost_respects_pair_cost_guard` -- verify rungs that would push pair_cost above max_pair_cost are trimmed
- `test_pair_recovery::test_boost_skips_killed_ladder` -- verify no boost on a market in `_killed_ladders`

### Task 6: Implement Phase B -- Force-Buy Pair Completion
**File**: `polybot/strategy/ladder_manager.py`
**What**: Replace the stub `try_complete_pair` method with a real implementation:

```python
def try_complete_pair(self, market: MarketWindow, now: float) -> dict | None:
    """Phase B: Force-buy the light side to complete a one-sided position.

    Trigger conditions (ALL must be true):
    1. Ladder exists (or position exists from a killed ladder)
    2. Window is >= force_buy_elapsed_pct (70%) elapsed
    3. Position is >75% one-sided (light_qty < 0.25 * heavy_qty)
    4. Hypothetical pair_cost (heavy_vwap + estimated_light_price) < force_buy_max_pair_cost
    5. Light side needs >= MIN_ORDER_SIZE worth of shares
    6. Market is not in _killed_ladders

    Action:
    - Estimate fill price for the needed quantity using estimate_fill_cost
    - Check pair cost guard: heavy_vwap + estimated_avg_price < force_buy_max_pair_cost
    - Place a single limit buy at best_ask for the light side for the deficit quantity
    - Credit the fill to position manager

    Returns dict with {side, price, qty, pair_cost} or None.
    """
```

Implementation details:
1. Get position from `self.positions.positions.get(market.market_id)`. If no position, return None.
2. Compute `elapsed_frac = (now - market.open_epoch) / market.timeframe_sec`. If < `self.cfg.force_buy_elapsed_pct`, return None.
3. Determine heavy and light sides. Compute `deficit = heavy_qty - light_qty`. If `light_qty >= 0.25 * heavy_qty`, return None (not one-sided enough).
4. Check minimum size: if `deficit * estimated_price < MIN_ORDER_SIZE`, return None.
5. Call `self.executor.estimate_fill_cost(light_token_id, deficit)`. If returns None (no liquidity), return None.
6. Compute `hypothetical_pair_cost = heavy_vwap + estimated_avg_price`. If >= `self.cfg.force_buy_max_pair_cost`, return None.
7. Place order: `self.executor.place_limit_buy(light_token_id, best_ask_price, deficit, market_id, light_side)`. Use `best_ask_price` (not the VWAP) because Polymarket GTC orders need a specific limit price. Use the best ask so the order fills immediately.
8. Track in `OrderTracker`.
9. Credit to `PositionManager` immediately (in paper mode, the fill is guaranteed because we're placing at best_ask; in live mode, the order may sit briefly but will fill quickly at the ask).
10. Return `{"side": light_side, "price": estimated_avg_price, "qty": deficit, "pair_cost": hypothetical_pair_cost}`.

**Important**: The force-buy places a single limit order at `best_ask` price for the full deficit quantity. This is NOT a market order (Polymarket does not support market orders). By pricing at the best ask, we get immediate execution for the first level of depth. If the deficit is larger than the best ask's available qty, the order will partial-fill and the remainder will sit as a resting order. This is acceptable -- the next tick will detect the resting order and either fill it or the window will settle with a partial completion.

**DRY_RUN parity**: Uses `self.executor.place_limit_buy` which handles DRY_RUN. In paper mode, the order enters `PaperClobClient._resting` and will fill on the next `tick()` call if midpoint <= order_price. The `process_paper_fills` path in bot.py will credit it normally.

**Why**: This is the core pair recovery mechanism. When the boost didn't work and the window is almost over, buying the light side at a small premium is still profitable if pair_cost < 1.00. The 0.93 ceiling leaves $0.07/pair profit margin.
**Tests to write**:
- `test_pair_recovery::test_force_buy_triggers_at_threshold` -- verify force-buy fires at 70%+ elapsed with one-sided position
- `test_pair_recovery::test_force_buy_skips_before_threshold` -- verify no action at 60% elapsed
- `test_pair_recovery::test_force_buy_skips_balanced_position` -- verify no action when light_qty >= 25% of heavy_qty
- `test_pair_recovery::test_force_buy_pair_cost_guard` -- verify no action when hypothetical pair_cost >= force_buy_max_pair_cost
- `test_pair_recovery::test_force_buy_returns_correct_dict` -- verify return dict has side, price, qty, pair_cost keys
- `test_pair_recovery::test_force_buy_places_at_best_ask` -- verify the limit order price equals best_ask
- `test_pair_recovery::test_force_buy_size_matches_deficit` -- verify order size = heavy_qty - light_qty
- `test_pair_recovery::test_force_buy_min_order_size_guard` -- verify no action when deficit is too small
- `test_pair_recovery::test_force_buy_no_liquidity` -- verify returns None when estimate_fill_cost returns None
- `test_pair_recovery::test_force_buy_skips_killed_ladder` -- verify no action on killed market
- `test_pair_recovery::test_force_buy_credits_position` -- verify position manager gets the fill credited

### Task 7: Wire Phase D and Phase B into bot.py trading loop
**File**: `polybot/bot.py`
**What**: Modify `_trading_loop_tick` to call `boost_light_side` and pass enriched data to `try_complete_pair`:

(a) After the imbalance guard call (line 700) and before the existing `try_complete_pair` loop (line 705), add a boost loop:
```python
# Phase D: Boost light side ladder
for market in active_list:
    if market.is_active(now) and self.ladder_manager.has_ladder(market.market_id):
        boosted = await asyncio.to_thread(
            self.ladder_manager.boost_light_side, market, now
        )
        if boosted > 0:
            self._record_activity(
                "BOOST", market.asset,
                f"reanchored light side with {boosted} new rungs on {market.market_id}",
            )
```

(b) In the existing `try_complete_pair` loop, add a guard to skip killed ladders:
```python
if self.ladder_manager.is_killed(market.market_id):
    continue
```

Note: the existing `try_complete_pair` call already passes `market` and `now`. No signature change is needed because `try_complete_pair` receives the full `MarketWindow` which contains `open_epoch` and `timeframe_sec` needed to compute elapsed fraction.

(c) After `check_loss_cap`, skip reprice on killed ladders. The current reprice call passes `market_map` which includes all active markets. The `reprice_if_needed` method iterates `self.ladders` -- since Task 3 removes killed ladders from `self.ladders`, this is already handled. No additional change needed here.

**DRY_RUN parity**: Both `boost_light_side` and `try_complete_pair` use `OrderExecutor` methods that already handle DRY_RUN. The paper fill path (`process_paper_fills`) will handle force-buy orders the same as any other order. No additional DRY_RUN code needed.

**Why**: The bot.py trading loop is the orchestrator. It needs to call Phase D before Phase B (boost first, force-buy later). The kill guard prevents wasted work on dead markets.
**Tests to write**:
- `test_pair_recovery::test_bot_calls_boost_before_force_buy` -- integration test: mock ladder_manager, verify boost_light_side is called before try_complete_pair in tick order
- `test_pair_recovery::test_bot_skips_killed_on_force_buy` -- verify try_complete_pair is not called for killed markets
- `test_pair_recovery::test_bot_records_boost_activity` -- verify BOOST activity event is recorded

### Task 8: Add `timeframe_sec` to LadderState and populate it
**File**: `polybot/strategy/ladder_manager.py`
**What**: 
(a) Add field `timeframe_sec: int = 900` to the `LadderState` dataclass (after `current_ask_dn`).

(b) In `post_ladder`, when creating the `LadderState` instance (around line 309), add `timeframe_sec=market.timeframe_sec`.

(c) In `post_ladder_pre_open` -- this delegates to `post_ladder`, so no separate change needed.

**Mirror to legacy**: Add the field to `polybot/ladder_manager.py::LadderState` as well.

**Why**: Task 4 needs `timeframe_sec` on the state to compute dynamic imbalance timeout. Task 5 needs it to compute elapsed fraction. Storing it on LadderState avoids threading the MarketWindow through every method.
**Tests to write**:
- `test_pair_recovery::test_ladder_state_has_timeframe_sec` -- verify field exists with default 900
- `test_pair_recovery::test_post_ladder_sets_timeframe_sec` -- verify `post_ladder` sets the correct value from the MarketWindow

## Acceptance Criteria
- [ ] Phase D boost fires when heavy_side >= 3 fills, light_side == 0 fills, and >= 20% elapsed
- [ ] Phase D boost only fires once per window (boosted_side flag)
- [ ] Phase B force-buy fires when >= 70% elapsed and position is > 75% one-sided
- [ ] Phase B pair cost guard rejects force-buy when hypothetical pair_cost >= 0.93 (configurable)
- [ ] Phase B returns correct dict with side, price, qty, pair_cost
- [ ] Loss-cap kill removes ladder from self.ladders and blocks repost/reprice
- [ ] Loss-cap kill logs once per market, not every tick
- [ ] Imbalance requires 3+ heavy fills (not 1) before locking
- [ ] Imbalance timeout is 30% of window timeframe (not fixed 120s) for 15m and 1h windows
- [ ] All four new config params have correct defaults and are loadable from env vars
- [ ] estimate_fill_cost correctly walks the order book and returns None on insufficient depth
- [ ] DRY_RUN mode handles all new code paths (boost + force-buy + loss-cap) identically to live mode
- [ ] All existing tests still pass (run `cd C:/Users/pc/Desktop/Bots/PolyBot && python -m pytest --collect-only -q 2>&1 | tail -1` to get current count before writing acceptance criteria)
- [ ] New tests cover the change

## Risk Notes

### Settlement and pair cost logic
This plan touches pair cost logic in two places: (1) the boost method trims rungs by pair cost, and (2) the force-buy method gates on a hypothetical pair cost. Both use the same `max_pair_cost` / `force_buy_max_pair_cost` config values and never bypass the guard. The pair cost guard invariant (`pair_cost < max_pair_cost`) is preserved -- the force-buy uses a SEPARATE, HIGHER threshold (`force_buy_max_pair_cost = 0.93`) that is explicitly configured and validated.

Settlement logic is NOT touched by this plan. The force-buy credits fills to `PositionManager` using the existing `update_position` method. Settlement de-duplication (`_settled_markets` set in bot.py) is unaffected.

### DRY_RUN parity
All new order paths (boost repost, force-buy) use `OrderExecutor.place_limit_buy` / `place_batch_limit_buys` which already branch on `cfg.dry_run`. Paper mode fills are handled by `PaperClobClient.tick()` + `process_paper_fills()`. No new DRY_RUN-specific code is needed.

The force-buy places a limit order at `best_ask`. In paper mode, `PaperClobClient.tick()` will fill this on the next tick if `midpoint <= order_price`. Since we place AT the ask, which is at or above midpoint, the fill probability is ~90% per tick. This is acceptable behavior.

### Dual ladder_manager files
The project has two copies of `LadderManager`: `polybot/strategy/ladder_manager.py` (production, imported by bot.py) and `polybot/ladder_manager.py` (legacy, used by some tests). This plan primarily modifies the strategy version. The legacy copy needs only: (1) `_killed_ladders` set, (2) `timeframe_sec` on LadderState, and (3) the `post_ladder` kill guard. The legacy copy does NOT have `check_loss_cap` or `try_complete_pair`, so those methods do not need mirroring.

### Force-buy partial fill risk
If the force-buy order at `best_ask` cannot fill the full deficit (because book depth at that level is less than deficit), the order will partially fill and the remainder will sit as a resting order. This remainder may or may not fill before the window closes. In the worst case, we get a partial pair completion, which is still better than a fully one-sided position. The `estimate_fill_cost` check mitigates this by verifying sufficient depth exists before placing the order.

### Config value discrepancy
Note: `BotConfig.max_pair_cost` has a dataclass default of `0.93` but `load_bot_config()` loads the env default as `"0.90"`. The effective runtime value is `0.90` when using `load_bot_config()` and `0.93` only in unit tests that construct `BotConfig()` directly. The new `force_buy_max_pair_cost` defaults to `0.93` in BOTH places to avoid this inconsistency. The Coder should ensure the `load_bot_config` env default matches the dataclass default for this new field.

## Do Not Touch List
- `polybot/types.py` -- no changes needed; MarketWindow already has `open_epoch`, `timeframe_sec`, `remaining()`
- `polybot/position_manager.py` / `polybot/strategy/position_manager.py` -- no changes; existing `update_position` handles the force-buy credit
- `polybot/order_tracker.py` -- no changes; existing `add`, `cancel_side`, `filled_count`, `filled_qty`, `filled_cost` methods are sufficient
- `polybot/risk_manager.py` -- no changes; `is_halted`, `exposure_factor` are unchanged
- `polybot/oms/clob_client.py` -- no changes; `PaperClobClient.tick()` and `get_order_book` already work for all new code paths
- Settlement logic in `bot.py` (`_settle_expired_windows`, `_settled_markets`) -- not touched
- Any existing test files -- not modified
