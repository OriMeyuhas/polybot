# PolyBot Infrastructure Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild PolyBot's infrastructure (data feeds, OMS, web UI, orchestrator) using polytrader's proven patterns while keeping the ladder market maker strategy unchanged.

**Architecture:** Strategy code (ladder_manager, position_manager, order_tracker) moves to `polybot/strategy/` untouched. New data layer in `polybot/data/` ports polytrader's price feeds, order books, and market discovery. OMS in `polybot/oms/` provides paper/live client wrapper. Web UI in `polybot/web/` ports polytrader's dashboard adapted for ladder MM. Bot orchestrator in `polybot/bot.py` ties everything together.

**Tech Stack:** Python 3.11+, asyncio, aiohttp, websockets, httpx, py-clob-client>=0.34.0

**Spec:** `docs/superpowers/specs/2026-03-19-polybot-infrastructure-rebuild-design.md`

**Reference project:** `/c/Users/pc/Desktop/polytrader/` — new modules are **designed for PolyBot's needs, inspired by polytrader's patterns**. They are NOT direct ports — interfaces are adapted for PolyBot's synchronous strategy layer and simpler architecture. When implementing, reference polytrader for the general approach but follow the interfaces defined in this plan.

---

## Task 1: Project Restructure & Foundation

**Files:**
- Create: `polybot/data/__init__.py`
- Create: `polybot/strategy/__init__.py`
- Create: `polybot/oms/__init__.py`
- Create: `polybot/utils/__init__.py`
- Modify: `polybot/__init__.py` (add re-exports)
- Create: `polybot/strategy/risk_stub.py`
- Create: `tests/test_risk_stub.py`

**Context:** Move existing strategy files into `polybot/strategy/`, create package structure, add backward-compatible re-exports in `polybot/__init__.py`. The risk stub is needed before any strategy integration.

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p polybot/data polybot/strategy polybot/oms polybot/utils
```

- [ ] **Step 2: Move strategy files to polybot/strategy/**

```bash
# Move strategy files (keep originals temporarily for import compat)
cp polybot/ladder_manager.py polybot/strategy/ladder_manager.py
cp polybot/position_manager.py polybot/strategy/position_manager.py
cp polybot/order_tracker.py polybot/strategy/order_tracker.py
```

- [ ] **Step 3: Create package __init__.py files**

Create `polybot/data/__init__.py`:
```python
"""Real-time data layer — price feeds, order books, market discovery."""
```

Create `polybot/strategy/__init__.py`:
```python
"""Ladder market maker strategy — kept unchanged from original PolyBot."""
from polybot.strategy.ladder_manager import LadderManager, LadderState, build_ladder_rungs
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.order_tracker import OrderTracker, TrackedOrder
```

Create `polybot/oms/__init__.py`:
```python
"""Order management system — client wrapper, executor, heartbeat."""
```

Create `polybot/utils/__init__.py`:
```python
"""Shared utilities."""
```

- [ ] **Step 4: Update polybot/__init__.py with backward-compatible re-exports**

```python
"""PolyBot — Passive limit order ladder market maker for Polymarket."""

# Backward-compatible re-exports so existing imports still work
from polybot.strategy.ladder_manager import LadderManager, LadderState, build_ladder_rungs
from polybot.strategy.position_manager import PositionManager
from polybot.strategy.order_tracker import OrderTracker, TrackedOrder

__all__ = [
    "LadderManager", "LadderState", "build_ladder_rungs",
    "PositionManager",
    "OrderTracker", "TrackedOrder",
]
```

- [ ] **Step 5: Write risk stub test**

Create `tests/test_risk_stub.py`:
```python
from polybot.strategy.risk_stub import RiskStub


def test_is_halted_always_false():
    stub = RiskStub()
    assert stub.is_halted() is False


def test_can_open_position_always_true():
    stub = RiskStub()
    assert stub.can_open_position(0) is True
    assert stub.can_open_position(100) is True


def test_can_trade_in_window_always_true():
    stub = RiskStub()
    assert stub.can_trade_in_window(None, 0) is True


def test_update_pnl_noop():
    stub = RiskStub()
    stub.update_pnl(100.0)  # Should not raise


def test_reset_daily_noop():
    stub = RiskStub()
    stub.reset_daily()  # Should not raise
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_risk_stub.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.strategy.risk_stub'`

- [ ] **Step 7: Implement risk stub**

Create `polybot/strategy/risk_stub.py`:
```python
"""No-op RiskManager stub — always allows trading.

Drop-in replacement for polybot.risk_manager.RiskManager.
Swap for the real implementation when risk management is added.
"""


class RiskStub:
    """Satisfies the RiskManager interface with no-op implementations."""

    def is_halted(self) -> bool:
        return False

    def can_open_position(self, current_count: int) -> bool:
        return True

    def can_trade_in_window(self, market, now_epoch: int) -> bool:
        return True

    def update_pnl(self, amount: float) -> None:
        pass

    def reset_daily(self) -> None:
        pass
```

- [ ] **Step 8: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_risk_stub.py -v`
Expected: All 5 tests PASS

- [ ] **Step 9: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/ polybot/strategy/ polybot/oms/ polybot/utils/ polybot/__init__.py tests/test_risk_stub.py
git commit -m "feat: create package structure and risk stub for infrastructure rebuild"
```

---

## Task 2: Update Errors & Config

**Files:**
- Modify: `polybot/errors.py` (already has ClobApiError, verify it's complete)
- Modify: `polybot/config.py` (add data layer config fields)
- Create: `tests/test_config_new_fields.py`

**Context:** Config needs new fields for the data layer (Binance WS, CLOB midpoint polling, market WS, etc.). Errors module already has ClobApiError — verify it covers all needed cases.

- [ ] **Step 1: Write config test for new fields**

Create `tests/test_config_new_fields.py`:
```python
from polybot.config import BotConfig


def test_new_data_layer_defaults():
    cfg = BotConfig()
    assert cfg.binance_ws_url == "wss://stream.binance.com:9443/ws"
    assert cfg.binance_fallback_interval_sec == 2.0
    assert cfg.clob_midpoint_poll_sec == 2.0
    assert cfg.market_ws_ping_sec == 10.0
    assert cfg.book_stale_sec == 30.0
    assert cfg.market_discovery_interval_sec == 60


def test_existing_fields_unchanged():
    cfg = BotConfig()
    assert cfg.ladder_rungs == 36
    assert cfg.ladder_spacing == 0.02
    assert cfg.max_pair_cost == 0.95
    assert cfg.web_port == 8080
    assert cfg.dry_run is True
```

- [ ] **Step 2: Run test to verify what fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_config_new_fields.py -v`
Expected: `test_new_data_layer_defaults` FAILS (fields don't exist yet), `test_existing_fields_unchanged` PASSES

- [ ] **Step 3: Add new fields to BotConfig**

Add these fields to `BotConfig` in `polybot/config.py` (after existing fields, before methods):
```python
    # Data layer config (new for infrastructure rebuild)
    binance_fallback_interval_sec: float = 2.0
    clob_midpoint_poll_sec: float = 2.0
    market_ws_ping_sec: float = 10.0
    book_stale_sec: float = 30.0
    coingecko_ids: tuple = ("bitcoin", "ethereum", "solana", "ripple")
    bankroll: float = 1000.0  # Default paper bankroll; overridable via env
```

Also update `load_bot_config()` to read these from env vars:
```python
    binance_fallback_interval_sec=float(os.getenv("BINANCE_FALLBACK_INTERVAL_SEC", "2.0")),
    clob_midpoint_poll_sec=float(os.getenv("CLOB_MIDPOINT_POLL_SEC", "2.0")),
    market_ws_ping_sec=float(os.getenv("MARKET_WS_PING_SEC", "10.0")),
    book_stale_sec=float(os.getenv("BOOK_STALE_SEC", "30.0")),
    bankroll=float(os.getenv("BANKROLL", "1000.0")),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_config_new_fields.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/config.py tests/test_config_new_fields.py
git commit -m "feat: add data layer config fields for infrastructure rebuild"
```

---

## Task 3: Data Layer — Order Book (`data/book.py`)

**Files:**
- Create: `polybot/data/book.py`
- Create: `tests/test_book.py`

**Context:** Port from `polytrader/polymarket_agent/data/book.py`. Order book state machine with PriceLevel, OrderBook, snapshot/delta application. This is a standalone module with no external dependencies.

- [ ] **Step 1: Write order book tests**

Create `tests/test_book.py`:
```python
import time
from decimal import Decimal
from polybot.data.book import OrderBook, PriceLevel, apply_book_snapshot, apply_price_change


def test_empty_book():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    assert book.best_bid is None
    assert book.best_ask is None
    assert book.mid is None
    assert book.spread is None


def test_apply_snapshot():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
        "asks": [{"price": "0.55", "size": "100"}, {"price": "0.56", "size": "200"}],
    }, ts=now)
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")
    assert book.mid == Decimal("0.50")
    assert book.spread == Decimal("0.10")


def test_apply_price_change_add():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }, ts=now)
    apply_price_change(book, [{"price": "0.46", "size": "50", "side": "BUY"}], ts=now)
    assert book.best_bid == Decimal("0.46")


def test_apply_price_change_remove():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    now = time.time()
    apply_book_snapshot(book, {
        "bids": [{"price": "0.45", "size": "100"}, {"price": "0.44", "size": "200"}],
        "asks": [{"price": "0.55", "size": "100"}],
    }, ts=now)
    apply_price_change(book, [{"price": "0.45", "size": "0", "side": "BUY"}], ts=now)
    assert book.best_bid == Decimal("0.44")


def test_stale_detection():
    book = OrderBook(asset_id="token_abc", market="token_abc")
    book._last_update = time.time() - 60
    assert book.is_stale(30) is True
    book._last_update = time.time()
    assert book.is_stale(30) is False
```

**Interface notes (adapted from polytrader, NOT a direct port):**
- `OrderBook(asset_id: str, market: str)` — requires two positional args
- `apply_book_snapshot(book, data, ts)` — `ts` is a float timestamp
- `apply_price_change(book, changes, ts)` — `ts` is a float timestamp
- Side values in changes are `"BUY"` (bids) and `"SELL"` (asks) — this matches Polymarket WS format

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_book.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement book.py**

Create `polybot/data/book.py` — adapted from polytrader's `book.py`. Key classes:
- `PriceLevel(price: Decimal, size: Decimal)` dataclass
- `OrderBook(asset_id: str, market: str)` with `bids`, `asks` lists, `best_bid`, `best_ask`, `mid`, `spread` properties, `is_stale(threshold_sec)` method, `_last_update` timestamp
- `apply_book_snapshot(book, data, ts)` — replaces full book from WS `book` message, `ts` is float timestamp
- `apply_price_change(book, changes, ts)` — applies delta updates (size="0" removes level), sides are `"BUY"`/`"SELL"` matching Polymarket WS format

Reference: `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/book.py`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_book.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/book.py tests/test_book.py
git commit -m "feat: add order book state machine (data/book.py)"
```

---

## Task 4: Data Layer — Book Manager (`data/book_manager.py`)

**Files:**
- Create: `polybot/data/book_manager.py`
- Create: `tests/test_book_manager.py`

**Context:** Port from polytrader's `book_manager.py`. Routes WebSocket messages to per-token OrderBook objects. Depends on `data/book.py` from Task 3.

- [ ] **Step 1: Write book manager tests**

Create `tests/test_book_manager.py`:
```python
from decimal import Decimal
from polybot.data.book_manager import BookManager


def test_register_and_get_book():
    bm = BookManager()
    bm.update_assets(["token_abc", "token_def"])
    book = bm.get_book("token_abc")
    assert book is not None
    assert book.best_bid is None  # Empty book


def test_process_book_message():
    bm = BookManager()
    bm.update_assets(["token_abc"])
    bm.process_message({
        "event_type": "book",
        "asset_id": "token_abc",
        "market": "token_abc",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
        "timestamp": "1710850000",
    })
    book = bm.get_book("token_abc")
    assert book.best_bid == Decimal("0.45")
    assert book.best_ask == Decimal("0.55")


def test_process_price_change():
    """Uses 'price_changes' key (matching actual Polymarket WS format)."""
    bm = BookManager()
    bm.update_assets(["token_abc"])
    bm.process_message({
        "event_type": "book",
        "asset_id": "token_abc",
        "market": "token_abc",
        "bids": [{"price": "0.45", "size": "100"}],
        "asks": [{"price": "0.55", "size": "100"}],
        "timestamp": "1710850000",
    })
    bm.process_message({
        "event_type": "price_change",
        "asset_id": "token_abc",
        "market": "token_abc",
        "price_changes": [{"price": "0.46", "size": "50", "side": "BUY"}],
        "timestamp": "1710850001",
    })
    assert bm.get_book("token_abc").best_bid == Decimal("0.46")


def test_unknown_asset_ignored():
    bm = BookManager()
    bm.process_message({
        "event_type": "book",
        "asset_id": "unknown",
        "market": "unknown",
        "bids": [],
        "asks": [],
        "timestamp": "1710850000",
    })
    assert bm.get_book("unknown") is None


def test_stale_check():
    bm = BookManager()
    bm.update_assets(["token_abc"])
    assert bm.is_stale("token_abc", 30) is True  # Never updated
```

**Message format notes (matching actual Polymarket WS):**
- Book snapshots: `{"event_type": "book", "asset_id": ..., "bids": [...], "asks": [...]}`
- Price changes: `{"event_type": "price_change", "asset_id": ..., "price_changes": [...]}`  (key is `price_changes` NOT `changes`)
- Side values: `"BUY"` (bids) and `"SELL"` (asks)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_book_manager.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement book_manager.py**

Create `polybot/data/book_manager.py` — adapted from polytrader's `book_manager.py`. Key elements:
- `BookManager` class with `_books: dict[str, OrderBook]`
- `update_assets(token_ids)` — register/unregister books
- `get_book(token_id) -> OrderBook | None`
- `process_message(msg)` — routes by `msg["event_type"]`:
  - `"book"`: calls `apply_book_snapshot(book, msg, ts)`
  - `"price_change"`: reads `msg["price_changes"]` (NOT `msg["changes"]`), calls `apply_price_change(book, changes, ts)`
  - `"tick_size_change"`: updates book tick size
  - `"last_trade_price"`: updates last trade on book
  - `"best_bid_ask"`: direct update to best prices
- `is_stale(token_id, threshold_sec) -> bool`

Reference: `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/book_manager.py`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_book_manager.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/book_manager.py tests/test_book_manager.py
git commit -m "feat: add multi-market book manager (data/book_manager.py)"
```

---

## Task 5: Data Layer — Market WebSocket (`data/market_ws.py`)

**Files:**
- Create: `polybot/data/market_ws.py`
- Create: `tests/test_market_ws.py`

**Context:** Port from polytrader's `market_ws.py`. WebSocket client connecting to `wss://ws-subscriptions-clob.polymarket.com/ws/market` with auto-reconnect, ping/pong, and subscription management. Routes messages to a callback.

- [ ] **Step 1: Write market WS tests**

Create `tests/test_market_ws.py` — test the subscription message format and reconnect backoff logic (unit tests, no real WS connection):
```python
from polybot.data.market_ws import MarketWSClient


def test_build_subscribe_message():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    msg = client._build_subscribe_msg(["token_a", "token_b"])
    assert msg["type"] == "market"
    assert set(msg["assets_ids"]) == {"token_a", "token_b"}


def test_backoff_capped():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    client._reconnect_count = 10
    delay = client._backoff_delay()
    assert delay <= 60.0


def test_initial_state():
    client = MarketWSClient(
        url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        on_message=lambda msg: None,
    )
    assert client.is_connected is False
    assert client._reconnect_count == 0
```

**Interface notes (new design inspired by polytrader, NOT a direct port):**
- Constructor: `MarketWSClient(url: str, on_message: Callable, ping_interval_sec: float = 10)`
- Properties: `is_connected: bool`
- Methods: `async run(token_ids)`, `async stop()`, `update_subscriptions(token_ids)`, `_build_subscribe_msg(token_ids) -> dict`, `_backoff_delay() -> float`
- Internal state: `_reconnect_count: int`

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_market_ws.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement market_ws.py**

Create `polybot/data/market_ws.py` — new module inspired by polytrader's `market_ws.py`. Key elements:
- `MarketWSClient` class
- Constructor: `url: str`, `on_message: Callable`, `ping_interval_sec: float = 10`
- `async run(token_ids: list[str])` — connect, subscribe, receive loop
- `async stop()` — graceful close
- `update_subscriptions(token_ids)` — dynamic subscribe/unsubscribe
- `_build_subscribe_msg(token_ids) -> dict` — format subscription JSON: `{"assets_ids": [...], "type": "market"}`
- `_backoff_delay() -> float` — exponential backoff: `min(2 ** _reconnect_count, 60.0)`
- `is_connected: bool` property
- `_reconnect_count: int` — reset on successful connect
- Ping/pong loop every `ping_interval_sec`
- Auto-reconnect with exponential backoff on disconnect

Reference (for general approach): `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/market_ws.py`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_market_ws.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/market_ws.py tests/test_market_ws.py
git commit -m "feat: add market WebSocket client (data/market_ws.py)"
```

---

## Task 6: Data Layer — Price Feed (`data/price_feed.py`)

**Files:**
- Create: `polybot/data/price_feed.py`
- Create: `tests/test_price_feed.py`

**Context:** Port from polytrader's `price_feed.py`. `MultiAssetPriceFeed` streams BTC/ETH/SOL/XRP from Binance combined WS. CoinGecko fallback ported from single-asset `BinanceWSPriceFeed._fallback_poll_loop` and extended to multi-asset.

- [ ] **Step 1: Write price feed tests**

Create `tests/test_price_feed.py`:
```python
from decimal import Decimal
from polybot.data.price_feed import MultiAssetPriceFeed


def test_initial_state():
    feed = MultiAssetPriceFeed(assets=("BTC", "ETH", "SOL", "XRP"))
    assert feed.get_price("BTC") is None
    assert feed.get_price("ETH") is None


def test_set_price():
    feed = MultiAssetPriceFeed(assets=("BTC",))
    feed._update_price("BTC", Decimal("65000.50"))
    assert feed.get_price("BTC") == Decimal("65000.50")


def test_binance_stream_url():
    feed = MultiAssetPriceFeed(assets=("BTC", "ETH"))
    url = feed._build_ws_url()
    assert "stream?streams=" in url
    assert "btcusdt@trade" in url
    assert "ethusdt@trade" in url


def test_coingecko_asset_mapping():
    feed = MultiAssetPriceFeed(
        assets=("BTC", "ETH", "SOL", "XRP"),
        coingecko_ids=("bitcoin", "ethereum", "solana", "ripple"),
    )
    assert feed._coingecko_id("BTC") == "bitcoin"
    assert feed._coingecko_id("XRP") == "ripple"


def test_unknown_asset_returns_none():
    feed = MultiAssetPriceFeed(assets=("BTC",))
    assert feed.get_price("DOGE") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_price_feed.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement price_feed.py**

Create `polybot/data/price_feed.py` — port `MultiAssetPriceFeed` from `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/price_feed.py` (lines 234-348) and extend with CoinGecko fallback from `BinanceWSPriceFeed._fallback_poll_loop` (lines 90-120). Key elements:
- `MultiAssetPriceFeed` class
- Constructor: `assets: tuple`, `coingecko_ids: tuple`, `ws_base_url: str`, `fallback_interval_sec: float`
- `_prices: dict[str, Decimal]`, `_last_ts: dict[str, float]`
- `get_price(asset) -> Decimal | None`
- `_update_price(asset, price)` — internal setter, updates timestamp
- `_build_ws_url() -> str` — builds Binance combined stream URL
- `async run()` — main loop: connect WS, parse trade messages, update prices
- `async _fallback_poll_loop()` — CoinGecko polling when Binance inactive >10s
- `async _bootstrap()` — fetch initial prices from Binance REST, fallback to CoinGecko
- `async stop()`
- `_coingecko_id(asset) -> str` — maps BTC->bitcoin, ETH->ethereum, etc.
- Exponential backoff reconnect (1s to 60s)
- `on_tick` optional callback

**Binance WS URL:** `wss://stream.binance.com:9443/stream?streams=btcusdt@trade/ethusdt@trade/solusdt@trade/xrpusdt@trade`
**Binance message format:** `{"stream": "btcusdt@trade", "data": {"p": "65000.50", ...}}`
**CoinGecko endpoint:** `https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,ripple&vs_currencies=usd`

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_price_feed.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/price_feed.py tests/test_price_feed.py
git commit -m "feat: add multi-asset price feed with CoinGecko fallback (data/price_feed.py)"
```

---

## Task 7: Data Layer — CLOB Midpoint Poller (`data/clob_midpoints.py`)

**Files:**
- Create: `polybot/data/clob_midpoints.py`
- Create: `tests/test_clob_midpoints.py`

**Context:** Port from polytrader's `clob_midpoints.py`. REST polling of CLOB `/midpoints` endpoint every 2s.

- [ ] **Step 1: Write midpoint poller tests**

Create `tests/test_clob_midpoints.py`:
```python
from decimal import Decimal
from polybot.data.clob_midpoints import ClobMidpointPoller


def test_register_tokens():
    poller = ClobMidpointPoller()
    poller.register_tokens(["token_a", "token_b"])
    assert "token_a" in poller._token_ids
    assert "token_b" in poller._token_ids


def test_remove_tokens():
    poller = ClobMidpointPoller()
    poller.register_tokens(["token_a", "token_b"])
    poller.remove_tokens(["token_a"])
    assert "token_a" not in poller._token_ids
    assert "token_b" in poller._token_ids


def test_get_mid_none_before_poll():
    poller = ClobMidpointPoller()
    assert poller.get_mid("token_a") is None


def test_get_mid_after_manual_set():
    poller = ClobMidpointPoller()
    poller._midpoints["token_a"] = Decimal("0.55")
    assert poller.get_mid("token_a") == Decimal("0.55")
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_clob_midpoints.py -v`

Create `polybot/data/clob_midpoints.py` — port from `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/clob_midpoints.py`. Key elements:
- `ClobMidpointPoller` class
- `_token_ids: set[str]`, `_midpoints: dict[str, Decimal]`
- `register_tokens(ids)`, `remove_tokens(ids)`, `get_mid(token_id) -> Decimal | None`
- `async run(clob_host: str, poll_interval: float)` — POST `/midpoints` with token_ids, parse response
- `async stop()`

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/clob_midpoints.py tests/test_clob_midpoints.py
git commit -m "feat: add CLOB midpoint poller (data/clob_midpoints.py)"
```

---

## Task 8: Data Layer — Market Discovery (`data/gamma.py`)

**Files:**
- Create: `polybot/data/gamma.py`
- Create: `tests/test_gamma.py`

**Context:** Port discovery from polytrader's `gamma.py`, add `to_market_window()` mapping function that converts `MarketInfo` to PolyBot's `MarketWindow` type. This is critical glue between polytrader's data format and PolyBot's strategy layer.

- [ ] **Step 1: Write gamma tests focusing on MarketInfo→MarketWindow mapping**

Create `tests/test_gamma.py`:
```python
from polybot.data.gamma import MarketInfo, to_market_window


def test_to_market_window_basic():
    info = MarketInfo(
        condition_id="cond_123",
        question="Will BTC go up in the next 15 minutes?",
        slug="btc-updown-15m-2026-03-19",
        clob_token_ids=["token_up", "token_dn"],
        outcomes=["Up", "Down"],
        event_start_iso="2026-03-19T12:00:00Z",
        end_date_iso="2026-03-19T12:15:00Z",
        price_to_beat="65000.00",
        active=True,
        liquidity=50000.0,
    )
    mw = to_market_window(info, asset="BTC")
    assert mw.market_id == "btc-updown-15m-2026-03-19"
    assert mw.condition_id == "cond_123"
    assert mw.asset == "BTC"
    assert mw.up_token_id == "token_up"
    assert mw.dn_token_id == "token_dn"
    assert mw.timeframe_sec == 900  # 15 minutes
    assert mw.close_epoch > mw.open_epoch


def test_to_market_window_reversed_outcomes():
    """If outcomes are ["Down", "Up"], tokens should map correctly."""
    info = MarketInfo(
        condition_id="cond_456",
        question="Will ETH go up?",
        slug="eth-updown-5m-2026-03-19",
        clob_token_ids=["token_dn", "token_up"],
        outcomes=["Down", "Up"],
        event_start_iso="2026-03-19T12:00:00Z",
        end_date_iso="2026-03-19T12:05:00Z",
        price_to_beat="3200.00",
        active=True,
        liquidity=10000.0,
    )
    mw = to_market_window(info, asset="ETH")
    assert mw.up_token_id == "token_up"
    assert mw.dn_token_id == "token_dn"


def test_slug_pattern_matching():
    from polybot.data.gamma import CRYPTO_SLUG_PATTERNS
    assert any("btc" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("eth" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("sol" in p for p in CRYPTO_SLUG_PATTERNS)
    assert any("xrp" in p for p in CRYPTO_SLUG_PATTERNS)
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/data/gamma.py` — port discovery logic from `/c/Users/pc/Desktop/polytrader/polymarket_agent/data/gamma.py`. Key elements:
- `MarketInfo` dataclass: `condition_id`, `question`, `slug`, `clob_token_ids`, `outcomes`, `event_start_iso`, `end_date_iso`, `price_to_beat`, `active`, `liquidity`
- `CRYPTO_SLUG_PATTERNS` — patterns for BTC/ETH/SOL/XRP 5m/15m/1h markets
- `ASSET_FROM_SLUG` — maps slug patterns to asset names
- `async discover_crypto_updown_markets(clob_host: str) -> list[MarketInfo]` — fetches from Gamma API, filters by slug patterns
- `to_market_window(info: MarketInfo, asset: str) -> MarketWindow` — the mapping function:
  - Matches "Up"/"Down" in `outcomes` to assign `up_token_id`/`dn_token_id` (case-insensitive, also handles "Yes"/"No")
  - Parses ISO dates to epoch ints for `open_epoch`/`close_epoch`
  - Computes `timeframe_sec = close_epoch - open_epoch`
  - Maps `slug` to `market_id`

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/data/gamma.py tests/test_gamma.py
git commit -m "feat: add Gamma API market discovery with MarketWindow mapping (data/gamma.py)"
```

---

## Task 9: OMS — Client Wrapper (`oms/clob_client.py`)

**Files:**
- Create: `polybot/oms/clob_client.py`
- Create: `tests/test_clob_client.py`

**Context:** Paper/live CLOB client wrapper. Paper mode uses real py-clob-client for reads, simulates writes. Live mode passes through. Both expose the same synchronous interface that `OrderExecutor` calls.

- [ ] **Step 1: Write client wrapper tests**

Create `tests/test_clob_client.py`:
```python
from polybot.oms.clob_client import PaperClobClient


def test_paper_post_order_returns_mock_id():
    client = PaperClobClient(book_manager=None)
    result = client.post_order(
        {"order": "signed_data"},
        orderType="GTC",
    )
    assert "orderID" in result
    assert result["orderID"].startswith("paper-")


def test_paper_resting_orders():
    client = PaperClobClient(book_manager=None)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    orders = client.get_open_orders()
    assert len(orders) == 1


def test_paper_cancel_order():
    client = PaperClobClient(book_manager=None)
    result = client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    oid = result["orderID"]
    client.cancel(oid)
    assert len(client.get_open_orders()) == 0


def test_paper_cancel_all():
    client = PaperClobClient(book_manager=None)
    for i in range(5):
        client.post_order(
            {"order": f"data_{i}", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
            orderType="GTC",
        )
    client.cancel_all()
    assert len(client.get_open_orders()) == 0


def test_paper_tick_fills_buy_when_ask_crosses():
    """Paper fill simulation: buy at 0.45, best ask drops to 0.44 -> fills."""
    from unittest.mock import MagicMock
    from decimal import Decimal
    from polybot.data.book import OrderBook, apply_book_snapshot
    from polybot.data.book_manager import BookManager
    import time

    bm = BookManager()
    bm.update_assets(["tok_a"])
    apply_book_snapshot(bm.get_book("tok_a"), {
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.44", "size": "200"}],
    }, ts=time.time())

    client = PaperClobClient(book_manager=bm)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    assert len(client.get_open_orders()) == 1

    fills = client.tick()
    assert len(fills) >= 1  # Should have filled
    assert len(client.get_open_orders()) == 0  # Fully filled, removed


def test_paper_tick_no_fill_when_ask_above():
    """Buy at 0.45, best ask is 0.55 -> no fill."""
    from polybot.data.book import apply_book_snapshot
    from polybot.data.book_manager import BookManager
    import time

    bm = BookManager()
    bm.update_assets(["tok_a"])
    apply_book_snapshot(bm.get_book("tok_a"), {
        "bids": [{"price": "0.40", "size": "500"}],
        "asks": [{"price": "0.55", "size": "200"}],
    }, ts=time.time())

    client = PaperClobClient(book_manager=bm)
    client.post_order(
        {"order": "data", "token_id": "tok_a", "price": "0.45", "size": "100", "side": "BUY"},
        orderType="GTC",
    )
    fills = client.tick()
    assert len(fills) == 0
    assert len(client.get_open_orders()) == 1  # Still resting
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/oms/clob_client.py`. Key elements:

**`PaperClobClient`** — for paper/dry-run mode:
- Constructor: `book_manager: BookManager | None` (injected for fill simulation)
- `_resting: dict[str, dict]` — resting orders keyed by order_id
- `post_order(signed, orderType) -> dict` — stores order in `_resting`, returns mock order_id
- `post_orders(signed_orders) -> list` — batch version
- `get_open_orders() -> list[dict]` — returns resting orders
- `cancel(order_id) -> dict` — removes from `_resting`
- `cancel_all() -> dict` — clears all resting
- `cancel_orders(order_ids) -> dict` — batch cancel
- `tick()` — fill simulation using BookManager data (if available)
- **Read-through methods** (proxy to real CLOB): `get_order_book`, `get_tick_size` — these use httpx to call CLOB REST directly (no auth needed for reads)
- `create_order(order_args) -> dict` — wraps order args into a dict (no signing needed in paper mode)
- `post_heartbeat()` — no-op in paper mode
- `get_balance_allowance() -> dict` — returns hardcoded balance

**`LiveClobClient`** — thin wrapper around real `py_clob_client.ClobClient`:
- Constructor: `host, key, chain_id, creds, signature_type, funder`
- All methods delegate to the real client
- Wraps exceptions as `ClobApiError` with typed status codes

**`create_clob_client(cfg: BotConfig, book_manager=None)`** — factory function:
- If `cfg.dry_run` or no `cfg.private_key`: returns `PaperClobClient`
- Otherwise: creates real `ClobClient`, derives API creds, returns `LiveClobClient`

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/oms/clob_client.py tests/test_clob_client.py
git commit -m "feat: add paper/live CLOB client wrapper (oms/clob_client.py)"
```

---

## Task 10: OMS — Order Executor (`oms/order_executor.py`)

**Files:**
- Create: `polybot/oms/order_executor.py`
- Create: `tests/test_order_executor_new.py`

**Context:** Rebuilt order executor with same public interface as original (`place_limit_buy`, `place_batch_limit_buys`, `get_best_ask`, etc.) but using the new client wrapper. Synchronous methods. Errors propagate up.

- [ ] **Step 1: Write order executor tests**

Create `tests/test_order_executor_new.py` — test against PaperClobClient:
```python
from polybot.config import BotConfig
from polybot.oms.clob_client import PaperClobClient
from polybot.oms.order_executor import OrderExecutor
from polybot.types import Side


def test_place_limit_buy():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    record = executor.place_limit_buy(
        token_id="tok_up",
        price=0.45,
        size=50.0,
        market_id="test-market",
        side=Side.UP,
    )
    assert record.order_id != ""
    assert record.price == 0.45
    assert record.size == 50.0


def test_get_open_orders():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    orders = executor.get_open_orders()
    assert len(orders) >= 1


def test_cancel_order():
    cfg = BotConfig()
    client = PaperClobClient(book_manager=None)
    executor = OrderExecutor(cfg=cfg, clob_client=client)
    record = executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    assert executor.cancel_order(record.order_id) is True


def test_error_propagation_not_swallowed():
    """Verify ClobApiError propagates up instead of being silently caught."""
    from unittest.mock import MagicMock
    from polybot.errors import ClobApiError

    cfg = BotConfig()
    mock_client = MagicMock()
    mock_client.create_order.side_effect = ClobApiError("rate limited", status_code=429, retry_after=5.0)
    executor = OrderExecutor(cfg=cfg, clob_client=mock_client)

    import pytest
    with pytest.raises(ClobApiError) as exc_info:
        executor.place_limit_buy("tok_up", 0.45, 50.0, "test-market", Side.UP)
    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after == 5.0
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/oms/order_executor.py` — same interface as original `polybot/order_executor.py` but:
- Uses the new client wrapper (PaperClobClient or LiveClobClient)
- Does NOT catch and swallow exceptions — lets `ClobApiError` propagate
- Tick size validation via `round_to_tick` before submission
- Batch orders capped at `cfg.batch_order_size` (15)

Reference the original at `/c/Users/pc/Desktop/PolyBot/polybot/order_executor.py` for exact method signatures.

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/oms/order_executor.py tests/test_order_executor_new.py
git commit -m "feat: add rebuilt order executor (oms/order_executor.py)"
```

---

## Task 11: OMS — Heartbeat (`oms/heartbeat.py`)

**Files:**
- Create: `polybot/oms/heartbeat.py`
- Create: `tests/test_heartbeat_new.py`

**Context:** Port heartbeat with health tracking. Posts to CLOB every 5s. Tracks consecutive failures. Sets `healthy = False` after 2 failures. Auto-recovers.

- [ ] **Step 1: Write heartbeat tests**

Create `tests/test_heartbeat_new.py`:
```python
import asyncio
from polybot.oms.heartbeat import Heartbeat


def test_initial_state():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    assert hb.is_healthy() is True


def test_record_failure():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    hb._record_failure()
    assert hb.is_healthy() is True  # 1 failure, threshold is 2
    hb._record_failure()
    assert hb.is_healthy() is False  # 2 failures


def test_record_success_resets():
    hb = Heartbeat(interval_sec=5.0, max_failures=2)
    hb._record_failure()
    hb._record_failure()
    assert hb.is_healthy() is False
    hb._record_success()
    assert hb.is_healthy() is True
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/oms/heartbeat.py` — port from polytrader's heartbeat pattern. Key elements:
- `Heartbeat` class
- Constructor: `interval_sec: float = 5.0`, `max_failures: int = 2`
- `is_healthy() -> bool`
- `_record_failure()`, `_record_success()` — internal state tracking
- `async run(client, on_connection_lost: Callable)` — async loop posting heartbeat
- `async stop()`

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/oms/heartbeat.py tests/test_heartbeat_new.py
git commit -m "feat: add CLOB heartbeat with health tracking (oms/heartbeat.py)"
```

---

## Task 12: Web UI — State Holder (`web/state.py`)

**Files:**
- Create: `polybot/web/state.py`
- Create: `tests/test_gui_state.py`

**Context:** Port from polytrader's `state.py`. Mutable state dict that triggers broadcast on update. Handles Decimal→float serialization.

- [ ] **Step 1: Write state holder tests**

Create `tests/test_gui_state.py`:
```python
from decimal import Decimal
from polybot.web.state import GuiStateHolder


def test_initial_state_has_all_spec_fields():
    """Verify all fields from spec's Web API Contract are present."""
    state = GuiStateHolder()
    data = state.get()
    assert data["mode"] == "dry_run"
    assert data["running"] is False
    assert data["heartbeat_healthy"] is True
    assert data["cancel_only_mode"] is False
    assert data["total_pnl"] == 0.0
    assert data["realized_pnl"] == 0.0
    assert data["unrealized_pnl"] == 0.0
    assert data["trade_count"] == 0
    assert data["position_count"] == 0
    assert data["pairs_completed"] == 0
    assert data["avg_pair_cost"] == 0.0
    assert data["imbalance_ratio"] == 0.0
    assert data["runtime_sec"] == 0
    assert data["markets_active"] == 0
    assert data["win_rate"] == 0.0
    assert data["prices"] == {}
    assert data["binance_prices"] == {}
    assert data["spots"] == {}
    assert data["active_markets"] == []
    assert data["activity_feed"] == []
    assert data["trades"] == []
    assert data["pending_settlements"] == []
    assert data["wallet"] is None


def test_update():
    state = GuiStateHolder()
    state.update(running=True, total_pnl=Decimal("42.50"))
    data = state.get()
    assert data["running"] is True
    assert data["total_pnl"] == Decimal("42.50")


def test_serialization_converts_decimals():
    state = GuiStateHolder()
    state.update(total_pnl=Decimal("42.50"))
    serialized = state.serialize()
    assert isinstance(serialized["total_pnl"], float)
    assert serialized["total_pnl"] == 42.50
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/web/state.py` — adapted from polytrader's `state.py`. Key elements:
- `GuiStateHolder` class
- `_data: dict` initialized with ALL spec payload fields:
  ```python
  _data = {
      "mode": "dry_run", "running": False, "heartbeat_healthy": True,
      "cancel_only_mode": False, "total_pnl": 0.0, "realized_pnl": 0.0,
      "unrealized_pnl": 0.0, "trade_count": 0, "position_count": 0,
      "pairs_completed": 0, "avg_pair_cost": 0.0, "imbalance_ratio": 0.0,
      "runtime_sec": 0, "markets_active": 0, "win_rate": 0.0,
      "prices": {}, "binance_prices": {}, "spots": {},
      "active_markets": [], "activity_feed": [], "trades": [],
      "pending_settlements": [], "wallet": None,
  }
  ```
- `set_broadcast(fn)` — wires async broadcast callback
- `update(**kwargs)` — updates fields, triggers broadcast
- `get() -> dict` — returns copy of current state
- `serialize() -> dict` — returns JSON-safe version (Decimal→float, recursive)

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/web/state.py tests/test_gui_state.py
git commit -m "feat: add GUI state holder (web/state.py)"
```

---

## Task 13: Web UI — Server (`web/server.py`)

**Files:**
- Create: `polybot/web/server.py` (replaces existing FastAPI server)
- Create: `tests/test_web_server.py`

**Context:** aiohttp server with REST endpoints and WebSocket broadcast. Port from polytrader's `server.py`, adapted for PolyBot's API contract.

- [ ] **Step 1: Write web server tests**

Create `tests/test_web_server.py`:
```python
import asyncio
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from polybot.web.server import create_app
from polybot.web.state import GuiStateHolder


class TestWebServer(AioHTTPTestCase):
    async def get_application(self):
        state = GuiStateHolder()
        return create_app(state=state, start_fn=None, stop_fn=None)

    @unittest_run_loop
    async def test_status_endpoint(self):
        resp = await self.client.request("GET", "/api/state")
        assert resp.status == 200
        data = await resp.json()
        assert "mode" in data
        assert "running" in data

    @unittest_run_loop
    async def test_static_files(self):
        resp = await self.client.request("GET", "/")
        assert resp.status == 200
```

- [ ] **Step 2: Run test, verify fail, implement, verify pass**

Create `polybot/web/server.py` — port from `/c/Users/pc/Desktop/polytrader/polymarket_agent/gui/server.py`. Key elements:
- `create_app(state, start_fn, stop_fn) -> web.Application`
- Routes:
  - `GET /` — serves `static/index.html`
  - `GET /api/state` — returns `state.serialize()` as JSON
  - `POST /api/start` — calls `start_fn()`
  - `POST /api/stop` — calls `stop_fn()`
  - `POST /api/set-bankroll` — updates bankroll in state
  - `GET /api/balance` — returns balance info
  - `GET /ws` — WebSocket handler (sends state on connect, broadcasts on updates)
- Static file serving from `polybot/web/static/`
- `_serialize(obj)` — Decimal→float recursive serializer
- `async start_gui_server(app, port)` — starts aiohttp runner
- WebSocket ping/pong every 20s
- Broadcast function wired to `GuiStateHolder.set_broadcast()`

- [ ] **Step 3: Run test to verify pass, commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/web/server.py tests/test_web_server.py
git commit -m "feat: add aiohttp web server (web/server.py)"
```

---

## Task 14: Web UI — Frontend (HTML/JS/CSS)

**Files:**
- Create: `polybot/web/static/index.html`
- Create: `polybot/web/static/app.js`
- Create: `polybot/web/static/styles.css`

**Context:** Port from polytrader's `gui/static/` and adapt for ladder MM. Remove risk management controls, trade option controls, config panel. Add ladder-specific metrics (rungs filled, spread, imbalance). Keep dark-mode design, price strips, market grid, PnL sparkline.

- [ ] **Step 1: Create index.html**

Port from `/c/Users/pc/Desktop/polytrader/polymarket_agent/gui/static/index.html`. Modifications:
- Change title to "PolyBot Dashboard"
- Remove config tab entirely (no settings panel)
- Remove kill switch button
- Remove mode selector dropdown (show mode as read-only badge)
- Remove "Close Positions" button
- Keep: header, hero metrics, price strips, market grid, activity feed, trades table
- Adapt hero metrics: replace "Volatility" with "Avg Pair Cost", replace "Countdown" with "Imbalance Ratio"
- Add to market cards: "Rungs: 24/36" badge, spread width display, imbalance indicator
- Change trades table columns: Time, Market, Side, Rung Price, Fill Size, Pair Cost, PnL

- [ ] **Step 2: Create app.js**

Port from `/c/Users/pc/Desktop/polytrader/polymarket_agent/gui/static/app.js`. Modifications:
- Remove all config/settings panel JS
- Remove mode selector change handler
- Remove kill switch handler
- Remove auto-optimize field locking
- Remove whale trades filtering (replace with ladder trades)
- Adapt `applyStatus()` for new payload fields (`pairs_completed`, `avg_pair_cost`, `imbalance_ratio`)
- Adapt `renderMarketGrid()` to show ladder info (rungs filled, spread, imbalance) instead of certainty badges
- Keep: WebSocket connection, polling fallback, sparkline, price strip rendering, activity feed, toast system

- [ ] **Step 3: Create styles.css**

Port from `/c/Users/pc/Desktop/polytrader/polymarket_agent/gui/static/styles.css`. Modifications:
- Remove config panel styles
- Add ladder-specific badge styles (`.rungs-badge`, `.spread-badge`, `.imbalance-indicator`)
- Keep: dark theme, design tokens, hero cards, price strip, market grid, modal, animations

- [ ] **Step 4: Manually test by opening in browser**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -c "from aiohttp import web; from polybot.web.server import create_app; from polybot.web.state import GuiStateHolder; web.run_app(create_app(GuiStateHolder(), None, None), port=8080)"`

Open `http://127.0.0.1:8080` and verify:
- Page loads with dark theme
- Hero metrics show (all zeros initially)
- Price strips visible (empty)
- Market grid visible (empty)
- No config panel, no kill switch

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/web/static/
git commit -m "feat: add adapted web dashboard from polytrader (web/static/)"
```

---

## Task 15: Bot Orchestrator (`bot.py`)

**Files:**
- Create: `polybot/bot.py` (replaces existing)
- Create: `tests/test_bot_orchestrator.py`

**Context:** Central coordinator tying all layers together. Manages lifecycle of data feeds, OMS, web UI. Runs main trading loop at 500ms. Dispatches strategy calls via `asyncio.to_thread()`. Handles settlement. Updates GUI state.

- [ ] **Step 1: Write orchestrator unit tests**

Create `tests/test_bot_orchestrator.py`:
```python
import asyncio
from polybot.config import BotConfig
from polybot.bot import Bot


def test_bot_creation():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    assert bot.running is False
    assert bot.mode == "dry_run"


def test_bot_state_snapshot():
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()
    assert state["mode"] == "dry_run"
    assert state["running"] is False
    assert "total_pnl" in state
    assert "active_markets" in state
```

- [ ] **Step 2: Run test, verify fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_bot_orchestrator.py -v`
Expected: FAIL

- [ ] **Step 3: Implement bot.py**

Create `polybot/bot.py`. Key elements:

```python
class Bot:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.running = False
        self.mode = "dry_run" if cfg.dry_run else "live"

        # Data layer
        self.price_feed = MultiAssetPriceFeed(assets=cfg.assets, ...)
        self.book_manager = BookManager()
        self.market_ws = MarketWSClient(on_message=self.book_manager.process_message)
        self.midpoint_poller = ClobMidpointPoller()

        # OMS
        self.clob_client = create_clob_client(cfg, book_manager=self.book_manager)
        self.order_executor = OrderExecutor(cfg, self.clob_client)
        self.heartbeat = Heartbeat(cfg.heartbeat_interval_sec, cfg.heartbeat_max_failures)

        # Strategy (unchanged)
        self.order_tracker = OrderTracker()
        self.position_manager = PositionManager(cfg, bankroll=cfg.bankroll)
        self.risk = RiskStub()
        self.tick_cache = TickSizeCache(self.clob_client, cfg.tick_size_ttl_sec)
        self.ladder_manager = LadderManager(
            cfg, self.order_executor, self.order_tracker,
            self.position_manager, self.risk, self.tick_cache,
        )

        # Web UI
        self.gui_state = GuiStateHolder()

        # State
        self._active_markets: dict[str, MarketWindow] = {}
        self._start_time = 0.0
        self._trade_count = 0
        self._total_pnl = 0.0
        self._realized_pnl = 0.0

    async def start(self):
        """Start all subsystems and begin trading loop."""

    async def stop(self):
        """Graceful shutdown."""

    async def _run_trading_loop(self):
        """Main loop at 500ms interval."""

    async def _discover_markets(self):
        """Gamma API market discovery with MarketWindow mapping."""

    async def _process_market(self, market: MarketWindow):
        """Strategy dispatch via asyncio.to_thread()."""

    async def _run_settlement_poller(self):
        """Settlement polling loop."""

    def build_state_snapshot(self) -> dict:
        """Build full state for GUI."""
```

**Detailed implementation guidance for the orchestrator (the current bot.py is 580+ lines — this rebuilt version must cover all these concerns):**

**`async start(self)`:**
1. Set `self.running = True`, `self._start_time = time.time()`
2. Launch concurrent tasks: `price_feed.run()`, `midpoint_poller.run()`, `market_ws.run([])`
3. If live mode: launch `heartbeat.run(client, self._on_heartbeat_lost)`
4. Launch `_run_trading_loop()` and `_run_settlement_poller()` as tasks
5. Update GUI state

**`async stop(self)`:**
1. Set `self.running = False`
2. If live mode: cancel all resting orders via `order_executor.cancel_all()`
3. Stop: `price_feed.stop()`, `midpoint_poller.stop()`, `market_ws.stop()`, `heartbeat.stop()`
4. Cancel async tasks
5. Log final state

**`async _run_trading_loop(self)` (every 500ms):**
```python
while self.running:
    if not self.heartbeat.is_healthy():
        await asyncio.sleep(0.5)
        continue

    # Market discovery (every 60s)
    if time.time() - self._last_discovery > self.cfg.market_discovery_interval_sec:
        await self._discover_markets()

    # Paper fill simulation
    if self.cfg.dry_run and hasattr(self.clob_client, 'tick'):
        fills = self.clob_client.tick()
        for fill in fills:
            # Update order_tracker and position_manager via asyncio.to_thread()
            pass

    # Strategy dispatch for each active market
    for market_id, market in self._active_markets.items():
        try:
            await self._process_market(market)
        except ClobApiError as e:
            if e.status_code == 429:
                await asyncio.sleep(e.retry_after or 1.0)
            elif e.cancel_only:
                self.gui_state.update(cancel_only_mode=True)
            else:
                logger.error("Order error for %s: %s", market_id, e)

    # Check expired markets
    now = int(time.time())
    for mid, mkt in list(self._active_markets.items()):
        if not mkt.is_active(now):
            self.position_manager.mark_pending_settlement(mid)

    # Update GUI state
    self.gui_state.update(**self.build_state_snapshot())
    await asyncio.sleep(self.cfg.poll_interval_ms / 1000)
```

**`async _run_settlement_poller(self)` (every 30s):**
- Check `position_manager.get_pending_settlements()`
- For each: call `settlement.try_resolve_once()` via httpx
- In paper mode: auto-resolve based on spot price vs open price
- On resolution: compute PnL, update `_realized_pnl`, call `position_manager.complete_settlement()`
- Queue redemption via `redeemer.queue_redemption()` (still a stub)

**`async _run_daily_reset(self)` — midnight timer:**
- Resets daily PnL counters
- Kept from current bot.py logic

**`build_state_snapshot(self) -> dict`:**
- Returns dict matching full spec payload (all fields from GuiStateHolder)
- Computes `runtime_sec`, `markets_active`, `win_rate` dynamically
- Builds `active_markets` list with ladder state per market (rungs filled, spread, imbalance)
- Includes `prices` (CLOB midpoints), `binance_prices` (spot), `spots` (combined)

- [ ] **Step 4: Run test to verify pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_bot_orchestrator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add polybot/bot.py tests/test_bot_orchestrator.py
git commit -m "feat: add rebuilt bot orchestrator (bot.py)"
```

---

## Task 16: Entry Point (`run_bot.py`)

**Files:**
- Modify: `run_bot.py` (replace existing)

**Context:** Clean entry point. Loads config, creates Bot, wires up web server, runs event loop. Replaces the current run_bot.py which has the MockClobClient embedded.

- [ ] **Step 1: Rewrite run_bot.py**

```python
"""PolyBot entry point — launch the ladder market maker."""

import asyncio
import logging
import sys

from polybot.config import load_bot_config
from polybot.bot import Bot
from polybot.web.server import create_app, start_gui_server


def main():
    cfg = load_bot_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level),
        format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("polybot.log"),
        ],
    )
    log = logging.getLogger("polybot")

    mode = "PAPER" if cfg.dry_run else "LIVE"
    log.info("Starting PolyBot in %s mode", mode)

    bot = Bot(cfg)

    app = create_app(
        state=bot.gui_state,
        start_fn=bot.start,
        stop_fn=bot.stop,
    )

    async def run():
        await start_gui_server(app, cfg.web_port)
        log.info("Dashboard at http://127.0.0.1:%d", cfg.web_port)

        if not cfg.start_paused:
            await bot.start()

        # Keep running until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await bot.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Verify it starts without crashing**

Run: `cd /c/Users/pc/Desktop/PolyBot && timeout 5 python run_bot.py 2>&1 || true`
Expected: Starts, logs "Starting PolyBot in PAPER mode", "Dashboard at http://127.0.0.1:8080", no crash

- [ ] **Step 3: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add run_bot.py
git commit -m "feat: rewrite entry point for rebuilt infrastructure"
```

---

## Task 17: Update requirements.txt

**Files:**
- Modify: `requirements.txt`

**Context:** Add aiohttp dependency (replacing FastAPI/uvicorn for web server). Keep existing deps.

- [ ] **Step 1: Update requirements.txt**

Add/update:
```
aiohttp>=3.9.0
```

Remove (no longer needed for web server):
```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
```

Keep all other existing dependencies.

- [ ] **Step 2: Install updated dependencies**

Run: `cd /c/Users/pc/Desktop/PolyBot && pip install -r requirements.txt`

- [ ] **Step 3: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add requirements.txt
git commit -m "chore: update deps — add aiohttp, remove fastapi/uvicorn"
```

---

## Task 18: Integration Test — Paper Mode End-to-End

**Files:**
- Create: `tests/test_integration_paper.py`

**Context:** Verify the full system works in paper mode: bot starts, discovers markets (may need mock if no live markets), price feeds connect, web UI serves, state broadcasts.

- [ ] **Step 1: Write integration test**

Create `tests/test_integration_paper.py`:
```python
import asyncio
import pytest
from aiohttp import ClientSession
from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.web.server import create_app, start_gui_server
from polybot.web.state import GuiStateHolder


@pytest.mark.asyncio
async def test_paper_mode_startup():
    """Verify bot starts in paper mode and web UI is accessible."""
    cfg = BotConfig(dry_run=True, web_port=18080, start_paused=True)
    bot = Bot(cfg)
    app = create_app(state=bot.gui_state, start_fn=bot.start, stop_fn=bot.stop)

    runner = await start_gui_server(app, 18080)

    try:
        async with ClientSession() as session:
            # Check web UI is serving
            async with session.get("http://127.0.0.1:18080/api/state") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["mode"] == "dry_run"
                assert data["running"] is False
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_bot_state_snapshot_has_required_fields():
    """Verify state snapshot matches spec payload."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)
    state = bot.build_state_snapshot()

    required_fields = [
        "mode", "running", "heartbeat_healthy", "total_pnl",
        "realized_pnl", "unrealized_pnl", "trade_count",
        "position_count", "pairs_completed", "prices",
        "binance_prices", "active_markets", "activity_feed", "trades",
    ]
    for field in required_fields:
        assert field in state, f"Missing field: {field}"
```

- [ ] **Step 2: Run integration test**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_integration_paper.py -v --timeout=30`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add tests/test_integration_paper.py
git commit -m "test: add paper mode integration tests"
```

---

## Task 19: Cleanup & Final Verification

**Files:**
- No new files — cleanup only

**Context:** Remove old files that are fully replaced, run full test suite, verify everything works.

- [ ] **Step 1: Remove duplicate strategy files from old locations**

The files were copied (not moved) in Task 1. Now that everything is wired through `polybot/strategy/`, remove the originals:
```bash
cd /c/Users/pc/Desktop/PolyBot
rm polybot/ladder_manager.py polybot/position_manager.py polybot/order_tracker.py
```

The re-exports in `polybot/__init__.py` ensure `from polybot.ladder_manager import LadderManager` still works.

- [ ] **Step 2: Run full test suite**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/ -v --timeout=60`
Expected: All new tests PASS. Old tests that imported from `polybot.order_executor` (the old one) or `polybot.web.server` (FastAPI) will need updating to use the new paths. Fix or remove old tests that test replaced code.

- [ ] **Step 2: Verify web UI manually**

Run: `cd /c/Users/pc/Desktop/PolyBot && python run_bot.py`
Open `http://127.0.0.1:8080` and verify:
- Dashboard loads with dark theme
- Price strips show (will populate once Binance WS connects)
- Market grid shows (will populate once Gamma API finds markets)
- Start/Stop buttons work
- WebSocket connection indicator shows connected

- [ ] **Step 3: Verify paper mode trading loop**

Click "Start" in the web UI. Observe logs for:
- "Market discovery: found N markets"
- "Ladder posted for [market]"
- Price updates appearing in price strips
- Market cards populating in grid

- [ ] **Step 4: Final commit**

```bash
cd /c/Users/pc/Desktop/PolyBot
git add -A
git commit -m "chore: infrastructure rebuild complete — paper mode verified"
```
