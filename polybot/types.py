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


@dataclass
class ActivityEvent:
    timestamp: float
    event_type: str  # LADDER, FILL, SETTLE, CANCEL, HEARTBEAT_LOST
    asset: str
    detail: str
    pnl: float | None = None
