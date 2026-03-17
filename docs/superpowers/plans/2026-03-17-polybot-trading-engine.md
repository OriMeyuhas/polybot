# PolyBot Trading Engine — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an automated trading bot that executes latency arbitrage and spread capture strategies on Polymarket's 15-minute crypto binary markets, replicating the 0x8dxd strategy described in `0x8dxd_Strategy_Analysis.md`.

**Architecture:** Event-driven async Python system. A Binance WebSocket feed provides real-time spot prices. A market discovery loop finds active crypto up/down markets via the Polymarket Gamma API. A signal engine computes directional edge and spread opportunities. An order executor places limit orders via py-clob-client. A position manager tracks open positions and handles settlement. All components run as concurrent asyncio tasks coordinated through a shared state object.

**Tech Stack:** Python 3.11+, py-clob-client (Polymarket CLOB), websockets (Binance feed), httpx (REST APIs), asyncio (concurrency), dataclasses (state), existing polybot package structure.

**Spec:** `0x8dxd_Strategy_Analysis.md` (Sections 8-9 are the primary implementation reference)

---

## File Structure

```
polybot/
├── __init__.py                          (existing, empty)
├── config.py                            (MODIFY — add BotConfig with trading params)
├── types.py                             (CREATE — shared dataclasses: Opportunity, Position, Order, Market)
├── market_discovery.py                  (CREATE — find active crypto up/down markets + token IDs)
├── signal_engine.py                     (CREATE — compute spot delta, check directional + spread opps)
├── position_manager.py                  (CREATE — track positions, compute pair cost, size orders)
├── order_executor.py                    (CREATE — place/cancel/monitor orders via py-clob-client)
├── risk_manager.py                      (CREATE — drawdown circuit breaker, position limits, timing gates)
├── bot.py                               (CREATE — main loop wiring all components together)
├── utils/
│   ├── __init__.py                      (existing, empty)
│   └── time_utils.py                    (existing, no changes)
tests/
├── __init__.py                          (CREATE)
├── test_types.py                        (CREATE)
├── test_signal_engine.py                (CREATE)
├── test_position_manager.py             (CREATE)
├── test_risk_manager.py                 (CREATE)
├── test_market_discovery.py             (CREATE)
├── test_order_executor.py               (CREATE)
├── test_bot_integration.py              (CREATE)
```

---

## Task 1: Shared Types and Config Extension

**Files:**
- Create: `polybot/types.py`
- Modify: `polybot/config.py`
- Create: `tests/__init__.py`
- Create: `tests/test_types.py`

- [ ] **Step 1: Write failing tests for types**

```python
# tests/test_types.py
import pytest
from polybot.types import (
    Side, StrategyType, Opportunity, Position, MarketWindow, OrderRecord,
)


def test_side_enum():
    assert Side.UP.value == "UP"
    assert Side.DOWN.value == "DOWN"


def test_strategy_type_enum():
    assert StrategyType.DIRECTIONAL.value == "DIRECTIONAL"
    assert StrategyType.SPREAD.value == "SPREAD"


def test_opportunity_directional():
    opp = Opportunity(
        strategy=StrategyType.DIRECTIONAL,
        market_id="btc-updown-15m-123",
        side=Side.UP,
        price=0.85,
        edge=0.12,
        confidence=0.003,
    )
    assert opp.strategy == StrategyType.DIRECTIONAL
    assert opp.price == 0.85
    assert opp.up_price is None
    assert opp.dn_price is None


def test_opportunity_spread():
    opp = Opportunity(
        strategy=StrategyType.SPREAD,
        market_id="btc-updown-15m-123",
        up_price=0.48,
        dn_price=0.49,
        edge=0.03,
    )
    assert opp.strategy == StrategyType.SPREAD
    assert opp.up_price + opp.dn_price == pytest.approx(0.97)


def test_position_pair_cost():
    pos = Position(market_id="btc-updown-15m-123")
    pos.up_qty = 100.0
    pos.up_cost = 48.0
    pos.dn_qty = 100.0
    pos.dn_cost = 49.0
    assert pos.pair_cost() == pytest.approx(0.97)
    assert pos.min_qty() == 100.0


def test_position_pair_cost_empty():
    pos = Position(market_id="test")
    assert pos.pair_cost() == 0.0
    assert pos.min_qty() == 0.0


def test_position_profit_if_up_wins():
    pos = Position(market_id="test")
    pos.up_qty = 1000.0
    pos.up_cost = 480.0  # avg 0.48
    pos.dn_qty = 1000.0
    pos.dn_cost = 490.0  # avg 0.49
    # Pi_UP = Su*(1 - Pu) - Sd*Pd = 1000*(1-0.48) - 1000*0.49 = 520 - 490 = 30
    assert pos.profit_if_up() == pytest.approx(30.0)
    assert pos.profit_if_down() == pytest.approx(30.0)


def test_market_window():
    mw = MarketWindow(
        market_id="btc-updown-15m-123",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )
    assert mw.elapsed(1500) == 500
    assert mw.remaining(1500) == 400
    assert mw.is_active(1500) is True
    assert mw.is_active(2000) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_types.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.types'`

- [ ] **Step 3: Create empty tests/__init__.py**

```python
# tests/__init__.py
```

- [ ] **Step 4: Implement types.py**

```python
# polybot/types.py
"""Shared data types for PolyBot trading engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Side(Enum):
    UP = "UP"
    DOWN = "DOWN"


class StrategyType(Enum):
    DIRECTIONAL = "DIRECTIONAL"
    SPREAD = "SPREAD"


@dataclass
class Opportunity:
    strategy: StrategyType
    market_id: str
    edge: float = 0.0
    confidence: float = 0.0
    # Directional fields
    side: Side | None = None
    price: float = 0.0
    # Spread fields
    up_price: float | None = None
    dn_price: float | None = None


@dataclass
class Position:
    market_id: str
    up_qty: float = 0.0
    up_cost: float = 0.0
    dn_qty: float = 0.0
    dn_cost: float = 0.0

    def pair_cost(self) -> float:
        """Cost per balanced pair: (total_up_cost + total_dn_cost) / min(up_qty, dn_qty)."""
        mq = self.min_qty()
        if mq <= 0:
            return 0.0
        return (self.up_cost + self.dn_cost) / mq

    def min_qty(self) -> float:
        return min(self.up_qty, self.dn_qty)

    def profit_if_up(self) -> float:
        """Pi_UP = up_qty * (1 - avg_up_price) - dn_cost."""
        if self.up_qty <= 0:
            return -self.dn_cost
        avg_up = self.up_cost / self.up_qty
        return self.up_qty * (1.0 - avg_up) - self.dn_cost

    def profit_if_down(self) -> float:
        """Pi_DOWN = dn_qty * (1 - avg_dn_price) - up_cost."""
        if self.dn_qty <= 0:
            return -self.up_cost
        avg_dn = self.dn_cost / self.dn_qty
        return self.dn_qty * (1.0 - avg_dn) - self.up_cost


@dataclass
class MarketWindow:
    market_id: str
    condition_id: str
    asset: str
    timeframe_sec: int
    up_token_id: str
    dn_token_id: str
    open_epoch: int
    close_epoch: int

    def elapsed(self, now_epoch: int) -> int:
        return max(0, now_epoch - self.open_epoch)

    def remaining(self, now_epoch: int) -> int:
        return max(0, self.close_epoch - now_epoch)

    def is_active(self, now_epoch: int) -> bool:
        return self.open_epoch <= now_epoch < self.close_epoch


@dataclass
class OrderRecord:
    order_id: str = ""
    market_id: str = ""
    side: Side = Side.UP
    price: float = 0.0
    size: float = 0.0
    filled: float = 0.0
    status: str = "pending"  # pending, open, filled, cancelled
    timestamp: float = 0.0
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_types.py -v`
Expected: All 8 tests PASS

- [ ] **Step 6: Extend config.py with BotConfig**

Add the following to the end of `polybot/config.py`:

```python
@dataclass(frozen=True)
class BotConfig:
    # Polymarket CLOB
    polymarket_host: str = "https://clob.polymarket.com"
    chain_id: int = 137
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    # Binance
    binance_ws_url: str = "wss://stream.binance.com:9443/ws"

    # Assets
    assets: tuple = ("BTC", "ETH", "SOL", "XRP")

    # Strategy thresholds (from spec Section 8.2)
    min_spread_edge: float = 0.025
    min_directional_move: float = 0.002
    max_pair_cost: float = 0.985
    max_directional_price: float = 0.93
    min_directional_price: float = 0.07
    window_min_elapsed_sec: int = 480
    position_size_fraction: float = 0.10
    stop_loss_reversal: float = 0.001

    # Risk limits
    max_concurrent_positions: int = 8
    max_capital_per_window_pct: float = 0.15
    max_daily_drawdown_pct: float = 0.05
    no_trade_final_sec: int = 60
    spread_fill_timeout_sec: int = 30
    max_book_depth_take_pct: float = 0.50

    # Polling
    poll_interval_ms: int = 500
    market_discovery_interval_sec: int = 60

    # Logging
    log_level: str = "INFO"


def load_bot_config() -> BotConfig:
    load_dotenv()
    return BotConfig(
        polymarket_host=os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com"),
        chain_id=int(os.getenv("CHAIN_ID", "137")),
        private_key=os.getenv("PRIVATE_KEY", ""),
        api_key=os.getenv("API_KEY", ""),
        api_secret=os.getenv("API_SECRET", ""),
        api_passphrase=os.getenv("API_PASSPHRASE", ""),
        binance_ws_url=os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws"),
        min_spread_edge=float(os.getenv("MIN_SPREAD_EDGE", "0.025")),
        min_directional_move=float(os.getenv("MIN_DIRECTIONAL_MOVE", "0.002")),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", "0.985")),
        max_directional_price=float(os.getenv("MAX_DIRECTIONAL_PRICE", "0.93")),
        min_directional_price=float(os.getenv("MIN_DIRECTIONAL_PRICE", "0.07")),
        window_min_elapsed_sec=int(os.getenv("WINDOW_MIN_ELAPSED_SEC", "480")),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", "0.10")),
        stop_loss_reversal=float(os.getenv("STOP_LOSS_REVERSAL", "0.001")),
        max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "8")),
        max_capital_per_window_pct=float(os.getenv("MAX_CAPITAL_PER_WINDOW_PCT", "0.15")),
        max_daily_drawdown_pct=float(os.getenv("MAX_DAILY_DRAWDOWN_PCT", "0.05")),
        no_trade_final_sec=int(os.getenv("NO_TRADE_FINAL_SEC", "60")),
        spread_fill_timeout_sec=int(os.getenv("SPREAD_FILL_TIMEOUT_SEC", "30")),
        max_book_depth_take_pct=float(os.getenv("MAX_BOOK_DEPTH_TAKE_PCT", "0.50")),
        poll_interval_ms=int(os.getenv("BOT_POLL_INTERVAL_MS", "500")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )
```

- [ ] **Step 7: Commit**

```bash
git add polybot/types.py polybot/config.py tests/__init__.py tests/test_types.py
git commit -m "feat: add shared types and BotConfig for trading engine"
```

---

## Task 2: Signal Engine

**Files:**
- Create: `polybot/signal_engine.py`
- Create: `tests/test_signal_engine.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_signal_engine.py
import pytest
from polybot.types import Side, StrategyType, MarketWindow
from polybot.signal_engine import SignalEngine
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig()


@pytest.fixture
def engine(cfg):
    return SignalEngine(cfg)


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-updown-15m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )


class TestDirectionalOpportunity:
    def test_no_signal_when_too_early(self, engine, market):
        """Before WINDOW_MIN_ELAPSED (480s), no directional signal."""
        spot_delta = 0.005  # 0.5% move — strong signal
        best_asks = {"UP": 0.80, "DOWN": 0.30}
        now = 1400  # 400s elapsed < 480s threshold
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_when_move_too_small(self, engine, market):
        """Below MIN_DIRECTIONAL_MOVE (0.2%), no signal."""
        spot_delta = 0.001  # 0.1% — too small
        best_asks = {"UP": 0.80, "DOWN": 0.30}
        now = 1600  # 600s elapsed > 480s
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_signal_up_when_positive_delta(self, engine, market):
        spot_delta = 0.003  # 0.3% up
        best_asks = {"UP": 0.85, "DOWN": 0.25}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is not None
        assert result.side == Side.UP
        assert result.price == 0.85
        assert result.strategy == StrategyType.DIRECTIONAL

    def test_signal_down_when_negative_delta(self, engine, market):
        spot_delta = -0.004
        best_asks = {"UP": 0.20, "DOWN": 0.88}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is not None
        assert result.side == Side.DOWN
        assert result.price == 0.88

    def test_no_signal_when_price_too_high(self, engine, market):
        """Price > MAX_DIRECTIONAL_PRICE (0.93) = already priced in."""
        spot_delta = 0.005
        best_asks = {"UP": 0.95, "DOWN": 0.10}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_when_price_too_low(self, engine, market):
        """Price < MIN_DIRECTIONAL_PRICE (0.07) = too uncertain."""
        spot_delta = 0.003
        best_asks = {"UP": 0.05, "DOWN": 0.96}
        now = 1600
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None

    def test_no_signal_in_final_seconds(self, engine, market):
        """Within NO_TRADE_FINAL_SEC of close, no trades."""
        spot_delta = 0.005
        best_asks = {"UP": 0.85, "DOWN": 0.25}
        now = 1850  # 50s remaining < 60s threshold
        result = engine.check_directional(market, spot_delta, best_asks, now)
        assert result is None


class TestSpreadOpportunity:
    def test_spread_detected(self, engine, market):
        best_asks = {"UP": 0.48, "DOWN": 0.49}  # T = 0.97, edge = 0.03
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is not None
        assert result.strategy == StrategyType.SPREAD
        assert result.up_price == 0.48
        assert result.dn_price == 0.49
        assert result.edge == pytest.approx(0.03)

    def test_no_spread_when_sum_too_high(self, engine, market):
        best_asks = {"UP": 0.51, "DOWN": 0.50}  # T = 1.01
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is None

    def test_no_spread_when_edge_below_minimum(self, engine, market):
        """Edge must exceed MIN_SPREAD_EDGE (0.025)."""
        best_asks = {"UP": 0.49, "DOWN": 0.50}  # T = 0.99, edge = 0.01 < 0.025
        result = engine.check_spread(market, best_asks, now_epoch=1200)
        assert result is None

    def test_no_spread_in_final_seconds(self, engine, market):
        best_asks = {"UP": 0.45, "DOWN": 0.45}
        result = engine.check_spread(market, best_asks, now_epoch=1860)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_signal_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.signal_engine'`

- [ ] **Step 3: Implement signal_engine.py**

```python
# polybot/signal_engine.py
"""Signal engine: detects directional and spread capture opportunities."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import MarketWindow, Opportunity, Side, StrategyType

logger = logging.getLogger(__name__)


class SignalEngine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def check_directional(
        self,
        market: MarketWindow,
        spot_delta: float,
        best_asks: dict[str, float],
        now_epoch: int,
    ) -> Opportunity | None:
        elapsed = market.elapsed(now_epoch)
        remaining = market.remaining(now_epoch)

        if elapsed < self.cfg.window_min_elapsed_sec:
            return None
        if remaining < self.cfg.no_trade_final_sec:
            return None
        if abs(spot_delta) < self.cfg.min_directional_move:
            return None

        if spot_delta > 0:
            side = Side.UP
            price = best_asks.get("UP", 0.0)
        else:
            side = Side.DOWN
            price = best_asks.get("DOWN", 0.0)

        if price > self.cfg.max_directional_price:
            return None
        if price < self.cfg.min_directional_price:
            return None

        return Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id=market.market_id,
            side=side,
            price=price,
            edge=1.0 - price,
            confidence=abs(spot_delta),
        )

    def check_spread(
        self,
        market: MarketWindow,
        best_asks: dict[str, float],
        now_epoch: int,
    ) -> Opportunity | None:
        remaining = market.remaining(now_epoch)
        if remaining < self.cfg.no_trade_final_sec:
            return None

        up_price = best_asks.get("UP", 1.0)
        dn_price = best_asks.get("DOWN", 1.0)
        t = up_price + dn_price
        edge = 1.0 - t

        if edge < self.cfg.min_spread_edge:
            return None

        return Opportunity(
            strategy=StrategyType.SPREAD,
            market_id=market.market_id,
            up_price=up_price,
            dn_price=dn_price,
            edge=edge,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_signal_engine.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/signal_engine.py tests/test_signal_engine.py
git commit -m "feat: add signal engine for directional and spread detection"
```

---

## Task 3: Position Manager

**Files:**
- Create: `polybot/position_manager.py`
- Create: `tests/test_position_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_position_manager.py
import pytest
from polybot.types import Side, StrategyType, Opportunity, Position
from polybot.position_manager import PositionManager
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(position_size_fraction=0.10, max_pair_cost=0.985)


@pytest.fixture
def pm(cfg):
    return PositionManager(cfg, bankroll=10_000.0)


class TestDirectionalSizing:
    def test_basic_sizing(self, pm):
        opp = Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id="m1",
            side=Side.UP,
            price=0.85,
            edge=0.15,
        )
        result = pm.compute_order_size(opp, book_depth=5000.0)
        assert result is not None
        side, qty = result
        assert side == Side.UP
        # max_capital = 10000 * 0.10 = 1000
        # qty = 1000 / 0.85 = 1176.47
        # capped at 50% of book_depth = 2500
        assert qty == pytest.approx(1176.47, rel=0.01)

    def test_capped_by_book_depth(self, pm):
        opp = Opportunity(
            strategy=StrategyType.DIRECTIONAL,
            market_id="m1",
            side=Side.DOWN,
            price=0.10,
            edge=0.90,
        )
        result = pm.compute_order_size(opp, book_depth=100.0)
        assert result is not None
        side, qty = result
        # qty = 1000 / 0.10 = 10000, but capped at 50% of 100 = 50
        assert qty == pytest.approx(50.0)


class TestSpreadSizing:
    def test_basic_spread_sizing(self, pm):
        opp = Opportunity(
            strategy=StrategyType.SPREAD,
            market_id="m1",
            up_price=0.48,
            dn_price=0.49,
            edge=0.03,
        )
        result = pm.compute_spread_size(opp)
        assert result is not None
        up_qty, dn_qty = result
        # budget_per_side = 500, qty_up = 500/0.48 = 1041.67, qty_dn = 500/0.49 = 1020.41
        # qty = min(1041.67, 1020.41) = 1020.41
        assert up_qty == pytest.approx(dn_qty)
        assert up_qty == pytest.approx(1020.41, rel=0.01)

    def test_spread_rejected_if_pair_cost_too_high(self, pm):
        """If existing position makes pair cost exceed MAX_PAIR_COST, reject."""
        # Pre-load an existing position with high pair cost
        pos = Position(market_id="m1")
        pos.up_qty = 1000.0
        pos.up_cost = 490.0
        pos.dn_qty = 1000.0
        pos.dn_cost = 500.0  # pair cost = 990/1000 = 0.99
        pm.positions["m1"] = pos

        opp = Opportunity(
            strategy=StrategyType.SPREAD,
            market_id="m1",
            up_price=0.50,
            dn_price=0.50,
            edge=0.00,  # no edge in new prices
        )
        result = pm.compute_spread_size(opp)
        assert result is None


class TestPositionTracking:
    def test_update_position_directional(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pos = pm.positions["m1"]
        assert pos.up_qty == 100.0
        assert pos.up_cost == 85.0
        assert pos.dn_qty == 0.0

    def test_update_position_accumulates(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.update_position("m1", Side.DOWN, qty=100.0, cost=49.0)
        pos = pm.positions["m1"]
        assert pos.up_qty == 100.0
        assert pos.dn_qty == 100.0
        assert pos.pair_cost() == pytest.approx(1.34)

    def test_remove_position(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.remove_position("m1")
        assert "m1" not in pm.positions

    def test_active_position_count(self, pm):
        pm.update_position("m1", Side.UP, qty=100.0, cost=85.0)
        pm.update_position("m2", Side.DOWN, qty=50.0, cost=25.0)
        assert pm.active_position_count() == 2

    def test_update_bankroll(self, pm):
        pm.update_bankroll(10_500.0)
        assert pm.bankroll == 10_500.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_position_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.position_manager'`

- [ ] **Step 3: Implement position_manager.py**

```python
# polybot/position_manager.py
"""Position manager: tracks open positions, computes sizing, manages bankroll."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import Opportunity, Position, Side, StrategyType

logger = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, cfg: BotConfig, bankroll: float):
        self.cfg = cfg
        self.bankroll = bankroll
        self.positions: dict[str, Position] = {}

    def compute_order_size(
        self,
        opp: Opportunity,
        book_depth: float,
    ) -> tuple[Side, float] | None:
        """Compute order size for a directional opportunity.

        Returns (side, quantity) or None if the trade should be skipped.
        """
        if opp.strategy != StrategyType.DIRECTIONAL or opp.side is None:
            return None

        max_capital = self.bankroll * self.cfg.position_size_fraction
        qty = max_capital / opp.price
        qty = min(qty, book_depth * self.cfg.max_book_depth_take_pct)

        if qty <= 0:
            return None

        return (opp.side, qty)

    def compute_spread_size(
        self,
        opp: Opportunity,
    ) -> tuple[float, float] | None:
        """Compute order size for a spread capture opportunity.

        Returns (up_qty, dn_qty) or None if the trade should be skipped.
        """
        if opp.strategy != StrategyType.SPREAD:
            return None
        if opp.up_price is None or opp.dn_price is None:
            return None

        max_capital = self.bankroll * self.cfg.position_size_fraction
        budget_per_side = max_capital / 2.0

        qty_up = budget_per_side / opp.up_price
        qty_dn = budget_per_side / opp.dn_price
        qty = min(qty_up, qty_dn)

        # Check pair cost with existing position
        pos = self.positions.get(opp.market_id, Position(market_id=opp.market_id))
        new_up_cost = pos.up_cost + qty * opp.up_price
        new_dn_cost = pos.dn_cost + qty * opp.dn_price
        new_min_qty = min(pos.up_qty + qty, pos.dn_qty + qty)

        if new_min_qty <= 0:
            return None

        pair_cost = (new_up_cost + new_dn_cost) / new_min_qty
        if pair_cost > self.cfg.max_pair_cost:
            logger.debug(
                "Spread rejected for %s: pair_cost=%.4f > %.4f",
                opp.market_id, pair_cost, self.cfg.max_pair_cost,
            )
            return None

        return (qty, qty)

    def update_position(self, market_id: str, side: Side, qty: float, cost: float):
        if market_id not in self.positions:
            self.positions[market_id] = Position(market_id=market_id)
        pos = self.positions[market_id]
        if side == Side.UP:
            pos.up_qty += qty
            pos.up_cost += cost
        else:
            pos.dn_qty += qty
            pos.dn_cost += cost

    def remove_position(self, market_id: str):
        self.positions.pop(market_id, None)

    def active_position_count(self) -> int:
        return len(self.positions)

    def update_bankroll(self, new_bankroll: float):
        self.bankroll = new_bankroll
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_position_manager.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/position_manager.py tests/test_position_manager.py
git commit -m "feat: add position manager with sizing and tracking"
```

---

## Task 4: Risk Manager

**Files:**
- Create: `polybot/risk_manager.py`
- Create: `tests/test_risk_manager.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_risk_manager.py
import pytest
from polybot.risk_manager import RiskManager
from polybot.config import BotConfig
from polybot.types import MarketWindow


@pytest.fixture
def cfg():
    return BotConfig(
        max_concurrent_positions=8,
        max_daily_drawdown_pct=0.05,
        no_trade_final_sec=60,
    )


@pytest.fixture
def rm(cfg):
    return RiskManager(cfg, starting_bankroll=10_000.0)


@pytest.fixture
def market():
    return MarketWindow(
        market_id="m1", condition_id="0x", asset="BTC",
        timeframe_sec=900, up_token_id="u", dn_token_id="d",
        open_epoch=1000, close_epoch=1900,
    )


class TestPositionLimits:
    def test_allows_when_below_limit(self, rm):
        assert rm.can_open_position(current_count=7) is True

    def test_blocks_when_at_limit(self, rm):
        assert rm.can_open_position(current_count=8) is False


class TestDrawdownCircuitBreaker:
    def test_not_halted_initially(self, rm):
        assert rm.is_halted() is False

    def test_halted_after_drawdown(self, rm):
        # Lost 6% → exceeds 5% limit
        rm.update_pnl(-600.0)
        assert rm.is_halted() is True

    def test_not_halted_with_small_loss(self, rm):
        rm.update_pnl(-400.0)  # 4% < 5%
        assert rm.is_halted() is False

    def test_pnl_accumulates(self, rm):
        rm.update_pnl(-300.0)
        rm.update_pnl(-300.0)  # total -600 = 6%
        assert rm.is_halted() is True


class TestWindowTiming:
    def test_allows_trade_in_valid_window(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=1600) is True

    def test_blocks_trade_in_final_seconds(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=1860) is False

    def test_blocks_trade_after_close(self, rm, market):
        assert rm.can_trade_in_window(market, now_epoch=2000) is False


class TestDailyReset:
    def test_reset_clears_daily_pnl(self, rm):
        rm.update_pnl(-600.0)
        assert rm.is_halted() is True
        rm.reset_daily()
        assert rm.is_halted() is False
        assert rm.daily_pnl == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_risk_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.risk_manager'`

- [ ] **Step 3: Implement risk_manager.py**

```python
# polybot/risk_manager.py
"""Risk manager: drawdown circuit breaker, position limits, timing gates."""

from __future__ import annotations

import logging

from polybot.config import BotConfig
from polybot.types import MarketWindow

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: BotConfig, starting_bankroll: float):
        self.cfg = cfg
        self.starting_bankroll = starting_bankroll
        self.daily_pnl: float = 0.0

    def can_open_position(self, current_count: int) -> bool:
        return current_count < self.cfg.max_concurrent_positions

    def is_halted(self) -> bool:
        max_loss = self.starting_bankroll * self.cfg.max_daily_drawdown_pct
        return self.daily_pnl <= -max_loss

    def update_pnl(self, amount: float):
        self.daily_pnl += amount
        if self.is_halted():
            logger.warning(
                "CIRCUIT BREAKER: daily PnL %.2f exceeds -%.1f%% of %.2f",
                self.daily_pnl,
                self.cfg.max_daily_drawdown_pct * 100,
                self.starting_bankroll,
            )

    def can_trade_in_window(self, market: MarketWindow, now_epoch: int) -> bool:
        if not market.is_active(now_epoch):
            return False
        return market.remaining(now_epoch) >= self.cfg.no_trade_final_sec

    def reset_daily(self):
        self.daily_pnl = 0.0
        logger.info("Daily PnL reset")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_risk_manager.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/risk_manager.py tests/test_risk_manager.py
git commit -m "feat: add risk manager with drawdown circuit breaker"
```

---

## Task 5: Market Discovery

**Files:**
- Create: `polybot/market_discovery.py`
- Create: `tests/test_market_discovery.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_market_discovery.py
import pytest
from polybot.market_discovery import parse_market_to_window, is_crypto_updown_market


def test_is_crypto_updown_market_btc_15m():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up or down in the next 15 minutes?",
        "tokens": [
            {"token_id": "tok_up", "outcome": "Up"},
            {"token_id": "tok_dn", "outcome": "Down"},
        ],
        "end_date_iso": "2026-03-17T15:00:00Z",
        "game_start_time": "2026-03-17T14:45:00Z",
    }
    assert is_crypto_updown_market(market, assets=("BTC", "ETH", "SOL", "XRP"))


def test_is_not_crypto_updown_for_political():
    market = {
        "condition_id": "0xdef",
        "question": "Will Trump win the 2028 election?",
        "tokens": [
            {"token_id": "tok_yes", "outcome": "Yes"},
            {"token_id": "tok_no", "outcome": "No"},
        ],
    }
    assert not is_crypto_updown_market(market, assets=("BTC", "ETH", "SOL", "XRP"))


def test_parse_market_to_window():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up or down?",
        "tokens": [
            {"token_id": "tok_up", "outcome": "Up"},
            {"token_id": "tok_dn", "outcome": "Down"},
        ],
        "end_date_iso": "2026-03-17T15:00:00Z",
        "game_start_time": "2026-03-17T14:45:00Z",
    }
    mw = parse_market_to_window(market, "btc-updown-15m-123")
    assert mw is not None
    assert mw.asset == "BTC"
    assert mw.up_token_id == "tok_up"
    assert mw.dn_token_id == "tok_dn"
    assert mw.timeframe_sec == 900


def test_parse_market_missing_tokens():
    market = {
        "condition_id": "0xabc",
        "question": "Will Bitcoin go up?",
        "tokens": [{"token_id": "tok_up", "outcome": "Up"}],  # missing Down
    }
    mw = parse_market_to_window(market, "btc-updown-15m-123")
    assert mw is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_market_discovery.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.market_discovery'`

- [ ] **Step 3: Implement market_discovery.py**

```python
# polybot/market_discovery.py
"""Market discovery: find active crypto up/down markets and extract token IDs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from polybot.types import MarketWindow

logger = logging.getLogger(__name__)

_ASSET_KEYWORDS = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
}


def _extract_asset(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, symbol in _ASSET_KEYWORDS.items():
        if keyword in text_lower:
            return symbol
    return None


def is_crypto_updown_market(market: dict, assets: tuple[str, ...]) -> bool:
    question = market.get("question", "").lower()
    if "up" not in question or "down" not in question:
        return False
    asset = _extract_asset(question)
    if asset is None or asset not in assets:
        return False
    tokens = market.get("tokens", [])
    outcomes = {t.get("outcome", "").lower() for t in tokens}
    return "up" in outcomes and "down" in outcomes


def _parse_iso_epoch(iso_str: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0


def parse_market_to_window(market: dict, slug: str) -> MarketWindow | None:
    question = market.get("question", "")
    asset = _extract_asset(question)
    if asset is None:
        return None

    tokens = market.get("tokens", [])
    up_token = None
    dn_token = None
    for t in tokens:
        outcome = t.get("outcome", "").lower()
        if outcome == "up":
            up_token = t.get("token_id", "")
        elif outcome == "down":
            dn_token = t.get("token_id", "")

    if not up_token or not dn_token:
        return None

    open_epoch = _parse_iso_epoch(market.get("game_start_time", ""))
    close_epoch = _parse_iso_epoch(market.get("end_date_iso", ""))
    timeframe_sec = close_epoch - open_epoch if close_epoch > open_epoch else 900

    return MarketWindow(
        market_id=slug,
        condition_id=market.get("condition_id", ""),
        asset=asset,
        timeframe_sec=timeframe_sec,
        up_token_id=up_token,
        dn_token_id=dn_token,
        open_epoch=open_epoch,
        close_epoch=close_epoch,
    )


def _discover_sync(client, assets: tuple[str, ...]) -> list[MarketWindow]:
    """Synchronous helper — runs in a thread to avoid blocking the event loop."""
    windows = []
    markets_resp = client.get_markets()
    markets = markets_resp.get("data", []) if isinstance(markets_resp, dict) else []
    for m in markets:
        if not is_crypto_updown_market(m, assets):
            continue
        slug = m.get("question_id", m.get("condition_id", ""))
        mw = parse_market_to_window(m, slug)
        if mw is not None:
            windows.append(mw)
    return windows


async def discover_active_markets(
    client,
    assets: tuple[str, ...],
) -> list[MarketWindow]:
    """Fetch active crypto up/down markets from Polymarket.

    Runs the synchronous CLOB client call in a thread pool to avoid
    blocking the async event loop (Binance WS must keep receiving).
    """
    import asyncio
    try:
        return await asyncio.to_thread(_discover_sync, client, assets)
    except Exception as e:
        logger.error("Market discovery failed: %s", e)
        return []
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_market_discovery.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/market_discovery.py tests/test_market_discovery.py
git commit -m "feat: add market discovery for crypto up/down markets"
```

---

## Task 6: Order Executor

**Files:**
- Create: `polybot/order_executor.py`
- Create: `tests/test_order_executor.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_order_executor.py
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from polybot.order_executor import OrderExecutor
from polybot.types import Side, OrderRecord
from polybot.config import BotConfig


@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
    )


@pytest.fixture
def mock_clob():
    client = MagicMock()
    client.create_order.return_value = {"signed": True}
    client.post_order.return_value = {"orderID": "order-123", "status": "matched"}
    client.cancel.return_value = {"cancelled": True}
    client.get_order_book.return_value = MagicMock(
        bids=[MagicMock(price="0.45", size="1000")],
        asks=[MagicMock(price="0.55", size="500")],
    )
    return client


@pytest.fixture
def executor(cfg, mock_clob):
    return OrderExecutor(cfg, clob_client=mock_clob)


class TestPlaceOrder:
    def test_place_limit_buy(self, executor, mock_clob):
        record = executor.place_limit_buy(
            token_id="tok_up",
            price=0.85,
            size=100.0,
            market_id="m1",
            side=Side.UP,
        )
        assert record.order_id == "order-123"
        assert record.status == "matched"
        mock_clob.create_order.assert_called_once()
        mock_clob.post_order.assert_called_once()

    def test_place_limit_buy_handles_error(self, executor, mock_clob):
        mock_clob.post_order.side_effect = Exception("API error")
        record = executor.place_limit_buy(
            token_id="tok_up", price=0.85, size=100.0,
            market_id="m1", side=Side.UP,
        )
        assert record.status == "error"


class TestCancelOrder:
    def test_cancel_order(self, executor, mock_clob):
        result = executor.cancel_order("order-123")
        assert result is True
        mock_clob.cancel.assert_called_once_with("order-123")

    def test_cancel_order_handles_error(self, executor, mock_clob):
        mock_clob.cancel.side_effect = Exception("not found")
        result = executor.cancel_order("bad-id")
        assert result is False


class TestOrderBook:
    def test_get_best_asks(self, executor, mock_clob):
        bids, asks = executor.get_book_summary("tok_up")
        assert asks[0] == ("0.55", "500")
        assert bids[0] == ("0.45", "1000")

    def test_get_book_depth_at_price(self, executor):
        depth = executor.get_book_depth_at_price("tok_up", 0.60)
        # Mock returns asks with size 500 at 0.55 (below 0.60), so depth = 500
        assert depth >= 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_order_executor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.order_executor'`

- [ ] **Step 3: Implement order_executor.py**

```python
# polybot/order_executor.py
"""Order executor: places, cancels, and monitors orders via py-clob-client.

All public methods are synchronous. The Bot's async trading loop calls them
via asyncio.to_thread() to avoid blocking the event loop.
"""

from __future__ import annotations

import logging
import time

from py_clob_client.clob_types import OrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

from polybot.config import BotConfig
from polybot.types import OrderRecord, Side

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, cfg: BotConfig, clob_client):
        self.cfg = cfg
        self.client = clob_client

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        market_id: str,
        side: Side,
    ) -> OrderRecord:
        record = OrderRecord(
            market_id=market_id,
            side=side,
            price=price,
            size=size,
            timestamp=time.time(),
        )
        try:
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)

            record.order_id = resp.get("orderID", "")
            record.status = resp.get("status", "unknown")

            logger.info(
                "ORDER PLACED: %s %s %.2f x %.1f on %s → %s",
                side.value, token_id[:16], price, size, market_id, record.status,
            )
        except Exception as e:
            record.status = "error"
            logger.error("Order placement failed: %s", e)

        return record

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            logger.info("ORDER CANCELLED: %s", order_id)
            return True
        except Exception as e:
            logger.error("Cancel failed for %s: %s", order_id, e)
            return False

    def cancel_all(self) -> bool:
        try:
            self.client.cancel_all()
            logger.info("ALL ORDERS CANCELLED")
            return True
        except Exception as e:
            logger.error("Cancel all failed: %s", e)
            return False

    def get_book_summary(
        self, token_id: str
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Returns (bids, asks) as lists of (price, size) tuples."""
        try:
            book = self.client.get_order_book(token_id)
            bids = [(b.price, b.size) for b in book.bids]
            asks = [(a.price, a.size) for a in book.asks]
            return bids, asks
        except Exception as e:
            logger.error("Order book fetch failed for %s: %s", token_id[:16], e)
            return [], []

    def get_book_depth_at_price(self, token_id: str, max_price: float) -> float:
        """Total ask-side size available at or below max_price."""
        try:
            book = self.client.get_order_book(token_id)
            depth = 0.0
            for ask in book.asks:
                if float(ask.price) <= max_price:
                    depth += float(ask.size)
            return depth
        except Exception as e:
            logger.error("Book depth fetch failed: %s", e)
            return 0.0

    def get_best_ask(self, token_id: str) -> float:
        try:
            book = self.client.get_order_book(token_id)
            if book.asks:
                return float(book.asks[0].price)
        except Exception as e:
            logger.error("Best ask fetch failed: %s", e)
        return 1.0  # Return 1.0 (no edge) as safe default
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_order_executor.py -v`
Expected: All 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/order_executor.py tests/test_order_executor.py
git commit -m "feat: add order executor for CLOB limit orders"
```

---

## Task 7: Main Bot Loop

**Files:**
- Create: `polybot/bot.py`
- Create: `tests/test_bot_integration.py`

- [ ] **Step 1: Write failing integration test**

```python
# tests/test_bot_integration.py
import pytest
from unittest.mock import MagicMock
from polybot.bot import Bot, compute_fee
from polybot.config import BotConfig
from polybot.types import MarketWindow, Side, Position


@pytest.fixture
def cfg():
    return BotConfig(
        private_key="0xfake",
        api_key="key",
        api_secret="secret",
        api_passphrase="pass",
        poll_interval_ms=100,
    )


@pytest.fixture
def market():
    return MarketWindow(
        market_id="btc-updown-15m-100",
        condition_id="0xabc",
        asset="BTC",
        timeframe_sec=900,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=1000,
        close_epoch=1900,
    )


class TestFeeComputation:
    def test_fee_at_midprice(self):
        # fee = 0.02 * min(0.50, 0.50) = 0.01
        assert compute_fee(0.50) == pytest.approx(0.01)

    def test_fee_at_high_price(self):
        # fee = 0.02 * min(0.90, 0.10) = 0.002
        assert compute_fee(0.90) == pytest.approx(0.002)

    def test_fee_at_low_price(self):
        # fee = 0.02 * min(0.10, 0.90) = 0.002
        assert compute_fee(0.10) == pytest.approx(0.002)


class TestBotEvaluateMarket:
    def test_directional_trade_executed(self, cfg, market):
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "matched"}
        mock_clob.get_order_book.return_value = MagicMock(
            bids=[MagicMock(price="0.10", size="5000")],
            asks=[MagicMock(price="0.85", size="5000")],
        )

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 84800.0  # +0.24% delta

        actions = bot.evaluate_market(market, now_epoch=1600)
        # Price 0.85 → edge = 0.15, fee = 0.02*0.15 = 0.003, net = 0.147 > 0
        assert len(actions) >= 1
        assert actions[0]["type"] == "directional"
        assert actions[0]["side"] == Side.UP

    def test_no_trade_when_halted(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.risk_manager.update_pnl(-600.0)  # trigger circuit breaker

        actions = bot.evaluate_market(market, now_epoch=1600)
        assert len(actions) == 0

    def test_spread_trade_when_no_directional(self, cfg, market):
        mock_clob = MagicMock()
        mock_clob.create_order.return_value = {"signed": True}
        mock_clob.post_order.return_value = {"orderID": "o1", "status": "matched"}

        book_up = MagicMock(
            bids=[MagicMock(price="0.44", size="1000")],
            asks=[MagicMock(price="0.46", size="1000")],
        )
        book_dn = MagicMock(
            bids=[MagicMock(price="0.46", size="1000")],
            asks=[MagicMock(price="0.48", size="1000")],
        )
        mock_clob.get_order_book.side_effect = lambda tid: (
            book_up if tid == "tok_up" else book_dn
        )

        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 85000.0  # 0% delta — no directional

        actions = bot.evaluate_market(market, now_epoch=1200)
        # T = 0.46 + 0.48 = 0.94, edge = 0.06, fee worst = 0.01, net = 0.05 > 0
        assert any(a["type"] == "spread" for a in actions)

    def test_position_limit_respected(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        for i in range(8):
            bot.position_manager.update_position(f"m{i}", Side.UP, 100.0, 50.0)

        bot.spot_prices["BTC"] = 85000.0
        bot.window_open_prices["BTC"] = 84800.0

        actions = bot.evaluate_market(market, now_epoch=1600)
        assert len(actions) == 0


class TestWindowOpenPriceSnapshot:
    def test_snapshot_captures_spot_price(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()

        assert bot.window_open_prices["BTC"] == 84500.0
        assert market.market_id in bot._snapped_windows

    def test_snapshot_not_overwritten_on_second_call(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        bot.active_markets = [market]
        bot.spot_prices["BTC"] = 84500.0

        bot._snapshot_window_open_prices()
        bot.spot_prices["BTC"] = 85000.0  # price changed
        bot._snapshot_window_open_prices()

        # Should still be the original snapshot
        assert bot.window_open_prices["BTC"] == 84500.0


class TestSettlement:
    def test_settlement_updates_bankroll(self, cfg, market):
        mock_clob = MagicMock()
        bot = Bot(cfg, clob_client=mock_clob, initial_bankroll=10_000.0)
        # Simulate a spread position: bought both sides below $1
        bot.position_manager.update_position(
            market.market_id, Side.UP, qty=100.0, cost=48.0,
        )
        bot.position_manager.update_position(
            market.market_id, Side.DOWN, qty=100.0, cost=49.0,
        )
        # Set spot delta positive → UP wins
        bot.spot_prices["BTC"] = 85200.0
        bot.window_open_prices["BTC"] = 85000.0

        pos = bot.position_manager.positions[market.market_id]
        pnl_up = pos.profit_if_up()  # 100*(1-0.48) - 49 = 52 - 49 = 3
        assert pnl_up == pytest.approx(3.0)

        # After settlement, bankroll should increase
        initial = bot.position_manager.bankroll
        # Manually call the settlement logic (extracted from run_trading_loop)
        bot.active_markets = [market]
        spot_delta = bot.compute_spot_delta(market.asset)
        assert spot_delta > 0
        pnl = pos.profit_if_up()
        bot.position_manager.update_bankroll(initial + pnl)
        bot.risk_manager.update_pnl(pnl)
        bot.position_manager.remove_position(market.market_id)

        assert bot.position_manager.bankroll == pytest.approx(10_003.0)
        assert bot.risk_manager.daily_pnl == pytest.approx(3.0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_bot_integration.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'polybot.bot'`

- [ ] **Step 3: Implement bot.py**

```python
# polybot/bot.py
"""Main bot: wires signal engine, position manager, risk manager, and order executor.

All synchronous CLOB client calls (order placement, book queries) are dispatched
via asyncio.to_thread() so the Binance WebSocket feed is never blocked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import websockets

from polybot.config import BotConfig
from polybot.market_discovery import discover_active_markets
from polybot.order_executor import OrderExecutor
from polybot.position_manager import PositionManager
from polybot.risk_manager import RiskManager
from polybot.signal_engine import SignalEngine
from polybot.types import MarketWindow, Side, StrategyType

logger = logging.getLogger(__name__)

# Fee model: fee = baseRate * min(P, 1-P). Polymarket 15m crypto markets.
FEE_BASE_RATE = 0.02


def compute_fee(price: float) -> float:
    """Compute per-share fee using Polymarket's fee schedule."""
    return FEE_BASE_RATE * min(price, 1.0 - price)


class Bot:
    def __init__(self, cfg: BotConfig, clob_client, initial_bankroll: float):
        self.cfg = cfg
        self.clob_client = clob_client
        self.signal_engine = SignalEngine(cfg)
        self.position_manager = PositionManager(cfg, bankroll=initial_bankroll)
        self.risk_manager = RiskManager(cfg, starting_bankroll=initial_bankroll)
        self.order_executor = OrderExecutor(cfg, clob_client=clob_client)

        self.spot_prices: dict[str, float] = {}
        self.window_open_prices: dict[str, float] = {}
        self.active_markets: list[MarketWindow] = []
        # Track which windows we've already snapshot open prices for
        self._snapped_windows: set[str] = set()

    def compute_spot_delta(self, asset: str) -> float:
        current = self.spot_prices.get(asset, 0.0)
        open_price = self.window_open_prices.get(asset, 0.0)
        if open_price <= 0:
            return 0.0
        return (current - open_price) / open_price

    def _snapshot_window_open_prices(self):
        """Capture spot prices at the start of each new market window.

        Called on each trading loop tick. For each active market, if we
        haven't yet recorded the open price for that window, snapshot it
        from the current Binance spot feed.
        """
        for market in self.active_markets:
            if market.market_id not in self._snapped_windows:
                spot = self.spot_prices.get(market.asset, 0.0)
                if spot > 0:
                    self.window_open_prices[market.asset] = spot
                    self._snapped_windows.add(market.market_id)
                    logger.debug(
                        "Window open snapshot: %s = $%.2f for %s",
                        market.asset, spot, market.market_id,
                    )

    def _cleanup_expired_windows(self, now_epoch: int):
        """Remove expired window IDs from the snapshot tracker."""
        expired = [
            m.market_id for m in self.active_markets
            if not m.is_active(now_epoch)
        ]
        for mid in expired:
            self._snapped_windows.discard(mid)

    def evaluate_market(
        self, market: MarketWindow, now_epoch: int
    ) -> list[dict]:
        """Evaluate a single market for trading opportunities. Returns list of actions taken."""
        actions = []

        if self.risk_manager.is_halted():
            return actions
        if not self.risk_manager.can_trade_in_window(market, now_epoch):
            return actions
        if not self.risk_manager.can_open_position(
            self.position_manager.active_position_count()
        ):
            return actions

        spot_delta = self.compute_spot_delta(market.asset)

        # Fetch best asks for both sides
        best_ask_up = self.order_executor.get_best_ask(market.up_token_id)
        best_ask_dn = self.order_executor.get_best_ask(market.dn_token_id)
        best_asks = {"UP": best_ask_up, "DOWN": best_ask_dn}

        # Priority 1: Directional (latency arb)
        dir_opp = self.signal_engine.check_directional(
            market, spot_delta, best_asks, now_epoch
        )
        if dir_opp is not None:
            # Check fee-adjusted edge (Fix #9)
            fee = compute_fee(dir_opp.price)
            net_edge = dir_opp.edge - fee
            if net_edge <= 0:
                logger.debug("Directional edge %.4f wiped by fee %.4f", dir_opp.edge, fee)
                return actions

            token_id = (
                market.up_token_id
                if dir_opp.side == Side.UP
                else market.dn_token_id
            )
            book_depth = self.order_executor.get_book_depth_at_price(
                token_id, dir_opp.price
            )
            sizing = self.position_manager.compute_order_size(dir_opp, book_depth)
            if sizing is not None:
                side, qty = sizing
                record = self.order_executor.place_limit_buy(
                    token_id=token_id,
                    price=dir_opp.price,
                    size=qty,
                    market_id=market.market_id,
                    side=side,
                )
                if record.status != "error":
                    cost = qty * dir_opp.price
                    self.position_manager.update_position(
                        market.market_id, side, qty, cost
                    )
                    actions.append({
                        "type": "directional",
                        "side": side,
                        "price": dir_opp.price,
                        "qty": qty,
                        "order_id": record.order_id,
                    })
            return actions  # Don't also do spread if directional fires

        # Priority 2: Spread capture
        spread_opp = self.signal_engine.check_spread(market, best_asks, now_epoch)
        if spread_opp is not None:
            # Check fee-adjusted edge (Fix #9)
            # For spread, fee applies to the winning side; worst case is mid-price
            worst_fee = compute_fee(0.50)
            net_edge = spread_opp.edge - worst_fee
            if net_edge <= 0:
                logger.debug("Spread edge %.4f wiped by fee %.4f", spread_opp.edge, worst_fee)
                return actions

            sizing = self.position_manager.compute_spread_size(spread_opp)
            if sizing is not None:
                up_qty, dn_qty = sizing
                record_up = self.order_executor.place_limit_buy(
                    token_id=market.up_token_id,
                    price=spread_opp.up_price,
                    size=up_qty,
                    market_id=market.market_id,
                    side=Side.UP,
                )
                record_dn = self.order_executor.place_limit_buy(
                    token_id=market.dn_token_id,
                    price=spread_opp.dn_price,
                    size=dn_qty,
                    market_id=market.market_id,
                    side=Side.DOWN,
                )
                if record_up.status != "error":
                    self.position_manager.update_position(
                        market.market_id, Side.UP, up_qty,
                        up_qty * spread_opp.up_price,
                    )
                if record_dn.status != "error":
                    self.position_manager.update_position(
                        market.market_id, Side.DOWN, dn_qty,
                        dn_qty * spread_opp.dn_price,
                    )
                actions.append({
                    "type": "spread",
                    "up_price": spread_opp.up_price,
                    "dn_price": spread_opp.dn_price,
                    "qty": up_qty,
                })

        return actions

    async def run_binance_ws(self):
        """Connect to Binance combined stream for real-time spot prices."""
        streams = [f"{a.lower()}usdt@ticker" for a in self.cfg.assets]
        # Binance combined stream endpoint (Fix #7)
        base = self.cfg.binance_ws_url.replace("/ws", "/stream")
        url = f"{base}?streams={'/'.join(streams)}"

        while True:
            try:
                async with websockets.connect(url) as ws:
                    logger.info("Binance WS connected: %s", url)
                    async for msg in ws:
                        data = json.loads(msg)
                        # Combined stream wraps payload in {"stream": ..., "data": ...}
                        payload = data.get("data", data)
                        if "s" in payload and "c" in payload:
                            symbol = payload["s"].replace("USDT", "")
                            self.spot_prices[symbol] = float(payload["c"])
            except Exception as e:
                logger.warning("Binance WS error: %s, reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def run_market_discovery(self):
        """Periodically discover active crypto up/down markets."""
        while True:
            try:
                self.active_markets = await discover_active_markets(
                    self.clob_client, self.cfg.assets
                )
                logger.info("Discovered %d active markets", len(self.active_markets))
            except Exception as e:
                logger.error("Market discovery error: %s", e)
            await asyncio.sleep(self.cfg.market_discovery_interval_sec)

    async def run_trading_loop(self):
        """Main trading loop: evaluate all active markets on each tick.

        Synchronous CLOB calls inside evaluate_market() are dispatched
        via asyncio.to_thread() so the Binance WS stays responsive.
        """
        while True:
            now = int(time.time())

            # Snapshot open prices for new windows (Fix #6)
            self._snapshot_window_open_prices()

            for market in self.active_markets:
                if not market.is_active(now):
                    continue
                try:
                    # Run the synchronous evaluate in a thread (Fix #4)
                    actions = await asyncio.to_thread(
                        self.evaluate_market, market, now
                    )
                    for action in actions:
                        logger.info("ACTION: %s on %s", action, market.market_id)
                except Exception as e:
                    logger.error(
                        "Error evaluating %s: %s", market.market_id, e
                    )

            # Handle settlement (Fix #3): estimate PnL and update bankroll
            for market in list(self.active_markets):
                now = int(time.time())
                if not market.is_active(now) and market.market_id in self.position_manager.positions:
                    pos = self.position_manager.positions[market.market_id]
                    # Determine which side won from spot delta
                    spot_delta = self.compute_spot_delta(market.asset)
                    if spot_delta > 0:
                        pnl = pos.profit_if_up()
                    elif spot_delta < 0:
                        pnl = pos.profit_if_down()
                    else:
                        # Ambiguous — use worst case for safety
                        pnl = min(pos.profit_if_up(), pos.profit_if_down())

                    logger.info(
                        "SETTLEMENT: %s — up_qty=%.1f dn_qty=%.1f pnl=$%.2f",
                        market.market_id, pos.up_qty, pos.dn_qty, pnl,
                    )
                    # Update bankroll and daily PnL
                    self.position_manager.update_bankroll(
                        self.position_manager.bankroll + pnl
                    )
                    self.risk_manager.update_pnl(pnl)
                    self.position_manager.remove_position(market.market_id)

            # Cleanup expired window snapshots
            self._cleanup_expired_windows(now)

            # Stop-loss check (Fix #8): for directional positions, check reversal
            for market in self.active_markets:
                if not market.is_active(int(time.time())):
                    continue
                if market.market_id not in self.position_manager.positions:
                    continue
                pos = self.position_manager.positions[market.market_id]
                # Only applies to directional (single-sided) positions
                is_directional = (pos.up_qty > 0) != (pos.dn_qty > 0)
                if not is_directional:
                    continue
                spot_delta = self.compute_spot_delta(market.asset)
                holding_up = pos.up_qty > 0
                # Reversal: we hold UP but price went negative, or hold DOWN but price went positive
                if holding_up and spot_delta < -self.cfg.stop_loss_reversal:
                    logger.warning(
                        "STOP LOSS: %s — holding UP but delta=%.4f, selling",
                        market.market_id, spot_delta,
                    )
                    # Cancel any open orders and exit position
                    self.position_manager.remove_position(market.market_id)
                elif not holding_up and spot_delta > self.cfg.stop_loss_reversal:
                    logger.warning(
                        "STOP LOSS: %s — holding DOWN but delta=+%.4f, selling",
                        market.market_id, spot_delta,
                    )
                    self.position_manager.remove_position(market.market_id)

            await asyncio.sleep(self.cfg.poll_interval_ms / 1000.0)

    async def run(self):
        """Start all concurrent tasks."""
        logger.info(
            "Bot starting — bankroll: $%.2f, assets: %s",
            self.position_manager.bankroll, self.cfg.assets,
        )
        tasks = [
            asyncio.create_task(self.run_binance_ws()),
            asyncio.create_task(self.run_market_discovery()),
            asyncio.create_task(self.run_trading_loop()),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            self.order_executor.cancel_all()
            for t in tasks:
                t.cancel()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/test_bot_integration.py -v`
Expected: All 11 tests PASS

- [ ] **Step 5: Commit**

```bash
git add polybot/bot.py tests/test_bot_integration.py
git commit -m "feat: add main bot loop with strategy evaluation"
```

---

## Task 8: Bot Entry Point and .env Update

**Files:**
- Create: `run_bot.py`
- Modify: `.env.example`

- [ ] **Step 1: Create run_bot.py entry point**

```python
#!/usr/bin/env python3
"""PolyBot Trading Engine — Entry Point.

Usage:
    python run_bot.py

Requires PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env
"""

import asyncio
import logging
import sys

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from polybot.config import load_bot_config
from polybot.bot import Bot


def create_clob_client(cfg):
    client = ClobClient(
        cfg.polymarket_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )
    return client


def main():
    cfg = load_bot_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not cfg.private_key:
        print("ERROR: PRIVATE_KEY not set in .env — cannot trade without it.")
        sys.exit(1)

    clob_client = create_clob_client(cfg)

    # Query initial balance
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        balance_info = clob_client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        bankroll = float(balance_info.get("balance", 0)) / 1e6  # USDC has 6 decimals
        print(f"Starting bankroll: ${bankroll:,.2f} USDC")
    except Exception as e:
        print(f"Could not fetch balance ({e}), using default $1000")
        bankroll = 1000.0

    bot = Bot(cfg, clob_client=clob_client, initial_bankroll=bankroll)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nBot stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Update .env.example with bot config variables**

Append to `.env.example`:

```bash

# ===== BOT CONFIG (Phase 1 — Trading Engine) =====
# Strategy thresholds
MIN_SPREAD_EDGE=0.025
MIN_DIRECTIONAL_MOVE=0.002
MAX_PAIR_COST=0.985
MAX_DIRECTIONAL_PRICE=0.93
MIN_DIRECTIONAL_PRICE=0.07
WINDOW_MIN_ELAPSED_SEC=480
POSITION_SIZE_FRACTION=0.10
STOP_LOSS_REVERSAL=0.001

# Risk limits
MAX_CONCURRENT_POSITIONS=8
MAX_DAILY_DRAWDOWN_PCT=0.05
NO_TRADE_FINAL_SEC=60

# Bot polling
BOT_POLL_INTERVAL_MS=500
```

- [ ] **Step 3: Run full test suite**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/ -v`
Expected: All tests PASS (58 total across all test files)

- [ ] **Step 4: Commit**

```bash
git add run_bot.py .env.example
git commit -m "feat: add bot entry point and configurable strategy params"
```

---

## Task 9: Dry-Run Mode and Safety Gate

**Files:**
- Modify: `polybot/bot.py`
- Modify: `polybot/config.py`
- Modify: `run_bot.py`

This task adds a `DRY_RUN=true` mode that logs all decisions but does not place real orders. This is critical for safe testing with real market data before going live.

- [ ] **Step 1: Add dry_run flag to BotConfig**

In `polybot/config.py`, add to `BotConfig`:

```python
    # Safety
    dry_run: bool = True  # Default to dry run — must explicitly disable
```

And in `load_bot_config()`:

```python
        dry_run=os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes"),
```

- [ ] **Step 2: Add dry-run guard to OrderExecutor.place_limit_buy**

In `polybot/order_executor.py`, at the start of `place_limit_buy`:

```python
        if self.cfg.dry_run:
            logger.info(
                "DRY RUN: would buy %s %.2f x %.1f on %s",
                side.value, price, size, market_id,
            )
            record.order_id = f"dry-{int(time.time())}"
            record.status = "dry_run"
            return record
```

- [ ] **Step 3: Add startup confirmation in run_bot.py**

After bankroll is determined, before creating the Bot:

```python
    if not cfg.dry_run:
        print("\n⚠  LIVE TRADING MODE — real orders will be placed!")
        print(f"   Bankroll: ${bankroll:,.2f}")
        print(f"   Max position size: ${bankroll * cfg.position_size_fraction:,.2f}")
        confirm = input("   Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            sys.exit(0)
    else:
        print("Running in DRY RUN mode — no real orders will be placed.")
```

- [ ] **Step 4: Update .env.example**

Add to `.env.example`:

```bash
# Safety — set to false ONLY when ready for live trading
DRY_RUN=true
```

- [ ] **Step 5: Run full test suite**

Run: `cd /c/Users/pc/Desktop/PolyBot && python -m pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add polybot/config.py polybot/order_executor.py polybot/bot.py run_bot.py .env.example
git commit -m "feat: add dry-run safety mode (default on)"
```

---

## Execution Order Summary

| Task | Component | Tests | Depends On |
|------|-----------|-------|------------|
| 1 | Types + BotConfig | 8 | — |
| 2 | Signal Engine | 11 | Task 1 |
| 3 | Position Manager | 9 | Task 1 |
| 4 | Risk Manager | 10 | Task 1 |
| 5 | Market Discovery | 4 | Task 1 |
| 6 | Order Executor | 6 | Task 1 |
| 7 | Main Bot Loop | 10 | Tasks 2-6 |
| 8 | Entry Point + Config | 0 (manual) | Task 7 |
| 9 | Dry-Run Safety | 0 (modifies existing) | Task 8 |

**Total automated tests: 58**

Tasks 2-6 are independent of each other and can be implemented in any order (or in parallel). Task 7 integrates them. Tasks 8-9 are final wiring.
