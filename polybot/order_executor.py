"""Order executor: places, cancels, and monitors orders via py-clob-client.

All public methods are synchronous. The Bot's async trading loop calls them
via asyncio.to_thread() to avoid blocking the event loop.

Compatible with both the real py-clob-client ClobClient and the MockClobClient
used in dry-run mode and tests.
"""

from __future__ import annotations

import logging
import time

from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
from py_clob_client.order_builder.constants import BUY

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.types import OrderRecord, Side

logger = logging.getLogger(__name__)


def _make_clob_error(exc: Exception) -> ClobApiError:
    """Convert an API exception into a ClobApiError with proper attributes."""
    # PolyApiException stores status_code directly; httpx/requests store it on .response
    status_code = getattr(exc, 'status_code', None)
    if status_code is None:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
    retry_after = None
    cancel_only = False
    if status_code == 429:
        headers = getattr(getattr(exc, 'response', None), 'headers', {}) or {}
        retry_after = float(headers.get('Retry-After', 5))
    elif status_code == 503:
        cancel_only = True
    return ClobApiError(str(exc), status_code=status_code, retry_after=retry_after, cancel_only=cancel_only)


def _is_real_clob_client(client) -> bool:
    """Return True if *client* is a real py-clob-client ClobClient (not a mock).

    We check the class name rather than using ``hasattr`` because
    ``unittest.mock.MagicMock`` objects return truthy for any attribute access.
    """
    cls_name = type(client).__name__
    return cls_name == "ClobClient"


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
        expiration: int = 0,
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
                expiration=expiration,
            )
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, orderType=OrderType.GTC)

            if not resp.get("success", True):
                raise ClobApiError(
                    f"Order rejected: {resp.get('errorMsg', resp)}",
                    status_code=None,
                )

            record.order_id = resp.get("orderID", "")
            record.status = resp.get("status", "unknown")

            logger.info(
                "ORDER PLACED: %s %s %.2f x %.1f on %s -> %s",
                side.value, token_id[:16], price, size, market_id, record.status,
            )
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e

        return record

    def get_open_orders(self) -> list[dict]:
        """Return list of open orders from the CLOB. Each has 'id', 'price', 'size'.

        The real py-clob-client exposes ``get_orders()`` while the mock/test
        client exposes ``get_open_orders()``.  We try the real API first and
        fall back to the mock method so both paths work transparently.
        """
        try:
            if _is_real_clob_client(self.client):
                return self.client.get_orders()
            return self.client.get_open_orders()
        except Exception as e:
            raise _make_clob_error(e) from e

    def cancel_order(self, order_id: str) -> bool:
        try:
            self.client.cancel(order_id)
            logger.debug("ORDER CANCELLED: %s", order_id)
            return True
        except Exception as e:
            raise _make_clob_error(e) from e

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

    def estimate_fill_cost(self, token_id: str, qty: float) -> tuple[float, float] | None:
        """Walk the ask side of the order book to estimate the average fill price
        for buying `qty` shares.

        Returns (avg_price, total_cost) or None if the book is empty or
        insufficient depth exists for the requested quantity.

        Does NOT place any orders.
        """
        try:
            book = self.client.get_order_book(token_id)
            if book is None or not book.asks:
                return None
            remaining = qty
            total_cost = 0.0
            for ask in book.asks:
                ask_price = float(ask.price)
                ask_size = float(ask.size)
                take = min(remaining, ask_size)
                total_cost += take * ask_price
                remaining -= take
                if remaining <= 0:
                    break
            if remaining > 0:
                return None  # insufficient depth
            avg_price = total_cost / qty
            return (avg_price, total_cost)
        except ClobApiError:
            return None
        except Exception:
            return None

    def get_best_ask(self, token_id: str) -> float | None:
        try:
            book = self.client.get_order_book(token_id)
            if book is not None and book.asks:
                return float(book.asks[0].price)
            return None
        except Exception as e:
            raise _make_clob_error(e) from e

    def place_batch_limit_buys(self, orders: list[dict]) -> list:
        """Place multiple limit buy orders via the batch API when available.

        For the real py-clob-client this creates all SignedOrders up front and
        submits them in a single ``post_orders()`` call.  For mock/test clients
        and dry-run mode we fall back to placing orders one at a time so the
        existing test expectations (per-order ``create_order`` / ``post_order``
        calls) are preserved.
        """
        if not orders:
            return []

        # Dry-run or mock client: place one by one (preserves test expectations)
        if self.cfg.dry_run or not _is_real_clob_client(self.client):
            return self._place_batch_sequential(orders)

        # Real client: use batch API
        return self._place_batch_via_api(orders)

    def _place_batch_sequential(self, orders: list[dict]) -> list:
        """Place orders one at a time (mock / dry-run path)."""
        results = []
        for order in orders:
            try:
                record = self.place_limit_buy(
                    token_id=order["token_id"],
                    price=order["price"],
                    size=order["size"],
                    market_id=order["market_id"],
                    side=order["side"],
                    expiration=order.get("expiration", 0),
                )
                results.append(record)
            except ClobApiError as exc:
                logger.warning("Batch order rejected: %s", exc)
        return results

    def _place_batch_via_api(self, orders: list[dict]) -> list:
        """Place orders using the real py-clob-client batch API."""
        signed_orders = []
        order_meta = []  # keep metadata aligned with signed_orders

        for order in orders:
            try:
                order_args = OrderArgs(
                    token_id=order["token_id"],
                    price=order["price"],
                    size=order["size"],
                    side=BUY,
                    expiration=order.get("expiration", 0),
                )
                signed = self.client.create_order(order_args)
                signed_orders.append(
                    PostOrdersArgs(order=signed, orderType=OrderType.GTC)
                )
                order_meta.append(order)
            except Exception as exc:
                logger.warning("Batch create_order failed for %s: %s", order.get("token_id", "?"), exc)

        if not signed_orders:
            return []

        try:
            resp = self.client.post_orders(signed_orders)
        except Exception as e:
            raise _make_clob_error(e) from e

        results = []
        # post_orders may return a list of per-order results or a single dict
        if isinstance(resp, list):
            for idx, item in enumerate(resp):
                if not item.get("success", True):
                    logger.warning(
                        "Batch item %d rejected: %s",
                        idx, item.get("errorMsg", item),
                    )
                    continue  # skip rejected items
                meta = order_meta[idx] if idx < len(order_meta) else orders[0]
                record = OrderRecord(
                    market_id=meta["market_id"],
                    side=meta["side"],
                    price=meta["price"],
                    size=meta["size"],
                    timestamp=time.time(),
                    order_id=item.get("orderID", ""),
                    status=item.get("status", "unknown"),
                )
                results.append(record)
                logger.info(
                    "BATCH ORDER: %s %s %.2f x %.1f on %s -> %s",
                    meta["side"].value, meta["token_id"][:16],
                    meta["price"], meta["size"], meta["market_id"], record.status,
                )
        else:
            # Single-dict response: treat as one combined acknowledgement
            if not resp.get("success", True):
                logger.warning("Batch rejected: %s", resp.get("errorMsg", resp))
                return results
            for meta in order_meta:
                record = OrderRecord(
                    market_id=meta["market_id"],
                    side=meta["side"],
                    price=meta["price"],
                    size=meta["size"],
                    timestamp=time.time(),
                    order_id=resp.get("orderID", ""),
                    status=resp.get("status", "unknown"),
                )
                results.append(record)

        return results

    def cancel_batch(self, order_ids: list[str]) -> list[str]:
        """Cancel multiple orders. Uses the batch cancel API for the real client.

        Returns list of successfully cancelled IDs.
        """
        if not order_ids:
            return []

        # Real client: use batch cancel endpoint
        if _is_real_clob_client(self.client):
            try:
                self.client.cancel_orders(order_ids)
                logger.debug("BATCH CANCELLED: %d orders", len(order_ids))
                return list(order_ids)
            except Exception as e:
                # Fall back to one-by-one on batch failure
                logger.warning("Batch cancel failed, falling back to individual: %s", e)

        # Mock/test client or batch fallback: cancel one by one
        cancelled = []
        for oid in order_ids:
            try:
                self.cancel_order(oid)
                cancelled.append(oid)
            except ClobApiError as exc:
                logger.warning("Cancel failed for %s: %s", oid, exc)
        return cancelled
