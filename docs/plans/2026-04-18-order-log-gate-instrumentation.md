# Order-Log Gate Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit 7 gate-decision fields on every POST event in `data/order_log_YYYY-MM-DD.jsonl` so that the cycle-28 debugger pattern can decompose losses into gate-fired / gate-missed / gate-wrong-direction buckets without any behavior change.

**Architecture:** The gate decision context already lives in two scopes: (1) local variables in `post_ladder()` at initial-post time, and (2) persisted `LadderState` fields (`gate_fired`, `gate_winner_side`, `gate_budget_cap`) at reprice time. The instrumentation thread is: capture those values → pass them through `DataRecorder.log_order()` as keyword arguments → write to JSONL. No execution path is altered; every branch that could `return 0` before posting already returns before `place_batch_limit_buys` is reached.

**Tech Stack:** Python 3.14, JSONL append-only logging (`DataRecorder`), `pytest` unit tests.

---

## File Map

| File | Role | Change |
|------|------|--------|
| `polybot/data/data_recorder.py` | Owns `log_order()` signature | Add 7 optional kwargs; include them in the written dict only when event == "post" |
| `polybot/oms/order_executor.py` | Two POST callsites (`place_limit_buy`, `place_limit_sell`) | Accept and forward gate context kwargs to `data_recorder.log_order` |
| `polybot/strategy/ladder_manager.py` | POST origin sites: `post_ladder()` (initial_post), `reprice_if_needed()` (reprice), `boost_light_side()`, `chase_pair()`, `directional_buy()`, `try_complete_pair()`, `sell_losing_side()` | Build gate_ctx dict at each site and pass to executor |
| `tests/test_order_log_gate_fields.py` | New test file | Assert all 7 fields present on every POST event |

**Do NOT touch:** `bot.py` (its `log_order("fill", ...)` call is a FILL event — no gate fields emitted there), settlement logic, risk_manager, position_manager, order_tracker, config.py, types.py.

---

## Critical Background: What Variables Exist Where

### `post_ladder()` — initial post scope (lines ~993–1352 of `ladder_manager.py`)

After the gate block completes (whether gate fired or not), these local variables are in scope before `place_batch_limit_buys` is called:

| Variable | Meaning |
|----------|---------|
| `book_mid_gate_fired` | `True` iff gate fired |
| `_cert_book` | gate certainty (set only when gate block ran past the spread check; `None` otherwise) |
| `_book_mid_up` | normalized mid (set only when gate block ran past the spread check; `None` otherwise) |
| `_spread_up` | best_ask_up − best_bid_up (set when `book_mid_gate_enabled`; `None` otherwise) |
| `_spread_dn` | best_ask_dn − best_bid_dn (set when `book_mid_gate_enabled`; `None` otherwise) |
| `_up_mid`, `_dn_mid` | individual midpoints queried by the gate (set when `book_mid_gate_enabled`) |
| `cert` | Binance FV certainty (line ~1125, always set before budgets) |
| `fair_up` | parameter to `post_ladder()` |

The `gate_reason` string must be computed from these local variables according to the following mapping (evaluated in order):

1. `book_mid_gate_enabled == False` → `'no_eval'`
2. `book_mid_gate_fired == True` → `'fired'`
3. `skip_on_gate_miss and not book_mid_gate_fired` → `'skip_on_gate_miss'` (**note:** this branch returns 0 before any orders are placed, so no POST events reach `log_order` on this path — but we still need the reason string defined for completeness)
4. `_is_crossed` (crossed book detected) → `'crossed_book'`
5. `_has_all_data and spread_ok and _cert_book < threshold` → `'fv_certainty_below_thresh'`
6. All other non-fire cases (spread too wide, missing data) → `'no_eval'`

The `book_mid` field is the UP-side mid **as seen by the gate**: `_book_mid_up` when available, else `None`.

The `fv_price` field is Binance-implied `fair_up` (passed into `post_ladder`).

The `fv_certainty` field is `cert` (the Binance FV certainty computed at line ~1125).

The `spread` field is the UP-side spread `_spread_up` when available, else `None`. (UP is the reference side; DN spread is omitted to keep the schema flat — callers can join book_log for full depth.)

### `reprice_if_needed()` — reprice scope (lines ~1451–1615 of `ladder_manager.py`)

Gate context comes entirely from the persisted `LadderState` fields (set by `post_ladder` at initial-post time):

| Field | Maps to log field |
|-------|------------------|
| `state.gate_fired` | `gate_fired` |
| `state.gate_winner_side` + `state.gate_budget_cap` → derive reason | `gate_reason` = `'fired'` if `state.gate_fired` else `'no_eval'` |
| no live book query at reprice time | `book_mid = None` |
| no live FV query at reprice time | `fv_price = None`, `fv_certainty = None` |
| no live spread query at reprice time | `spread = None` |
| `'reprice'` | `origin` |

**This is the critical rule the prompt specified:** emit the PERSISTED state, not a re-evaluated state. The gate was evaluated once at `post_ladder()` time and that result is in `state.gate_fired`. Reprice must log what the gate decided then, not re-run the gate.

### Other POST callsites — `boost_light_side()`, `chase_pair()`, `directional_buy()`, `try_complete_pair()`, `sell_losing_side()`

These are mid-window tactical moves that do not evaluate the book-mid gate. Use:
- `gate_fired = False` (these moves bypass the gate)
- `gate_reason = 'no_eval'`
- `book_mid = None`, `fv_price = None`, `fv_certainty = None`, `spread = None`
- `origin = 'initial_post'` (they are first-time posts on those specific orders, not reprices)

---

## Ordered Change List

### Task 1 — Extend `DataRecorder.log_order()` signature

**Files:**
- Modify: `polybot/data/data_recorder.py:101-113`
- Test: `tests/test_order_log_gate_fields.py` (created in Task 4)

- [ ] **Step 1.1: Read current `log_order` signature**

Current signature (line 101):
```python
def log_order(self, ts: float, event: str, market_id: str, side: str,
              price: float, size: float, order_id: str = "", reason: str = ""):
```
Current body writes a fixed dict with 8 keys.

- [ ] **Step 1.2: Add 7 optional kwargs with `None` defaults**

Replace lines 101–113 with:

```python
def log_order(self, ts: float, event: str, market_id: str, side: str,
              price: float, size: float, order_id: str = "", reason: str = "",
              # Gate-decision context — only included on POST events
              gate_fired: bool | None = None,
              gate_reason: str | None = None,
              book_mid: float | None = None,
              fv_price: float | None = None,
              fv_certainty: float | None = None,
              spread: float | None = None,
              origin: str | None = None):
    """Log an order lifecycle event (post, reprice, cancel, fill).

    Gate context fields (gate_fired, gate_reason, book_mid, fv_price,
    fv_certainty, spread, origin) are written only when event == 'post'
    and gate_fired is not None.  Older log entries without these fields
    will not break analyzers that use .get() with defaults.
    """
    record: dict = {
        "ts": round(ts, 3),
        "event": event,
        "market_id": market_id,
        "side": side,
        "price": price,
        "size": size,
        "order_id": order_id[:16] if order_id else "",
        "reason": reason,
    }
    if event == "post" and gate_fired is not None:
        record["gate_fired"] = gate_fired
        record["gate_reason"] = gate_reason if gate_reason is not None else "no_eval"
        record["book_mid"] = book_mid
        record["fv_price"] = fv_price
        record["fv_certainty"] = fv_certainty
        record["spread"] = spread
        record["origin"] = origin if origin is not None else "initial_post"
    self._append("order_log", record, ts)
```

- [ ] **Step 1.3: Verify no other callers break**

Grep for all `log_order(` calls — confirm every existing call omits the new kwargs (they default to `None`), so no existing call sites need changes until Tasks 2–3.

Expected existing callers (all positional, no gate args):
- `polybot/oms/order_executor.py` lines 173–176, 239–242, 294–296
- `polybot/bot.py` line 782–785

All of those pass `gate_fired=None` by default → gate fields are omitted from their records. Backward-compat is maintained.

- [ ] **Step 1.4: Commit**

```bash
git add polybot/data/data_recorder.py
git commit -m "feat(recorder): add 7 optional gate-context kwargs to log_order (POST-only)"
```

---

### Task 2 — Forward gate context through `OrderExecutor.place_limit_buy` and `place_limit_sell`

**Files:**
- Modify: `polybot/oms/order_executor.py:110-177` (`place_limit_buy`)
- Modify: `polybot/oms/order_executor.py:179-243` (`place_limit_sell`)

The executor is the only place that calls `data_recorder.log_order` for POST events. Gate context must be threaded through as kwargs so callers in `ladder_manager.py` can pass the values without bypassing the executor.

- [ ] **Step 2.1: Extend `place_limit_buy` signature**

Add 7 gate-context kwargs after `expiration`:

```python
def place_limit_buy(
    self,
    token_id: str,
    price: float,
    size: float,
    market_id: str,
    side: Side,
    expiration: int = 0,
    # Gate-decision context forwarded to log_order
    gate_fired: bool | None = None,
    gate_reason: str | None = None,
    book_mid: float | None = None,
    fv_price: float | None = None,
    fv_certainty: float | None = None,
    spread: float | None = None,
    origin: str | None = None,
) -> OrderRecord:
```

- [ ] **Step 2.2: Forward kwargs in the `log_order` call inside `place_limit_buy`**

Replace lines 172–176 with:

```python
if self._data_recorder:
    self._data_recorder.log_order(
        time.time(), "post", market_id, side.value,
        validated_price, size, record.order_id, "ladder",
        gate_fired=gate_fired,
        gate_reason=gate_reason,
        book_mid=book_mid,
        fv_price=fv_price,
        fv_certainty=fv_certainty,
        spread=spread,
        origin=origin,
    )
```

- [ ] **Step 2.3: Extend `place_limit_sell` signature identically**

Add the same 7 kwargs after `expiration` in `place_limit_sell`:

```python
def place_limit_sell(
    self,
    token_id: str,
    price: float,
    size: float,
    market_id: str,
    side: Side,
    expiration: int = 0,
    gate_fired: bool | None = None,
    gate_reason: str | None = None,
    book_mid: float | None = None,
    fv_price: float | None = None,
    fv_certainty: float | None = None,
    spread: float | None = None,
    origin: str | None = None,
) -> OrderRecord:
```

- [ ] **Step 2.4: Forward kwargs in the `log_order` call inside `place_limit_sell`**

Replace lines 238–242 with:

```python
if self._data_recorder:
    self._data_recorder.log_order(
        time.time(), "post", market_id, side.value,
        validated_price, size, record.order_id, "sell",
        gate_fired=gate_fired,
        gate_reason=gate_reason,
        book_mid=book_mid,
        fv_price=fv_price,
        fv_certainty=fv_certainty,
        spread=spread,
        origin=origin,
    )
```

- [ ] **Step 2.5: Extend `place_batch_limit_buys` to pass gate context per-order**

`place_batch_limit_buys` delegates to `place_limit_buy`. Each order dict may carry gate context. Extend the method to read the 7 keys from each order dict and forward them:

```python
def place_batch_limit_buys(self, orders: list[dict]) -> list[OrderRecord]:
    """Place multiple limit buy orders, capped at cfg.batch_order_size.

    Each order dict may optionally include gate-context keys:
    gate_fired, gate_reason, book_mid, fv_price, fv_certainty, spread, origin.
    These are forwarded to place_limit_buy unchanged.
    """
    if not orders:
        return []

    cap = self.cfg.batch_order_size
    results: list[OrderRecord] = []

    for chunk_start in range(0, len(orders), cap):
        chunk = orders[chunk_start : chunk_start + cap]
        for order in chunk:
            try:
                record = self.place_limit_buy(
                    token_id=order["token_id"],
                    price=order["price"],
                    size=order["size"],
                    market_id=order["market_id"],
                    side=order["side"],
                    expiration=order.get("expiration", 0),
                    gate_fired=order.get("gate_fired"),
                    gate_reason=order.get("gate_reason"),
                    book_mid=order.get("book_mid"),
                    fv_price=order.get("fv_price"),
                    fv_certainty=order.get("fv_certainty"),
                    spread=order.get("spread"),
                    origin=order.get("origin"),
                )
                results.append(record)
            except ClobApiError as exc:
                logger.warning(
                    "Batch order rejected for %s: %s",
                    order.get("token_id", "?"),
                    exc,
                )

    return results
```

- [ ] **Step 2.6: Commit**

```bash
git add polybot/oms/order_executor.py
git commit -m "feat(executor): thread gate-context kwargs through place_limit_buy/sell/batch"
```

---

### Task 3 — Inject gate context at every POST callsite in `ladder_manager.py`

**Files:**
- Modify: `polybot/strategy/ladder_manager.py`

There are 6 distinct POST-originating sites. Each needs a `gate_ctx` dict injected into the order dicts (for batch calls) or into `place_limit_buy`/`sell` kwargs (for single calls).

#### 3A — `post_ladder()` initial post (lines ~1264–1301)

Gate context is fully available as local variables at this point. Construct a single `gate_ctx` dict after the gate block and before building `up_order_dicts`/`dn_order_dicts`.

- [ ] **Step 3A.1: Derive `_gate_reason_str` from local variables**

Insert this block immediately after `cert = fv_certainty(...)` is assigned (around line 1125, after the gate block closes) and before `up_order_dicts` is built:

```python
# --- Gate instrumentation context (pure observability, no behavior change) ---
if not self.cfg.book_mid_gate_enabled:
    _gate_reason_str = "no_eval"
elif book_mid_gate_fired:
    _gate_reason_str = "fired"
elif getattr(self, '_gate_is_crossed', False):
    _gate_reason_str = "crossed_book"
elif getattr(self, '_gate_cert_too_low', False):
    _gate_reason_str = "fv_certainty_below_thresh"
else:
    _gate_reason_str = "no_eval"
```

**Wait** — the existing code uses ephemeral `if`/`elif` branches but does not set persistent flags like `_gate_is_crossed`. Instead of adding those flags, derive `_gate_reason_str` directly from the local variables that are already in scope. The gate block sets `_has_all_data`, `_is_crossed`, `_cert_book` (or not, if the branch was not reached). Use the following approach instead:

```python
# Build gate context for log instrumentation.
# All variables come from the gate block above; use getattr/locals for safety.
if not self.cfg.book_mid_gate_enabled:
    _log_gate_reason = "no_eval"
    _log_book_mid = None
    _log_fv_certainty_gate = None
    _log_spread = None
elif book_mid_gate_fired:
    _log_gate_reason = "fired"
    _log_book_mid = _book_mid_up  # set in the fired branch
    _log_fv_certainty_gate = _cert_book
    _log_spread = _spread_up
else:
    # Gate did not fire — determine why
    # All _has_all_data / _is_crossed / _spread_up / _spread_dn / _cert_book
    # are local vars set in the gate block above.
    _local_has_data = locals().get("_has_all_data", False)
    _local_is_crossed = locals().get("_is_crossed", False)
    _local_spread_ok = (
        locals().get("_spread_up") is not None
        and locals().get("_spread_dn") is not None
        and locals().get("_spread_up", 999) <= self.cfg.book_mid_gate_max_spread
        and locals().get("_spread_dn", 999) <= self.cfg.book_mid_gate_max_spread
    )
    _local_cert = locals().get("_cert_book")
    if _local_has_data and _local_is_crossed:
        _log_gate_reason = "crossed_book"
    elif _local_has_data and _local_spread_ok and _local_cert is not None:
        _log_gate_reason = "fv_certainty_below_thresh"
    else:
        _log_gate_reason = "no_eval"
    _log_book_mid = locals().get("_book_mid_up")
    _log_fv_certainty_gate = _local_cert
    _log_spread = locals().get("_spread_up")
```

**Note:** `locals()` is safe here because this is a synchronous function with no closures over these variables. The alternative is to lift the classification into the existing gate branches (setting a `_gate_reason_str` variable inside each `if/elif` block). That is cleaner and preferred — see Step 3A.2.

- [ ] **Step 3A.2: Add `_log_gate_reason` assignment inside each gate branch (preferred approach)**

Inside `post_ladder()`, within the `if self.cfg.book_mid_gate_enabled:` block, set `_log_gate_reason`, `_log_book_mid`, `_log_fv_certainty_gate`, and `_log_spread` at the end of each branch:

**Branch 1** — `_has_all_data and _is_crossed` (crossed book):
```python
# Add at end of this branch (after the logger.debug call):
_log_gate_reason = "crossed_book"
_log_book_mid = None
_log_fv_certainty_gate = None
_log_spread = _spread_up
```

**Branch 2** — `_has_all_data and spread OK and _cert_book >= threshold` (gate fires):
```python
# Add immediately after `book_mid_gate_fired = True`:
_log_gate_reason = "fired"
_log_book_mid = _book_mid_up
_log_fv_certainty_gate = _cert_book
_log_spread = _spread_up
```

**Branch 3** — `_has_all_data and spread OK and _cert_book < threshold` (certainty too low):
```python
# Add at end of this elif branch (after the logger.debug call):
_log_gate_reason = "fv_certainty_below_thresh"
_log_book_mid = _book_mid_up
_log_fv_certainty_gate = _cert_book
_log_spread = _spread_up
```

**Branch 4** — `_has_all_data` but spread too wide:
```python
# Add at end of this elif branch (after the logger.debug call):
_log_gate_reason = "no_eval"
_log_book_mid = None
_log_fv_certainty_gate = None
_log_spread = _spread_up
```

**Branch 5** — missing data:
```python
# Add at end of the else branch (after the logger.debug call):
_log_gate_reason = "no_eval"
_log_book_mid = None
_log_fv_certainty_gate = None
_log_spread = None
```

Also add a fallback **before** the `if self.cfg.book_mid_gate_enabled:` block (so these variables always exist even when gate is disabled):

```python
# Defaults — overwritten inside the gate block when enabled
_log_gate_reason = "no_eval"
_log_book_mid = None
_log_fv_certainty_gate = None
_log_spread = None
```

- [ ] **Step 3A.3: Build `gate_ctx` dict and inject into order dicts**

Immediately before `up_order_dicts` is built (line ~1265), add:

```python
_gate_ctx = {
    "gate_fired": book_mid_gate_fired,
    "gate_reason": _log_gate_reason,
    "book_mid": _log_book_mid,
    "fv_price": fair_up,
    "fv_certainty": cert,
    "spread": _log_spread,
    "origin": "initial_post",
}
```

Then merge `_gate_ctx` into each order dict. Replace the existing list comprehensions:

```python
up_order_dicts = [
    {
        "token_id": market.up_token_id, "price": price, "size": size,
        "market_id": market.market_id, "side": Side.UP,
        "expiration": expiration,
        **_gate_ctx,
    }
    for price, size in up_rungs
]
dn_order_dicts = [
    {
        "token_id": market.dn_token_id, "price": price, "size": size,
        "market_id": market.market_id, "side": Side.DOWN,
        "expiration": expiration,
        **_gate_ctx,
    }
    for price, size in dn_rungs
]
```

- [ ] **Step 3A.4: Commit checkpoint**

```bash
git add polybot/strategy/ladder_manager.py
git commit -m "feat(ladder): inject gate_ctx into post_ladder() initial_post order dicts"
```

#### 3B — `reprice_if_needed()` reprice (lines ~1555–1605)

Gate context comes from persisted `LadderState` fields. No live FV or book query.

- [ ] **Step 3B.1: Build `_reprice_gate_ctx` from state fields**

Inside `reprice_if_needed()`, after `state.gate_fired` is checked (around line 1508), add:

```python
_reprice_gate_ctx = {
    "gate_fired": state.gate_fired,
    "gate_reason": "fired" if state.gate_fired else "no_eval",
    "book_mid": None,   # gate was evaluated at initial-post; no re-eval at reprice
    "fv_price": None,
    "fv_certainty": None,
    "spread": None,
    "origin": "reprice",
}
```

- [ ] **Step 3B.2: Inject into reprice order dicts**

For the UP reprice block (around line 1555), replace:

```python
up_order_dicts = [
    {"token_id": market.up_token_id, "price": price, "size": size,
     "market_id": mid, "side": Side.UP,
     "expiration": reprice_expiration}
    for price, size in up_rungs
]
```

with:

```python
up_order_dicts = [
    {
        "token_id": market.up_token_id, "price": price, "size": size,
        "market_id": mid, "side": Side.UP,
        "expiration": reprice_expiration,
        **_reprice_gate_ctx,
    }
    for price, size in up_rungs
]
```

For the DN reprice block (around line 1592), replace similarly:

```python
dn_order_dicts = [
    {
        "token_id": market.dn_token_id, "price": price, "size": size,
        "market_id": mid, "side": Side.DOWN,
        "expiration": reprice_expiration,
        **_reprice_gate_ctx,
    }
    for price, size in dn_rungs
]
```

- [ ] **Step 3B.3: Commit checkpoint**

```bash
git add polybot/strategy/ladder_manager.py
git commit -m "feat(ladder): inject persisted gate_ctx into reprice_if_needed() order dicts"
```

#### 3C — `boost_light_side()` (lines ~880–908)

Tactical move — no gate eval. Use `no_eval` defaults.

- [ ] **Step 3C.1: Add `_boost_gate_ctx` before `order_dicts` construction in `boost_light_side()`**

```python
_boost_gate_ctx = {
    "gate_fired": False,
    "gate_reason": "no_eval",
    "book_mid": None,
    "fv_price": None,
    "fv_certainty": None,
    "spread": None,
    "origin": "initial_post",
}
```

Inject `**_boost_gate_ctx` into the existing `order_dicts` list comprehension (line ~875):

```python
order_dicts = [
    {
        "token_id": light_token, "price": price, "size": size,
        "market_id": mid, "side": light_side,
        "expiration": expiration,
        **_boost_gate_ctx,
    }
    for price, size in new_rungs
]
```

#### 3D — `chase_pair()` (lines ~590–596)

Same pattern as boost.

- [ ] **Step 3D.1: Add `_chase_gate_ctx` before `order_dicts` construction in `chase_pair()`**

```python
_chase_gate_ctx = {
    "gate_fired": False,
    "gate_reason": "no_eval",
    "book_mid": None,
    "fv_price": None,
    "fv_certainty": None,
    "spread": None,
    "origin": "initial_post",
}
```

Inject into the existing `order_dicts` list comprehension:

```python
order_dicts = [
    {
        "token_id": chase_token, "price": price, "size": size,
        "market_id": mid, "side": chase_side,
        "expiration": expiration,
        **_chase_gate_ctx,
    }
    for price, size in chase_rungs
]
```

#### 3E — `directional_buy()` (lines ~696–702)

Single-order post via `place_limit_buy`. Pass gate context as kwargs.

- [ ] **Step 3E.1: Pass gate context kwargs to `place_limit_buy` in `directional_buy()`**

Replace the existing call:

```python
record = self.executor.place_limit_buy(
    buy_token, best_ask, qty, mid, buy_side,
    expiration=expiration,
)
```

with:

```python
record = self.executor.place_limit_buy(
    buy_token, best_ask, qty, mid, buy_side,
    expiration=expiration,
    gate_fired=False,
    gate_reason="no_eval",
    book_mid=None,
    fv_price=fair_up,
    fv_certainty=cert,
    spread=None,
    origin="initial_post",
)
```

(`cert` is already computed at line ~651 in `directional_buy()`.)

#### 3F — `try_complete_pair()` (lines ~302–306)

Single-order post via `place_limit_buy`.

- [ ] **Step 3F.1: Pass gate context kwargs to `place_limit_buy` in `try_complete_pair()`**

Replace:

```python
record = self.executor.place_limit_buy(
    light_token, best_ask, deficit, mid, light_side,
)
```

with:

```python
record = self.executor.place_limit_buy(
    light_token, best_ask, deficit, mid, light_side,
    gate_fired=False,
    gate_reason="no_eval",
    book_mid=None,
    fv_price=None,
    fv_certainty=None,
    spread=None,
    origin="initial_post",
)
```

#### 3G — `sell_losing_side()` (lines ~443–448)

Single sell post via `place_limit_sell`.

- [ ] **Step 3G.1: Pass gate context kwargs to `place_limit_sell` in `sell_losing_side()`**

Replace:

```python
record = self.executor.place_limit_sell(
    sell_token, sell_price, sell_qty, mid, sell_side,
    expiration=expiration,
)
```

with:

```python
record = self.executor.place_limit_sell(
    sell_token, sell_price, sell_qty, mid, sell_side,
    expiration=expiration,
    gate_fired=False,
    gate_reason="no_eval",
    book_mid=None,
    fv_price=None,
    fv_certainty=None,
    spread=None,
    origin="initial_post",
)
```

- [ ] **Step 3H: Commit all ladder_manager changes**

```bash
git add polybot/strategy/ladder_manager.py
git commit -m "feat(ladder): gate_ctx injected at all POST callsites (boost/chase/directional/forcebuy/exit)"
```

---

### Task 4 — Write tests

**Files:**
- Create: `tests/test_order_log_gate_fields.py`

- [ ] **Step 4.1: Write the failing tests first**

Create `tests/test_order_log_gate_fields.py`:

```python
"""Tests: gate-decision fields appear on every POST event in order_log.

Verifies that all 7 gate-context fields (gate_fired, gate_reason, book_mid,
fv_price, fv_certainty, spread, origin) appear on every 'post' event emitted
by DataRecorder.log_order(), regardless of which ladder path produced the order.

CANCEL and FILL events must NOT include these fields (backward-compat invariant).
"""

import json
import pathlib
import tempfile
import time
import pytest
from unittest.mock import MagicMock, patch

from polybot.data.data_recorder import DataRecorder
from polybot.config import BotConfig
from polybot.oms.order_executor import OrderExecutor
from polybot.strategy.ladder_manager import LadderManager
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.types import MarketWindow, Side


GATE_FIELDS = {"gate_fired", "gate_reason", "book_mid", "fv_price", "fv_certainty", "spread", "origin"}
VALID_REASONS = {"fired", "skip_on_gate_miss", "crossed_book", "fv_certainty_below_thresh", "no_eval"}


# ---------------------------------------------------------------------------
# DataRecorder unit tests
# ---------------------------------------------------------------------------

class TestDataRecorderLogOrder:
    def _recorder(self, tmpdir):
        return DataRecorder(data_dir=tmpdir)

    def _read_last(self, tmpdir) -> dict:
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        assert files, "No order_log file written"
        lines = files[0].read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_post_event_includes_all_7_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="ladder",
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=0.60, fv_certainty=0.82, spread=0.02, origin="initial_post",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field in record, f"Missing field: {field}"

    def test_post_event_gate_reason_is_valid_enum(self, tmp_path):
        rec = self._recorder(tmp_path)
        for reason in VALID_REASONS:
            rec.log_order(
                ts=time.time(), event="post", market_id="mkt1", side="UP",
                price=0.50, size=10.0,
                gate_fired=False, gate_reason=reason, book_mid=None,
                fv_price=0.50, fv_certainty=0.0, spread=None, origin="initial_post",
            )
            record = self._read_last(tmp_path)
            assert record["gate_reason"] in VALID_REASONS

    def test_cancel_event_does_not_include_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="cancel", market_id="", side="",
            price=0, size=0, order_id="o1", reason="cancel",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field unexpectedly present on cancel: {field}"

    def test_fill_event_does_not_include_gate_fields(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="fill", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="detected",
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field unexpectedly present on fill: {field}"

    def test_post_without_gate_fired_omits_gate_fields(self, tmp_path):
        """Backward-compat: callers that do not pass gate_fired get no gate fields."""
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0, order_id="o1", reason="ladder",
            # No gate kwargs
        )
        record = self._read_last(tmp_path)
        for field in GATE_FIELDS:
            assert field not in record, f"Gate field present when not supplied: {field}"

    def test_post_gate_fields_null_values_written(self, tmp_path):
        """book_mid, fv_price, fv_certainty, spread may legitimately be None."""
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="DN",
            price=0.45, size=20.0, order_id="o2", reason="ladder",
            gate_fired=False, gate_reason="no_eval", book_mid=None,
            fv_price=None, fv_certainty=None, spread=None, origin="reprice",
        )
        record = self._read_last(tmp_path)
        assert record["book_mid"] is None
        assert record["fv_price"] is None
        assert record["origin"] == "reprice"

    def test_reprice_origin_written_correctly(self, tmp_path):
        rec = self._recorder(tmp_path)
        rec.log_order(
            ts=time.time(), event="post", market_id="mkt1", side="UP",
            price=0.50, size=10.0,
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=None, fv_certainty=None, spread=None,
            origin="reprice",
        )
        record = self._read_last(tmp_path)
        assert record["origin"] == "reprice"


# ---------------------------------------------------------------------------
# OrderExecutor integration: gate kwargs thread through to recorder
# ---------------------------------------------------------------------------

class TestOrderExecutorGateForwarding:
    def _make_executor(self, tmp_path):
        cfg = BotConfig(private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p")
        clob = MagicMock()
        clob.create_order.return_value = {"signed": True}
        clob.post_order.return_value = {"orderID": "ord1", "status": "resting"}
        recorder = DataRecorder(data_dir=tmp_path)
        return OrderExecutor(cfg, clob_client=clob, data_recorder=recorder), tmp_path

    def _read_last(self, tmp_path) -> dict:
        files = list(pathlib.Path(tmp_path).glob("order_log_*.jsonl"))
        lines = files[0].read_text().strip().splitlines()
        return json.loads(lines[-1])

    def test_place_limit_buy_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.place_limit_buy(
            token_id="tok1", price=0.50, size=10.0,
            market_id="mkt1", side=Side.UP,
            gate_fired=True, gate_reason="fired", book_mid=0.55,
            fv_price=0.60, fv_certainty=0.80, spread=0.03, origin="initial_post",
        )
        record = self._read_last(tmpdir)
        assert record["event"] == "post"
        assert record["gate_fired"] is True
        assert record["gate_reason"] == "fired"
        assert record["book_mid"] == pytest.approx(0.55)
        assert record["origin"] == "initial_post"

    def test_place_limit_sell_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.place_limit_sell(
            token_id="tok1", price=0.40, size=10.0,
            market_id="mkt1", side=Side.UP,
            gate_fired=False, gate_reason="no_eval", book_mid=None,
            fv_price=None, fv_certainty=None, spread=None, origin="initial_post",
        )
        record = self._read_last(tmpdir)
        assert record["event"] == "post"
        assert record["gate_fired"] is False
        assert record["gate_reason"] == "no_eval"
        assert record["book_mid"] is None

    def test_place_batch_limit_buys_forwards_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        orders = [
            {
                "token_id": "tok1", "price": 0.50, "size": 10.0,
                "market_id": "mkt1", "side": Side.UP,
                "gate_fired": True, "gate_reason": "fired",
                "book_mid": 0.55, "fv_price": 0.60, "fv_certainty": 0.82,
                "spread": 0.03, "origin": "initial_post",
            },
            {
                "token_id": "tok2", "price": 0.48, "size": 8.0,
                "market_id": "mkt1", "side": Side.UP,
                "gate_fired": True, "gate_reason": "fired",
                "book_mid": 0.55, "fv_price": 0.60, "fv_certainty": 0.82,
                "spread": 0.03, "origin": "initial_post",
            },
        ]
        executor.place_batch_limit_buys(orders)
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
        post_records = [r for r in records if r["event"] == "post"]
        assert len(post_records) == 2
        for r in post_records:
            for field in GATE_FIELDS:
                assert field in r, f"Batch post missing gate field: {field}"

    def test_cancel_order_does_not_emit_gate_fields(self, tmp_path):
        executor, tmpdir = self._make_executor(tmp_path)
        executor.cancel_order("ord1")
        record = self._read_last(tmpdir)
        assert record["event"] == "cancel"
        for field in GATE_FIELDS:
            assert field not in record


# ---------------------------------------------------------------------------
# LadderManager integration: end-to-end post_ladder emits gate fields
# ---------------------------------------------------------------------------

def _make_market(market_id="mkt-test", timeframe_sec=900):
    now = int(time.time())
    return MarketWindow(
        market_id=market_id,
        condition_id="0xcond",
        asset="BTC",
        timeframe_sec=timeframe_sec,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=now - 60,
        close_epoch=now + (timeframe_sec - 60),
    )


def _make_ladder_manager(tmp_path, book_mid_gate_enabled=False, skip_on_gate_miss=False):
    cfg = BotConfig(
        private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p",
        ladder_rungs=4,
        ladder_width=0.10,
        ladder_spacing=0.02,
        ladder_size_skew=2.0,
        book_mid_gate_enabled=book_mid_gate_enabled,
        book_mid_gate_certainty_threshold=0.55,
        book_mid_gate_max_spread=0.05,
        skip_on_gate_miss=skip_on_gate_miss,
        fair_value_enabled=False,
        fv_gate_enabled=False,
    )
    clob = MagicMock()
    clob.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.44", size="5000")],
        asks=[MagicMock(price="0.46", size="5000")],
    )
    clob.create_order.return_value = {"signed": True}
    clob.post_order.return_value = {"orderID": "o1", "status": "resting"}
    clob.get_open_orders.return_value = []

    recorder = DataRecorder(data_dir=tmp_path)
    executor = OrderExecutor(cfg, clob_client=clob, data_recorder=recorder)
    tracker = OrderTracker()
    positions = PositionManager(cfg, bankroll=5000.0)
    risk = RiskManager(cfg, starting_bankroll=5000.0)
    mgr = LadderManager(cfg, executor, tracker, positions, risk)
    return mgr, recorder, tmp_path


def _read_post_records(tmp_path) -> list[dict]:
    files = list(pathlib.Path(tmp_path).glob("order_log_*.jsonl"))
    if not files:
        return []
    records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
    return [r for r in records if r["event"] == "post"]


class TestLadderManagerGateInstrumentation:
    def test_initial_post_gate_disabled_emits_no_eval(self, tmp_path):
        """When book_mid_gate_enabled=False, all POST events have gate_reason='no_eval'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        assert len(posts) > 0, "Expected at least one POST event"
        for r in posts:
            for field in GATE_FIELDS:
                assert field in r, f"POST missing gate field '{field}': {r}"
            assert r["gate_fired"] is False
            assert r["gate_reason"] == "no_eval"
            assert r["origin"] == "initial_post"

    def test_initial_post_gate_fires_emits_fired(self, tmp_path):
        """When gate fires (tight book + high cert), posts have gate_reason='fired'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=True)
        # Stub book to produce book_mid_up >> 0.5 so gate fires (cert > 0.55 threshold)
        # book_mid_up = up_mid / (up_mid + dn_mid); set up_mid=0.75, dn_mid=0.25 → cert=0.50
        # Use 0.80/0.20 → cert=0.60 > 0.55 threshold
        up_mid = 0.80
        dn_mid = 0.20
        # executor.get_midpoint called twice per side
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.78", size="5000")],
            asks=[MagicMock(price="0.82", size="5000")],
        )
        with patch.object(mgr.executor, "get_midpoint", side_effect=[up_mid, dn_mid, up_mid, dn_mid]):
            with patch.object(mgr.executor, "get_best_bid", return_value=0.78):
                with patch.object(mgr.executor, "get_best_ask", return_value=0.82):
                    market = _make_market()
                    mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        assert len(posts) > 0
        for r in posts:
            assert r["gate_fired"] is True
            assert r["gate_reason"] == "fired"
            assert r["origin"] == "initial_post"
            assert r["book_mid"] is not None

    def test_reprice_emits_persisted_gate_state(self, tmp_path):
        """Reprice events emit persisted LadderState.gate_fired, not a re-evaluated gate."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        # Initial post
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        # Manually set gate_fired on the LadderState to simulate a gate-fire window
        state = mgr.ladders[market.market_id]
        from polybot.types import Side as _Side
        state.gate_fired = True
        state.gate_winner_side = _Side.UP
        state.gate_budget_cap = 18.0
        # Force reprice by making best_ask move beyond threshold
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],  # moved from 0.46
        )
        state.last_reprice_at = 0  # force cooldown bypass
        markets = {market.market_id: market}
        mgr.reprice_if_needed(markets)
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        assert len(reprice_posts) > 0, "Expected at least one reprice POST event"
        for r in reprice_posts:
            for field in GATE_FIELDS:
                assert field in r, f"Reprice POST missing gate field: {field}"
            assert r["gate_fired"] is True
            assert r["gate_reason"] == "fired"
            assert r["origin"] == "reprice"
            # Reprice does NOT re-evaluate live book data for gate
            assert r["book_mid"] is None
            assert r["fv_price"] is None

    def test_reprice_gate_not_fired_emits_no_eval(self, tmp_path):
        """Reprice of a non-gate-fired ladder emits gate_reason='no_eval'."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        state = mgr.ladders[market.market_id]
        # gate_fired defaults to False
        assert state.gate_fired is False
        mgr.executor.client.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.54", size="5000")],
            asks=[MagicMock(price="0.56", size="5000")],
        )
        state.last_reprice_at = 0
        mgr.reprice_if_needed({market.market_id: market})
        posts = _read_post_records(tmpdir)
        reprice_posts = [r for r in posts if r.get("origin") == "reprice"]
        for r in reprice_posts:
            assert r["gate_fired"] is False
            assert r["gate_reason"] == "no_eval"

    def test_all_7_fields_present_on_every_post_event(self, tmp_path):
        """Integration: every POST event from post_ladder() carries all 7 gate fields."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        posts = _read_post_records(tmpdir)
        assert len(posts) > 0
        for r in posts:
            for field in GATE_FIELDS:
                assert field in r, f"POST event missing required gate field '{field}': {r}"

    def test_cancel_events_have_no_gate_fields(self, tmp_path):
        """CANCEL events from ladder_manager must not carry gate fields."""
        mgr, _, tmpdir = _make_ladder_manager(tmp_path, book_mid_gate_enabled=False)
        market = _make_market()
        mgr.post_ladder(market, spot_delta=0.0, fair_up=0.5)
        mgr.cancel_ladder(market.market_id)
        files = list(pathlib.Path(tmpdir).glob("order_log_*.jsonl"))
        records = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
        cancel_records = [r for r in records if r["event"] == "cancel"]
        for r in cancel_records:
            for field in GATE_FIELDS:
                assert field not in r, f"CANCEL event has unexpected gate field: {field}"
```

- [ ] **Step 4.2: Run tests to verify they all fail before implementation**

```bash
cd C:/Users/pc/Desktop/Bots/PolyBot
python -m pytest tests/test_order_log_gate_fields.py -v 2>&1 | head -60
```

Expected: multiple `ImportError` or `AttributeError` failures (gate kwargs don't exist yet).

- [ ] **Step 4.3: After implementing Tasks 1–3, run tests to verify they all pass**

```bash
python -m pytest tests/test_order_log_gate_fields.py -v
```

Expected output: all tests `PASSED`.

- [ ] **Step 4.4: Run full test suite to ensure no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all existing tests still pass plus the new test file.

- [ ] **Step 4.5: Commit**

```bash
git add tests/test_order_log_gate_fields.py
git commit -m "test: gate-context fields on POST events in order_log"
```

---

### Task 5 — Verify `cancel_ladder()` is not affected

`LadderManager.cancel_ladder()` cancels orders via `executor.cancel_batch()` → `executor.cancel_order()` → `data_recorder.log_order(..., event="cancel", ...)`. The recorder now only writes gate fields when `event == "post"` AND `gate_fired is not None`. Cancel events are untouched. No changes needed here — just confirm the test in 4.1 passes.

---

## Invariants Verified Before Writing This Plan

1. **Pair cost guard** — `reprice_if_needed()` pair-cost guard at lines 1551–1553/1589–1590 is untouched. This plan adds only `**_reprice_gate_ctx` to order dicts (new keys), which `place_batch_limit_buys` forwards via `order.get(...)`. No existing logic reads the new keys.

2. **Settlement dedup** — `_settled_markets` in `bot.py` is not touched.

3. **Bankroll tiers** — `get_trading_rules()` in `config.py` is not touched.

4. **Reprice-path gate persistence (commit 8d200ba / Cycle 29)** — The plan reads `state.gate_fired` / `state.gate_winner_side` / `state.gate_budget_cap` at reprice time purely for logging. The budget decisions at lines 1508–1530 are preserved unchanged. The logging block is inserted after those decisions are made.

5. **Backward compat** — `DataRecorder.log_order()` only writes gate fields when `gate_fired is not None`. All existing callers (`bot.py` fill log, executor cancel log) pass no gate kwargs → `gate_fired=None` → fields omitted. Log analyzers using `.get("gate_fired", False)` get the default without error.

---

## Do Not Touch

- `polybot/bot.py` — the `log_order("fill", ...)` call there is a fill event and correctly receives no gate fields.
- `polybot/settlement.py` — settlement logic is fully independent.
- `polybot/risk_manager.py` — no order logging here.
- `polybot/config.py` — no new config fields needed; gate params already exist.
- `polybot/types.py` — no schema changes; `OrderRecord` is unchanged.
- `polybot/strategy/order_tracker.py` — unchanged.
- `polybot/strategy/position_manager.py` — unchanged.
- `polybot/strategy/fair_value.py` — unchanged.
- `polybot/data/data_recorder.py` streams 1, 2, 4, 5, 6 — only `log_order` (stream 3) changes.
- Any file in `polybot/web/` — dashboard is read-only from logs.

---

## Summary of All POST Callsites

| Callsite | Method | Gate context source | `origin` |
|----------|--------|--------------------|---------| 
| `post_ladder()` UP rungs | `place_batch_limit_buys` | local gate variables | `'initial_post'` |
| `post_ladder()` DN rungs | `place_batch_limit_buys` | local gate variables | `'initial_post'` |
| `reprice_if_needed()` UP | `place_batch_limit_buys` | `state.gate_fired` persisted | `'reprice'` |
| `reprice_if_needed()` DN | `place_batch_limit_buys` | `state.gate_fired` persisted | `'reprice'` |
| `boost_light_side()` | `place_batch_limit_buys` | no gate eval → `False/no_eval` | `'initial_post'` |
| `chase_pair()` | `place_batch_limit_buys` | no gate eval → `False/no_eval` | `'initial_post'` |
| `directional_buy()` | `place_limit_buy` | no gate eval → `False/no_eval` | `'initial_post'` |
| `try_complete_pair()` | `place_limit_buy` | no gate eval → `False/no_eval` | `'initial_post'` |
| `sell_losing_side()` | `place_limit_sell` | no gate eval → `False/no_eval` | `'initial_post'` |

All 9 callsites are covered. CANCEL and FILL events are not touched.
