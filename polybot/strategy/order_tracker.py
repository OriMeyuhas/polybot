"""Order tracker: registry of all resting/filled/cancelled orders."""

from __future__ import annotations

from dataclasses import dataclass, field
from polybot.fees import compute_fee
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
    status: str = "resting"  # resting, partial, filled, cancelled, cancelling
    placed_at: float = 0.0
    credited_to_pm: float = 0.0


class OrderTracker:
    def __init__(self):
        self.orders: dict[str, TrackedOrder] = {}
        self._by_market: dict[str, list[str]] = {}

    def add(self, order: TrackedOrder) -> None:
        if not order.order_id:
            return
        self.orders[order.order_id] = order
        self._by_market.setdefault(order.market_id, []).append(order.order_id)

    def update_fill(self, order_id: str, filled_qty: float) -> None:
        order = self.orders.get(order_id)
        if order is None:
            return
        order.filled += filled_qty
        if order.filled >= order.size * 0.999:
            order.filled = order.size
            order.status = "filled"
        else:
            order.status = "partial"

    def cancel(self, order_id: str) -> None:
        order = self.orders.get(order_id)
        if order is not None:
            order.status = "cancelled"

    def cancel_market(self, market_id: str) -> list[str]:
        """Cancel all resting/partial orders for a market. Returns cancelled order IDs.

        Sets status to 'cancelling' (transient) — reconcile() will skip these
        in the 'disappeared = filled' path. Call confirm_cancels() after the
        exchange cancel succeeds to finalize as 'cancelled'.
        """
        cancelled = []
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.status in ("resting", "partial", "unknown"):
                order.status = "cancelling"
                cancelled.append(oid)
        return cancelled

    def cancel_side(self, market_id: str, side: Side) -> list[str]:
        """Cancel all resting/partial/unknown orders for one side of a market.

        Sets status to 'cancelling' — see cancel_market docstring.
        """
        cancelled = []
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order and order.side == side and order.status in ("resting", "partial", "unknown"):
                order.status = "cancelling"
                cancelled.append(oid)
        return cancelled

    def confirm_cancels(self, order_ids: list[str]) -> None:
        """Finalize cancelling orders as cancelled after exchange confirms."""
        for oid in order_ids:
            order = self.orders.get(oid)
            if order and order.status == "cancelling":
                order.status = "cancelled"

    def revert_cancels(self, order_ids: list[str]) -> None:
        """Revert cancelling orders back to resting if exchange cancel failed."""
        for oid in order_ids:
            order = self.orders.get(oid)
            if order and order.status == "cancelling":
                order.status = "resting"

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

    def filled_cost(self, market_id: str, side: Side, fee_rate: float = 0.0) -> float:
        total = 0.0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.filled > 0:
                total += o.filled * (o.price + compute_fee(o.price, fee_rate))
        return total

    def filled_count(self, market_id: str, side: Side) -> int:
        """Count of fully filled orders for a side."""
        count = 0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.status == "filled":
                count += 1
        return count

    def total_count(self, market_id: str, side: Side) -> int:
        """Count of active orders (resting + partial + filled, excl. cancelled)."""
        count = 0
        for oid in self._by_market.get(market_id, []):
            o = self.orders.get(oid)
            if o and o.side == side and o.status in ("resting", "partial", "filled"):
                count += 1
        return count

    def has_orders(self, market_id: str) -> bool:
        return len(self.get_resting(market_id)) > 0

    def all_resting_ids(self) -> set[str]:
        return {
            oid for oid, o in self.orders.items()
            if o.status in ("resting", "partial")
        }

    def get_uncredited_fills(self, market_id: str) -> list[tuple[TrackedOrder, float]]:
        """Return (order, delta) pairs where delta = order.filled - order.credited_to_pm > 0."""
        result = []
        for oid in self._by_market.get(market_id, []):
            order = self.orders.get(oid)
            if order is None:
                continue
            delta = order.filled - order.credited_to_pm
            if delta > 0:
                result.append((order, delta))
        return result

    def flush_uncredited(self, market_id: str) -> list[tuple[TrackedOrder, float]]:
        """Extract uncredited fills and mark them as credited. Atomic extract-and-mark."""
        uncredited = self.get_uncredited_fills(market_id)
        for order, _delta in uncredited:
            order.credited_to_pm = order.filled
        return uncredited

    def mark_all_unknown(self) -> None:
        for order in self.orders.values():
            if order.status not in ("filled",):
                order.status = "unknown"

    def reconcile(self, open_orders: list[dict], settled_markets: set[str] | None = None) -> dict:
        """Reconcile tracked orders against exchange state.

        Args:
            open_orders: list of order dicts from exchange
            settled_markets: set of market_ids that have been settled — orders
                on these markets that disappear are treated as cancelled (exchange
                auto-cancels on resolution), NOT as filled.
        """
        settled = settled_markets or set()
        exchange_ids = {o.get("id", o.get("orderID", "")) for o in open_orders}
        # Index open orders by ID for partial fill checking
        exchange_by_id = {}
        for o in open_orders:
            oid = o.get("id", o.get("orderID", ""))
            if oid:
                exchange_by_id[oid] = o

        filled = []
        partial = []
        reverted = []
        for order in list(self.orders.values()):
            if order.status == "filled":
                continue
            on_exchange = order.order_id in exchange_ids

            if on_exchange:
                # Check for partial fills via size_matched field
                exch_order = exchange_by_id.get(order.order_id, {})
                size_matched = exch_order.get("size_matched")
                if size_matched is not None:
                    matched = float(size_matched)
                    new_fill = matched - order.filled
                    if new_fill > 0.001:
                        self.update_fill(order.order_id, new_fill)
                        if order.status == "filled":
                            filled.append(order)
                        else:
                            partial.append(order)

                # Revert "unknown" or "cancelled" if still on exchange
                if order.status in ("unknown", "cancelled"):
                    order.status = "resting"
                    reverted.append(order.order_id)
            elif order.status in ("resting", "partial"):
                if order.market_id in settled:
                    # Market settled — exchange auto-cancelled this order, NOT a fill
                    order.status = "cancelled"
                else:
                    # Order disappeared from exchange — treat remaining as filled
                    fill_qty = order.size - order.filled
                    if fill_qty > 0:
                        self.update_fill(order.order_id, fill_qty)
                        filled.append(order)
            elif order.status == "cancelling" and not on_exchange:
                # Cancel confirmed by exchange disappearance — finalize
                order.status = "cancelled"

        our_ids = set(self.orders.keys())
        orphaned = [oid for oid in exchange_ids if oid and oid not in our_ids]
        return {"filled": filled, "partial": partial, "reverted": reverted, "orphaned": orphaned}

    def get_unknown_ids(self) -> list[str]:
        """Return all order IDs currently in 'unknown' status."""
        return [oid for oid, o in self.orders.items() if o.status == "unknown"]

    def resolve_unknowns(self, statuses: dict[str, str]) -> dict:
        """Resolve unknown orders using exchange status lookup results.

        Args:
            statuses: mapping of {order_id: exchange_status} where status is
                      one of LIVE, OPEN, MATCHED, FILLED, CANCELLED, etc.

        Returns:
            {"reverted": [...], "filled": [...], "cancelled": [...]}
        """
        reverted = []
        filled = []
        cancelled = []

        for oid, exchange_status in statuses.items():
            order = self.orders.get(oid)
            if order is None or order.status != "unknown":
                continue

            status_upper = exchange_status.upper()
            if status_upper in ("LIVE", "OPEN"):
                order.status = "resting"
                reverted.append(oid)
            elif status_upper in ("MATCHED", "FILLED"):
                fill_qty = order.size - order.filled
                if fill_qty > 0:
                    self.update_fill(oid, fill_qty)
                filled.append(oid)
            else:
                # CANCELLED, not found, or any other status
                order.status = "cancelled"
                cancelled.append(oid)

        return {"reverted": reverted, "filled": filled, "cancelled": cancelled}

    def cleanup_market(self, market_id: str) -> None:
        """Remove all orders for a settled/expired market."""
        for oid in self._by_market.pop(market_id, []):
            self.orders.pop(oid, None)
