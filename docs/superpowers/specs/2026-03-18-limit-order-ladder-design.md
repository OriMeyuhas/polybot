# Limit Order Ladder System — Design Spec

## Problem

The bot currently places single market-taking orders at best ask. The whale (0x8dxd) posts passive limit order ladders across multiple price levels, achieving combined UP+DOWN costs averaging $0.88 (12% below $1). His edge comes entirely from execution — being the market maker, not the taker.

## Solution

Replace the signal-engine + evaluate-market flow with a **Continuous Ladder Manager** that maintains resting limit orders on every active market. When a window opens, it posts a full ladder (both sides). It tracks fills, reprices as the book moves, manages fill imbalance, and cancels unfilled orders before expiry.

---

## Architecture

### New Components

**`polybot/ladder_manager.py`** — Core orchestrator. Builds ladders, posts orders, tracks fills, reprices, manages imbalance, cancels on expiry.

**`polybot/order_tracker.py`** — Registry of all resting orders keyed by `order_id`. Tracks state transitions: resting → partial → filled → cancelled.

### Modified Components

| File | Change |
|------|--------|
| `bot.py` | Replace `evaluate_market` loop with `ladder_manager` tick-based flow |
| `order_executor.py` | Add `place_limit_sell()` and `get_open_orders()` |
| `config.py` | Add ladder params, remove signal engine params |
| `display.py` | Show ladder status per market (rungs posted/filled/cancelled) |
| `position_manager.py` | Update on fill detection, not on order placement |
| `run_bot.py` | Probability-based mock with resting order state and simulated fills |
| `ALGORITHM.md` | Document ladder strategy |

### Deleted Components

| File | Reason |
|------|--------|
| `signal_engine.py` | Subsumed by ladder manager |
| `tests/test_signal_engine.py` | Replaced by ladder manager tests |

---

## Ladder Construction

When a new market window is discovered and at least 10% of the window has elapsed, the ladder manager builds and posts orders for both sides.

### Parameters

| Param | Default | Source |
|-------|---------|--------|
| `ladder_rungs` | 16 | Whale avg: 16 rungs per side |
| `ladder_spacing` | 0.01 | Whale: 60.5% of gaps are $0.01 |
| `ladder_width` | 0.15 | Half-width from best ask to cheapest rung |
| `ladder_size_skew` | 2.0 | Expensive-to-cheap size ratio |
| `reprice_threshold` | 0.02 | Book movement required to trigger reprice |
| `max_imbalance_ratio` | 0.60 | Imbalance threshold for cancelling heavy side |
| `imbalance_timeout_sec` | 30 | Time to wait for lagging side before accepting imbalance |
| `mock_base_fill_rate` | 0.15 | Dry-run fill probability per tick for closest rung |

### Anchor Pricing

The ladder anchors below the current best ask:

```
anchor = best_ask - ladder_width

Rungs: anchor, anchor + spacing, anchor + 2*spacing, ..., anchor + (rungs-1)*spacing
```

Example — DOWN side, best ask = $0.48:
```
anchor = 0.48 - 0.15 = 0.33
Rungs: $0.33, $0.34, $0.35, ..., $0.48  (16 rungs, $0.01 apart)
```

The cheapest rung ($0.33) is farthest from market — least likely to fill, smallest size. The most expensive rung ($0.48) is at market — most likely to fill, largest size.

### Size Distribution

Total budget per side = `bankroll * position_size_fraction / 2`.

Sizes follow a linear skew from cheapest to most expensive:

```python
weight[i] = 1.0 + (ladder_size_skew - 1.0) * (i / (rungs - 1))
# Normalize: total_cost = sum(weight[i] * price[i]) should equal budget_per_side
scale = budget_per_side / sum(weight[i] * price[i] for all i)
size[i] = scale * weight[i]
```

With `ladder_size_skew = 2.0`, the most expensive rung gets 2x the shares of the cheapest.

### Pair Cost Guard

Before posting, verify worst-case combined cost:

```python
vwap_up = sum(price[i] * size[i]) / sum(size[i])  # for UP ladder
vwap_dn = sum(price[i] * size[i]) / sum(size[i])  # for DOWN ladder
if vwap_up + vwap_dn > max_pair_cost:
    # Tighten ladder_width and rebuild
```

---

## Order Tracking

### TrackedOrder Dataclass

```python
@dataclass
class TrackedOrder:
    order_id: str
    market_id: str
    token_id: str
    side: Side          # UP or DOWN
    price: float
    size: float         # originally placed size
    filled: float       # filled so far
    status: str         # "resting", "partial", "filled", "cancelled"
    placed_at: float    # epoch timestamp
```

### OrderTracker Registry

```python
class OrderTracker:
    orders: dict[str, TrackedOrder]       # keyed by order_id
    by_market: dict[str, list[str]]       # market_id -> list of order_ids

    def add(order) -> None
    def update_fill(order_id, filled_qty) -> None
    def cancel(order_id) -> None
    def cancel_market(market_id) -> None
    def get_resting(market_id) -> list[TrackedOrder]
    def get_filled(market_id) -> list[TrackedOrder]
    def filled_qty(market_id, side) -> float
    def filled_cost(market_id, side) -> float
```

### Fill Detection

Each tick, the ladder manager compares tracked orders against the CLOB:

```python
open_order_ids = set(order_executor.get_open_orders())
for order in tracker.get_resting(market_id):
    if order.order_id not in open_order_ids:
        # Order disappeared from book — it filled
        tracker.update_fill(order.order_id, order.size)
        position_manager.update_position(market_id, order.side, order.size, order.size * order.price)
```

Key change: **positions update on fill, not on placement.** This means positions reflect actual holdings, not intended orders.

---

## Repricing

Each tick, for each active ladder, check if the book has moved enough to warrant repricing:

```python
if abs(current_best_ask - ladder_anchor) > reprice_threshold:
    cancel unfilled rungs for this side
    rebuild ladder around new anchor
    post new rungs
```

`reprice_threshold` defaults to 0.02 ($0.02). This naturally gates refresh frequency — if the book is stable, no repricing occurs.

Timeframe behavior (matching whale data):
- **1h:** Rarely reprices (book doesn't move $0.02 often in 1h binary markets)
- **15m:** Reprices a few times per window
- **5m:** Reprices frequently as book is more volatile

---

## Imbalance Guard

The main risk: filling one side but not the other, leaving an unintended directional position.

### Detection

Each tick, compute fill imbalance per market:

```python
up_qty = tracker.filled_qty(market_id, Side.UP)
dn_qty = tracker.filled_qty(market_id, Side.DOWN)
total = up_qty + dn_qty
if total == 0:
    imbalance = 0.0
else:
    imbalance = abs(up_qty - dn_qty) / max(up_qty, dn_qty)
```

### Escalating Response

| Imbalance | Action |
|-----------|--------|
| < 30% | Normal — keep both ladders active |
| 30–60% | **Boost lagging side** — tighten that ladder (reduce `ladder_width` by 50% to move rungs closer to market) |
| > 60% | **Cancel heavy side's unfilled rungs.** Start `imbalance_timeout_sec` timer. If lagging side doesn't catch up, accept the imbalanced position as directional — existing stop-loss logic manages it from there |

### State Tracking

```python
@dataclass
class LadderState:
    market_id: str
    anchor_up: float
    anchor_dn: float
    posted_at: float
    imbalance_alert_at: float | None  # when >60% imbalance first detected
    boosted_side: Side | None         # which side got tightened
```

---

## Early Exit

Stays from current implementation. When one side of a filled spread has appreciated 50%+ above entry cost, sell it:

```python
avg_entry = filled_cost / filled_qty
current_ask = get_best_ask(token_id)
gain_pct = (current_ask - avg_entry) / avg_entry
if gain_pct >= early_exit_profit_pct:
    place_limit_sell(token_id, price=current_ask, size=filled_qty)
```

Requires new `place_limit_sell()` in order executor (mirrors `place_limit_buy()` but uses SELL constant).

---

## Bot Loop

The `run_trading_loop()` becomes:

```python
async def run_trading_loop(self):
    while True:
        now = int(time.time())

        # 1. Snapshot open prices for new windows
        self._snapshot_window_open_prices()

        # 2. Post ladders on new markets
        for market in self.active_markets:
            if market.is_active(now) and not self.ladder_manager.has_ladder(market.market_id):
                elapsed_pct = market.elapsed(now) / market.timeframe_sec
                if elapsed_pct >= 0.10:  # wait for 10% of window
                    await asyncio.to_thread(self.ladder_manager.post_ladder, market)

        # 3. Check fills on all active ladders
        await asyncio.to_thread(self.ladder_manager.check_fills)

        # 4. Reprice if book moved
        await asyncio.to_thread(self.ladder_manager.reprice_if_needed)

        # 5. Imbalance guard
        self.ladder_manager.check_imbalance(now)

        # 6. Early exit check
        await asyncio.to_thread(self.ladder_manager.check_early_exits)

        # 7. Cancel rungs on expiring windows
        for market in self.active_markets:
            if market.is_active(now) and market.remaining(now) < self.cfg.no_trade_final_sec:
                self.ladder_manager.cancel_ladder(market.market_id)

        # 8. Settlement (unchanged logic, extracted to method)
        self._settle_expired_windows(now)

        # 9. Stop-loss on one-sided positions
        self._check_stop_losses(now)

        await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)
```

---

## Probability-Based Mock Client

For dry-run testing, `MockClobClient` gains order state tracking and simulated fills.

### State

```python
class MockClobClient:
    _resting: dict[str, dict]    # order_id -> {token_id, price, size, remaining}
    _next_order_id: int
```

### Fill Simulation (called each tick)

```python
def tick(self, mid_prices: dict[str, float]):
    for order_id, order in list(self._resting.items()):
        token_asset = extract_asset(order["token_id"])
        mid = mid_prices.get(token_asset, 0.5)

        distance = abs(order["price"] - mid)
        max_dist = 0.50  # max possible distance in binary market
        fill_prob = base_fill_rate * (1.0 - distance / max_dist)
        fill_prob = max(0.01, fill_prob)

        if random() < fill_prob:
            # Partial fill: 20-100% of remaining
            fill_pct = uniform(0.20, 1.00)
            fill_qty = order["remaining"] * fill_pct
            order["remaining"] -= fill_qty
            if order["remaining"] < 0.01:
                del self._resting[order_id]
```

### New Methods

```python
def get_open_orders(self) -> list[dict]:
    return [{"id": oid, **info} for oid, info in self._resting.items()]

def post_order(self, signed, order_type):
    # Add to _resting instead of instant fill
    order_id = f"mock-{self._next_order_id}"
    self._resting[order_id] = {...}
    return {"orderID": order_id, "status": "resting"}
```

---

## Configuration Changes

### New Parameters

```python
# Ladder
ladder_rungs: int = 16
ladder_spacing: float = 0.01
ladder_width: float = 0.15
ladder_size_skew: float = 2.0
reprice_threshold: float = 0.02
max_imbalance_ratio: float = 0.60
imbalance_timeout_sec: int = 30
mock_base_fill_rate: float = 0.15
```

### Removed Parameters

```python
# Signal engine params (no longer needed)
min_spread_edge         # ladder replaces spread detection
min_directional_move    # ladder replaces directional detection
window_min_elapsed_sec  # replaced by 10% elapsed check
spread_min_elapsed_pct  # same — baked into ladder post logic
max_directional_price   # no directional signal anymore
min_directional_price   # no directional signal anymore
```

### Kept Parameters

```python
position_size_fraction: float = 0.10
max_pair_cost: float = 0.985
max_concurrent_positions: int = 8
early_exit_profit_pct: float = 0.50
stop_loss_reversal: float = 0.001
no_trade_final_sec: int = 60
max_book_depth_take_pct: float = 0.50
poll_interval_ms: int = 500
market_discovery_interval_sec: int = 60
```

---

## Testing Strategy

### New Tests

**`tests/test_ladder_manager.py`:**
- `test_build_ladder_correct_rungs` — 16 rungs, $0.01 spacing, correct anchor
- `test_build_ladder_size_skew` — most expensive rung is 2x the cheapest
- `test_pair_cost_guard_rejects` — tightens ladder when combined VWAP > max_pair_cost
- `test_reprice_triggered` — reprices when book moves > threshold
- `test_reprice_not_triggered` — no reprice when book is stable
- `test_cancel_on_expiry` — cancels unfilled rungs when window nears close
- `test_imbalance_boost` — tightens lagging side when imbalance 30-60%
- `test_imbalance_cancel` — cancels heavy side when imbalance > 60%
- `test_imbalance_timeout` — accepts directional after timeout
- `test_early_exit_on_appreciation` — sells appreciated side at 50%+ gain
- `test_full_window_lifecycle` — post → fills → reprice → cancel → settle

**`tests/test_order_tracker.py`:**
- `test_add_and_retrieve` — add orders, query by market
- `test_fill_updates_status` — resting → partial → filled transitions
- `test_cancel_removes` — cancelled orders excluded from active queries
- `test_filled_qty_and_cost` — aggregation per market/side

**`tests/test_mock_client.py`:**
- `test_resting_orders_tracked` — post_order adds to resting state
- `test_fill_probability_distance` — closer orders fill more often
- `test_partial_fills` — orders fill in chunks, not all at once
- `test_cancel_removes_from_resting` — cancel clears order state
- `test_get_open_orders` — returns current resting orders

### Modified Tests

- `tests/test_bot_integration.py` — update to use ladder manager instead of evaluate_market
- `tests/test_signal_engine.py` — deleted (signal engine removed)

---

## Display Changes

The dashboard's "Active Positions" panel gains ladder status:

```
ACTIVE POSITIONS
Market     | Side | Rungs | Filled | Resting | VWAP   | PnL    | Imbalance
btc_5m_42  | UP   | 16    | 8/16   | 8       | $0.49  | +$2.10 | 15% OK
btc_5m_42  | DN   | 16    | 6/16   | 10      | $0.42  | -$1.50 | 15% OK
eth_15m_7  | UP   | 16    | 3/16   | 13      | $0.51  |  $0.00 | 62% CANCEL
```

---

## Thread Safety

No changes — same as before. All state mutations happen in the asyncio event loop. CLOB calls dispatched via `asyncio.to_thread()` only return data; mutations happen back in the loop. The mock client's `tick()` is called from the event loop, not from a thread.
