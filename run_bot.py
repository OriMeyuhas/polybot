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
# Simulates realistic 3-minute window cycles with rotating markets.
# ---------------------------------------------------------------------------
import datetime as _dt
import random as _random

# Multi-timeframe windows matching real Polymarket (5m, 15m, 1h)
_TIMEFRAMES = [
    (300, "5m"),    # 5-minute windows
    (900, "15m"),   # 15-minute windows
    (3600, "1h"),   # 1-hour windows
]


class _MockAsk:
    def __init__(self, price, size):
        self.price = str(price)
        self.size = str(size)


class _MockOrderBook:
    def __init__(self, bid_price, ask_price, size=5000):
        self.bids = [_MockAsk(bid_price, size)]
        self.asks = [_MockAsk(ask_price, size)]


class MockClobClient:
    """Simulates Polymarket CLOB API responses for dry-run testing.

    Generates multi-timeframe windows (5m, 15m, 1h) anchored to clock time
    so they expire naturally. Each window has a unique ID so the bot sees
    proper open → trade → settle → new window cycles.
    """

    def __init__(self):
        self._assets = [
            ("BTC", "Bitcoin"),
            ("ETH", "Ethereum"),
        ]

    def get_markets(self):
        now = int(time.time())
        markets = []
        for duration, label in _TIMEFRAMES:
            win_start = now - (now % duration)
            win_end = win_start + duration
            win_num = win_start // duration

            for symbol, name in self._assets:
                sym_lower = symbol.lower()
                markets.append({
                    "condition_id": f"0x{sym_lower}_{label}_{win_num}",
                    "question": f"Will {name} go up or down in the next {label}?",
                    "tokens": [
                        {"token_id": f"{sym_lower}_up_{label}_{win_num}", "outcome": "Up"},
                        {"token_id": f"{sym_lower}_dn_{label}_{win_num}", "outcome": "Down"},
                    ],
                    "game_start_time": _epoch_to_iso(win_start),
                    "end_date_iso": _epoch_to_iso(win_end),
                })
        return {"data": markets}

    def get_order_book(self, token_id):
        # Add small random noise to prices each call to simulate real market
        noise = _random.uniform(-0.02, 0.02)
        if "up" in token_id:
            ask = round(0.46 + noise, 2)
            ask = max(0.10, min(0.90, ask))
            return _MockOrderBook(bid_price=round(ask - 0.02, 2), ask_price=ask)
        else:
            ask = round(0.48 + noise, 2)
            ask = max(0.10, min(0.90, ask))
            return _MockOrderBook(bid_price=round(ask - 0.02, 2), ask_price=ask)

    def create_order(self, order_args):
        return {"signed": True}

    def post_order(self, signed, order_type):
        return {"orderID": f"dry-mock-{int(time.time())}", "status": "dry_run"}

    def cancel(self, order_id):
        return {"cancelled": True}

    def cancel_all(self):
        return {"cancelled": True}

    def get_balance_allowance(self, params=None):
        return {"balance": 1_000_000_000}  # $1000 USDC (6 decimals)


def _epoch_to_iso(epoch: int) -> str:
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


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
        clob_client = MockClobClient()
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
