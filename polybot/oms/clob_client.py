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

    def _get_real_book_depth(self, token_id: str, order_price: float, side: str) -> tuple[bool, float]:
        """Check if our order price is realistic given the real order book.

        Returns (can_fill, available_size):
        - can_fill: True if the order is at a plausible price
        - available_size: estimate of fillable size (from real book depth)

        Binary option market structure:
        - Asks cluster at $0.99 (max payout), bids at fair value (~$0.40-0.60)
        - Real fills happen when sellers market-sell INTO resting bids
        - So for BUY orders: check if our bid is NEAR the real best bid
          (within 10c). Sellers cross the spread to hit bids near best bid.
        - Orders far below the real best bid (>10c) are unrealistic.
        - Orders far above the real best bid are aggressive (fill immediately).
        """
        if not self._book_manager:
            # No book manager configured (unit tests). Allow probabilistic fills.
            return True, float("inf")

        book = self._book_manager.get_book(token_id)
        if book is None or book._last_update == 0:
            # Book manager exists but no data for this token yet (WS lag).
            # Block fill — don't allow unvalidated fills during book warmup.
            return False, 0.0

        try:
            if side == "BUY":
                if book.asks:
                    best_ask = float(book.best_ask)
                    if order_price > best_ask + 0.005:
                        # Passive maker BUY priced above the real ask can't legitimately
                        # rest there — a taker would hit the ask first. Block this fill
                        # to prevent phantom profit on inflated paper prices.
                        return False, 0.0
                if not book.bids:
                    return False, 0.0  # no bid data — block fill
                best_bid = float(book.best_bid)
                gap_below_bid = best_bid - order_price
                if gap_below_bid > 0.05:
                    # Our bid is >5c below the real best bid — no seller would
                    # cross this far when they can sell at the bid. Block fill.
                    return False, 0.0
                elif gap_below_bid > 0.03:
                    # 3-5c below best bid — rare, aggressive seller only. Tiny size.
                    bid_depth = sum(
                        float(lvl.size) for lvl in book.bids
                        if float(lvl.price) >= order_price
                    )
                    return True, min(bid_depth * 0.05, 10.0)
                else:
                    # Within 3c of best bid or above it — realistic fill zone.
                    bid_depth = sum(
                        float(lvl.size) for lvl in book.bids
                        if float(lvl.price) >= order_price - 0.01
                    )
                    return True, max(bid_depth * 0.2, 10.0)
            else:  # SELL
                if not book.asks:
                    return False, 0.0  # no ask data — block fill
                best_ask = float(book.best_ask)
                gap_above_ask = order_price - best_ask

                if gap_above_ask > 0.05:
                    return False, 0.0
                elif gap_above_ask > 0.03:
                    ask_depth = sum(
                        float(lvl.size) for lvl in book.asks
                        if float(lvl.price) <= order_price
                    )
                    return True, min(ask_depth * 0.05, 10.0)
                else:
                    ask_depth = sum(
                        float(lvl.size) for lvl in book.asks
                        if float(lvl.price) <= order_price + 0.01
                    )
                    return True, max(ask_depth * 0.2, 10.0)
        except Exception:
            return True, float("inf")

    @staticmethod
    def _fill_probability(order_price: float, market_price: float, side: str = "BUY") -> float:
        """Distance-dependent fill probability per tick for binary options.

        For BUY orders:
        - If order_price >= market_price (aggressive — overpaying), high fill prob.
        - If order_price < market_price (passive — waiting for seller), lower prob.

        Conservative calibration: targets ~5-15 fills/side over a 15m window,
        matching realistic Polymarket fill rates for passive resting orders.
        """
        distance = abs(order_price - market_price)

        if side == "BUY" and order_price >= market_price:
            return min(0.60, 0.30 + distance * 3)
        elif side == "SELL" and order_price <= market_price:
            return min(0.60, 0.30 + distance * 3)

        # Passive: our order is away from market, waiting for counterparty.
        # More conservative than before — real Polymarket has thin books and
        # passive orders far from mid rarely fill.
        if distance < 0.02:       # Within 2 cents of midpoint
            return 0.04
        elif distance < 0.05:     # 2-5 cents away
            return 0.01
        elif distance < 0.10:     # 5-10 cents away
            return 0.003
        elif distance < 0.20:     # 10-20 cents away
            return 0.0005
        else:                     # Very deep (>20 cents)
            return 0.0            # ZERO: no fills for orders >20c from mid

    def tick(self, midpoints: dict[str, float] | None = None) -> list[dict]:
        """Simulate fills using real book data + distance-dependent probability.

        Fill realism rules:
        1. If real book data is available, only fill if counterparty liquidity
           exists near the order price (asks for BUY, bids for SELL).
        2. Cap fill size at available real book depth at that level.
        3. Orders >20c from midpoint NEVER fill (no real counterparty).
        4. One fill per token per tick, nearest-to-market first.
        5. Expired orders auto-cancelled.
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
            order_size = float(order.get("size", "0"))
            side = order.get("side", "BUY").upper()

            # Hard cutoff: orders >20c from midpoint never fill
            distance = abs(order_price - market_price)
            if distance > 0.20:
                continue

            # Check real book for counterparty liquidity
            can_fill, available_size = self._get_real_book_depth(
                token_id, order_price, side
            )

            if not can_fill:
                # No real counterparty liquidity — skip this order entirely
                continue

            # Cap fill size at real book depth (if book data available)
            if available_size < float("inf"):
                if available_size <= 0:
                    continue  # no depth at this level
                # Only fill up to what the real book could support
                effective_size = min(order_size, available_size)
            else:
                effective_size = order_size

            # Distance-based fill probability
            prob = self._fill_probability(order_price, market_price, side)
            if self._rng.random() < prob:
                # Apply size cap from book depth
                if effective_size < order_size:
                    order["size"] = str(round(effective_size, 1))
                    order["remaining"] = order["size"]
                order["status"] = "filled"
                fills.append(order)
                to_remove.append(oid)
                filled_tokens.add(token_id)

        for oid in to_remove:
            self._resting.pop(oid, None)

        return fills


def create_clob_client(cfg, book_manager=None):
    """Factory: returns PaperClobClient for dry_run, LiveClobClient otherwise.

    Live path imports V2 SDK and runs a pUSD balance gate before returning.
    """
    if cfg.dry_run or not cfg.private_key:
        logger.info("Creating PaperClobClient (dry_run=%s)", cfg.dry_run)
        return PaperClobClient(book_manager=book_manager)

    # Live mode — V2 SDK
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import ApiCreds

    client = ClobClient(
        host=cfg.polymarket_host,
        key=cfg.private_key,
        chain=cfg.chain,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )
    logger.info("Created live V2 ClobClient at %s", cfg.polymarket_host)

    _ensure_collateral(cfg)
    return client


def _ensure_collateral(cfg):
    """Placeholder — Task 7 implements the actual pUSD balance gate."""
    return None
