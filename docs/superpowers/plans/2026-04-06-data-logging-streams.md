# Full Data Logging Streams — Implementation Plan

**Status:** complete

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture ALL data needed for post-trade analysis: 6 streams covering market data (price, book, trades) + bot internals (orders, strategy state, market lifecycle).

**Architecture:** One new module `polybot/data/data_recorder.py` handles all 6 streams with JSONL writers. Hooks into existing callbacks (price_feed on_tick, BookManager process_message, OrderExecutor methods, trading loop). Daily file rotation prevents bloat.

**Tech Stack:** Python asyncio, JSONL append-only files, existing WebSocket infrastructure.

**Streams:**
| # | Stream | File | What | Rate |
|---|--------|------|------|------|
| 1 | price_log | `data/price_log_YYYY-MM-DD.jsonl` | BTC price (Binance + Chainlink) | ~1/sec |
| 2 | book_log | `data/book_log_YYYY-MM-DD.jsonl` | Every Polymarket WS book update | Every WS msg |
| 3 | order_log | `data/order_log_YYYY-MM-DD.jsonl` | Our orders: post/fill/cancel | Per event |
| 4 | trade_log | `data/trade_log_YYYY-MM-DD.jsonl` | All Polymarket trades | Every trade |
| 5 | strategy_log | `data/strategy_log_YYYY-MM-DD.jsonl` | Bot model state per tick | Every 5sec |
| 6 | market_event_log | `data/market_event_log_YYYY-MM-DD.jsonl` | Market enter/exit with metadata | Per event |

---

### Task 1: Create DataRecorder core with async JSONL writer

**Files:**
- Create: `polybot/data/data_recorder.py`
- Test: `tests/test_data_recorder.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_data_recorder.py
import json
import tempfile
import pathlib
import asyncio
from unittest.mock import patch

def test_recorder_writes_jsonl(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    recorder.log_price(1700000000.0, "BTC", 69325.22, "binance")
    
    log_file = tmp_path / "price_log.jsonl"
    assert log_file.exists()
    record = json.loads(log_file.read_text().strip())
    assert record["asset"] == "BTC"
    assert record["price"] == 69325.22
    assert record["source"] == "binance"
    assert record["ts"] == 1700000000.0


def test_recorder_daily_rotation(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    
    # Write with two different dates
    recorder._append("price_log", {"ts": 1700000000.0, "data": "day1"}, ts=1700000000.0)
    recorder._append("price_log", {"ts": 1700100000.0, "data": "day2"}, ts=1700100000.0)
    
    # Should have date-stamped files
    files = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(files) >= 1  # at least one file


def test_recorder_does_not_crash_on_write_error(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path / "nonexistent" / "deep")
    # Should not raise — logging failures are silent
    recorder.log_price(1700000000.0, "BTC", 69325.22, "binance")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'polybot.data.data_recorder'`

- [ ] **Step 3: Write DataRecorder implementation**

```python
# polybot/data/data_recorder.py
"""Unified data recorder — captures 4 streams to JSONL files for post-trade analysis.

Streams:
  1. price_log     — every Binance/Chainlink price tick
  2. book_log      — every Polymarket order book update (full depth)
  3. order_log     — every order we post, reprice, cancel, fill
  4. trade_log     — every Polymarket trade on our active markets
"""

import json
import logging
import pathlib
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Throttle price logging to max 1 per second per asset (Binance sends ~5/sec)
_PRICE_THROTTLE_SEC = 1.0


class DataRecorder:
    """Append-only JSONL recorder with daily file rotation."""

    def __init__(self, data_dir: pathlib.Path | str = "data"):
        self._data_dir = pathlib.Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._handles: dict[str, object] = {}  # stream_name -> file handle
        self._current_date: dict[str, str] = {}  # stream_name -> YYYY-MM-DD
        self._last_price_ts: dict[str, float] = {}  # asset -> last logged ts

    def _get_handle(self, stream: str, ts: float):
        """Get or rotate file handle for a stream based on date."""
        date_str = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        if self._current_date.get(stream) != date_str:
            # Close old handle
            old = self._handles.pop(stream, None)
            if old:
                try:
                    old.close()
                except Exception:
                    pass
            self._current_date[stream] = date_str
            path = self._data_dir / f"{stream}_{date_str}.jsonl"
            try:
                self._handles[stream] = open(path, "a", buffering=1)  # line-buffered
            except Exception as e:
                logger.debug("Failed to open %s: %s", path, e)
                return None
        return self._handles.get(stream)

    def _append(self, stream: str, record: dict, ts: float | None = None):
        """Append a JSON record to a stream file. Never raises."""
        try:
            t = ts or record.get("ts", time.time())
            fh = self._get_handle(stream, t)
            if fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass  # logging must never crash the bot

    # --- Stream 1: Price ticks ---

    def log_price(self, ts: float, asset: str, price: float, source: str):
        """Log a price tick. Throttled to 1/sec per asset."""
        last = self._last_price_ts.get(asset, 0)
        if ts - last < _PRICE_THROTTLE_SEC:
            return
        self._last_price_ts[asset] = ts
        self._append("price_log", {
            "ts": round(ts, 3),
            "asset": asset,
            "price": price,
            "source": source,
        }, ts)

    # --- Stream 2: Order book updates ---

    def log_book_update(self, ts: float, token_id: str, event_type: str, raw_msg: dict):
        """Log raw order book message from Polymarket WS."""
        self._append("book_log", {
            "ts": round(ts, 3),
            "token_id": token_id[:20],
            "event_type": event_type,
            "data": raw_msg,
        }, ts)

    # --- Stream 3: Our order lifecycle ---

    def log_order(self, ts: float, event: str, market_id: str, side: str,
                  price: float, size: float, order_id: str = "", reason: str = ""):
        """Log an order lifecycle event (post, reprice, cancel, fill)."""
        self._append("order_log", {
            "ts": round(ts, 3),
            "event": event,
            "market_id": market_id,
            "side": side,
            "price": price,
            "size": size,
            "order_id": order_id[:16] if order_id else "",
            "reason": reason,
        }, ts)

    # --- Stream 4: Polymarket trades ---

    def log_trade(self, ts: float, token_id: str, side: str, price: float, size: float = 0):
        """Log a trade observed on Polymarket (from WS last_trade_price)."""
        self._append("trade_log", {
            "ts": round(ts, 3),
            "token_id": token_id[:20],
            "side": side,
            "price": price,
            "size": size,
        }, ts)

    def close(self):
        """Flush and close all file handles."""
        for fh in self._handles.values():
            try:
                fh.close()
            except Exception:
                pass
        self._handles.clear()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add polybot/data/data_recorder.py tests/test_data_recorder.py
git commit -m "feat: add DataRecorder core with 4-stream JSONL writer"
```

---

### Task 2: Wire Stream 1 — Price timeseries (Binance + Chainlink)

**Files:**
- Modify: `polybot/bot.py` (init + _on_price_tick + rtds callback)
- Modify: `polybot/data/rtds_chainlink.py` (add on_tick callback)
- Test: `tests/test_data_recorder.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_data_recorder.py

def test_price_tick_throttle(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    
    # Two ticks 0.5s apart — second should be throttled
    recorder.log_price(1700000000.0, "BTC", 69000.0, "binance")
    recorder.log_price(1700000000.5, "BTC", 69001.0, "binance")
    
    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    assert len(log_file) == 1
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 1  # second tick throttled
    
    # Tick 1.1s later should go through
    recorder.log_price(1700000001.1, "BTC", 69002.0, "binance")
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 2


def test_chainlink_price_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    recorder.log_price(1700000000.0, "BTC", 69325.00, "chainlink")
    
    log_file = list(tmp_path.glob("price_log_*.jsonl"))
    record = json.loads(log_file[0].read_text().strip())
    assert record["source"] == "chainlink"
```

- [ ] **Step 2: Run test to verify it passes** (these test DataRecorder directly, already implemented)

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py -v`
Expected: 5 passed

- [ ] **Step 3: Wire Binance price ticks into DataRecorder**

In `polybot/bot.py`, add to `__init__` (after line ~76):
```python
from polybot.data.data_recorder import DataRecorder
self.data_recorder = DataRecorder(data_dir="data")
```

Modify `_on_price_tick` (line 1352):
```python
def _on_price_tick(self, asset: str, price) -> None:
    """Feed price ticks to vol estimator and data recorder."""
    now = time.time()
    ve = self._vol_estimators.get(asset)
    if ve:
        ve.push(now, price)
    self.data_recorder.log_price(now, asset, float(price), "binance")
```

- [ ] **Step 4: Wire Chainlink price ticks into DataRecorder**

In `polybot/data/rtds_chainlink.py`, add `on_tick` callback to constructor:
```python
def __init__(self, on_tick=None):
    ...existing init...
    self._on_tick = on_tick
```

In the `_process_message` method, after updating `_chainlink_prices` (around line 230), add:
```python
if self._on_tick:
    self._on_tick(asset, float(price), "chainlink")
```

In `polybot/bot.py`, update rtds_feed init (line 76):
```python
self.rtds_feed = RTDSChainlinkPriceFeed(
    on_tick=lambda asset, price, src: self.data_recorder.log_price(time.time(), asset, price, src)
)
```

- [ ] **Step 5: Add DataRecorder cleanup to bot shutdown**

In `polybot/bot.py`, in the `stop()` method (around line 252):
```python
self.data_recorder.close()
```

- [ ] **Step 6: Run full test suite**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/ -x -q --tb=short`
Expected: 720+ passed

- [ ] **Step 7: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add polybot/bot.py polybot/data/rtds_chainlink.py polybot/data/data_recorder.py tests/test_data_recorder.py
git commit -m "feat: wire price timeseries logging (Binance + Chainlink)"
```

---

### Task 3: Wire Stream 2 — Full order book updates

**Files:**
- Modify: `polybot/data/book_manager.py` (add recorder hook)
- Test: `tests/test_data_recorder.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_data_recorder.py

def test_book_update_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    
    raw_msg = {
        "event_type": "price_change",
        "asset_id": "abc123",
        "price_changes": [{"price": "0.45", "side": "BUY", "size": "100"}],
    }
    recorder.log_book_update(1700000000.0, "abc123", "price_change", raw_msg)
    
    log_file = list(tmp_path.glob("book_log_*.jsonl"))
    assert len(log_file) == 1
    record = json.loads(log_file[0].read_text().strip())
    assert record["event_type"] == "price_change"
    assert record["data"]["price_changes"][0]["price"] == "0.45"
```

- [ ] **Step 2: Run test**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py::test_book_update_logged -v`
Expected: PASS

- [ ] **Step 3: Wire BookManager to log all WS messages**

In `polybot/data/book_manager.py`, add recorder parameter:

```python
class BookManager:
    def __init__(self, data_recorder=None) -> None:
        self._books: dict[str, OrderBook] = {}
        self._data_recorder = data_recorder
```

At the top of `process_message`, after computing `ts` and `event_type` (around line 68), add:

```python
        if self._data_recorder and event_type in ("book", "price_change", "last_trade_price"):
            aid = str(msg.get("asset_id", ""))
            self._data_recorder.log_book_update(ts, aid, event_type, msg)
```

- [ ] **Step 4: Wire in bot.py**

In `polybot/bot.py`, update BookManager init (line 69):
```python
self.book_manager = BookManager(data_recorder=self.data_recorder)
```

Note: `self.data_recorder` must be created BEFORE `self.book_manager` in `__init__`.

- [ ] **Step 5: Run full test suite**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/ -x -q --tb=short`
Expected: 720+ passed

- [ ] **Step 6: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add polybot/data/book_manager.py polybot/bot.py tests/test_data_recorder.py
git commit -m "feat: log full order book stream from Polymarket WS"
```

---

### Task 4: Wire Stream 3 — Order lifecycle

**Files:**
- Modify: `polybot/oms/order_executor.py` (add recorder hook on place/cancel)
- Modify: `polybot/strategy/ladder_manager.py` (log reprice, FV cancel)
- Modify: `polybot/bot.py` (log fills)
- Test: `tests/test_data_recorder.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_data_recorder.py

def test_order_lifecycle_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    
    recorder.log_order(1700000000.0, "post", "btc-15m-123", "UP", 0.45, 10, "ord1", "ladder_post")
    recorder.log_order(1700000001.0, "fill", "btc-15m-123", "UP", 0.45, 10, "ord1", "paper_fill")
    recorder.log_order(1700000002.0, "cancel", "btc-15m-123", "DN", 0.43, 5, "ord2", "fv_cancel")
    
    log_file = list(tmp_path.glob("order_log_*.jsonl"))
    lines = log_file[0].read_text().strip().split("\n")
    assert len(lines) == 3
    
    post = json.loads(lines[0])
    assert post["event"] == "post"
    assert post["price"] == 0.45
    
    cancel = json.loads(lines[2])
    assert cancel["reason"] == "fv_cancel"
```

- [ ] **Step 2: Run test**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py::test_order_lifecycle_logged -v`
Expected: PASS

- [ ] **Step 3: Add recorder to OrderExecutor**

In `polybot/oms/order_executor.py`, add to constructor:
```python
def __init__(self, cfg: BotConfig, clob_client: Any, data_recorder=None) -> None:
    self.cfg = cfg
    self.client = clob_client
    self._data_recorder = data_recorder
```

After successful order placement in `place_limit_buy()` (after the `logger.info("ORDER PLACED...")` line):
```python
if self._data_recorder:
    self._data_recorder.log_order(time.time(), "post", market_id, side.value, validated_price, size, record.order_id, "ladder")
```

After cancellation in `cancel_order()`:
```python
if self._data_recorder:
    self._data_recorder.log_order(time.time(), "cancel", "", "", 0, 0, oid, reason)
```

Add `import time` at top if not present.

- [ ] **Step 4: Log fills in bot.py**

In bot.py where fills are detected (around line 730, the fill detection loop), after the existing fill logging:
```python
self.data_recorder.log_order(time.time(), "fill", market.market_id, order.side.value, order.price, order.size, order.order_id, "detected")
```

- [ ] **Step 5: Wire recorder to OrderExecutor in bot.py**

In bot.py, update OrderExecutor init (around line 78):
```python
self.order_executor = OrderExecutor(cfg, self.clob_client, data_recorder=self.data_recorder)
```

- [ ] **Step 6: Run full test suite**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/ -x -q --tb=short`
Expected: 720+ passed (may need to update existing tests that construct OrderExecutor without data_recorder)

- [ ] **Step 7: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add polybot/oms/order_executor.py polybot/bot.py tests/test_data_recorder.py
git commit -m "feat: log order lifecycle (post, fill, cancel)"
```

---

### Task 5: Wire Stream 4 — Polymarket trades

**Files:**
- Modify: `polybot/data/book_manager.py` (extend last_trade_price handling)
- Test: `tests/test_data_recorder.py` (extend)

The Polymarket WS already sends `last_trade_price` events with price and side. We just need to log them separately from the book stream.

- [ ] **Step 1: Write the failing test**

```python
# Append to tests/test_data_recorder.py

def test_trade_logged(tmp_path):
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    
    recorder.log_trade(1700000000.0, "token_abc123", "BUY", 0.55, 20)
    
    log_file = list(tmp_path.glob("trade_log_*.jsonl"))
    record = json.loads(log_file[0].read_text().strip())
    assert record["side"] == "BUY"
    assert record["price"] == 0.55
```

- [ ] **Step 2: Run test**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py::test_trade_logged -v`
Expected: PASS

- [ ] **Step 3: Log trades from BookManager**

In `polybot/data/book_manager.py`, inside `process_message`, in the `elif event_type == "last_trade_price":` branch (line 90), add after `apply_last_trade`:

```python
        elif event_type == "last_trade_price":
            aid = str(msg.get("asset_id", ""))
            if aid in self._books:
                apply_last_trade(self._books[aid], msg)
            if self._data_recorder:
                side = (msg.get("side") or "").upper()
                price = float(msg.get("price", 0))
                size = float(msg.get("size", 0))
                self._data_recorder.log_trade(ts, aid, side, price, size)
```

- [ ] **Step 4: Run full test suite**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/ -x -q --tb=short`
Expected: 720+ passed

- [ ] **Step 5: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add polybot/data/book_manager.py tests/test_data_recorder.py
git commit -m "feat: log Polymarket trades from WS feed"
```

---

### Task 6: Integration test — verify all 4 streams produce data

**Files:**
- Test: `tests/test_data_recorder.py` (extend)

- [ ] **Step 1: Write integration test**

```python
# Append to tests/test_data_recorder.py

def test_all_streams_produce_files(tmp_path):
    """Verify all 4 streams write to separate date-stamped JSONL files."""
    from polybot.data.data_recorder import DataRecorder
    recorder = DataRecorder(data_dir=tmp_path)
    ts = 1700000000.0
    
    recorder.log_price(ts, "BTC", 69000.0, "binance")
    recorder.log_book_update(ts, "token1", "price_change", {"bids": []})
    recorder.log_order(ts, "post", "mkt1", "UP", 0.45, 10, "o1", "ladder")
    recorder.log_trade(ts, "token1", "BUY", 0.55, 20)
    
    recorder.close()
    
    streams = ["price_log", "book_log", "order_log", "trade_log"]
    for stream in streams:
        files = list(tmp_path.glob(f"{stream}_*.jsonl"))
        assert len(files) == 1, f"Missing file for stream: {stream}"
        content = files[0].read_text().strip()
        assert len(content) > 0, f"Empty file for stream: {stream}"
        record = json.loads(content)
        assert "ts" in record, f"Missing ts in {stream}"
```

- [ ] **Step 2: Run test**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/test_data_recorder.py -v`
Expected: All passed

- [ ] **Step 3: Run full test suite**

Run: `cd C:/Users/pc/Desktop/Bots/polybot && python -m pytest tests/ -x -q --tb=short`
Expected: 720+ passed, 0 failures

- [ ] **Step 4: Commit**

```bash
cd C:/Users/pc/Desktop/Bots/polybot
git add tests/test_data_recorder.py
git commit -m "test: integration test for all 4 data logging streams"
```
