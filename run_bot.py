#!/usr/bin/env python3
"""PolyBot Trading Engine — Entry Point.

Usage:
    python run_bot.py

Requires PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env
"""

import asyncio
import logging
import sys

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

from polybot.config import load_bot_config
from polybot.bot import Bot


def create_clob_client(cfg):
    client = ClobClient(
        cfg.polymarket_host,
        key=cfg.private_key,
        chain_id=cfg.chain_id,
        creds=ApiCreds(
            api_key=cfg.api_key,
            api_secret=cfg.api_secret,
            api_passphrase=cfg.api_passphrase,
        ),
    )
    return client


def main():
    cfg = load_bot_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not cfg.private_key:
        print("ERROR: PRIVATE_KEY not set in .env — cannot trade without it.")
        sys.exit(1)

    clob_client = create_clob_client(cfg)

    # Query initial balance
    try:
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        balance_info = clob_client.get_balance_allowance(
            params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        bankroll = float(balance_info.get("balance", 0)) / 1e6  # USDC has 6 decimals
        print(f"Starting bankroll: ${bankroll:,.2f} USDC")
    except Exception as e:
        print(f"Could not fetch balance ({e}), using default $1000")
        bankroll = 1000.0

    bot = Bot(cfg, clob_client=clob_client, initial_bankroll=bankroll)

    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        print("\nBot stopped.")


if __name__ == "__main__":
    main()
