# Live Trading Hardening — Design Spec

**Date:** 2026-03-19
**Status:** Review
**Scope:** Fix all bugs, edge cases, and missing safeguards required before deploying PolyBot with real money on Polymarket's CLOB.

---

## 1. Problem Statement

The bot's core ladder logic works in dry-run simulation but has critical gaps for live trading:

- **Phantom fills**: API failures cause `get_open_orders()` to return `[]`, making the bot think all orders filled.
- **No heartbeat**: Polymarket cancels ALL open orders after 10 seconds of silence. Bot has no heartbeat mechanism.
- **Cancel failures ignored**: Failed cancel API calls leave local state out of sync with the exchange.
- **Early exit books phantom PnL**: Profits credited to bankroll without actually selling.
- **No batch orders**: 72 individual API calls per ladder — wasteful and rate-limit-risky.
- **Stale tick sizes**: py-clob-client caches tick sizes forever; dynamic changes cause order rejections.
- **Spot price race**: Settlement guesses outcome from spot delta, which may not be available yet.
- **No on-chain redemption**: Winning tokens sit unredeemed; bankroll doesn't reflect actual gains.
- **Committed capital drift**: Incremental tracking goes out of sync on cancel/fill edge cases.

---

## 2. Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Heartbeat failure response | Full state reset after 2 missed heartbeats | Polymarket already cancelled everything; honest reset avoids ghost orders |
| Position exit strategy | Hold to settlement (no early exit) | Whale data shows edge comes from entry price, not exit timing; removes a major failure mode. **One-sided risk**: imbalance timeout can produce directional positions with max loss of `position_size_fraction * bankroll / 2` (5% for 15m, 1.65% for 5m) — acceptable given the whale's 61% win rate on 15m. |
| Fill detection under API failure | Raise exception, skip fill check | Simple and safe; holding to settlement means delayed fill detection is harmless |
| Rate limiting | Batch order API (default 15 per request, Polymarket allows up to 50) | Conservative default to reduce blast radius of partial failures; cuts API calls by ~85%; ladder appears atomically on book |
| Cancel verification | Optimistic cancel + reconciliation | Don't block repricing; eventual consistency via next successful `get_open_orders()` |
| Tick size handling | TTL cache (60s) + rejection-triggered refresh | Efficient in common case, self-correcting in edge case |
| Settlement PnL | Use actual Polymarket resolution, not spot delta | Eliminates spot price race entirely |
| On-chain redemption | Automated via `redeemPositions` after settlement | Closes the capital loop; bankroll reflects real USDC.e |
| WebSocket for fills/book | Out of scope (v2) | Known unfixed server-side silent freeze bug (#292) makes it unreliable |

---

## 3. Architecture Overview

```
Bot Main Loop (run_trading_loop)
  |
  |-- [gate] heartbeat.is_healthy() -- skip all trading if unhealthy
  |
  |-- 1. Post ladders (batch API, tick-size-aware)
  |-- 2. Check fills (error-aware, with reconciliation)
  |-- 3. Reprice if needed (batch cancel + batch post)
  |-- 4. Imbalance guard
  |-- 5. Cancel rungs on expiring windows
  |-- 6. Mark expired positions as pending_settlement
  |
Heartbeat Task (independent async)
  |-- Send heartbeat every 5s
  |-- On 2 consecutive failures: trigger full state reset
  |
Settlement Task (independent async)
  |-- Poll pending_settlement positions for resolution
  |-- On resolution: queue for redemption
  |
Redeemer Task (independent async)
  |-- Call redeemPositions on-chain
  |-- On success: credit USDC.e to bankroll
  |-- On failure: retry with backoff
```

---

## 4. New Modules

### 4.1 `polybot/heartbeat.py`

**Responsibility:** Keep the Polymarket session alive and detect connection loss.

**Interface:**
- `start(client, on_connection_lost: Callable) -> None` — begins the heartbeat loop as an async task
- `is_healthy() -> bool` — returns False if 2+ consecutive heartbeats failed
- `on_connection_lost` — callback passed to `start()`, invoked on 2 consecutive failures to trigger state reset in bot

**Behavior:**
- Sends heartbeat every 5 seconds using the most recent `heartbeat_id`
- Tracks consecutive failure count
- On 2 consecutive failures:
  - Sets `healthy = False`
  - Calls `on_connection_lost` callback
  - Continues retrying heartbeat
- On recovery:
  - Sets `healthy = True`
  - Caller is responsible for rebuilding state

### 4.2 `polybot/tick_size_cache.py`

**Responsibility:** Provide correct tick sizes for order price rounding.

**Interface:**
- `get_tick_size(condition_id: str) -> float` — returns cached or freshly fetched tick size
- `invalidate(condition_id: str) -> None` — forces re-fetch on next call

**Behavior:**
- Cache entries expire after 60 seconds (configurable TTL)
- On cache miss or expiry: fetch from `GET /tick-size?condition_id=...`
- On order rejection for tick size violation: caller invalidates and retries once

### 4.3 `polybot/settlement.py`

**Responsibility:** Resolve market outcomes via Polymarket APIs.

**Extracted from:** `polybot/tracker/settlement_tracker.py` (functions `_resolve_via_clob`, `_resolve_via_gamma`, `_fetch_condition_id_from_gamma`)

**Interface:**
- `try_resolve_once(client, clob_host, slug, condition_id) -> dict | None`
- `fetch_condition_id(client, slug) -> str`

**Used by:** Both `polybot/bot.py` (live settlement) and `polybot/tracker/settlement_tracker.py` (tracker settlement).

### 4.4 `polybot/redeemer.py`

**Responsibility:** Convert winning conditional tokens to USDC.e on-chain after settlement.

**Interface:**
- `queue_redemption(condition_id: str, token_ids: list[str]) -> None`
- `start(client) -> None` — begins the redemption loop as an async task

**Behavior:**
- Maintains a queue of markets awaiting redemption
- Calls `redeemPositions` on the CTF Exchange contract
- On success: credit actual USDC.e received to bankroll, remove from queue
- On failure: retry with exponential backoff (2s, 4s, 8s... up to 5 minutes)
- After 10 failed attempts: move to `failed_redemptions` list, log for manual review
- Config: `redemption_retry_max`, `redemption_retry_backoff_sec`

---

## 5. Module Changes

### 5.1 `polybot/order_executor.py`

**New exception:** `ClobApiError` — raised on any API failure (timeout, 429, 5xx, network error).

**Changed methods:**
- `get_open_orders()`: raise `ClobApiError` on failure instead of returning `[]`
- `get_best_ask()`: raise `ClobApiError` on failure instead of returning `1.0`
- `place_limit_buy()`: raise `ClobApiError` on failure instead of returning `status="error"`
- `cancel_order()`: raise `ClobApiError` on failure instead of returning `False`

**New methods:**
- `place_batch_limit_buys(orders: list[dict]) -> list[OrderRecord]` — uses `POST /orders` (chunks of `batch_order_size`, default 15). Must support `dry_run` mode via mock client for testing.
- `cancel_batch(order_ids: list[str]) -> list[str]` — uses `DELETE /orders` (chunks of `batch_order_size`), returns list of successfully cancelled IDs. Must support `dry_run` mode.

**Removed:**
- `place_limit_sell()` — dead code after early exit removal, delete entirely.

**Rate limit handling:** On HTTP 429, raise `ClobApiError` with a `retry_after` attribute. Callers can choose to wait or skip.

### 5.2 `polybot/order_tracker.py`

**New order status:** `unknown` — used during heartbeat state reset. Orders in `unknown` state are reconciled on the next successful `get_open_orders()` call.

**Changed fill threshold** (relative threshold scales correctly across Polymarket's wide order size range of 5 to 10,000+ shares):
```python
# Old
if order.filled >= order.size - 0.001
# New
if order.filled >= order.size * 0.999
```

**New methods:**
- `mark_all_unknown() -> None` — sets all non-filled orders to `unknown` status (heartbeat reset)
- `get_resting(market_id: str) -> list[TrackedOrder]` — returns all resting orders for a market (used by derived committed capital)
- `reconcile(open_orders: list[dict]) -> dict` — compares local state against exchange state, returns `{filled: [...], reverted: [...], orphaned: [...]}`. **filled**: orders locally marked `unknown` or `resting` that are no longer on exchange — credit fills to position_manager via `update_position()`. **reverted**: orders locally marked `cancelled` that are still on exchange — revert to `resting`. **orphaned**: orders on exchange not in our tracker — cancel them via batch cancel.

### 5.3 `polybot/ladder_manager.py`

**Removed:**
- `check_early_exits()` — entire method deleted
- `committed_capital` field from `LadderState` — replaced with derived computation

**New field on `LadderState`:**
- `imbalance_accepted: bool = False` — set to `True` on imbalance timeout (when bot accepts one-sided position). Prevents `check_imbalance()` from re-triggering on subsequent ticks. Reset to `False` on reprice (market context changed).

**Changed: `_total_committed()`** (derived computation is O(total_orders) per call but correct by construction; with max ~576 orders across 8 markets this is negligible; add dirty-flag cache later if profiling shows it matters):
```python
def _total_committed(self) -> float:
    total = 0.0
    for mid in self.ladders:
        for order in self.tracker.get_resting(mid):
            total += order.price * (order.size - order.filled)
    return total
```

**Changed: `post_ladder()`:**
- Uses `get_tick_size()` to round all rung prices
- Filters out rungs with `size < 5.0` shares (Polymarket minimum)
- Uses `place_batch_limit_buys()` instead of individual calls
- Wraps API calls in try/except `ClobApiError`

**Changed: `reprice_if_needed()`:**
- Uses `cancel_batch()` instead of individual cancel calls
- Uses `place_batch_limit_buys()` for new rungs
- Wraps in try/except `ClobApiError`, skips market on failure

**Changed: `check_fills()`:**
- Wraps `get_open_orders()` in try/except `ClobApiError`, returns 0 on failure
- Runs reconciliation: cancelled orders still on book → reverted to resting
- Orphaned orders (on book but not in tracker) → cancel them

**Changed: `check_imbalance()`:**
- Skips markets where `imbalance_accepted` is True
- `imbalance_accepted` reset to False on reprice

### 5.4 `polybot/bot.py`

**Removed from trading loop:**
- Step 5: `check_early_exits()`
- Step 9: `_check_stop_losses()`

**Added to trading loop:**
- Heartbeat health gate at top of loop
- Reconciliation integrated into fill check step

**Changed: `run()` method:**
- Launches 3 new async tasks alongside existing ones: heartbeat, settlement poller, redeemer
- All tasks supervised with restart-on-failure (same pattern as tracker runner)

**Changed: `_settle_expired_windows()`:**
- No longer computes PnL from spot delta
- Calls `cancel_ladder(market_id)` to cancel all resting orders **on the exchange** (not just local cleanup) — this is a fallback in case step 5 of the trading loop (cancel on expiring windows) was missed during heartbeat recovery or other interruption
- Moves position to `pending_settlement` state
- Settlement poller (async task) handles resolution using `Position.profit_if_up()` or `Position.profit_if_down()` based on the actual resolved outcome, then queues for redemption

**New: `_on_connection_lost()` callback:**
- Wipes all `LadderState` entries
- Calls `tracker.mark_all_unknown()`
- Pauses ladder posting until heartbeat recovers
- On recovery: calls `get_open_orders()`, reconciles, rebuilds

### 5.5 `polybot/position_manager.py`

**New position states:**
- `pending_settlement` — window expired, waiting for Polymarket to resolve
- `failed_settlement` — resolution failed after timeout, needs manual review

**New method:**
- `mark_pending_settlement(market_id: str) -> None`
- `get_pending_settlements() -> list[str]` — returns market IDs awaiting resolution

### 5.6 `polybot/config.py`

**Note:** `BotConfig` is `frozen=True`, so all new fields must also be added to `load_bot_config()` with corresponding env var mappings.

**New fields on `BotConfig`:**
```python
# Heartbeat
heartbeat_interval_sec: float = 5.0
heartbeat_max_failures: int = 2

# Tick size cache
tick_size_ttl_sec: float = 60.0

# Batch orders (Polymarket allows up to 50; 15 is conservative default)
batch_order_size: int = 15

# Redemption
redemption_retry_max: int = 10
redemption_retry_backoff_sec: float = 2.0

# Settlement (bot-side)
bot_settlement_give_up_sec: float = 14400.0
```

### 5.7 `polybot/tracker/settlement_tracker.py`

**Changed:** Resolution functions extracted to `polybot/settlement.py`. This module imports from there:
```python
from polybot.settlement import try_resolve_once, fetch_condition_id
```

No behavior change — just code reuse.

---

## 6. Position Lifecycle (End-to-End)

```
1. Market discovered
   └─ post_ladder() → batch orders placed on both sides

2. Orders fill over time
   └─ check_fills() detects fills via get_open_orders()
   └─ position_manager accumulates qty and cost per side

3. Book moves
   └─ reprice_if_needed() → batch cancel unfilled + batch post new rungs

4. Window approaches expiry (< 60s remaining)
   └─ cancel_ladder() → batch cancel all unfilled orders

5. Window expires
   └─ _settle_expired_windows() → cancel_ladder() on exchange (fallback if step 4 missed) → mark position as pending_settlement

6. Settlement poller resolves outcome
   └─ try_resolve_once() → checks CLOB and gamma API
   └─ On resolution: compute PnL via Position.profit_if_up()/profit_if_down(), queue for redemption

7. Redeemer converts tokens on-chain
   └─ redeemPositions() → USDC.e credited
   └─ Bankroll updated with actual on-chain amount

8. Cleanup
   └─ Position, ladder state, tracker orders all removed
```

---

## 7. Error Handling Matrix

| Failure | Detection | Response |
|---|---|---|
| `get_open_orders()` fails | `ClobApiError` raised | Skip fill check for this tick |
| `place_batch_limit_buys()` partial failure | Response lists rejected orders | Track accepted orders only; log rejections |
| Tick size rejection in batch | Error code in batch response | Invalidate cache, rebuild rungs, retry once |
| Cancel API fails | `ClobApiError` raised | Order stays "cancelled" locally; reconciliation reverts if still on book |
| Heartbeat fails 2x | `heartbeat.is_healthy()` returns False | Full state reset; pause trading until recovery |
| HTTP 429 rate limit | `ClobApiError` with `retry_after` | Skip this tick; natural backoff from poll interval |
| HTTP 425 matching engine restart (Polymarket-specific status code) | `ClobApiError` | Skip this tick; retries naturally on next poll |
| HTTP 503 cancel-only mode | `ClobApiError` with `cancel_only` flag | Set `cancel_only_mode = True` on bot; gate `post_ladder()` and `reprice_if_needed()` behind this flag; clear flag on next successful non-503 API call |
| Settlement not resolved after 4h | Timeout in settlement poller | Move to `failed_settlement`; log for manual review |
| On-chain redemption fails 10x | Retry counter exceeded | Move to `failed_redemptions`; log for manual review |
| Orphaned orders on exchange | Reconciliation in `check_fills()` | Cancel them via batch cancel |

---

## 8. Testing Strategy

**Unit tests (per module):**
- `test_heartbeat.py` — health flag transitions, callback invocation, recovery
- `test_tick_size_cache.py` — TTL expiry, invalidation, fetch on miss
- `test_settlement.py` — CLOB resolution, gamma fallback, condition_id fetch
- `test_redeemer.py` — queue management, retry logic, bankroll credit

**Integration tests (failure scenarios):**
- API timeout during fill check → no phantom fills
- Cancel failure → reconciliation reverts status
- Heartbeat loss → state reset → recovery → reconciliation rebuilds
- Batch order partial rejection → only accepted orders tracked
- Tick size change mid-session → rejection → refresh → retry succeeds

**Existing tests:** Tests for removed methods (`TestEarlyExit`, `TestStopLoss`, and any tests calling `check_early_exits()` or `_check_stop_losses()`) will be deleted. The `build_ladder_rungs` function gains a `tick_size` parameter with a default of `0.01` for backward compatibility. The minimum order size filter (0.1 → 5.0 shares) may reduce rung counts in tests with small budgets — update affected test assertions. All remaining tests must continue to pass.

---

## 9. Out of Scope

- **WebSocket for fills/book updates**: Known unfixed server-side silent freeze bug (py-clob-client #292, confirmed March 2026). REST polling with batch APIs is reliable enough for v1.
- **Multi-asset position correlation**: No cross-market hedging or portfolio-level risk.
- **Maker/taker fee optimization**: Fees are factored into spread calculations but not dynamically optimized.
- **NegRisk multi-outcome markets**: Bot only trades binary UP/DOWN markets. NegRisk flag handling added for safety but multi-outcome logic is not implemented.
