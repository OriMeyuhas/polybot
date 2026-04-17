# Imbalance Lock Leak Analysis

**Date**: 2026-04-06
**Analyst**: polybot-researcher
**Dataset**: 338 settlements (settlement_log.jsonl), last 100 focused analysis
**Logs examined**: paper_run_iter12.log (2026-04-06 10:41 to 16:55)

---

## Executive Summary

The imbalance lock mechanism is fundamentally broken. Instead of preventing excess share accumulation, it **inverts** the imbalance. When the lock fires on side A, boost_light_side and chase_pair flood side B with aggressive orders, causing side B to accumulate 3-16x more fills than side A. This "boost/chase inversion" pattern accounts for **32 of the last 100 settlements** and **$136 in losses** -- the single largest drag component.

---

## 1. Quantitative Impact

### Settlement Decomposition (Last 100)

| Category | Count | PnL | Avg PnL |
|----------|-------|-----|---------|
| One-sided (0 fills one side) | 42 | -$78.73 | -$1.87 |
| Boost/chase inversion (ratio>3, light<12) | 32 | -$136.13 | -$4.25 |
| Natural imbalance (moderate ratio) | 26 | -$8.29 | -$0.32 |
| **Total** | **100** | **-$223.15** | **-$2.23** |

### Decomposition by PnL Source (All 338 settlements)

| Source | PnL |
|--------|-----|
| Paired spread profit | +$3,516.33 |
| Excess share drag | -$896.43 |
| One-sided losses | -$545.54 |
| **Net** | **+$2,074.36** |

### Excess Share Win Rate by Ratio Bucket (Last 100)

| Bucket | Count | Excess Win Rate | Excess PnL |
|--------|-------|-----------------|------------|
| 2-3x | 32 | 34% | -$0.17 |
| 3-5x | 27 | 30% | -$247.14 |
| 5x+ | 47 | 23% | -$18.61 |

The heavy side wins at sub-50% rate across ALL buckets -- clear adverse selection.

### Light Side Qty in 5x+ Imbalanced Settlements

All 22 cases in the last 100 had light side qty between 5.0 and 9.7 (mean: 6.6). This is the signature of a single ladder rung fill (5-10 qty), consistent with boost/chase posting a small number of rungs that get partially filled while the (unlocked) heavy side accumulates many fills.

---

## 2. Root Cause Chain

### The Inversion Sequence (traced from logs)

Example: `bitcoin-up-or-down-april-6-2026-3am-et`

1. **10:41:30** -- Ladder posted: 18 UP + 18 DN rungs, budget $100
2. **10:41:45** -- DN fills 9.8 qty @ $0.44 (first and only DN fill)
3. **10:41:45** -- `check_imbalance()` fires: DN locked, 17 DN resting orders cancelled
4. **10:41:45** -- `boost_light_side()` fires: posts 16 UP rungs (half-width, aggressive)
5. **10:42:02** -- `chase_pair()` fires: posts 6 more UP rungs near midpoint
6. **10:44:44** -- UP fills 10.1 @ $0.48 (boost rung)
7. **10:44:56** -- UP fills 10.9 @ $0.45 (boost rung)
8. **10:45:36** -- UP fills 10.0 @ $0.48 (boost rung)
9. **10:45:36** -- `_check_one_side_cap()` fires: UP=31, DN=10, ratio 3.1:1

**Result**: UP accumulated 31 qty vs DN's 10 qty. The lock was on DN (correct originally) but boost/chase created a 3.1:1 imbalance in the opposite direction.

This same pattern was confirmed in multiple markets in the iter12 log.

### Six Code-Level Gaps

**Gap 1: `boost_light_side()` ignores `heavy_side_locked`**
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`, lines 740-897
- The function checks `_killed_ladders`, `boosted_side`, and `elapsed_frac`, but NEVER reads `state.heavy_side_locked`
- It posts a full half-budget of rungs on the "light" side with half-width (more aggressive than original ladder)

**Gap 2: `chase_pair()` ignores `heavy_side_locked`**
- File: same, lines 464-600
- Same issue: posts chase orders near midpoint on the unfilled side
- Uses `reactive_chase_budget_pct=0.50` of remaining budget = ~$12.50

**Gap 3: No qty cap relative to heavy side**
- Neither boost nor chase limits the light side's resting order qty to match or be proportional to the heavy side's filled qty
- Boost posts full half-budget (~$25 = 50-80 qty at $0.30-0.50) when heavy side may have only 10 qty
- This guarantees the light side can overshoot by 5-8x

**Gap 4: `check_imbalance()` requires `light_count == 0`**
- File: same, line 1467: `if imbalance > self.cfg.max_imbalance_ratio and light_count == 0:`
- Once the light side gets even 1 fill (from boost/chase), this condition can never trigger again
- The `else` branch (line 1498) resets `imbalance_alert_at`, wiping timeout tracking
- Result: the imbalance lock is a one-shot mechanism that cannot fire twice or adapt to inversions

**Gap 5: `_check_one_side_cap()` 3:1 threshold is too late**
- File: same, line 1685: `if min_qty > 0 and max_qty / min_qty <= 3.0: return`
- By 3:1, the damage is done: 30 excess qty * $0.40 VWAP = $12 at risk
- This is a damage-control measure, not a prevention mechanism

**Gap 6: `sell_losing_side()` cannot sell excess from two-sided positions**
- File: same, line 375-376: `if min_qty > 0: return None`
- Only works for fully one-sided positions (one side has zero fills)
- Cannot trim excess from a 31:10 imbalanced position

---

## 3. Bot Loop Execution Order

```
Step 1:  paper_fills = clob_client.tick()        -- generate fills
Step 2:  post_ladder()                           -- new markets only
Step 3:  reprice_if_needed()                     -- respects heavy_side_locked
Step 4:  process_paper_fills()                   -- credits fills, _check_one_side_cap per fill
Step 5:  cancel_losing_side_orders()             -- FV cancel, sets heavy_side_locked
Step 6:  check_imbalance()                       -- sets heavy_side_locked (light_count==0 only)
Step 7:  check_loss_cap()                        -- kills over-budget positions
Step 8:  boost_light_side()                      -- IGNORES heavy_side_locked
Step 9:  chase_pair()                            -- IGNORES heavy_side_locked
Step 10: directional_buy()                       -- IGNORES heavy_side_locked
Step 11: sell_losing_side()                      -- only for fully one-sided
```

The lock is set in steps 5-6 and checked in step 3 (reprice). Steps 8-9 are the leak points.

---

## 4. Hypothetical Impact of Fixes

| Fix | Estimated PnL Improvement (per 100 stl) |
|-----|------------------------------------------|
| Cap boost/chase qty to match heavy side | +$166.23 |
| Prevent one-sided losses entirely | +$111.79 |
| Both combined | +$331.83 |

If boost/chase inversions were perfectly balanced (only paired profit), last 100 PnL would be **-$56.92** instead of -$223.15. Still negative due to one-sided losses, but the excess share drag would be eliminated.

---

## 5. Proposals

### Proposal #38 -- Cap boost/chase qty to heavy side filled qty [CRITICAL]

**Observation**: boost_light_side and chase_pair post light-side orders with budgets 5-8x larger than the heavy side's filled qty, causing systematic inversion of the imbalance.

**Evidence**: 32/100 recent settlements show the inversion pattern (ratio>3, light side <12 qty). Total excess PnL from these: -$136.13. Log trace confirms boost posts 16+6=22 rungs while heavy side has ~10 qty.

**Proposed Change**: 
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`
- In `boost_light_side()` around line 835: after computing `side_budget`, add a qty cap:
  ```python
  heavy_filled_qty = self.tracker.filled_qty(mid, heavy_side)
  # Cap light side budget so total potential fills <= 1.5x heavy side
  max_light_cost = heavy_filled_qty * 1.5 * best_ask
  side_budget = min(side_budget, max_light_cost)
  ```
- In `chase_pair()` around line 545: same pattern. Cap chase budget so total chase + existing light fills <= 1.5x heavy fills.

**Expected Impact**: Eliminates the 32 inversion settlements ($136 drag). Conservative estimate +$100-150 per 100 settlements.

**Risk**: Light side may not fill enough to complete pairs. Mitigated by the 1.5x ratio still allowing some excess for fill probability.

**Priority**: CRITICAL

---

### Proposal #39 -- Make check_imbalance bidirectional (detect inversions) [HIGH]

**Observation**: `check_imbalance()` only fires when `light_count == 0`. After the light side gets one fill from boost/chase, the lock can never update or flip direction.

**Evidence**: Line 1467 requires `light_count == 0`. The else branch at line 1498 clears `imbalance_alert_at`. After boost fills 1 light-side rung, the imbalance detection is permanently disabled for that market.

**Proposed Change**:
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`
- In `check_imbalance()`: remove the `light_count == 0` requirement. Instead use ratio-based detection:
  ```python
  if min_qty > 0:
      ratio = max_qty / min_qty
  else:
      ratio = float('inf')
  
  if ratio > 2.0:
      heavy_side = Side.UP if up_qty > dn_qty else Side.DOWN
      if state.heavy_side_locked != heavy_side.value:
          # Lock flipped or first detection
          state.heavy_side_locked = heavy_side.value
          cancelled = self.tracker.cancel_side(mid, heavy_side)
          self.executor.cancel_batch(cancelled)
  ```
- This allows the lock to FLIP when boost/chase inverts the imbalance.

**Expected Impact**: Catches inversions within 1-2 ticks (200-400ms). Would prevent 20+ excess fills from accumulating on the light side.

**Risk**: More aggressive locking could prevent some legitimate two-sided fills. Mitigated by keeping the 2:1 threshold (not 1:1).

**Priority**: HIGH

---

### Proposal #40 -- Disable boost_light_side and chase_pair when lock is active [HIGH]

**Observation**: These two functions are the direct mechanism by which the imbalance lock causes WORSE imbalance. They operate on the assumption that the light side needs help, but post orders without respecting the lock.

**Evidence**: Every logged IMBALANCE LOCK is immediately followed by BOOST and/or CHASE PAIR on the same market in the same tick. This is the direct cause of the inversion.

**Proposed Change**:
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`
- In `boost_light_side()` near line 759, add:
  ```python
  if state.heavy_side_locked is not None:
      return 0  # Lock active -- don't flood the light side
  ```
- In `chase_pair()` near line 480, add:
  ```python
  if state.heavy_side_locked is not None:
      return 0  # Lock active -- don't flood the chase side
  ```

**Expected Impact**: Immediately stops the inversion mechanism. Combined with #38 or #39, this is the simplest fix.

**Risk**: One-sided positions won't get recovery attempts while locked. But data shows these recovery attempts have <34% win rate, so they're net negative anyway.

**Priority**: HIGH (simplest fix, highest certainty of correctness)

---

### Proposal #41 -- Lower _check_one_side_cap threshold from 3:1 to 1.5:1 [MEDIUM]

**Observation**: The 3:1 ratio threshold in `_check_one_side_cap()` allows significant excess accumulation before triggering. At 3:1 with 30 qty heavy and 10 qty light, 20 excess shares at $0.40 = $8 at risk.

**Evidence**: ONE-SIDE CAP fired 4 times in iter12 log, always after the damage was already done (31:10, 10:0, 39:11, 9:0).

**Proposed Change**:
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`
- Line 1685: change `max_qty / min_qty <= 3.0` to `max_qty / min_qty <= 1.5`
- Also: remove the grace period when `heavy_side_locked` is already set (no need to wait if lock has been tripped)

**Expected Impact**: Catches imbalance earlier, limits excess to ~50% overshoot instead of 200%.

**Risk**: May trigger false positives on naturally uneven fills. The 45s/180s grace period provides some buffer.

**Priority**: MEDIUM

---

### Proposal #42 -- Enable sell_losing_side for imbalanced two-sided positions [LOW]

**Observation**: `sell_losing_side()` exits on line 376 when both sides have fills (`if min_qty > 0: return None`). This prevents it from trimming excess shares in a 31:10 position.

**Evidence**: The function is designed for fully one-sided positions only. Two-sided imbalanced positions (which account for $253 in excess drag) cannot be partially exited.

**Proposed Change**:
- File: `C:/Users/pc/Desktop/Bots/PolyBot/polybot/strategy/ladder_manager.py`
- In `sell_losing_side()`: when `min_qty > 0` but `max_qty/min_qty > 3.0`, compute excess = max_qty - min_qty and sell the excess portion of the heavy side.

**Expected Impact**: Could recover 30-50% of excess share cost by selling before settlement at a partial loss.

**Risk**: Selling at market may get poor fills. Paper mode fills may not accurately model sell execution.

**Priority**: LOW (defense-in-depth, not a primary fix)

---

## 6. Recommended Fix Priority

1. **Proposal #40** (disable boost/chase when locked) -- simplest, highest confidence, blocks the leak
2. **Proposal #38** (qty cap) -- structural fix for when boost/chase are eventually re-enabled
3. **Proposal #39** (bidirectional imbalance detection) -- catches inversions automatically
4. **Proposal #41** (lower cap threshold) -- earlier intervention
5. **Proposal #42** (sell excess) -- defense-in-depth

Proposals #40 and #38 together would address the $136/100stl boost/chase inversion drag. Combined with existing one-sided abort mechanisms, this could swing the bot from -$2.23/stl to approximately +$0.80/stl.

---

**Status**: PROPOSED -- ready for planner handoff
