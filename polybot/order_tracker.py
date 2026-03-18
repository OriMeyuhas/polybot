"""Order tracker: registry of all resting/filled/cancelled orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from polybot.types import Side


@dataclass
class TrackedOrder:
    order_id: str
    market_id: str
    token_id: str
    side: Side
    price: float
    size: float
    filled: float = 0.0
    status: str = "resting"  # resting, partial, filled, cancelled
    placed_at: float = 0.0


class OrderTracker:
    def __init__(self):
        self.orders: dict[str, TrackedOrder] = {}
        self._by_market: dict[str, list[str]] = {}

    def add(self, order: TrackedOrder) -> None:
        self.orders[order.order_id] = order
        self._by_market.setdefault(order.market_id, []).append(order.order_id)

    def update_fill(self, order_id: str, filled_qty: float) -> None:
        order = self.orders.get(order_id)
        if order is None:
            return
        order.filled += filled_qty
        if order.filled >= order.size - 0.001:
            order.filled = order.size
            order.status = "filled"
        else:
            order.status = "partial"

    def cancel(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if order is not None:
            order.status = "cancelled"

    def cancel_market(self, market_id: str) -> list[str]:
        """Cancel all resting/partial orders for a market. Returns cancelled order IDs."""
        cancelled = []
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.status in ("resting", "partial"):
                order.status = "cancelled"
                cancelled.append(oid)
        return cancelled

    def cancel_side(self, market_id: str, side: Side) -> list[str]:
        """Cancel all resting/partial orders for one side of a market."""
        cancelled = []
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.side == side and order.status in ("resting", "partial"):
                order.status = "cancelled"
                cancelled.append(oid)
        return cancelled

    def get_resting(self, market_id: str) -> list[TrackedOrder]:
        return [
            self.orders[oid]
            for oid in self._by_market.get(market_id, [])
            if oid in self.orders and self.orders[oid].status in ("resting", "partial")
        ]

    def get_resting_side(self, market_id: str, side: Side) -> list[TrackedOrder]:
        return [
            o for o in self.get_resting(market_id) if o.side == side
        ]

    def get_filled(self, market_id: str) -> list[TrackedOrder]:
        return [
            self.orders[oid]
            for oid in self._by_market.get(market_id, [])
            if oid in self.orders
            and self.orders[oid].status in ("partial", "filled")
            and self.orders[oid].filled > 0
        ]

    def filled_qty(self, market_id: str, side: Side) -> float:
        total = 0.0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.filled > 0:
                total += o.filled
        return total

    def filled_cost(self, market_id: str, side: Side) -> float:
        total = 0.0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.filled > 0:
                total += o.filled * o.price
        return total

    def has_orders(self, market_id: str) -> bool:
        return len(self.get_resting(market_id)) > 0

    def all_resting_ids(self) -> set[str]:
        return {
            oid for oid, o in self.orders.items()
            if o.status in ("resting", "partial")
        }

    def cleanup_market(self, market_id: str) -> None:
        """Remove all orders for a settled/expired market."""
        for oid in self._by_market.pop(market_id, []):
            self.orders.pop(oid, None)
