import argparse
import asyncio
import logging
import time
from pathlib import Path
from uuid import uuid4

from rich.console import Console
from rich.live import Live

from polybot.config import load_config
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter
from polybot.tracker.trade_poller import run_trade_poller
from polybot.tracker.spot_recorder import run_spot_recorder
from polybot.tracker.settlement_tracker import run_settlement_tracker
from polybot.tracker.book_recorder import run_book_recorder
from polybot.tracker.dashboard import build_display

logger = logging.getLogger("tracker")
console = Console()

MAX_RESTARTS = 3


async def _supervised_task(name: str, coro_factory, *args):
    """Run a collector with restart-on-failure (up to MAX_RESTARTS)."""
    restarts = 0
    while restarts <= MAX_RESTARTS:
        try:
            await coro_factory(*args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            restarts += 1
            if restarts > MAX_RESTARTS:
                logger.error(f"{name} failed {MAX_RESTARTS} times, giving up: {e}")
                return
            logger.critical(f"{name} crashed (restart {restarts}/{MAX_RESTARTS}): {e}")
            await asyncio.sleep(2)


async def main():
    parser = argparse.ArgumentParser(description="0x8dxd Tracker — Modular Pipeline")
    parser.add_argument("--no-display", action="store_true", help="Run headless")
    args = parser.parse_args()

    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    session_id = uuid4().hex[:12]
    state = TrackerState(session_id=session_id)
    data_dir = Path(__file__).resolve().parent.parent.parent / "data" / "tracker"
    writer = TrackerCSVWriter(data_dir, session_id)

    start_time = time.time()

    console.print(f"[bold cyan]Starting 0x8dxd Tracker (session {session_id})...[/]")
    console.print(f"  Wallet: {cfg.tracked_wallet}")
    console.print(f"  Poll interval: {cfg.trade_poll_interval_sec}s")
    console.print(f"  CSV output: {data_dir}")
    console.print()

    # Launch collectors
    tasks = [
        asyncio.create_task(
            _supervised_task("spot_recorder", run_spot_recorder, cfg, state, writer)
        ),
        asyncio.create_task(
            _supervised_task("trade_poller", run_trade_poller, cfg, state, writer)
        ),
        asyncio.create_task(
            _supervised_task("settlement_tracker", run_settlement_tracker, cfg, state, writer)
        ),
        asyncio.create_task(
            _supervised_task("book_recorder", run_book_recorder, cfg, state, writer)
        ),
    ]

    try:
        if args.no_display:
            # Headless — just wait
            await asyncio.gather(*tasks)
        else:
            with Live(
                build_display(cfg, state, start_time),
                console=console,
                refresh_per_second=1,
            ) as live:
                while True:
                    await asyncio.sleep(1)
                    live.update(build_display(cfg, state, start_time))
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/]")
    finally:
        for t in tasks:
            t.cancel()
        # Wait for tasks to finish cancellation
        await asyncio.gather(*tasks, return_exceptions=True)
        writer.close()
        trade_count = sum(len(t) for t in state.whale_trades.values())
        console.print(f"[green]Session {session_id} complete. {trade_count} trades captured.[/]")
        console.print(f"[green]CSV output: {data_dir}[/]")
