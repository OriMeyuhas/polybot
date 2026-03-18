#!/usr/bin/env python3
"""
PolyBot — 0x8dxd Live Activity Tracker (Modular Pipeline)

Polls Polymarket for wallet 0x8dxd's trades, tracks settlements and PnL,
records Binance spot prices and CLOB order book depth to CSV for offline analysis.

Usage:
    python tracker.py              # with live dashboard
    python tracker.py --no-display # headless collection
"""

import asyncio
from polybot.tracker.runner import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
