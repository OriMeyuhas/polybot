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
        self._dry_id = 0

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
        if self.cfg.dry_run:
            logger.debug(
                "DRY RUN: would buy %s %.2f x %.1f on %s",
                side.value, price, size, market_id,
            )
            self._dry_id += 1
            record.order_id = f"dry-{self._dry_id}"
            record.status = "dry_run"
            return record
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
                "ORDER PLACED: %s %s %.2f x %.1f on %s -> %s",
                side.value, token_id[:16], price, size, market_id, record.status,
            )
        except Exception as e:
            record.status = "error"
            logger.error("Order placement failed: %s", e)

        return record

    def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: float,
        market_id: str,
        side: Side,
    ) -> OrderRecord:
        """Place a limit sell order (for early exit of appreciated positions)."""
        record = OrderRecord(
            market_id=market_id,
            side=side,
            price=price,
            size=size,
            timestamp=time.time(),
        )
        if self.cfg.dry_run:
            logger.debug(
                "DRY RUN: would sell %s %.2f x %.1f on %s",
                side.value, price, size, market_id,
            )
            self._dry_id += 1
            record.order_id = f"dry-sell-{self._dry_id}"
            record.status = "dry_run"
            return record
        try:
            from py_clob_client.order_builder.constants import SELL
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=SELL,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, OrderType.GTC)

            record.order_id = resp.get("orderID", "")
            record.status = resp.get("status", "unknown")

            logger.info(
                "SELL ORDER: %s %s %.2f x %.1f on %s -> %s",
                side.value, token_id[:16], price, size, market_id, record.status,
            )
        except Exception as e:
            record.status = "error"
            logger.error("Sell order failed: %s", e)

        return record

    def get_open_orders(self) -> list[dict]:
        """Return list of open orders from the CLOB. Each has 'id', 'price', 'size'."""
        try:
            return self.client.get_open_orders()
        except Exception as e:
            logger.error("Get open orders failed: %s", e)
            return []

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            logger.debug("ORDER CANCELLED: %s", order_id)
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
        try:
            book = self.client.get_order_book(token_id)
            bids = [(b.price, b.size) for b in book.bids]
            asks = [(a.price, a.size) for a in book.asks]
            return bids, asks
        except Exception as e:
            logger.error("Order book fetch failed for %s: %s", token_id[:16], e)
            return [], []

    def get_book_depth_at_price(self, token_id: str, max_price: float) -> float:
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
        return 1.0
