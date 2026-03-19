"""CLOB client wrapper — paper mode (simulated) and live mode (real API)."""

import logging
import time
import uuid
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class PaperClobClient:
    """Paper trading client — simulates order placement, uses real book data for fills."""

    def __init__(self, book_manager=None):
        self._book_manager = book_manager
        self._resting: dict[str, dict] = {}
        self._fills: list[dict] = []
        self._order_counter = 0

    def _next_id(self) -> str:
        self._order_counter += 1
        return f"paper-{self._order_counter:06d}"

    def post_order(self, signed: dict, orderType: str = "GTC") -> dict:
        oid = self._next_id()
        order = {
            "orderID": oid,
            "token_id": signed.get("token_id", ""),
            "price": signed.get("price", "0"),
            "size": signed.get("size", "0"),
            "side": signed.get("side", "BUY"),
            "status": "resting",
            "placed_at": time.time(),
            "remaining": signed.get("size", "0"),
        }
        self._resting[oid] = order
        return {"orderID": oid}

    def post_orders(self, signed_orders: list) -> list:
        results = []
        for so in signed_orders:
            results.append(self.post_order(so))
        return results

    def get_open_orders(self) -> list[dict]:
        return list(self._resting.values())

    def cancel(self, order_id: str) -> dict:
        self._resting.pop(order_id, None)
        return {"orderID": order_id}

    def cancel_all(self) -> dict:
        count = len(self._resting)
        self._resting.clear()
        return {"cancelled": count}

    def cancel_orders(self, order_ids: list[str]) -> dict:
        cancelled = []
        for oid in order_ids:
            if oid in self._resting:
                del self._resting[oid]
                cancelled.append(oid)
        return {"cancelled": cancelled}

    def create_order(self, order_args) -> dict:
        """Wrap order args into a dict (no signing needed in paper mode)."""
        return {
            "order": "paper_signed",
            "token_id": getattr(order_args, "token_id", ""),
            "price": str(getattr(order_args, "price", 0)),
            "size": str(getattr(order_args, "size", 0)),
            "side": getattr(order_args, "side", "BUY"),
        }

    def get_order_book(self, token_id: str) -> Any:
        """Read-through: get book from BookManager if available."""
        if self._book_manager:
            return self._book_manager.get_book(token_id)
        return None

    def get_tick_size(self, condition_id: str) -> float:
        return 0.01

    def post_heartbeat(self, heartbeat_id=None) -> dict:
        return {"status": "ok"}

    def get_orders(self, params=None) -> list[dict]:
        return list(self._resting.values())

    def get_balance_allowance(self, params=None) -> dict:
        return {"balance": "10000.00", "allowance": "10000.00"}

    def tick(self) -> list[dict]:
        """Simulate fills against real book data.

        For each resting buy: fill if book's best_ask <= order price.
        For each resting sell: fill if book's best_bid >= order price.
        Returns list of filled orders.
        """
        if not self._book_manager:
            return []

        fills = []
        to_remove = []

        for oid, order in self._resting.items():
            token_id = order.get("token_id", "")
            book = self._book_manager.get_book(token_id)
            if not book:
                continue

            order_price = Decimal(str(order.get("price", "0")))
            side = order.get("side", "BUY").upper()

            if side == "BUY" and book.best_ask is not None:
                if book.best_ask <= order_price:
                    order["status"] = "filled"
                    fills.append(order)
                    to_remove.append(oid)
            elif side == "SELL" and book.best_bid is not None:
                if book.best_bid >= order_price:
                    order["status"] = "filled"
                    fills.append(order)
                    to_remove.append(oid)

        for oid in to_remove:
            self._resting.pop(oid, None)

        return fills


def create_clob_client(cfg, book_manager=None):
    """Factory: returns PaperClobClient for dry_run, LiveClobClient otherwise."""
    if cfg.dry_run or not cfg.private_key:
        logger.info("Creating PaperClobClient (dry_run=%s)", cfg.dry_run)
        return PaperClobClient(book_manager=book_manager)

    # Live mode — import and create real client
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds

    client = ClobClient(
        host=cfg.polymarket_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )
    logger.info("Created live ClobClient at %s", cfg.polymarket_host)
    return client
