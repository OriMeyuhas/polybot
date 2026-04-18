"""Backfill pre-window Binance kline data for dome snapshot markets.

For each dome snapshot file in data/dome_snapshots/, fetches the 2-minute
pre-window Binance 1s klines (window_start - 120s to window_start + 30s)
and writes them to data/dome_snapshots_binance_prewindow/{market_id}.jsonl.

Usage:
    python tools/dome_binance_backfill.py [--dome-dir <path>] [--out-dir <path>]

Rate-limit: ~50ms sleep between requests → ~1755 markets ≈ 90s wall-clock.
Idempotent: skips markets where output file already exists.

Output format (JSONL, one row per 1s kline):
    {"ts_ms": <int>, "close_price": <float>}
"""
from __future__ import annotations

import json
import logging
import pathlib
import sys
import time

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dome_binance_backfill")

# Binance REST endpoint for klines
_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"

# Pre-window range: 2 min lookback + 30s into window
_LOOKBACK_SEC = 120
_FORWARD_SEC = 30

# Rate-limit sleep between requests (50ms)
_SLEEP_SEC = 0.05

# Progress log every N markets
_LOG_EVERY = 100


def backfill_market(
    dome_file: pathlib.Path,
    out_dir: pathlib.Path,
    market_id: str,
    window_start: int,
) -> str:
    """Fetch and write pre-window Binance klines for a single market.

    Returns:
        "skipped" if output file already exists (idempotent).
        "written" if data was fetched and written.
        "error"   if the HTTP request or parsing failed.
    """
    out_file = out_dir / f"{market_id}.jsonl"

    # Idempotent: skip if already done
    if out_file.exists():
        return "skipped"

    start_ms = (window_start - _LOOKBACK_SEC) * 1000
    end_ms = (window_start + _FORWARD_SEC) * 1000

    params = {
        "symbol": "BTCUSDT",
        "interval": "1s",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 1000,  # max; our range is 150s → 1 request
    }

    try:
        resp = requests.get(_BINANCE_KLINES_URL, params=params, timeout=15)
        resp.raise_for_status()
        klines = resp.json()
    except Exception as exc:
        logger.warning("HTTP error for %s: %s", market_id, exc)
        return "error"

    rows = []
    for kline in klines:
        # kline format: [open_time_ms, open, high, low, close, ...]
        try:
            ts_ms = int(kline[0])
            close_price = float(kline[4])
            rows.append({"ts_ms": ts_ms, "close_price": close_price})
        except (IndexError, ValueError, TypeError) as exc:
            logger.debug("Bad kline entry for %s: %s -> %s", market_id, kline, exc)
            continue

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")

    return "written"


def run_backfill(
    dome_dir: pathlib.Path,
    out_dir: pathlib.Path,
) -> None:
    """Run backfill for all dome snapshot files in dome_dir.

    Processes all *.jsonl files whose header type is "header".
    Sleeps _SLEEP_SEC between HTTP requests to respect Binance rate limits.
    Logs progress every _LOG_EVERY markets.
    """
    dome_files = sorted(dome_dir.glob("*.jsonl"))
    logger.info("Found %d dome snapshot files in %s", len(dome_files), dome_dir)

    written = 0
    skipped = 0
    errors = 0

    for idx, dome_file in enumerate(dome_files):
        # Read header to extract market_id and window_start
        market_id: str | None = None
        window_start: int | None = None
        try:
            with open(dome_file, encoding="utf-8") as f:
                first_line = f.readline().strip()
            if not first_line:
                continue
            header = json.loads(first_line)
            if header.get("type") != "header":
                continue
            market_id = header.get("market_slug", "")
            window_start = int(header.get("window_start", 0))
        except Exception as exc:
            logger.warning("Cannot parse header of %s: %s", dome_file.name, exc)
            continue

        if not market_id or not window_start:
            continue

        result = backfill_market(
            dome_file=dome_file,
            out_dir=out_dir,
            market_id=market_id,
            window_start=window_start,
        )

        if result == "written":
            written += 1
            time.sleep(_SLEEP_SEC)
        elif result == "skipped":
            skipped += 1
        elif result == "error":
            errors += 1

        if (idx + 1) % _LOG_EVERY == 0:
            logger.info(
                "Progress: %d/%d | written=%d skipped=%d errors=%d",
                idx + 1, len(dome_files), written, skipped, errors,
            )

    logger.info(
        "Backfill complete: written=%d skipped=%d errors=%d total=%d",
        written, skipped, errors, len(dome_files),
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Backfill pre-window Binance klines for dome snapshot markets."
    )
    parser.add_argument(
        "--dome-dir",
        default="data/dome_snapshots",
        help="Directory containing dome snapshot JSONL files (default: data/dome_snapshots)",
    )
    parser.add_argument(
        "--out-dir",
        default="data/dome_snapshots_binance_prewindow",
        help="Output directory for pre-window kline files "
             "(default: data/dome_snapshots_binance_prewindow)",
    )
    args = parser.parse_args()

    dome_dir = pathlib.Path(args.dome_dir)
    out_dir = pathlib.Path(args.out_dir)

    if not dome_dir.exists():
        logger.error("Dome directory not found: %s", dome_dir)
        sys.exit(1)

    run_backfill(dome_dir=dome_dir, out_dir=out_dir)


if __name__ == "__main__":
    main()
