#!/usr/bin/env python3
"""
PolyBot Phase 0 — 0x8dxd Live Activity Tracker

Polls Polymarket for wallet 0x8dxd's trades, cross-references Binance spot
prices, classifies strategies, logs to CSV, and displays a live terminal UI.

Usage:
    python tracker.py
"""

import asyncio
import csv
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets
from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text

from polybot.config import load_config, TrackerConfig
from polybot.utils.time_utils import utc_now, parse_iso, format_duration

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
console = Console()
logger = logging.getLogger("tracker")

# Spot prices updated by Binance websocket
spot_prices: dict[str, float] = {}

# All trades we've seen (keyed by tx_hash + outcome to dedup)
seen_trade_keys: set[str] = set()

# Recent trades for display (ring buffer)
recent_trades: list[dict] = []
MAX_RECENT = 20

# Running stats
stats = {
    "total_trades": 0,
    "start_time": None,
    "directional_prices": [],
    "spread_prices": [],
    "spread_pairs": 0,
    "by_asset": {},
    "by_timeframe": {},
    "timing_buckets": {"early": 0, "mid": 0, "late": 0},
}

# Markets we've seen trades in — keyed by slug
# Used for spread capture detection (both UP and DOWN in same market)
market_sides: dict[str, set[str]] = {}


# ---------------------------------------------------------------------------
# CSV Logger
# ---------------------------------------------------------------------------
class CSVLogger:
    COLUMNS = [
        "timestamp", "tx_hash", "asset", "timeframe", "market_id", "side",
        "price", "size_usd", "spot_price", "spot_delta_pct",
        "window_elapsed_sec", "window_total_sec", "strategy_guess",
        "book_best_bid", "book_best_ask", "book_spread_pct",
    ]

    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        date_str = utc_now().strftime("%Y%m%d")
        self.path = data_dir / f"0x8dxd_{date_str}.csv"
        self._file = None
        self._writer = None

    def open(self):
        is_new = not self.path.exists()
        self._file = open(self.path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=self.COLUMNS)
        if is_new:
            self._writer.writeheader()
            self._file.flush()

    def write(self, row: dict):
        if self._writer:
            self._writer.writerow(row)
            self._file.flush()

    def close(self):
        if self._file:
            self._file.close()


# ---------------------------------------------------------------------------
# Slug Parsing
# ---------------------------------------------------------------------------
# Map full names that appear in slugs to ticker symbols
_ASSET_NORMALIZE = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "xrp": "XRP", "btc": "BTC", "eth": "ETH", "sol": "SOL",
    "ftse": "FTSE", "spx": "SPX", "ndx": "NDX",
}


def _normalize_asset(raw: str) -> str:
    return _ASSET_NORMALIZE.get(raw.lower(), raw.upper())


def parse_slug(slug: str) -> dict:
    """
    Parse a Polymarket event slug. Handles two known formats:

    Format 1 (intraday):  btc-updown-15m-1773756900
    Format 2 (hourly):    xrp-up-or-down-march-17-2026-10am-et
                          bitcoin-up-or-down-march-17-2026-10am-et

    Returns {'asset', 'timeframe', 'window_start_epoch'}.
    """
    # Format 1: {asset}-updown-{Nm|Nh}-{epoch}
    m = re.match(r"^([a-z0-9]+)-updown-(\d+[mh])-(\d+)$", slug)
    if m:
        return {
            "asset": _normalize_asset(m.group(1)),
            "timeframe": m.group(2),
            "window_start_epoch": int(m.group(3)),
        }

    # Format 2: {asset}-up-or-down-{month}-{day}-{year}-{hour}am/pm-et
    # e.g. xrp-up-or-down-march-17-2026-10am-et
    m2 = re.match(
        r"^([a-z0-9]+)-up-or-down-([a-z]+-\d+-\d+-\d+[ap]m(?:-\d+[ap]m)?)-et$",
        slug,
    )
    if m2:
        asset = m2.group(1).upper()
        time_part = m2.group(2)  # e.g. march-17-2026-10am or march-17-2026-10am-11am

        # Check if it's a range (contains two time tokens) → infer timeframe
        times = re.findall(r"(\d+)([ap]m)", time_part)
        if len(times) == 2:
            # Two times → compute window duration
            def to_minutes(hour_str, ampm):
                h = int(hour_str)
                if ampm == "pm" and h != 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                return h * 60
            start_min = to_minutes(*times[0])
            end_min = to_minutes(*times[1])
            diff = (end_min - start_min) % (24 * 60)
            if diff == 60:
                tf = "1h"
            elif diff == 30:
                tf = "30m"
            elif diff == 15:
                tf = "15m"
            else:
                tf = f"{diff}m"
        else:
            # Single hour token → assume 1h window
            tf = "1h"

        return {"asset": _normalize_asset(asset), "timeframe": tf, "window_start_epoch": 0}

    return {"asset": "UNKNOWN", "timeframe": "?", "window_start_epoch": 0}


def parse_title_fallback(title: str) -> dict:
    """Extract asset and timeframe from title when slug parsing fails."""
    result = {"asset": "UNKNOWN", "timeframe": "?", "window_start_epoch": 0}

    title_lower = title.lower()
    for keyword, symbol in _ASSET_NORMALIZE.items():
        if keyword in title_lower:
            result["asset"] = symbol
            break

    # "10:15AM-10:30AM" → 15m window
    range_match = re.search(
        r"(\d{1,2}):(\d{2})[AP]M\s*-\s*(\d{1,2}):(\d{2})[AP]M", title, re.IGNORECASE
    )
    if range_match:
        start_min = int(range_match.group(1)) * 60 + int(range_match.group(2))
        end_min = int(range_match.group(3)) * 60 + int(range_match.group(4))
        diff = (end_min - start_min) % (24 * 60)
        if diff > 0:
            result["timeframe"] = f"{diff}m" if diff < 60 else f"{diff // 60}h"
        return result

    # "10AM ET" with no range → 1h
    if re.search(r"\d{1,2}[AP]M\s+ET", title, re.IGNORECASE):
        result["timeframe"] = "1h"

    return result


TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
}


# ---------------------------------------------------------------------------
# Binance WebSocket for Spot Prices
# ---------------------------------------------------------------------------
async def binance_spot_ws(cfg: TrackerConfig):
    """Subscribe to Binance spot price tickers for tracked assets."""
    streams = [f"{a.lower()}usdt@ticker" for a in cfg.assets]
    url = f"{cfg.binance_ws_url}/{'/'.join(streams)}"

    while True:
        try:
            async with websockets.connect(url) as ws:
                logger.info("Binance WS connected")
                async for msg in ws:
                    data = json.loads(msg)
                    if "s" in data and "c" in data:
                        # Symbol like BTCUSDT -> BTC
                        symbol = data["s"].replace("USDT", "")
                        spot_prices[symbol] = float(data["c"])
        except Exception as e:
            logger.warning(f"Binance WS error: {e}, reconnecting in 5s...")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# CLOB Order Book Snapshot
# ---------------------------------------------------------------------------
async def fetch_orderbook(token_id: str, client: httpx.AsyncClient) -> dict:
    """
    Fetch the current CLOB order book for a token and return best bid/ask/spread.
    The `asset` field on every trade IS the CLOB token ID — no Gamma lookup needed.
    """
    try:
        resp = await client.get(
            "https://clob.polymarket.com/book",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        book = resp.json()

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        best_bid = float(bids[0]["price"]) if bids else 0.0
        best_ask = float(asks[0]["price"]) if asks else 0.0
        spread_pct = ((best_ask - best_bid) / best_ask * 100) if best_ask > 0 else 0.0

        return {
            "book_best_bid": f"{best_bid:.4f}",
            "book_best_ask": f"{best_ask:.4f}",
            "book_spread_pct": f"{spread_pct:.2f}%",
        }
    except Exception as e:
        logger.debug(f"Orderbook fetch failed for {token_id[:16]}...: {e}")
        return {"book_best_bid": "N/A", "book_best_ask": "N/A", "book_spread_pct": "N/A"}


# ---------------------------------------------------------------------------
# Polymarket Poller
# ---------------------------------------------------------------------------
async def poll_trades(cfg: TrackerConfig, csv_logger: CSVLogger):
    """Poll Polymarket for new trades from the tracked wallet."""
    url = f"{cfg.polymarket_data_api}/activity"

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                resp = await client.get(url, params={
                    "user": cfg.tracked_wallet,
                    "limit": 50,
                })
                resp.raise_for_status()
                trades = resp.json()

                for trade in trades:
                    await process_trade(trade, csv_logger, client)

            except httpx.HTTPStatusError as e:
                logger.warning(f"API error {e.response.status_code}: {e}")
            except Exception as e:
                logger.warning(f"Poll error: {e}")

            await asyncio.sleep(cfg.poll_interval_sec)


async def process_trade(trade: dict, csv_logger: CSVLogger, client: httpx.AsyncClient):
    """Process a single trade, dedup, fetch order book, classify, log."""
    tx_hash = trade.get("transactionHash", "")
    outcome = trade.get("outcome", "")
    trade_key = f"{tx_hash}:{outcome}"

    if trade_key in seen_trade_keys:
        return
    seen_trade_keys.add(trade_key)

    # Parse market info
    slug = trade.get("eventSlug", "") or trade.get("slug", "")
    parsed = parse_slug(slug)
    if parsed["asset"] == "UNKNOWN":
        parsed = parse_title_fallback(trade.get("title", ""))

    asset = parsed["asset"]
    timeframe = parsed["timeframe"]
    window_start = parsed["window_start_epoch"]

    # Trade details
    trade_ts = trade.get("timestamp", 0)
    side_raw = trade.get("side", "")  # BUY or SELL
    outcome_raw = trade.get("outcome", "")  # Up or Down
    price = float(trade.get("price", 0))
    size_shares = float(trade.get("size", 0))
    size_usd = float(trade.get("usdcSize", 0)) or (price * size_shares)

    # Determine effective direction:
    # BUY "Up" = betting UP, BUY "Down" = betting DOWN
    # SELL "Up" = exiting UP position, SELL "Down" = exiting DOWN position
    if side_raw == "BUY":
        effective_side = "UP" if outcome_raw == "Up" else "DOWN"
    else:
        effective_side = "EXIT_UP" if outcome_raw == "Up" else "EXIT_DOWN"

    # Spot price at trade time
    spot = spot_prices.get(asset, 0.0)
    spot_delta_pct = 0.0

    # Window timing
    window_total = TIMEFRAME_SECONDS.get(timeframe, 0)
    window_elapsed = (trade_ts - window_start) if window_start > 0 else 0

    # Strategy classification
    strategy = classify_strategy(
        slug, effective_side, window_elapsed, window_total, spot_delta_pct, asset
    )

    # Order book snapshot — token ID is the `asset` field from the trade
    token_id = trade.get("asset", "")
    book = await fetch_orderbook(token_id, client) if token_id else {
        "book_best_bid": "N/A", "book_best_ask": "N/A", "book_spread_pct": "N/A"
    }

    # Build trade record
    record = {
        "timestamp": datetime.fromtimestamp(trade_ts, tz=timezone.utc).isoformat(),
        "tx_hash": tx_hash[:16] + "...",
        "asset": asset,
        "timeframe": timeframe,
        "market_id": slug,
        "side": effective_side,
        "price": f"{price:.4f}",
        "size_usd": f"{size_usd:.2f}",
        "spot_price": f"{spot:.2f}" if spot > 0 else "N/A",
        "spot_delta_pct": f"{spot_delta_pct:+.2f}%",
        "window_elapsed_sec": window_elapsed,
        "window_total_sec": window_total,
        "strategy_guess": strategy,
        **book,
    }

    # Log to CSV
    csv_logger.write(record)

    # Update display buffer
    display_record = {
        **record,
        "time_short": datetime.fromtimestamp(trade_ts, tz=timezone.utc).strftime("%H:%M:%S"),
        "window_phase": f"{window_elapsed // 60:02d}:{window_elapsed % 60:02d}/{window_total // 60:02d}:00"
            if window_total > 0 else "N/A",
    }
    recent_trades.insert(0, display_record)
    if len(recent_trades) > MAX_RECENT:
        recent_trades.pop()

    # Update stats
    update_stats(record, asset, timeframe, price, effective_side, window_elapsed, window_total)

    logger.info(
        f"NEW: {asset} {timeframe} {effective_side} ${price:.2f} "
        f"${size_usd:.1f} [{strategy}]"
    )


def classify_strategy(
    slug: str, side: str, elapsed: int, total: int, spot_delta: float, asset: str
) -> str:
    """Classify the likely strategy behind a trade."""
    # Track market sides for spread detection
    if slug not in market_sides:
        market_sides[slug] = set()
    market_sides[slug].add(side.replace("EXIT_", ""))

    # Spread Capture: both UP and DOWN seen in same market
    if len(market_sides.get(slug, set())) >= 2:
        stats["spread_pairs"] = sum(
            1 for s in market_sides.values() if len(s) >= 2
        )
        return "Spread Capture"

    # Latency Arb: late in window + significant spot movement
    if total > 0 and elapsed > 0:
        pct_elapsed = elapsed / total
        if pct_elapsed > 0.53 and abs(spot_delta) > 0.2:
            return "Latency Arb"

    # Pre-positioning: very early in window
    if total > 0 and elapsed > 0:
        pct_elapsed = elapsed / total
        if pct_elapsed < 0.15:
            return "Pre-positioning"

    # Exit: selling existing position
    if side.startswith("EXIT"):
        return "Exit"

    return "Directional"


def update_stats(
    record: dict, asset: str, timeframe: str, price: float,
    side: str, elapsed: int, total: int
):
    """Update running statistics."""
    stats["total_trades"] += 1

    # Asset breakdown
    stats["by_asset"][asset] = stats["by_asset"].get(asset, 0) + 1

    # Timeframe breakdown
    stats["by_timeframe"][timeframe] = stats["by_timeframe"].get(timeframe, 0) + 1

    # Price tracking by strategy type
    if record["strategy_guess"] == "Spread Capture":
        stats["spread_prices"].append(price)
    else:
        stats["directional_prices"].append(price)

    # Timing bucket
    if total > 0 and elapsed > 0:
        pct = elapsed / total
        if pct < 0.33:
            stats["timing_buckets"]["early"] += 1
        elif pct < 0.66:
            stats["timing_buckets"]["mid"] += 1
        else:
            stats["timing_buckets"]["late"] += 1


# ---------------------------------------------------------------------------
# Terminal Display (Rich)
# ---------------------------------------------------------------------------
def build_display(cfg: TrackerConfig) -> Layout:
    """Build the rich terminal layout."""
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="spot", size=3),
        Layout(name="trades", ratio=3),
        Layout(name="stats", size=8),
    )

    # Header
    uptime = format_duration(time.time() - stats["start_time"]) if stats["start_time"] else "00:00:00"
    wallet_short = f"{cfg.tracked_wallet[:6]}...{cfg.tracked_wallet[-4:]}"
    header_text = Text.from_markup(
        f"[bold cyan]0x8dxd TRACKER — Live Activity Monitor[/]\n"
        f"Tracking: {wallet_short} | Uptime: {uptime} | "
        f"Trades seen: {stats['total_trades']}"
    )
    layout["header"].update(Panel(header_text, style="bold"))

    # Spot prices
    spot_parts = []
    for asset in cfg.assets:
        p = spot_prices.get(asset, 0)
        if p > 0:
            spot_parts.append(f"[bold]{asset}:[/] ${p:,.2f}")
        else:
            spot_parts.append(f"[dim]{asset}:[/] --")
    spot_text = "   ".join(spot_parts)
    layout["spot"].update(Panel(Text.from_markup(f"[bold]SPOT PRICES[/]  {spot_text}"), style="blue"))

    # Recent trades table
    trades_table = Table(
        title="RECENT TRADES", expand=True, show_lines=False,
        title_style="bold white",
    )
    trades_table.add_column("Time", style="dim", width=8)
    trades_table.add_column("Asset", width=5)
    trades_table.add_column("TF", width=4)
    trades_table.add_column("Side", width=8)
    trades_table.add_column("Price", justify="right", width=7)
    trades_table.add_column("Size", justify="right", width=8)
    trades_table.add_column("Bid", justify="right", width=6)
    trades_table.add_column("Ask", justify="right", width=6)
    trades_table.add_column("Sprd%", justify="right", width=6)
    trades_table.add_column("Win Phase", width=10)
    trades_table.add_column("Strategy", width=15)

    for t in recent_trades[:12]:
        side_style = "green" if "UP" in t["side"] else "red"
        if "EXIT" in t["side"]:
            side_style = "yellow"
        strategy_style = {
            "Spread Capture": "magenta",
            "Latency Arb": "cyan",
            "Pre-positioning": "blue",
            "Exit": "yellow",
        }.get(t["strategy_guess"], "white")

        # Highlight if trade price is outside bid/ask (taker vs maker)
        try:
            trade_price = float(t["price"])
            best_ask = float(t.get("book_best_ask", 0) or 0)
            best_bid = float(t.get("book_best_bid", 0) or 0)
            price_str = f"${trade_price:.2f}"
            if best_ask > 0 and trade_price >= best_ask:
                price_display = Text(price_str, style="red")   # paid ask or above
            elif best_bid > 0 and trade_price <= best_bid:
                price_display = Text(price_str, style="green") # got bid or below
            else:
                price_display = Text(price_str)
        except (ValueError, TypeError):
            price_display = Text(t["price"])

        trades_table.add_row(
            t["time_short"],
            t["asset"],
            t["timeframe"],
            Text(t["side"], style=side_style),
            price_display,
            f"${float(t['size_usd']):,.0f}",
            t.get("book_best_bid", "N/A"),
            t.get("book_best_ask", "N/A"),
            t.get("book_spread_pct", "N/A"),
            t.get("window_phase", "N/A"),
            Text(t["strategy_guess"], style=strategy_style),
        )

    layout["trades"].update(Panel(trades_table))

    # Stats
    avg_dir = (
        sum(stats["directional_prices"]) / len(stats["directional_prices"])
        if stats["directional_prices"] else 0
    )
    avg_spread = (
        sum(stats["spread_prices"]) / len(stats["spread_prices"])
        if stats["spread_prices"] else 0
    )
    timing = stats["timing_buckets"]
    total_timing = sum(timing.values()) or 1
    late_pct = timing["late"] / total_timing * 100

    # Asset split
    asset_parts = []
    total_asset_trades = sum(stats["by_asset"].values()) or 1
    for a, count in sorted(stats["by_asset"].items(), key=lambda x: -x[1]):
        asset_parts.append(f"{a} {count / total_asset_trades * 100:.0f}%")

    # Timeframe split
    tf_parts = []
    for tf, count in sorted(stats["by_timeframe"].items(), key=lambda x: -x[1]):
        tf_parts.append(f"{tf}: {count}")

    stats_text = Text.from_markup(
        f"[bold]STATS[/]\n"
        f"Avg entry (directional): ${avg_dir:.2f} | "
        f"Avg entry (spread): ${avg_spread:.2f}\n"
        f"Preferred timing: {late_pct:.0f}% after min {timing.get('late', 0)} | "
        f"Asset split: {', '.join(asset_parts)}\n"
        f"Spread capture pairs detected: {stats['spread_pairs']} | "
        f"Timeframes: {', '.join(tf_parts)}\n"
        f"Timing: early {timing['early']} / mid {timing['mid']} / late {timing['late']}"
    )
    layout["stats"].update(Panel(stats_text, style="green"))

    return layout


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    cfg = load_config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    stats["start_time"] = time.time()

    # CSV logger
    data_dir = Path(__file__).parent / "data" / "tracker"
    csv_log = CSVLogger(data_dir)
    csv_log.open()

    console.print("[bold cyan]Starting 0x8dxd Tracker...[/]")
    console.print(f"  Wallet: {cfg.tracked_wallet}")
    console.print(f"  Poll interval: {cfg.poll_interval_sec}s")
    console.print(f"  CSV output: {csv_log.path}")
    console.print()

    # Launch concurrent tasks
    tasks = [
        asyncio.create_task(binance_spot_ws(cfg)),
        asyncio.create_task(poll_trades(cfg, csv_log)),
    ]

    # Live display
    try:
        with Live(build_display(cfg), console=console, refresh_per_second=1) as live:
            while True:
                await asyncio.sleep(1)
                live.update(build_display(cfg))
    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Shutting down...[/]")
    finally:
        for t in tasks:
            t.cancel()
        csv_log.close()
        console.print(f"[green]CSV saved to: {csv_log.path}[/]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
