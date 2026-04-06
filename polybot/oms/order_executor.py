"""Rebuilt order executor for the OMS layer.

Uses the new client wrapper (PaperClobClient or live ClobClient) from
polybot/oms/clob_client.py.

- All raw py-clob-client exceptions are converted to ClobApiError via
  _make_clob_error() so callers can rely on a single exception type with
  status_code / retry_after / cancel_only attributes.
- ClobApiError raised by the client propagates unchanged (no double-wrapping).
- Tick size validation via round_to_tick is applied before order submission.
- Batch orders are capped at cfg.batch_order_size (default 15).
- No dry_run branching here — paper/live behaviour is handled by the client.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

try:
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.order_builder.constants import BUY
except ImportError:
    # Fallback when py-clob-client is not installed (tests / paper-only envs).
    # Must mirror the real OrderArgs fields so attribute-presence checks pass.
    @dataclass
    class OrderArgs:  # type: ignore[no-redef]
        token_id: str = ""
        price: float = 0.0
        size: float = 0.0
        side: str = "BUY"
        fee_rate_bps: str = ""
        nonce: int = 0
        expiration: int = 0
        taker: str = "0x0000000000000000000000000000000000000000"

    BUY = "BUY"

SELL = "SELL"

from polybot.config import BotConfig
from polybot.errors import ClobApiError
from polybot.tick_size_cache import round_to_tick
from polybot.types import OrderRecord, Side

logger = logging.getLogger(__name__)

# Default tick size used when the client does not expose get_tick_size
_DEFAULT_TICK = 0.01


def _make_clob_error(exc: Exception) -> ClobApiError:
    """Convert a raw API exception into a ClobApiError with proper attributes.

    Extracts status_code from exc.response (if present), parses Retry-After
    header for 429s (default 5s), and sets cancel_only for 503s.
    """
    # PolyApiException stores status_code directly; httpx/requests store it on .response
    status_code = getattr(exc, 'status_code', None)
    if status_code is None:
        status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
    retry_after = None
    cancel_only = False
    if status_code == 429:
        # Try exc.response.headers first (PolyApiException doesn't carry headers)
        headers = getattr(getattr(exc, 'response', None), 'headers', {}) or {}
        retry_after = float(headers.get('Retry-After', 5))
    elif status_code == 503:
        cancel_only = True
    return ClobApiError(
        str(exc), status_code=status_code, retry_after=retry_after, cancel_only=cancel_only
    )


def _get_tick_size(client: Any, token_id: str) -> float:
    """Return tick size for *token_id* without raising."""
    getter = getattr(client, "get_tick_size", None)
    if getter is not None:
        try:
            return float(getter(token_id))
        except Exception:
            pass
    return _DEFAULT_TICK


class OrderExecutor:
    """Place, cancel, and query orders via the injected CLOB client.

    Parameters
    ----------
    cfg:
        Bot configuration (used for batch_order_size and logging).
    clob_client:
        A PaperClobClient or live ClobClient instance (from
        polybot.oms.clob_client).  The executor never imports or constructs
        its own client.
    """

    def __init__(self, cfg: BotConfig, clob_client: Any, data_recorder=None) -> None:
        self.cfg = cfg
        self.client = clob_client
        self._data_recorder = data_recorder

    # ------------------------------------------------------------------
    # Single-order operations
    # ------------------------------------------------------------------

    def place_limit_buy(
        self,
        token_id: str,
        price: float,
        size: float,
        market_id: str,
        side: Side,
        expiration: int = 0,
    ) -> OrderRecord:
        """Place a single limit buy and return an OrderRecord.

        Tick size validation is applied to *price* before submission.
        Raw exceptions are wrapped into ClobApiError via _make_clob_error().
        When *expiration* is a positive Unix timestamp the order auto-cancels
        at that time (GTD — Good-Til-Date).
        """
        tick = _get_tick_size(self.client, token_id)
        validated_price = round_to_tick(price, tick)

        record = OrderRecord(
            market_id=market_id,
            side=side,
            price=validated_price,
            size=size,
            timestamp=time.time(),
        )

        # Build OrderArgs compatible with both PaperClobClient and live SDK.
        order_args = OrderArgs(
            token_id=token_id,
            price=validated_price,
            size=size,
            side=BUY,
            expiration=expiration,
        )

        try:
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, orderType="GTC")
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e

        if not resp.get("success", True):
            raise ClobApiError(
                f"Order rejected: {resp.get('errorMsg', resp)}",
                status_code=None,
            )

        record.order_id = resp.get("orderID", "")
        record.status = resp.get("status", "unknown")

        logger.info(
            "ORDER PLACED: %s %s %.2f x %.1f on %s -> %s",
            side.value,
            token_id[:16],
            validated_price,
            size,
            market_id,
            record.status,
        )
        if self._data_recorder:
            self._data_recorder.log_order(
                time.time(), "post", market_id, side.value,
                validated_price, size, record.order_id, "ladder",
            )
        return record

    def place_limit_sell(
        self,
        token_id: str,
        price: float,
        size: float,
        market_id: str,
        side: Side,
        expiration: int = 0,
    ) -> OrderRecord:
        """Place a single limit sell and return an OrderRecord.

        Used for exiting losing one-sided positions mid-window.
        Tick size validation is applied to *price* before submission.
        """
        tick = _get_tick_size(self.client, token_id)
        validated_price = round_to_tick(price, tick)

        record = OrderRecord(
            market_id=market_id,
            side=side,
            price=validated_price,
            size=size,
            timestamp=time.time(),
        )

        order_args = OrderArgs(
            token_id=token_id,
            price=validated_price,
            size=size,
            side=SELL,
            expiration=expiration,
        )

        try:
            signed = self.client.create_order(order_args)
            resp = self.client.post_order(signed, orderType="GTC")
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e

        if not resp.get("success", True):
            raise ClobApiError(
                f"Sell order rejected: {resp.get('errorMsg', resp)}",
                status_code=None,
            )

        record.order_id = resp.get("orderID", "")
        record.status = resp.get("status", "unknown")

        logger.info(
            "SELL ORDER PLACED: %s %s %.2f x %.1f on %s -> %s",
            side.value,
            token_id[:16],
            validated_price,
            size,
            market_id,
            record.status,
        )
        if self._data_recorder:
            self._data_recorder.log_order(
                time.time(), "post", market_id, side.value,
                validated_price, size, record.order_id, "sell",
            )
        return record

    def get_open_orders(self) -> list[dict]:
        """Return list of open orders from the client.

        Supports both PaperClobClient (get_open_orders) and live ClobClient
        (get_orders).  Raw exceptions are wrapped into ClobApiError.
        """
        try:
            # Prefer get_open_orders (paper client); fall back to get_orders (live)
            if hasattr(self.client, "get_open_orders") and callable(self.client.get_open_orders):
                return self.client.get_open_orders()
            return self.client.get_orders()
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e

    def get_recent_matched_orders(self) -> list[dict]:
        """Return recently matched/filled orders from the CLOB.

        Used on startup to detect stale fills from a previous session.
        Paper client always returns [] (no cross-session state).
        """
        # Paper client has no cross-session state
        if hasattr(self.client, '_resting'):
            return []
        try:
            orders = self.client.get_orders()
            return [
                o for o in orders
                if str(o.get("status", "")).upper() in ("MATCHED", "FILLED")
            ]
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID.  Returns True on success.

        Raw exceptions are wrapped into ClobApiError.
        """
        try:
            self.client.cancel(order_id)
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e
        logger.debug("ORDER CANCELLED: %s", order_id)
        if self._data_recorder:
            self._data_recorder.log_order(
                time.time(), "cancel", "", "", 0, 0, order_id, "cancel",
            )
        return True

    def cancel_all(self) -> bool:
        """Cancel all open orders.  Returns True on success, False on error."""
        try:
            self.client.cancel_all()
            logger.info("ALL ORDERS CANCELLED")
            return True
        except ClobApiError:
            raise
        except Exception as exc:
            raise _make_clob_error(exc) from exc

    # ------------------------------------------------------------------
    # Book queries
    # ------------------------------------------------------------------

    def get_book_summary(
        self, token_id: str
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Return (bids, asks) from the order book, or ([], []) on error."""
        try:
            book = self.client.get_order_book(token_id)
            if book is None:
                return [], []
            bids = [(b.price, b.size) for b in book.bids]
            asks = [(a.price, a.size) for a in book.asks]
            return bids, asks
        except Exception as exc:
            logger.error("Order book fetch failed for %s: %s", token_id[:16], exc)
            return [], []

    def get_book_depth_at_price(self, token_id: str, max_price: float) -> float:
        """Return total ask depth at or below *max_price*."""
        try:
            book = self.client.get_order_book(token_id)
            if book is None:
                return 0.0
            depth = 0.0
            for ask in book.asks:
                if float(ask.price) <= max_price:
                    depth += float(ask.size)
            return depth
        except Exception as exc:
            logger.error("Book depth fetch failed: %s", exc)
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
        """Return best ask price, or None if no asks.  ClobApiError propagates."""
        try:
            book = self.client.get_order_book(token_id)
        except ClobApiError:
            raise
        except Exception as e:
            raise _make_clob_error(e) from e
        if book is not None and book.asks:
            return float(book.asks[0].price)
        return None

    def get_midpoint(self, token_id: str) -> float | None:
        """Return CLOB midpoint for a token, or None."""
        try:
            import httpx
            resp = httpx.get(
                f"https://clob.polymarket.com/midpoint?token_id={token_id}",
                timeout=3.0,
            )
            if resp.status_code == 200:
                return float(resp.json().get("mid", 0))
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    def place_batch_limit_buys(self, orders: list[dict]) -> list[OrderRecord]:
        """Place multiple limit buy orders, capped at cfg.batch_order_size.

        Orders are chunked into batches of at most cfg.batch_order_size and
        placed sequentially.  ClobApiError from any individual order is logged
        as a warning and that order is skipped; a ClobApiError from the batch
        post propagates to the caller.
        """
        if not orders:
            return []

        cap = self.cfg.batch_order_size
        results: list[OrderRecord] = []

        for chunk_start in range(0, len(orders), cap):
            chunk = orders[chunk_start : chunk_start + cap]
            for order in chunk:
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
                    logger.warning(
                        "Batch order rejected for %s: %s",
                        order.get("token_id", "?"),
                        exc,
                    )

        return results

    def cancel_batch(self, order_ids: list[str]) -> list[str]:
        """Cancel multiple orders one by one.  Returns list of cancelled IDs."""
        if not order_ids:
            return []

        cancelled: list[str] = []
        for oid in order_ids:
            try:
                self.cancel_order(oid)
                cancelled.append(oid)
            except ClobApiError as exc:
                logger.warning("Cancel failed for %s: %s", oid, exc)
        return cancelled
