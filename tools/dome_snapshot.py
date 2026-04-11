"""
Dome historical snapshot tool for 15m BTC up/down markets.

Usage:
    python tools/dome_snapshot.py --date 2026-04-10 --asset BTC --timeframe 15m \
        --out data/dome_snapshots/ --hours 1

For each market window it fetches:
  1. Market metadata (market_slug → condition_id, token_ids)
  2. Candlesticks (1m interval) for the window
  3. Orderbook snapshots for the UP token (token_ids[0])
  4. Orderbook snapshots for the DN token (token_ids[1]), if present
  5. Binance BTCUSDT prices
  6. Chainlink BTC/USD prices

Output: one JSONL file per market under <out>/<market_slug>.jsonl
  Line 1: {"type": "header", "market_slug": ..., "condition_id": ..., ...}
  Following lines:
    {"type": "candle",     "data": {...}}
    {"type": "orderbook",  "side": "UP", "data": {...}}   ← UP token orderbook
    {"type": "orderbook",  "side": "DN", "data": {...}}   ← DN token orderbook (new)
    {"type": "binance",    "data": {...}}
    {"type": "chainlink",  "data": {...}}

The "side" field on orderbook entries distinguishes the UP and DN token books.
Old files (pre-schema-upgrade) have no "side" field; the backtester falls back
to the UP-bid approximation for those.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
import datetime
import logging
from typing import Iterator

# Make tools/ importable regardless of cwd
_TOOLS_DIR = pathlib.Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# Load DOME_API_KEY from project .env if not already set in env
if not os.environ.get("DOME_API_KEY"):
    _env_path = _TOOLS_DIR.parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            if _line.startswith("DOME_API_KEY="):
                os.environ["DOME_API_KEY"] = _line.split("=", 1)[1].strip()
                break

from dome_client import DomeClient, DomeAPIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dome_snapshot")

# Pause duration when persistent rate-limit is hit
_RATE_LIMIT_PAUSE_SEC = 60


# ---------------------------------------------------------------------------
# Market slug generation
# ---------------------------------------------------------------------------

def _15m_windows_for_date(date: datetime.date, hours: int) -> list[tuple[int, int]]:
    """Return list of (window_start_sec, window_end_sec) for each 15m slot.

    Starts at midnight UTC of *date*, up to *hours* hours.
    """
    midnight_utc = int(
        datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc).timestamp()
    )
    windows = []
    n_windows = hours * 4  # 4 × 15 min = 1 hour
    for i in range(n_windows):
        start = midnight_utc + i * 900  # 900 s = 15 min
        end = start + 900
        windows.append((start, end))
    return windows


def _market_slug(asset: str, timeframe: str, window_start_sec: int) -> str:
    """Construct the Polymarket market slug.

    Pattern: ``btc-updown-15m-<epoch_sec>``
    """
    return f"{asset.lower()}-updown-{timeframe}-{window_start_sec}"


# ---------------------------------------------------------------------------
# Core snapshot logic
# ---------------------------------------------------------------------------

def fetch_market_snapshot(
    client: DomeClient,
    slug: str,
    window_start: int,
    window_end: int,
    currency_binance: str,
    currency_chainlink: str,
) -> list[dict]:
    """Fetch all data for one market window.  Returns list of dicts to write as JSONL."""
    lines: list[dict] = []

    # 1. Market metadata
    market_data = client.get_market(slug)
    # Extract condition_id and token_ids from the response
    # Dome returns the market object directly or wrapped — handle both
    market_obj = market_data
    if "markets" in market_data:
        # Shape: {"markets": [...], "pagination": {...}}
        markets_list = market_data["markets"]
        market_obj = markets_list[0] if markets_list else {}
    elif "market" in market_data:
        market_obj = market_data["market"]
    elif isinstance(market_data, list) and market_data:
        market_obj = market_data[0]

    condition_id: str = market_obj.get("condition_id", "")

    # token_ids: prefer explicit list, fall back to side_a/side_b token IDs (real Dome shape)
    token_ids_raw = market_obj.get("token_ids", [])
    if isinstance(token_ids_raw, str):
        token_ids = [t.strip() for t in token_ids_raw.split(",") if t.strip()]
    else:
        token_ids = list(token_ids_raw)

    if not token_ids:
        # Real Dome shape uses side_a.id / side_b.id as token IDs
        side_a = market_obj.get("side_a", {})
        side_b = market_obj.get("side_b", {})
        if side_a.get("id"):
            token_ids = [str(side_a["id"])]
            if side_b.get("id"):
                token_ids.append(str(side_b["id"]))

    up_token_id = token_ids[0] if token_ids else ""
    dn_token_id = token_ids[1] if len(token_ids) > 1 else ""

    lines.append({
        "type": "header",
        "market_slug": slug,
        "condition_id": condition_id,
        "token_ids": token_ids,
        "up_token_id": up_token_id,
        "dn_token_id": dn_token_id,
        "window_start": window_start,
        "window_end": window_end,
        "fetched_at": int(time.time()),
        "raw_market": market_obj,
    })

    # 2. Candlesticks
    if condition_id:
        candles = client.get_candlesticks(condition_id, window_start, window_end, interval="1m")
        for c in candles:
            lines.append({"type": "candle", "data": c})
    else:
        logger.warning("%s: no condition_id — skipping candles", slug)

    # 3. Orderbook snapshots — UP token
    if up_token_id:
        snapshots = client.get_orderbook_snapshots(up_token_id, window_start, window_end)
        for s in snapshots:
            lines.append({"type": "orderbook", "side": "UP", "data": s})
    else:
        logger.warning("%s: no UP token_id — skipping UP orderbook", slug)

    # 4. Orderbook snapshots — DN token (enables accurate paired-fill simulation)
    if dn_token_id:
        dn_snapshots = client.get_orderbook_snapshots(dn_token_id, window_start, window_end)
        for s in dn_snapshots:
            lines.append({"type": "orderbook", "side": "DN", "data": s})
    else:
        logger.warning("%s: no DN token_id — skipping DN orderbook", slug)

    # 4. Binance prices
    binance_prices = client.get_binance_prices(currency_binance, window_start, window_end)
    for p in binance_prices:
        lines.append({"type": "binance", "data": p})

    # 5. Chainlink prices
    chainlink_prices = client.get_chainlink_prices(currency_chainlink, window_start, window_end)
    for p in chainlink_prices:
        lines.append({"type": "chainlink", "data": p})

    return lines


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    date: datetime.date,
    asset: str,
    timeframe: str,
    out_dir: pathlib.Path,
    hours: int,
    force: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    currency_map = {
        "BTC": ("btcusdt", "btc/usd"),
        "ETH": ("ethusdt", "eth/usd"),
        "SOL": ("solusdt", "sol/usd"),
    }
    asset_upper = asset.upper()
    if asset_upper not in currency_map:
        logger.error("Unknown asset %r — supported: %s", asset, ", ".join(currency_map))
        sys.exit(1)
    currency_binance, currency_chainlink = currency_map[asset_upper]

    windows = _15m_windows_for_date(date, hours)
    total = len(windows)

    total_requests = 0
    total_bytes = 0
    errors: list[str] = []
    skipped = 0
    fetched = 0

    cache_dir = out_dir / "_cache"
    # Historical data is immutable -- 30 day TTL is fine
    with DomeClient(cache_dir=cache_dir, cache_ttl_sec=30 * 86_400) as client:
        for idx, (w_start, w_end) in enumerate(windows, 1):
            slug = _market_slug(asset, timeframe, w_start)
            out_file = out_dir / f"{slug}.jsonl"

            # Idempotency check
            if not force and out_file.exists() and out_file.stat().st_size > 0:
                print(f"[{idx:3d}/{total}] {slug}  (skipped — file exists)")
                skipped += 1
                continue

            t0 = time.monotonic()
            try:
                lines = fetch_market_snapshot(
                    client, slug, w_start, w_end, currency_binance, currency_chainlink
                )
                # Estimate requests: market + candles + UP-ob + DN-ob + binance + chainlink = 6
                total_requests += 6

                jsonl_text = "\n".join(json.dumps(ln) for ln in lines)
                out_file.write_text(jsonl_text, encoding="utf-8")
                total_bytes += len(jsonl_text.encode())
                elapsed = time.monotonic() - t0
                n_candles = sum(1 for l in lines if l["type"] == "candle")
                n_ob = sum(1 for l in lines if l["type"] == "orderbook")
                n_binance = sum(1 for l in lines if l["type"] == "binance")
                print(
                    f"[{idx:3d}/{total}] {slug}  "
                    f"candles={n_candles} ob={n_ob} binance={n_binance}  "
                    f"{elapsed:.1f}s"
                )
                fetched += 1

            except DomeAPIError as exc:
                if exc.status_code == 429:
                    print(
                        f"[{idx:3d}/{total}] {slug}  RATE LIMITED — pausing {_RATE_LIMIT_PAUSE_SEC}s"
                    )
                    time.sleep(_RATE_LIMIT_PAUSE_SEC)
                    # Retry once after pause
                    try:
                        lines = fetch_market_snapshot(
                            client, slug, w_start, w_end, currency_binance, currency_chainlink
                        )
                        out_file.write_text(
                            "\n".join(json.dumps(ln) for ln in lines), encoding="utf-8"
                        )
                        fetched += 1
                        print(f"  -> retry succeeded for {slug}")
                    except Exception as exc2:
                        err = f"{slug}: {exc2}"
                        errors.append(err)
                        print(f"  -> retry failed: {exc2}")
                else:
                    err = f"{slug}: HTTP {exc.status_code} {exc.body[:100]}"
                    errors.append(err)
                    print(f"[{idx:3d}/{total}] {slug}  ERROR {exc.status_code}")

            except Exception as exc:
                err = f"{slug}: {exc}"
                errors.append(err)
                print(f"[{idx:3d}/{total}] {slug}  ERROR {exc}")

    # Summary
    print()
    print("=" * 60)
    print(f"Summary for {date} {asset} {timeframe} ({hours}h)")
    print(f"  Markets fetched : {fetched}")
    print(f"  Markets skipped : {skipped}")
    print(f"  Markets errored : {len(errors)}")
    print(f"  Total requests  : {total_requests}")
    print(f"  Total bytes     : {total_bytes:,}")
    if errors:
        print()
        print("Errors:")
        for e in errors:
            print(f"  {e}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Fetch historical Dome data for 15m BTC up/down markets"
    )
    parser.add_argument(
        "--date",
        required=True,
        help="Date to fetch (YYYY-MM-DD, UTC)",
    )
    parser.add_argument("--asset", default="BTC", help="Asset: BTC, ETH, SOL (default: BTC)")
    parser.add_argument("--timeframe", default="15m", help="Timeframe (default: 15m)")
    parser.add_argument(
        "--out",
        default="data/dome_snapshots",
        help="Output directory (default: data/dome_snapshots)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=1,
        help="Number of hours to fetch (default: 1). 1h = 4 markets.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch even if output file already exists",
    )
    args = parser.parse_args(argv)

    try:
        date = datetime.date.fromisoformat(args.date)
    except ValueError:
        print(f"Invalid date {args.date!r} — expected YYYY-MM-DD")
        sys.exit(1)

    run(
        date=date,
        asset=args.asset,
        timeframe=args.timeframe,
        out_dir=pathlib.Path(args.out),
        hours=args.hours,
        force=args.force,
    )


if __name__ == "__main__":
    main()
