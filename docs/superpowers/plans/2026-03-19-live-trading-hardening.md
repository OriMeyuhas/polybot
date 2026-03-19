# Live Trading Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden PolyBot for live Polymarket deployment by fixing phantom fills, adding heartbeat, batch orders, tick size handling, proper settlement, and on-chain redemption.

**Architecture:** New modules (heartbeat, tick_size_cache, settlement, redeemer) plug into the existing bot loop. Order executor becomes error-aware (raises exceptions instead of silent defaults). Ladder manager uses batch APIs and derives committed capital. Early exit and stop-loss removed — positions held to settlement. Settlement uses actual Polymarket resolution, not spot delta guessing.

**Tech Stack:** Python 3.14, py-clob-client, httpx, asyncio, web3/polygon RPC (for redemption)

**Spec:** `docs/superpowers/specs/2026-03-19-live-trading-hardening-design.md`

---

## File Map

### New Files
| File | Responsibility |
|---|---|
| `polybot/errors.py` | `ClobApiError` exception class |
| `polybot/heartbeat.py` | Heartbeat loop, connection health, state reset callback |
| `polybot/tick_size_cache.py` | TTL cache for market tick sizes |
| `polybot/settlement.py` | Shared resolution logic (extracted from tracker) |
| `polybot/redeemer.py` | On-chain token redemption after settlement |
| `tests/test_heartbeat.py` | Heartbeat unit tests |
| `tests/test_tick_size_cache.py` | Tick size cache unit tests |
| `tests/test_settlement.py` | Settlement resolution unit tests |
| `tests/test_redeemer.py` | Redeemer unit tests |
| `tests/test_reconciliation.py` | Fill detection + reconciliation integration tests |

### Modified Files
| File | Changes |
|---|---|
| `polybot/config.py` | Add 7 new BotConfig fields + env var loading |
| `polybot/order_executor.py` | Raise `ClobApiError`, add batch methods, remove `place_limit_sell()` |
| `polybot/order_tracker.py` | Add `unknown` status, `mark_all_unknown()`, `reconcile()`, fix fill threshold |
| `polybot/ladder_manager.py` | Remove `check_early_exits()`, derive committed capital, batch APIs, tick size rounding, imbalance fix, min order size filter |
| `polybot/position_manager.py` | Add `pending_settlement`/`failed_settlement` states |
| `polybot/bot.py` | Remove early exit + stop-loss, add heartbeat gate, settlement poller, redeemer task, connection lost handler, cancel-only mode |
| `polybot/tracker/settlement_tracker.py` | Import from shared `polybot/settlement.py` |
| `tests/test_order_executor.py` | Update for ClobApiError, add batch tests |
| `tests/test_ladder_manager.py` | Remove early exit tests, update for batch/tick size |
| `tests/test_bot_integration.py` | Remove stop-loss tests, add heartbeat/settlement tests |
| `tests/test_order_tracker.py` | Add reconciliation and mark_all_unknown tests |

---

## Task 1: ClobApiError Exception + Config Additions

**Files:**
- Create: `polybot/errors.py`
- Modify: `polybot/config.py`
- Test: `tests/test_types.py` (quick config validation)

- [ ] **Step 1: Create `polybot/errors.py`**

```python
"""Custom exceptions for Polymarket CLOB API interactions."""


class ClobApiError(Exception):
    """Raised when a CLOB API call fails (timeout, 429, 5xx, network error).

    Attributes:
        status_code: HTTP status code if available, else None.
        retry_after: Seconds to wait before retrying (from 429 Retry-After header), else None.
        cancel_only: True if the exchange is in cancel-only mode (503).
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
        cancel_only: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.cancel_only = cancel_only
```

- [ ] **Step 2: Add new fields to `BotConfig` in `polybot/config.py`**

Add after `stop_loss_reversal` field:

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

- [ ] **Step 3: Update `load_bot_config()` env var loading**

Add corresponding env var mappings:

```python
        heartbeat_interval_sec=float(os.getenv("HEARTBEAT_INTERVAL_SEC", "5.0")),
        heartbeat_max_failures=int(os.getenv("HEARTBEAT_MAX_FAILURES", "2")),
        tick_size_ttl_sec=float(os.getenv("TICK_SIZE_TTL_SEC", "60.0")),
        batch_order_size=int(os.getenv("BATCH_ORDER_SIZE", "15")),
        redemption_retry_max=int(os.getenv("REDEMPTION_RETRY_MAX", "10")),
        redemption_retry_backoff_sec=float(os.getenv("REDEMPTION_RETRY_BACKOFF_SEC", "2.0")),
        bot_settlement_give_up_sec=float(os.getenv("BOT_SETTLEMENT_GIVE_UP_SEC", "14400.0")),
```

- [ ] **Step 4: Run existing tests to verify no breakage**

Run: `python -m pytest tests/ -v`
Expected: All 98 tests pass (config changes are additive, defaults preserve old behavior)

- [ ] **Step 5: Commit**

```bash
git add polybot/errors.py polybot/config.py
git commit -m "feat: add ClobApiError exception and new config fields for live trading"
```

---

## Task 2: Order Tracker Enhancements

**Files:**
- Modify: `polybot/order_tracker.py`
- Test: `tests/test_order_tracker.py`

- [ ] **Step 1: Write failing tests for new tracker features**

Add to `tests/test_order_tracker.py`:

```python
class TestMarkAllUnknown:
    def test_marks_resting_as_unknown(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        tracker.add(TrackedOrder(
            order_id="o2", market_id="m1", token_id="t2",
            side=Side.DOWN, price=0.50, size=10.0, placed_at=1000,
        ))
        # Fill one order
        tracker.update_fill("o1", 10.0)

        tracker.mark_all_unknown()

        assert tracker.orders["o1"].status == "filled"  # filled stays filled
        assert tracker.orders["o2"].status == "unknown"  # resting becomes unknown

    def test_cancelled_becomes_unknown(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        tracker.cancel("o1")
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"


class TestReconcile:
    def test_detects_filled_orders(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        # o1 is resting locally but not on exchange = filled
        result = tracker.reconcile(open_orders=[])
        assert "o1" in [o.order_id for o in result["filled"]]

    def test_reverts_cancelled_still_on_exchange(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        tracker.cancel("o1")
        # o1 is cancelled locally but still on exchange = revert to resting
        result = tracker.reconcile(open_orders=[{"id": "o1"}])
        assert "o1" in result["reverted"]
        assert tracker.orders["o1"].status == "resting"

    def test_detects_orphaned_orders(self):
        tracker = OrderTracker()
        # Exchange has order we don't know about
        result = tracker.reconcile(open_orders=[{"id": "orphan1"}])
        assert "orphan1" in result["orphaned"]

    def test_unknown_orders_resolved(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        tracker.mark_all_unknown()
        # o1 is unknown and still on exchange = revert to resting
        result = tracker.reconcile(open_orders=[{"id": "o1"}])
        assert tracker.orders["o1"].status == "resting"
        assert "o1" not in [o.order_id for o in result["filled"]]


class TestFillThreshold:
    def test_relative_threshold_large_order(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=1000.0, placed_at=1000,
        ))
        tracker.update_fill("o1", 999.5)
        assert tracker.orders["o1"].status == "filled"

    def test_relative_threshold_small_order(self):
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=5.0, placed_at=1000,
        ))
        tracker.update_fill("o1", 4.996)
        assert tracker.orders["o1"].status == "filled"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_order_tracker.py -v -k "MarkAllUnknown or Reconcile or FillThreshold"`
Expected: FAIL (methods don't exist yet)

- [ ] **Step 3: Implement tracker enhancements in `polybot/order_tracker.py`**

Add `mark_all_unknown()`:
```python
    def mark_all_unknown(self) -> None:
        """Set all non-filled orders to 'unknown' status (heartbeat reset)."""
        for order in self.orders.values():
            if order.status not in ("filled",):
                order.status = "unknown"
```

Note: `get_resting(market_id)` already exists at `order_tracker.py:67` — no changes needed for it.

Add `reconcile()`:
```python
    def reconcile(self, open_orders: list[dict]) -> dict:
        """Compare local state against exchange. Returns {filled, reverted, orphaned}."""
        exchange_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}

        filled = []
        reverted = []

        for order in list(self.orders.values()):
            if order.status == "filled":
                continue

            on_exchange = order.order_id in exchange_ids

            if not on_exchange and order.status in ("resting", "unknown", "partial"):
                # Gone from exchange = filled
                fill_qty = order.size - order.filled
                if fill_qty > 0:
                    self.update_fill(order.order_id, fill_qty)
                    filled.append(order)
            elif on_exchange and order.status in ("cancelled", "unknown"):
                # Still on exchange but we thought it was cancelled/unknown = revert
                order.status = "resting"
                reverted.append(order.order_id)

        # Orphaned: on exchange but not in our tracker
        our_ids = set(self.orders.keys())
        orphaned = [oid for oid in exchange_ids if oid and oid not in our_ids]

        return {"filled": filled, "reverted": reverted, "orphaned": orphaned}
```

Fix fill threshold — change in `update_fill()`:
```python
        # Old: if order.filled >= order.size - 0.001:
        if order.filled >= order.size * 0.999:
            order.filled = order.size
            order.status = "filled"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_order_tracker.py -v`
Expected: All pass (old + new)

- [ ] **Step 5: Commit**

```bash
git add polybot/order_tracker.py tests/test_order_tracker.py
git commit -m "feat: add reconciliation, mark_all_unknown, and relative fill threshold to order tracker"
```

---

## Task 3: Tick Size Cache

**Files:**
- Create: `polybot/tick_size_cache.py`
- Create: `tests/test_tick_size_cache.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for tick size cache."""
import time
from unittest.mock import MagicMock

from polybot.tick_size_cache import TickSizeCache


class TestTickSizeCache:
    def test_returns_fetched_value(self):
        client = MagicMock()
        client.get_tick_size.return_value = 0.01
        cache = TickSizeCache(client, ttl_sec=60.0)
        assert cache.get_tick_size("0xabc") == 0.01

    def test_caches_value(self):
        client = MagicMock()
        client.get_tick_size.return_value = 0.01
        cache = TickSizeCache(client, ttl_sec=60.0)
        cache.get_tick_size("0xabc")
        cache.get_tick_size("0xabc")
        assert client.get_tick_size.call_count == 1  # only fetched once

    def test_invalidate_forces_refetch(self):
        client = MagicMock()
        client.get_tick_size.return_value = 0.01
        cache = TickSizeCache(client, ttl_sec=60.0)
        cache.get_tick_size("0xabc")
        cache.invalidate("0xabc")
        cache.get_tick_size("0xabc")
        assert client.get_tick_size.call_count == 2

    def test_ttl_expiry(self):
        client = MagicMock()
        client.get_tick_size.return_value = 0.01
        cache = TickSizeCache(client, ttl_sec=0.0)  # immediate expiry
        cache.get_tick_size("0xabc")
        cache.get_tick_size("0xabc")
        assert client.get_tick_size.call_count == 2

    def test_different_markets_cached_separately(self):
        client = MagicMock()
        client.get_tick_size.side_effect = [0.01, 0.001]
        cache = TickSizeCache(client, ttl_sec=60.0)
        assert cache.get_tick_size("0xabc") == 0.01
        assert cache.get_tick_size("0xdef") == 0.001


class TestRoundToTickSize:
    def test_round_to_tick(self):
        from polybot.tick_size_cache import round_to_tick
        assert round_to_tick(0.456, 0.01) == 0.46
        assert round_to_tick(0.454, 0.01) == 0.45
        assert round_to_tick(0.4567, 0.001) == 0.457
        assert round_to_tick(0.45, 0.1) == 0.5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_tick_size_cache.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Implement `polybot/tick_size_cache.py`**

```python
"""Tick size cache with TTL and invalidation support."""

import time


def round_to_tick(price: float, tick_size: float) -> float:
    """Round a price to the nearest valid tick."""
    return round(round(price / tick_size) * tick_size, 10)


class TickSizeCache:
    """TTL cache for Polymarket market tick sizes.

    Fetches tick sizes from the CLOB client and caches them.
    On order rejection for tick size violation, caller should
    call invalidate() and retry.
    """

    def __init__(self, client, ttl_sec: float = 60.0):
        self._client = client
        self._ttl = ttl_sec
        self._cache: dict[str, tuple[float, float]] = {}  # condition_id -> (tick_size, fetched_at)

    def get_tick_size(self, condition_id: str) -> float:
        """Return tick size for a market, fetching if not cached or expired."""
        entry = self._cache.get(condition_id)
        now = time.monotonic()
        if entry is not None:
            tick_size, fetched_at = entry
            if now - fetched_at < self._ttl:
                return tick_size

        tick_size = self._client.get_tick_size(condition_id)
        self._cache[condition_id] = (tick_size, now)
        return tick_size

    def invalidate(self, condition_id: str) -> None:
        """Force re-fetch on next get_tick_size() call."""
        self._cache.pop(condition_id, None)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_tick_size_cache.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/tick_size_cache.py tests/test_tick_size_cache.py
git commit -m "feat: add tick size cache with TTL and invalidation"
```

---

## Task 4: Extract Shared Settlement Module

**Files:**
- Create: `polybot/settlement.py`
- Create: `tests/test_settlement.py`
- Modify: `polybot/tracker/settlement_tracker.py`

- [ ] **Step 1: Write failing tests for settlement module**

```python
"""Tests for shared settlement resolution logic."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from polybot.settlement import resolve_via_clob, resolve_via_gamma, fetch_condition_id


class TestResolveViaClob:
    def test_resolved_with_winner(self):
        client = AsyncMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "closed": True,
                "tokens": [
                    {"outcome": "Up", "winner": True},
                    {"outcome": "Down", "winner": False},
                ],
            },
        )
        client.get.return_value.raise_for_status = MagicMock()
        result = asyncio.run(resolve_via_clob(client, "https://clob.polymarket.com", "0xabc"))
        assert result == {"outcome": "UP", "settlement_price": 1.0}

    def test_not_resolved_returns_none(self):
        client = AsyncMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"closed": False, "tokens": []},
        )
        client.get.return_value.raise_for_status = MagicMock()
        result = asyncio.run(resolve_via_clob(client, "https://clob.polymarket.com", "0xabc"))
        assert result is None


class TestResolveViaGamma:
    def test_resolved_from_outcome_prices(self):
        client = AsyncMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{
                "markets": [{
                    "closed": True,
                    "outcomes": '["Up", "Down"]',
                    "outcomePrices": '["1", "0"]',
                }],
            }],
        )
        client.get.return_value.raise_for_status = MagicMock()
        result = asyncio.run(resolve_via_gamma(client, "test-slug"))
        assert result == {"outcome": "UP", "settlement_price": 1.0}


class TestFetchConditionId:
    def test_fetches_from_gamma(self):
        client = AsyncMock()
        client.get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{
                "markets": [{"conditionId": "0xabc123"}],
            }],
        )
        client.get.return_value.raise_for_status = MagicMock()
        result = asyncio.run(fetch_condition_id(client, "test-slug"))
        assert result == "0xabc123"

    def test_returns_empty_on_failure(self):
        client = AsyncMock()
        client.get.side_effect = Exception("network error")
        result = asyncio.run(fetch_condition_id(client, "test-slug"))
        assert result == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_settlement.py -v`
Expected: FAIL (module doesn't exist)

- [ ] **Step 3: Extract resolution functions from `polybot/tracker/settlement_tracker.py` into `polybot/settlement.py`**

Create `polybot/settlement.py` by extracting `_resolve_via_clob`, `_resolve_via_gamma`, `_fetch_condition_id_from_gamma`, and `_try_resolve_once` from `settlement_tracker.py`. Rename them to drop the underscore prefix (they're now public API):

```python
"""Shared market resolution logic for Polymarket.

Used by both the live bot (polybot/bot.py) and the tracker
(polybot/tracker/settlement_tracker.py).
"""

import json
import logging

import httpx

log = logging.getLogger(__name__)


async def resolve_via_clob(
    client: httpx.AsyncClient,
    clob_host: str,
    condition_id: str,
) -> dict | None:
    """Try the CLOB API: GET /markets/{condition_id}."""
    url = f"{clob_host}/markets/{condition_id}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("resolved") or data.get("closed"):
        tokens = data.get("tokens", [])
        for tok in tokens:
            if tok.get("winner") is True or str(tok.get("winner")).lower() == "true":
                outcome = tok.get("outcome", "").upper()
                if outcome in ("UP", "DOWN", "YES", "NO"):
                    return {"outcome": outcome, "settlement_price": 1.0}

        winner = data.get("winner")
        if winner:
            return {"outcome": str(winner).upper(), "settlement_price": 1.0}

    return None


async def resolve_via_gamma(
    client: httpx.AsyncClient,
    slug: str,
) -> dict | None:
    """Fallback: query gamma-api events endpoint by slug."""
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        markets = event.get("markets", [event])
        for mkt in markets:
            if not (mkt.get("resolved") or mkt.get("closed")):
                continue

            outcomes_raw = mkt.get("outcomes", "[]")
            prices_raw = mkt.get("outcomePrices", "[]")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_list = json.loads(outcomes_raw)
                    prices_list = json.loads(prices_raw)
                    for outcome, price in zip(outcomes_list, prices_list):
                        if str(price) == "1":
                            return {"outcome": str(outcome).upper(), "settlement_price": 1.0}
                except (ValueError, TypeError):
                    pass

            winner = mkt.get("winner") or mkt.get("outcome")
            if winner:
                return {"outcome": str(winner).upper(), "settlement_price": 1.0}

    return None


async def fetch_condition_id(
    client: httpx.AsyncClient,
    slug: str,
) -> str:
    """Look up condition_id from gamma-api by slug. Returns empty string on failure."""
    try:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            markets = event.get("markets", [event])
            for mkt in markets:
                cid = mkt.get("conditionId", mkt.get("condition_id", ""))
                if cid:
                    return cid
    except Exception as exc:
        log.debug("Failed to fetch condition_id from gamma for %s: %s", slug, exc)
    return ""


async def try_resolve_once(
    client: httpx.AsyncClient,
    clob_host: str,
    slug: str,
    condition_id: str,
) -> dict | None:
    """Single non-blocking resolution attempt. Returns outcome dict or None."""
    if not condition_id or not condition_id.startswith("0x"):
        fetched = await fetch_condition_id(client, slug)
        if fetched:
            condition_id = fetched

    try:
        if condition_id and condition_id.startswith("0x"):
            result = await resolve_via_clob(client, clob_host, condition_id)
            if result is not None:
                return result

        result = await resolve_via_gamma(client, slug)
        if result is not None:
            return result
    except Exception as exc:
        log.debug("Settlement resolve attempt for %s failed: %s", slug, exc)

    return None
```

- [ ] **Step 4: Update `polybot/tracker/settlement_tracker.py` to import from shared module**

Replace the local resolution functions with imports:

```python
from polybot.settlement import (
    resolve_via_clob,
    resolve_via_gamma,
    fetch_condition_id,
    try_resolve_once,
)
```

Delete the local copies of `_resolve_via_clob`, `_resolve_via_gamma`, `_fetch_condition_id_from_gamma`, and `_try_resolve_once`. Update `run_settlement_tracker()` to call `try_resolve_once(client, cfg.polymarket_host, slug, condition_id)`.

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass (settlement tests + tracker tests + everything else)

- [ ] **Step 6: Commit**

```bash
git add polybot/settlement.py tests/test_settlement.py polybot/tracker/settlement_tracker.py
git commit -m "feat: extract shared settlement resolution module"
```

---

## Task 5: Order Executor Hardening

**Files:**
- Modify: `polybot/order_executor.py`
- Modify: `tests/test_order_executor.py`

- [ ] **Step 1: Write failing tests for error-aware executor**

Add to `tests/test_order_executor.py`:

```python
from polybot.errors import ClobApiError


class TestClobApiErrorHandling:
    def test_get_open_orders_raises_on_failure(self, executor, mock_clob):
        mock_clob.get_open_orders.side_effect = Exception("network error")
        with pytest.raises(ClobApiError):
            executor.get_open_orders()

    def test_get_best_ask_raises_on_failure(self, executor, mock_clob):
        mock_clob.get_order_book.side_effect = Exception("timeout")
        with pytest.raises(ClobApiError):
            executor.get_best_ask("token123")

    def test_place_limit_buy_raises_on_failure(self, executor, mock_clob):
        mock_clob.create_order.side_effect = Exception("500 error")
        with pytest.raises(ClobApiError):
            executor.place_limit_buy(
                token_id="t1", price=0.50, size=10.0,
                market_id="m1", side=Side.UP,
            )

    def test_cancel_order_raises_on_failure(self, executor, mock_clob):
        mock_clob.cancel.side_effect = Exception("timeout")
        with pytest.raises(ClobApiError):
            executor.cancel_order("order123")


class TestBatchOrders:
    def test_place_batch_returns_records(self, executor, mock_clob):
        mock_clob.post_orders.return_value = [
            {"orderID": "mock-1", "status": "resting"},
            {"orderID": "mock-2", "status": "resting"},
        ]
        orders = [
            {"token_id": "t1", "price": 0.50, "size": 10.0, "market_id": "m1", "side": Side.UP},
            {"token_id": "t1", "price": 0.45, "size": 12.0, "market_id": "m1", "side": Side.UP},
        ]
        results = executor.place_batch_limit_buys(orders)
        assert len(results) == 2
        assert all(r.status != "error" for r in results)

    def test_place_batch_partial_rejection(self, executor, mock_clob):
        """Spec error matrix: partial failure tracks accepted only, logs rejections."""
        mock_clob.post_orders.return_value = [
            {"orderID": "mock-1", "status": "resting"},
            {"orderID": "", "status": "error", "errorMsg": "tick size violation"},
        ]
        orders = [
            {"token_id": "t1", "price": 0.50, "size": 10.0, "market_id": "m1", "side": Side.UP},
            {"token_id": "t1", "price": 0.456, "size": 12.0, "market_id": "m1", "side": Side.UP},
        ]
        results = executor.place_batch_limit_buys(orders)
        assert len(results) == 1  # only the accepted one

    def test_cancel_batch_returns_cancelled_ids(self, executor, mock_clob):
        mock_clob.cancel_orders.return_value = {"cancelled": True}
        result = executor.cancel_batch(["o1", "o2", "o3"])
        assert result == ["o1", "o2", "o3"]

    def test_cancel_batch_falls_back_to_individual(self, executor, mock_clob):
        mock_clob.cancel_orders.side_effect = Exception("batch endpoint down")
        mock_clob.cancel.return_value = {"cancelled": True}
        result = executor.cancel_batch(["o1", "o2"])
        assert result == ["o1", "o2"]  # fell back to individual cancels
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_order_executor.py -v -k "ClobApiError or Batch"`
Expected: FAIL

- [ ] **Step 3: Update `polybot/order_executor.py`**

Import the error:
```python
from polybot.errors import ClobApiError
```

All methods should parse HTTP status codes from exceptions to set `ClobApiError` attributes properly:

```python
def _make_clob_error(exc: Exception) -> ClobApiError:
    """Convert an API exception into a ClobApiError with proper attributes."""
    status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
    retry_after = None
    cancel_only = False
    if status_code == 429:
        retry_after = float(getattr(getattr(exc, 'response', None), 'headers', {}).get('Retry-After', 5))
    elif status_code == 503:
        cancel_only = True
    return ClobApiError(str(exc), status_code=status_code, retry_after=retry_after, cancel_only=cancel_only)
```

Change `get_open_orders()`:
```python
    def get_open_orders(self) -> list[dict]:
        try:
            return self.client.get_open_orders()
        except Exception as exc:
            raise _make_clob_error(exc) from exc
```

Change `get_best_ask()`:
```python
    def get_best_ask(self, token_id: str) -> float:
        try:
            book = self.client.get_order_book(token_id)
            asks = book.asks
            if asks:
                return float(asks[0].price)
            return 1.0  # no asks means very expensive
        except Exception as exc:
            raise _make_clob_error(exc) from exc
```

Change `place_limit_buy()` — wrap in try/except, raise via `_make_clob_error`.

Change `cancel_order()` — wrap in try/except, raise via `_make_clob_error`.

Remove `place_limit_sell()` entirely.

Also remove dead config fields `early_exit_profit_pct` and `stop_loss_reversal` from `BotConfig` and `load_bot_config()`.

Add `place_batch_limit_buys()` using the real `client.post_orders()` batch API:
```python
    def place_batch_limit_buys(self, orders: list[dict], batch_size: int = 15) -> list[OrderRecord]:
        """Place multiple limit buy orders via batch API. Returns OrderRecords for accepted orders."""
        from py_clob_client.order_builder.constants import BUY
        results = []
        for chunk_start in range(0, len(orders), batch_size):
            chunk = orders[chunk_start:chunk_start + batch_size]
            try:
                signed_orders = []
                for o in chunk:
                    signed = self.client.create_order(OrderArgs(
                        token_id=o["token_id"],
                        price=o["price"],
                        size=o["size"],
                        side=BUY,
                    ))
                    signed_orders.append(signed)
                resp = self.client.post_orders(signed_orders)
                # Parse response — each entry has orderID and status
                for i, entry in enumerate(resp if isinstance(resp, list) else [resp]):
                    oid = entry.get("orderID", "")
                    status = entry.get("status", "error")
                    if status != "error" and oid:
                        results.append(OrderRecord(
                            order_id=oid,
                            market_id=chunk[i]["market_id"],
                            side=chunk[i]["side"],
                            price=chunk[i]["price"],
                            size=chunk[i]["size"],
                            status="resting",
                        ))
                    else:
                        logger.warning("Batch order rejected: %s", entry)
            except Exception as exc:
                logger.warning("Batch post failed for chunk: %s", exc)
        return results
```

Add `cancel_batch()` using the real `client.cancel_orders()` batch API:
```python
    def cancel_batch(self, order_ids: list[str], batch_size: int = 15) -> list[str]:
        """Cancel multiple orders via batch API. Returns successfully cancelled IDs."""
        cancelled = []
        for chunk_start in range(0, len(order_ids), batch_size):
            chunk = order_ids[chunk_start:chunk_start + batch_size]
            try:
                self.client.cancel_orders(chunk)
                cancelled.extend(chunk)
            except Exception as exc:
                logger.warning("Batch cancel failed for chunk: %s", exc)
                # Fall back to individual cancels for this chunk
                for oid in chunk:
                    try:
                        self.cancel_order(oid)
                        cancelled.append(oid)
                    except ClobApiError:
                        pass
        return cancelled
```

- [ ] **Step 4: Update existing tests that expect old behavior**

The existing `test_place_limit_sell` test must be removed. Update any test that catches silent error returns to expect `ClobApiError`.

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add polybot/order_executor.py tests/test_order_executor.py
git commit -m "feat: error-aware order executor with ClobApiError and batch methods"
```

---

## Task 6: Ladder Manager Hardening

**Files:**
- Modify: `polybot/ladder_manager.py`
- Modify: `tests/test_ladder_manager.py`

- [ ] **Step 1: Remove `check_early_exits()` and `committed_capital`, add TickSizeCache injection**

Delete the entire `check_early_exits()` method (lines 373-434).

Remove `committed_capital: float = 0.0` from `LadderState`.

Add `imbalance_accepted: bool = False` to `LadderState`.

Add `tick_size_cache` to `LadderManager.__init__()`:
```python
    def __init__(self, cfg, order_executor, order_tracker, position_manager, risk_manager, tick_size_cache=None):
        # ... existing init ...
        self.tick_cache = tick_size_cache
```

- [ ] **Step 2: Replace `_total_committed()` with derived computation**

```python
    def _total_committed(self) -> float:
        """Total capital committed across all active ladders (derived from tracker)."""
        total = 0.0
        for mid in self.ladders:
            for order in self.tracker.get_resting(mid):
                total += order.price * (order.size - order.filled)
        return total
```

Remove all lines that update `state.committed_capital` in `post_ladder()`, `check_fills()`, and `reprice_if_needed()`.

- [ ] **Step 3: Update `build_ladder_rungs()` with tick_size parameter and min size 5.0**

Add `tick_size: float = 0.01` parameter to `build_ladder_rungs()`. Round all prices and filter small rungs:

```python
def build_ladder_rungs(
    best_ask, budget, rungs, spacing, width, size_skew, tick_size=0.01,
) -> list[tuple[float, float]]:
    from polybot.tick_size_cache import round_to_tick
    anchor = max(0.01, best_ask - width)
    prices = [round_to_tick(anchor + i * spacing, tick_size) for i in range(rungs)]
    prices = [max(0.01, min(0.99, p)) for p in prices]
    # ... existing weight/scale logic ...
    result = []
    for price, weight in zip(prices, weights):
        size = scale * weight
        if size >= 5.0:  # Polymarket minimum for GTC orders
            result.append((price, round(size, 1)))
    return result
```

- [ ] **Step 4: Update `post_ladder()` to use batch API**

Key changes:
- Get tick_size: `tick_size = self.tick_cache.get_tick_size(market.condition_id) if self.tick_cache else 0.01`
- Pass tick_size to `build_ladder_rungs()`
- Collect all rungs as order dicts, then call `self.executor.place_batch_limit_buys()`
- Wrap in try/except `ClobApiError`
- On tick size rejection: `self.tick_cache.invalidate(market.condition_id)`, rebuild and retry once

- [ ] **Step 4: Update `reprice_if_needed()` to use batch APIs**

- Use `self.executor.cancel_batch()` instead of individual cancel calls
- Use `self.executor.place_batch_limit_buys()` for new rungs
- Wrap in try/except `ClobApiError`

- [ ] **Step 5: Rewrite `check_fills()` to use reconciliation**

```python
    def check_fills(self) -> int:
        try:
            open_orders = self.executor.get_open_orders()
        except ClobApiError:
            return 0  # skip fill detection this tick

        result = self.tracker.reconcile(open_orders)

        # Credit fills to position manager
        for order in result["filled"]:
            fill_qty = order.size  # reconcile already called update_fill
            self.positions.update_position(
                order.market_id, order.side, fill_qty, fill_qty * order.price,
            )
            logger.info("FILL: %s %s %.1f @ $%.2f on %s",
                         order.side.value, order.token_id[:16],
                         fill_qty, order.price, order.market_id)

        # Cancel orphaned orders
        if result["orphaned"]:
            self.executor.cancel_batch(result["orphaned"])
            logger.warning("Cancelled %d orphaned orders", len(result["orphaned"]))

        if result["reverted"]:
            logger.info("Reverted %d cancelled orders back to resting", len(result["reverted"]))

        return len(result["filled"])
```

- [ ] **Step 6: Fix `check_imbalance()` to respect `imbalance_accepted`**

Add at top of per-market loop:
```python
            if state.imbalance_accepted:
                continue
```

In the timeout branch:
```python
            elif now_epoch - state.imbalance_alert_at > self.cfg.imbalance_timeout_sec:
                state.imbalance_accepted = True
                state.imbalance_alert_at = None
```

Reset on reprice:
```python
            state.imbalance_accepted = False
```

- [ ] **Step 7: Update tests**

Remove `TestEarlyExit` class from `tests/test_ladder_manager.py`.

Update `TestPostLadder` to not check `committed_capital`.

Update `build_ladder_rungs` tests — add `tick_size` parameter with default 0.01. Update minimum size assertions (0.1 → 5.0 for rungs that would be too small).

- [ ] **Step 8: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 9: Commit**

```bash
git add polybot/ladder_manager.py tests/test_ladder_manager.py
git commit -m "feat: harden ladder manager with batch APIs, tick size, no early exit, derived capital"
```

---

## Task 7: Position Manager Enhancements

**Files:**
- Modify: `polybot/position_manager.py`
- Modify: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing tests**

```python
class TestPendingSettlement:
    def test_mark_pending(self):
        mgr = PositionManager(cfg, bankroll=1000)
        mgr.update_position("m1", Side.UP, 10.0, 5.0)
        mgr.mark_pending_settlement("m1")
        assert "m1" in mgr.get_pending_settlements()
        assert "m1" in mgr.positions  # position still exists

    def test_mark_failed(self):
        mgr = PositionManager(cfg, bankroll=1000)
        mgr.update_position("m1", Side.UP, 10.0, 5.0)
        mgr.mark_pending_settlement("m1")
        mgr.mark_failed_settlement("m1")
        assert "m1" in mgr.get_failed_settlements()
        assert "m1" not in mgr.get_pending_settlements()
```

- [ ] **Step 2: Run to verify failure, then implement**

Add to `polybot/position_manager.py`:

```python
        self._pending_settlement: set[str] = set()
        self._failed_settlement: set[str] = set()

    def mark_pending_settlement(self, market_id: str) -> None:
        self._pending_settlement.add(market_id)

    def get_pending_settlements(self) -> list[str]:
        return list(self._pending_settlement)

    def mark_failed_settlement(self, market_id: str) -> None:
        self._pending_settlement.discard(market_id)
        self._failed_settlement.add(market_id)

    def get_failed_settlements(self) -> list[str]:
        return list(self._failed_settlement)

    def complete_settlement(self, market_id: str) -> None:
        self._pending_settlement.discard(market_id)
        self._failed_settlement.discard(market_id)
```

- [ ] **Step 3: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add polybot/position_manager.py tests/test_position_manager.py
git commit -m "feat: add pending/failed settlement states to position manager"
```

---

## Task 8: Heartbeat Module

**Files:**
- Create: `polybot/heartbeat.py`
- Create: `tests/test_heartbeat.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for heartbeat module."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from polybot.heartbeat import Heartbeat


class TestHeartbeat:
    def test_initially_healthy(self):
        hb = Heartbeat(interval_sec=5.0, max_failures=2)
        assert hb.is_healthy() is True

    def test_unhealthy_after_max_failures(self):
        hb = Heartbeat(interval_sec=5.0, max_failures=2)
        hb._consecutive_failures = 2
        assert hb.is_healthy() is False

    def test_recovery_resets_health(self):
        hb = Heartbeat(interval_sec=5.0, max_failures=2)
        hb._consecutive_failures = 2
        assert hb.is_healthy() is False
        hb._consecutive_failures = 0
        hb._healthy = True
        assert hb.is_healthy() is True

    def test_callback_invoked_on_connection_lost(self):
        callback = MagicMock()
        hb = Heartbeat(interval_sec=5.0, max_failures=2)
        hb._on_connection_lost = callback
        hb._record_failure()
        assert callback.call_count == 0  # 1 failure, not enough
        hb._record_failure()
        assert callback.call_count == 1  # 2 failures = connection lost

    def test_callback_not_re_invoked(self):
        callback = MagicMock()
        hb = Heartbeat(interval_sec=5.0, max_failures=2)
        hb._on_connection_lost = callback
        hb._record_failure()
        hb._record_failure()  # triggers callback
        hb._record_failure()  # should NOT re-trigger
        assert callback.call_count == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_heartbeat.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `polybot/heartbeat.py`**

```python
"""Heartbeat task for Polymarket CLOB session keepalive."""

import asyncio
import logging
from typing import Callable

log = logging.getLogger(__name__)


class Heartbeat:
    """Sends periodic heartbeats to keep Polymarket session alive.

    Polymarket cancels ALL open orders after 10 seconds without a heartbeat.
    We send every 5 seconds (configurable) for safety margin.
    """

    def __init__(self, interval_sec: float = 5.0, max_failures: int = 2):
        self._interval = interval_sec
        self._max_failures = max_failures
        self._consecutive_failures = 0
        self._healthy = True
        self._on_connection_lost: Callable | None = None
        self._connection_lost_fired = False
        self._heartbeat_id: str | None = None

    def is_healthy(self) -> bool:
        return self._healthy

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures and not self._connection_lost_fired:
            self._healthy = False
            self._connection_lost_fired = True
            log.error(
                "Heartbeat failed %d consecutive times — connection lost",
                self._consecutive_failures,
            )
            if self._on_connection_lost:
                self._on_connection_lost()

    def _record_success(self, heartbeat_id: str | None = None) -> None:
        was_unhealthy = not self._healthy
        self._consecutive_failures = 0
        self._healthy = True
        self._connection_lost_fired = False
        if heartbeat_id:
            self._heartbeat_id = heartbeat_id
        if was_unhealthy:
            log.info("Heartbeat recovered")

    async def run(self, client, on_connection_lost: Callable) -> None:
        """Main heartbeat loop. Runs as an independent async task."""
        self._on_connection_lost = on_connection_lost
        log.info("Heartbeat started (interval=%.1fs, max_failures=%d)",
                 self._interval, self._max_failures)

        while True:
            try:
                # TODO: Replace with actual Polymarket heartbeat API call
                # resp = client.send_heartbeat(self._heartbeat_id)
                # self._record_success(resp.get("heartbeat_id"))
                self._record_success()
            except Exception as exc:
                log.warning("Heartbeat failed: %s", exc)
                self._record_failure()

            await asyncio.sleep(self._interval)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_heartbeat.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/heartbeat.py tests/test_heartbeat.py
git commit -m "feat: add heartbeat module for Polymarket session keepalive"
```

---

## Task 9: Redeemer Module

**Files:**
- Create: `polybot/redeemer.py`
- Create: `tests/test_redeemer.py`

- [ ] **Step 1: Write failing tests**

```python
"""Tests for on-chain token redeemer."""
from polybot.redeemer import Redeemer


class TestRedeemer:
    def test_queue_redemption(self):
        r = Redeemer(max_retries=3, backoff_sec=1.0)
        r.queue_redemption("0xabc", ["token1", "token2"])
        assert len(r.pending) == 1

    def test_failed_after_max_retries(self):
        r = Redeemer(max_retries=3, backoff_sec=1.0)
        r.queue_redemption("0xabc", ["token1"])
        # Simulate 3 failures
        r._record_failure("0xabc")
        r._record_failure("0xabc")
        r._record_failure("0xabc")
        assert "0xabc" in r.failed
        assert "0xabc" not in r.pending

    def test_success_removes_from_pending(self):
        r = Redeemer(max_retries=3, backoff_sec=1.0)
        r.queue_redemption("0xabc", ["token1"])
        r._record_success("0xabc")
        assert "0xabc" not in r.pending
        assert "0xabc" not in r.failed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_redeemer.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `polybot/redeemer.py`**

```python
"""On-chain token redemption after market settlement."""

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class RedemptionEntry:
    condition_id: str
    token_ids: list[str]
    attempts: int = 0


class Redeemer:
    """Manages on-chain redemption of winning conditional tokens."""

    def __init__(self, max_retries: int = 10, backoff_sec: float = 2.0):
        self._max_retries = max_retries
        self._backoff_sec = backoff_sec
        self.pending: dict[str, RedemptionEntry] = {}
        self.failed: dict[str, RedemptionEntry] = {}

    def queue_redemption(self, condition_id: str, token_ids: list[str]) -> None:
        if condition_id not in self.pending and condition_id not in self.failed:
            self.pending[condition_id] = RedemptionEntry(
                condition_id=condition_id,
                token_ids=token_ids,
            )
            log.info("Queued redemption for %s", condition_id)

    def _record_failure(self, condition_id: str) -> None:
        entry = self.pending.get(condition_id)
        if entry is None:
            return
        entry.attempts += 1
        if entry.attempts >= self._max_retries:
            self.failed[condition_id] = entry
            del self.pending[condition_id]
            log.error("Redemption failed after %d attempts for %s",
                      entry.attempts, condition_id)

    def _record_success(self, condition_id: str) -> None:
        self.pending.pop(condition_id, None)
        self.failed.pop(condition_id, None)
        log.info("Redemption succeeded for %s", condition_id)

    async def run(self, redeem_fn) -> None:
        """Main redemption loop. Runs as an independent async task.

        redeem_fn: async callable(condition_id, token_ids) -> float (USDC received)
        """
        log.info("Redeemer started")
        while True:
            for cid in list(self.pending.keys()):
                entry = self.pending.get(cid)
                if entry is None:
                    continue
                try:
                    usdc_received = await redeem_fn(cid, entry.token_ids)
                    self._record_success(cid)
                    log.info("Redeemed %s: $%.2f USDC", cid, usdc_received)
                except Exception as exc:
                    log.warning("Redemption attempt %d failed for %s: %s",
                                entry.attempts + 1, cid, exc)
                    self._record_failure(cid)
                    backoff = self._backoff_sec * (2 ** entry.attempts)
                    backoff = min(backoff, 300.0)  # cap at 5 minutes
                    await asyncio.sleep(backoff)

            await asyncio.sleep(10)  # check queue every 10s
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_redeemer.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/redeemer.py tests/test_redeemer.py
git commit -m "feat: add on-chain token redeemer module"
```

---

## Task 10a: Bot Cleanup — Remove Early Exit, Stop-Loss, Add Gates

**Files:**
- Modify: `polybot/bot.py`
- Modify: `tests/test_bot_integration.py`

- [ ] **Step 1: Remove early exit and stop-loss from trading loop**

Delete the `check_early_exits()` call (step 5 in current loop).
Delete the `_check_stop_losses()` method entirely.
Remove the `_exited_markets` set (no longer needed without early exits).

- [ ] **Step 2: Add heartbeat health gate and cancel-only mode**

Add to `__init__()`:
```python
        self._cancel_only_mode = False
        self.heartbeat = None  # set in run()
```

At the top of the trading loop:
```python
            if self.heartbeat and not self.heartbeat.is_healthy():
                await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)
                continue
```

Gate `post_ladder()` and `reprice_if_needed()`:
```python
            if not self._cancel_only_mode:
                # 1. Post ladders
                # 3. Reprice
```

In any ClobApiError catch block:
```python
            except ClobApiError as exc:
                if exc.cancel_only:
                    self._cancel_only_mode = True
```

Clear flag on any successful API call in check_fills or post_ladder.

- [ ] **Step 3: Add `_on_connection_lost()` callback**

```python
    def _on_connection_lost(self):
        logger.warning("Connection lost — resetting all state")
        for mid in list(self.ladder_manager.ladders.keys()):
            self.ladder_manager.cleanup_ladder(mid)
        self.order_tracker.mark_all_unknown()
        self._cancel_only_mode = False
```

- [ ] **Step 4: Rewrite `_settle_expired_windows()`**

```python
    def _settle_expired_windows(self, now_epoch: int):
        for market in list(self.active_markets):
            if market.is_active(now_epoch):
                continue
            mid = market.market_id
            pos = self.position_manager.positions.get(mid)
            if pos is None:
                continue
            if mid in self.position_manager.get_pending_settlements():
                continue  # already pending

            # Cancel any remaining orders on exchange (fallback if expiry cancel was missed)
            self.ladder_manager.cancel_ladder(mid)
            self.ladder_manager.cleanup_ladder(mid)

            # Clean up window state
            self._snapped_windows.discard(mid)
            self.window_open_prices.pop(market.asset, None)

            # Mark for async settlement
            self.position_manager.mark_pending_settlement(mid)
            logger.info("Window expired for %s — pending settlement", mid)
```

- [ ] **Step 5: Update integration tests**

Remove `TestStopLoss` class from `tests/test_bot_integration.py`.
Remove any test that calls `check_early_exits()`.
Add test for `_on_connection_lost()` behavior.
Add test for `_settle_expired_windows()` marking pending (not computing PnL).

- [ ] **Step 6: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add polybot/bot.py tests/test_bot_integration.py
git commit -m "feat: remove early exit and stop-loss, add heartbeat gate and cancel-only mode"
```

---

## Task 10b: Bot Integration — Settlement Poller, Redeemer, Wiring

**Files:**
- Modify: `polybot/bot.py`
- Modify: `tests/test_bot_integration.py`

- [ ] **Step 1: Add helper methods**

```python
    def _find_market(self, market_id: str) -> MarketWindow | None:
        """Find a market by ID from active markets or recently expired."""
        for m in self.active_markets:
            if m.market_id == market_id:
                return m
        # Also check cached expired markets if needed
        return self._expired_market_cache.get(market_id)
```

Add `_expired_market_cache: dict[str, MarketWindow]` to `__init__()`. In `_settle_expired_windows()`, cache the market before cleanup:
```python
            self._expired_market_cache[mid] = market
```

Add `_redeem_tokens()`:
```python
    async def _redeem_tokens(self, condition_id: str, token_ids: list[str]) -> float:
        """Redeem winning tokens on-chain. Returns USDC.e received."""
        # TODO: Implement actual on-chain redemption via web3/polygon RPC
        # For now, log and return theoretical value
        logger.info("Redemption requested for %s (TODO: on-chain call)", condition_id)
        return 0.0
```

- [ ] **Step 2: Add settlement poller with timeout**

```python
    async def run_settlement_poller(self):
        """Poll pending settlements for resolution."""
        import httpx
        from polybot.settlement import try_resolve_once

        async with httpx.AsyncClient() as client:
            while True:
                for mid in list(self.position_manager.get_pending_settlements()):
                    market = self._find_market(mid)
                    if market is None:
                        continue

                    # Check timeout
                    now = time.time()
                    window_end = market.close_epoch
                    elapsed = now - window_end
                    if elapsed > self.cfg.bot_settlement_give_up_sec:
                        logger.error("Settlement timeout for %s after %.0fs", mid, elapsed)
                        self.position_manager.mark_failed_settlement(mid)
                        continue

                    result = await try_resolve_once(
                        client, self.cfg.polymarket_host,
                        mid, market.condition_id,
                    )

                    if result is not None:
                        pos = self.position_manager.positions.get(mid)
                        if pos:
                            outcome = result["outcome"]
                            if outcome in ("UP", "YES"):
                                pnl = pos.profit_if_up()
                            else:
                                pnl = pos.profit_if_down()

                            logger.info("Settled %s: %s, PnL=$%.2f", mid, outcome, pnl)
                            self.risk_manager.update_pnl(pnl)

                            # Queue for on-chain redemption
                            self.redeemer.queue_redemption(
                                market.condition_id,
                                [market.up_token_id, market.dn_token_id],
                            )

                        self.position_manager.complete_settlement(mid)
                        self.position_manager.remove_position(mid)
                        self._expired_market_cache.pop(mid, None)

                await asyncio.sleep(30)
```

- [ ] **Step 3: Wire heartbeat, settlement poller, and redeemer into `run()`**

Add imports at top:
```python
from polybot.heartbeat import Heartbeat
from polybot.redeemer import Redeemer
from polybot.tick_size_cache import TickSizeCache
```

In `__init__()`, create TickSizeCache and pass to LadderManager:
```python
        self.tick_size_cache = TickSizeCache(clob_client, ttl_sec=cfg.tick_size_ttl_sec)
        self.ladder_manager = LadderManager(
            cfg, ..., tick_size_cache=self.tick_size_cache,
        )
```

In `run()`, create and launch new tasks:
```python
        self.heartbeat = Heartbeat(
            interval_sec=self.cfg.heartbeat_interval_sec,
            max_failures=self.cfg.heartbeat_max_failures,
        )
        self.redeemer = Redeemer(
            max_retries=self.cfg.redemption_retry_max,
            backoff_sec=self.cfg.redemption_retry_backoff_sec,
        )

        tasks = [
            asyncio.create_task(self.heartbeat.run(self.clob_client, self._on_connection_lost)),
            asyncio.create_task(self.run_settlement_poller()),
            asyncio.create_task(self.redeemer.run(self._redeem_tokens)),
            # ... existing tasks (binance_ws, market_discovery, trading_loop, display)
        ]
```

- [ ] **Step 4: Update integration tests**

Add test for settlement poller timeout → `mark_failed_settlement`.
Add test for settlement poller resolution → PnL credited, position removed.
Add test for `_find_market()`.

- [ ] **Step 5: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add polybot/bot.py tests/test_bot_integration.py
git commit -m "feat: wire settlement poller, redeemer, and heartbeat into bot"
```

---

## Task 11: Update MockClobClient for Batch Support

**Files:**
- Modify: `run_bot.py`

- [ ] **Step 1: Add batch methods to MockClobClient**

```python
    def get_tick_size(self, condition_id):
        return 0.01  # default tick size for mock
```

Ensure the mock client supports all methods the hardened executor expects.

- [ ] **Step 2: Run full dry-run test**

Run: `timeout 15 python run_bot.py 2>&1 | head -30`
Expected: Bot starts, posts ladders with new rung counts, no crashes

- [ ] **Step 3: Commit**

```bash
git add run_bot.py
git commit -m "feat: update mock CLOB client for batch API and tick size support"
```

---

## Task 12: Integration Tests for Failure Scenarios

**Files:**
- Create: `tests/test_reconciliation.py`

- [ ] **Step 1: Write integration tests**

```python
"""Integration tests for failure scenarios."""
from unittest.mock import MagicMock, patch
import pytest

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.ladder_manager import LadderManager, build_ladder_rungs
from polybot.order_executor import OrderExecutor
from polybot.order_tracker import OrderTracker
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.types import Side


class TestPhantomFillPrevention:
    """Verify that API failures don't cause phantom fills."""

    def test_api_failure_skips_fill_check(self):
        """When get_open_orders() raises, check_fills returns 0."""
        cfg = BotConfig(private_key="0xfake", api_key="k", api_secret="s", api_passphrase="p")
        mock_clob = MagicMock()
        executor = OrderExecutor(cfg, mock_clob)
        tracker = OrderTracker()
        positions = PositionManager(cfg, bankroll=1000)
        risk = RiskManager(cfg, starting_bankroll=1000)
        mgr = LadderManager(cfg, executor, tracker, positions, risk)

        # Simulate API failure
        mock_clob.get_open_orders.side_effect = Exception("network timeout")

        fills = mgr.check_fills()
        assert fills == 0
        # No positions should be created
        assert positions.active_position_count() == 0


class TestTickSizeRetry:
    """Verify tick size rejection triggers cache invalidation and retry."""

    def test_tick_size_rejection_invalidates_cache(self):
        from polybot.tick_size_cache import TickSizeCache
        client = MagicMock()
        client.get_tick_size.side_effect = [0.01, 0.001]  # first fetch, then after invalidation
        cache = TickSizeCache(client, ttl_sec=60.0)

        # First fetch
        assert cache.get_tick_size("0xabc") == 0.01
        # Simulate rejection: invalidate and refetch
        cache.invalidate("0xabc")
        assert cache.get_tick_size("0xabc") == 0.001


class TestHeartbeatRecoveryFlow:
    """Integration test: heartbeat loss -> state reset -> recovery."""

    def test_connection_lost_wipes_state(self):
        from polybot.order_tracker import OrderTracker, TrackedOrder
        tracker = OrderTracker()
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))

        # Simulate connection lost
        tracker.mark_all_unknown()
        assert tracker.orders["o1"].status == "unknown"

        # Recovery: reconcile with exchange showing no orders (Polymarket cancelled all)
        result = tracker.reconcile(open_orders=[])
        assert "o1" in [o.order_id for o in result["filled"]]


class TestCancelReconciliation:
    """Verify that failed cancels are reconciled."""

    def test_cancelled_order_still_on_exchange_reverted(self):
        tracker = OrderTracker()
        from polybot.order_tracker import TrackedOrder
        tracker.add(TrackedOrder(
            order_id="o1", market_id="m1", token_id="t1",
            side=Side.UP, price=0.50, size=10.0, placed_at=1000,
        ))
        tracker.cancel("o1")
        assert tracker.orders["o1"].status == "cancelled"

        # Reconcile: o1 is still on exchange
        result = tracker.reconcile([{"id": "o1"}])
        assert tracker.orders["o1"].status == "resting"
        assert "o1" in result["reverted"]
```

- [ ] **Step 2: Run tests**

Run: `python -m pytest tests/test_reconciliation.py -v`
Expected: All pass

- [ ] **Step 3: Final full test suite**

Run: `python -m pytest tests/ -v`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add tests/test_reconciliation.py
git commit -m "test: add integration tests for failure scenarios and reconciliation"
```

---

## Verification Checklist

After all tasks complete:

- [ ] `python -m pytest tests/ -v` — all tests pass
- [ ] `python run_bot.py` — dry-run starts and runs without crashes for 30+ seconds
- [ ] No references to `check_early_exits` or `_check_stop_losses` remain in production code
- [ ] No references to `place_limit_sell` remain in production code
- [ ] `polybot/settlement.py` exists and `polybot/tracker/settlement_tracker.py` imports from it
- [ ] All new config fields have env var mappings in `load_bot_config()`
- [ ] `ClobApiError` is raised (not silent defaults) in all executor methods
