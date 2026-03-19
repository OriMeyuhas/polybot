#!/usr/bin/env python3
"""PolyBot Trading Engine — Entry Point.

Usage:
    python run_bot.py

In DRY_RUN=true mode (default), no credentials are required — a mock CLOB
client is used so you can observe signal detection and sizing without an account.

Requires PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env for live mode.
"""

import asyncio
import logging
import sys
import time

from polybot.config import load_bot_config
from polybot.bot import Bot


# ---------------------------------------------------------------------------
# Mock CLOB client — used in dry-run mode when no credentials are provided.
# Fetches REAL markets and order books from Polymarket APIs (no auth needed
# for reads), but simulates order placement and fills locally.
# ---------------------------------------------------------------------------
import random as _random

import httpx as _httpx

_GAMMA_API = "https://gamma-api.polymarket.com"
_CLOB_API = "https://clob.polymarket.com"

# Cache TTL for order book fetches (seconds)
_BOOK_CACHE_TTL = 5.0


class _MockAsk:
    def __init__(self, price, size):
        self.price = str(price)
        self.size = str(size)


class _MockOrderBook:
    def __init__(self, bid_price, ask_price, tick_size="0.01", size=5000):
        self.bids = [_MockAsk(bid_price, size)]
        self.asks = [_MockAsk(ask_price, size)]
        self.tick_size = tick_size


class MockClobClient:
    """Dry-run CLOB client: real Polymarket market data, simulated fills.

    - get_markets(): fetches real active crypto up/down markets from the Gamma API
    - get_order_book(): fetches real order books from the CLOB API (cached 5s)
    - post_order/cancel/tick: local simulation (no real orders placed)
    """

    def __init__(self, base_fill_rate: float = 0.15):
        self._http = _httpx.Client(timeout=10.0)
        self._resting: dict[str, dict] = {}  # order_id -> order info
        self._next_id = 1
        self._base_fill_rate = base_fill_rate
        self._book_cache: dict[str, tuple[float, object]] = {}  # token_id -> (timestamp, book)
        self._markets_cache: tuple[float, list] | None = None  # (timestamp, markets)

    def get_markets(self):
        """Fetch real active crypto up/down markets from Polymarket Gamma API."""
        now = time.time()
        # Cache markets for 30 seconds
        if self._markets_cache and now - self._markets_cache[0] < 30:
            return {"data": self._markets_cache[1]}

        try:
            resp = self._http.get(
                f"{_GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": "100"},
            )
            resp.raise_for_status()
            markets = resp.json()
            # Gamma API returns a list directly
            if isinstance(markets, list):
                self._markets_cache = (now, markets)
                return {"data": markets}
            # Or it might return {"data": [...]}
            data = markets.get("data", markets) if isinstance(markets, dict) else []
            self._markets_cache = (now, data if isinstance(data, list) else [])
            return {"data": self._markets_cache[1]}
        except Exception as e:
            print(f"[MockClobClient] Gamma API fetch failed: {e}")
            # Return cached data if available, else empty
            if self._markets_cache:
                return {"data": self._markets_cache[1]}
            return {"data": []}

    def get_order_book(self, token_id):
        """Fetch real order book from Polymarket CLOB API (cached 5s)."""
        now = time.time()
        cached = self._book_cache.get(token_id)
        if cached and now - cached[0] < _BOOK_CACHE_TTL:
            return cached[1]

        try:
            resp = self._http.get(
                f"{_CLOB_API}/book",
                params={"token_id": token_id},
            )
            resp.raise_for_status()
            data = resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])
            tick_size = data.get("tick_size", "0.01")

            # Build a book object matching the py-clob-client interface
            book = type("OrderBook", (), {
                "bids": [_MockAsk(b["price"], b["size"]) for b in bids[:10]],
                "asks": [_MockAsk(a["price"], a["size"]) for a in asks[:10]],
                "tick_size": str(tick_size),
            })()

            self._book_cache[token_id] = (now, book)
            return book
        except Exception:
            # Fallback: return a synthetic book near 0.50
            noise = _random.uniform(-0.02, 0.02)
            ask = round(0.50 + noise, 2)
            ask = max(0.10, min(0.90, ask))
            book = _MockOrderBook(bid_price=round(ask - 0.02, 2), ask_price=ask)
            return book

    def create_order(self, order_args):
        return {"signed": True, "_args": order_args}

    def post_order(self, signed, orderType=None):
        order_id = f"mock-{self._next_id}"
        self._next_id += 1
        args = signed.get("_args")
        if args is not None:
            self._resting[order_id] = {
                "token_id": getattr(args, "token_id", ""),
                "price": float(getattr(args, "price", 0)),
                "size": float(getattr(args, "size", 0)),
                "remaining": float(getattr(args, "size", 0)),
            }
        return {"orderID": order_id, "status": "resting"}

    def get_open_orders(self) -> list[dict]:
        return [
            {"id": oid, **info}
            for oid, info in self._resting.items()
        ]

    def tick(self):
        """Simulate fills on resting orders. Called each bot tick."""
        for oid in list(self._resting.keys()):
            order = self._resting.get(oid)
            if order is None:
                continue

            mid = 0.50  # neutral mid for binary markets
            distance = abs(order["price"] - mid)
            max_dist = 0.50
            fill_prob = self._base_fill_rate * (1.0 - distance / max_dist)
            fill_prob = max(0.005, fill_prob)

            if _random.random() < fill_prob:
                fill_pct = _random.uniform(0.05, 0.40)
                fill_qty = order["remaining"] * fill_pct
                order["remaining"] -= fill_qty
                if order["remaining"] < 0.1:
                    del self._resting[oid]

    def cancel(self, order_id):
        self._resting.pop(order_id, None)
        return {"cancelled": True}

    def cancel_all(self):
        self._resting.clear()
        return {"cancelled": True}

    def get_tick_size(self, condition_id):
        """Return default tick size for any market."""
        return 0.01

    def post_orders(self, signed_orders):
        """Batch-post orders by delegating to post_order for each."""
        results = []
        for signed in signed_orders:
            results.append(self.post_order(signed, order_type="GTC"))
        return results

    def cancel_orders(self, order_ids):
        """Batch-cancel orders by delegating to cancel for each."""
        for oid in order_ids:
            self.cancel(oid)
        return {"cancelled": True}

    def post_heartbeat(self, heartbeat_id=None):
        """Mock heartbeat — always succeeds."""
        return {"heartbeat_id": heartbeat_id or "mock-heartbeat"}

    def get_orders(self, params=None):
        """Alias for get_open_orders (real API name)."""
        return self.get_open_orders()

    def get_balance_allowance(self, params=None):
        return {"balance": 1_000_000_000}  # $1000 USDC (6 decimals)


# ---------------------------------------------------------------------------

def create_clob_client(cfg):
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    return ClobClient(
        cfg.polymarket_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )


def main():
    cfg = load_bot_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        filename="polybot.log",
        filemode="a",
    )

    if cfg.dry_run and not cfg.private_key:
        print("DRY RUN mode — no credentials found, using mock CLOB client.")
        clob_client = MockClobClient(base_fill_rate=cfg.mock_base_fill_rate)
        import os
        bankroll = float(os.getenv("DRY_RUN_BANKROLL", "1000"))
    elif not cfg.private_key:
        print("ERROR: PRIVATE_KEY not set in .env — required for live trading.")
        sys.exit(1)
    else:
        clob_client = create_clob_client(cfg)
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            balance_info = clob_client.get_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            bankroll = float(balance_info.get("balance", 0)) / 1e6
            print(f"Starting bankroll: ${bankroll:,.2f} USDC")
        except Exception as e:
            print(f"Could not fetch balance ({e}), using default $1000")
            bankroll = 1000.0

    if not cfg.dry_run:
        print("\n!!  LIVE TRADING MODE — real orders will be placed!")
        print(f"   Bankroll: ${bankroll:,.2f}")
        print(f"   Max position size: ${bankroll * cfg.position_size_fraction:,.2f}")
        confirm = input("   Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            sys.exit(0)
    else:
        print(f"Running in DRY RUN mode — no real orders will be placed. Bankroll: ${bankroll:,.2f}")

    bot = Bot(cfg, clob_client=clob_client, initial_bankroll=bankroll)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nBot stopped.")


if __name__ == "__main__":
    main()
