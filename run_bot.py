#!/usr/bin/env python3
"""PolyBot entry point — launch the ladder market maker.

Usage:
    python run_bot.py

In DRY_RUN=true mode (default), no credentials are required — a paper CLOB
client simulates orders using real market data for fill simulation.

Requires PRIVATE_KEY, API_KEY, API_SECRET, API_PASSPHRASE in .env for live mode.
"""

import asyncio
import logging
import sys

from polybot.config import load_bot_config
from polybot.bot import Bot
from polybot.web.server import create_app, start_gui_server


def main():
    cfg = load_bot_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("polybot.log"),
        ],
    )
    log = logging.getLogger("polybot")

    mode = "PAPER" if cfg.dry_run else "LIVE"
    log.info("Starting PolyBot in %s mode", mode)

    if not cfg.dry_run and not cfg.private_key:
        log.error("PRIVATE_KEY not set — required for live trading")
        sys.exit(1)

    if not cfg.dry_run:
        print("\n!!  LIVE TRADING MODE — real orders will be placed!")
        print(f"   Bankroll: ${cfg.bankroll:,.2f}")
        confirm = input("   Type 'CONFIRM' to proceed: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            sys.exit(0)

    bot = Bot(cfg)

    app = create_app(
        state=bot.gui_state,
        start_fn=lambda: bot.ui_start_full(),
        stop_fn=bot.ui_stop,
    )

    async def run():
        runner = await start_gui_server(app, cfg.web_port)
        log.info("Dashboard at http://127.0.0.1:%d", cfg.web_port)
        log.info("Press Start in the dashboard to begin trading")

        # Keep alive — bot.run() is triggered by the Start button
        try:
            await bot.run_standby()
        except asyncio.CancelledError:
            pass
        finally:
            await bot.stop()
            await runner.cleanup()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)


if __name__ == "__main__":
    main()
