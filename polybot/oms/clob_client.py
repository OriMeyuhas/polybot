"""CLOB client wrapper — paper mode (simulated) and live mode (real API)."""

import logging
import random
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
        self._rng = random.Random()  # deterministic in tests via seed

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
            "expiration": signed.get("expiration", 0),
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
            "expiration": getattr(order_args, "expiration", 0),
        }

    def get_order(self, order_id: str) -> dict:
        """Return order status. LIVE if resting, CANCELLED otherwise."""
        if order_id in self._resting:
            return {"orderID": order_id, "status": "LIVE"}
        return {"orderID": order_id, "status": "CANCELLED"}

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

    _book_cache: dict[str, tuple[float, dict]] = {}  # token_id -> (ts, book_data)
    _BOOK_CACHE_TTL = 5.0  # seconds

    def _fetch_real_book(self, token_id: str) -> dict | None:
        """Fetch book from CLOB API with 5-second cache."""
        now = time.time()
        cached = self._book_cache.get(token_id)
        if cached and now - cached[0] < self._BOOK_CACHE_TTL:
            return cached[1]
        try:
            import httpx
            resp = httpx.get(
                f"https://clob.polymarket.com/book?token_id={token_id}",
                timeout=3.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                self._book_cache[token_id] = (now, data)
                return data
        except Exception:
            pass
        return None

    def _book_validates_fill(self, token_id: str, order_price: float, side: str) -> bool:
        """Check if the real CLOB order book supports this fill.

        Fetches the live book from Polymarket CLOB API (cached 5s).
        For BUY orders: our bid should be within $0.05 of the real best bid.
        If our bid is far above the real best bid, the fill is unrealistic.
        """
        try:
            data = self._fetch_real_book(token_id)
            if data is None:
                return True  # API error, don't block

            bids = data.get("bids", [])
            asks = data.get("asks", [])

            if side == "BUY":
                real_best_bid = float(bids[-1]["price"]) if bids else None
                if real_best_bid is not None:
                    gap = order_price - real_best_bid
                    if gap > 0.05:
                        return False
            elif side == "SELL":
                real_best_ask = float(asks[0]["price"]) if asks else None
                if real_best_ask is not None:
                    gap = real_best_ask - order_price
                    if gap > 0.05:
                        return False
        except Exception:
            return True
        return True

    @staticmethod
    def _fill_probability(order_price: float, market_price: float) -> float:
        """Distance-dependent fill probability per tick.

        Near-market orders fill frequently; deep orders rarely fill.
        Calibrated against whale fill rate data (96 fills/market over 300s).
        """
        distance = abs(order_price - market_price)
        if distance < 0.015:      # At or near market (within ~1 cent)
            return 0.90
        elif distance < 0.035:    # Close (1-3 cents)
            return 0.50
        elif distance < 0.065:    # Mid-range (3-6 cents)
            return 0.20
        elif distance < 0.105:    # Deep (6-10 cents)
            return 0.08
        else:                     # Very deep (>10 cents)
            return 0.02

    def tick(self, midpoints: dict[str, float] | None = None) -> list[dict]:
        """Simulate fills using CLOB midpoint prices with distance-dependent probability.

        Near-market orders fill frequently; deep orders rarely fill.
        This models real queue position effects in the order book.
        One fill per token per tick, nearest-to-market first.
        Expired orders (expiration > 0 and time.time() > expiration) are auto-cancelled.
        """
        # Auto-cancel expired orders (GTD expiration)
        now_ts = time.time()
        expired_ids = [
            oid for oid, order in self._resting.items()
            if order.get("expiration", 0) > 0 and now_ts > order["expiration"]
        ]
        for oid in expired_ids:
            self._resting.pop(oid, None)

        if not midpoints:
            return []

        fills = []
        to_remove = []
        filled_tokens: set[str] = set()

        # Sort: highest-priced BUY first (nearest to market, fills first)
        sorted_orders = sorted(
            self._resting.items(),
            key=lambda item: -Decimal(str(item[1].get("price", "0"))),
        )

        for oid, order in sorted_orders:
            token_id = order.get("token_id", "")
            if token_id in filled_tokens:
                continue

            market_price = midpoints.get(token_id)
            if market_price is None:
                continue

            order_price = float(order.get("price", "0"))
            side = order.get("side", "BUY").upper()

            # Validate against real order book if available
            if self._book_manager and not self._book_validates_fill(
                token_id, order_price, side
            ):
                continue

            if side == "BUY" and market_price <= order_price:
                prob = self._fill_probability(order_price, market_price)
                if self._rng.random() < prob:
                    order["status"] = "filled"
                    fills.append(order)
                    to_remove.append(oid)
                    filled_tokens.add(token_id)
            elif side == "SELL" and market_price >= order_price:
                prob = self._fill_probability(order_price, market_price)
                if self._rng.random() < prob:
                    order["status"] = "filled"
                    fills.append(order)
                    to_remove.append(oid)
                    filled_tokens.add(token_id)

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
