import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from polybot.config import TrackerConfig
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter

log = logging.getLogger(__name__)


def _compute_depth(levels: list[dict], best_price: float, threshold: float) -> float:
    """Sum USD value (price * size) of all levels within *threshold* of *best_price*."""
    total = 0.0
    for level in levels:
        p = float(level["price"])
        s = float(level["size"])
        if abs(p - best_price) <= threshold:
            total += p * s
    return total


async def run_book_recorder(
    cfg: TrackerConfig, state: TrackerState, writer: TrackerCSVWriter
) -> None:
    """Poll CLOB REST API for order-book depth snapshots and write rows to CSV."""

    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(cfg.book_snapshot_interval_sec)

            active_markets = dict(state.active_markets)
            if not active_markets:
                continue

            for slug, market_info in active_markets.items():
                for side in ("UP", "DN"):
                    token_id = market_info.get(f"token_id_{side.lower()}", "")
                    if not token_id:
                        continue

                    try:
                        resp = await client.get(
                            cfg.clob_book_poll_url,
                            params={"token_id": token_id},
                            timeout=5,
                        )
                        resp.raise_for_status()
                        book = resp.json()

                        bids = book.get("bids", [])
                        asks = book.get("asks", [])

                        best_bid = float(bids[0]["price"]) if bids else 0.0
                        best_ask = float(asks[0]["price"]) if asks else 0.0
                        spread_pct = (
                            (best_ask - best_bid) / best_ask * 100
                            if best_ask > 0
                            else 0.0
                        )
                        mid_price = (
                            (best_bid + best_ask) / 2 if best_ask > 0 else 0.0
                        )

                        depth_1c_bid = _compute_depth(bids, best_bid, 0.01)
                        depth_1c_ask = _compute_depth(asks, best_ask, 0.01)
                        depth_5c_bid = _compute_depth(bids, best_bid, 0.05)
                        depth_5c_ask = _compute_depth(asks, best_ask, 0.05)
                        depth_10c_bid = _compute_depth(bids, best_bid, 0.10)
                        depth_10c_ask = _compute_depth(asks, best_ask, 0.10)

                        num_bid_levels = len(bids)
                        num_ask_levels = len(asks)

                        writer.write_book(
                            {
                                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                                "market_slug": slug,
                                "token_id": token_id,
                                "side": side,
                                "best_bid": best_bid,
                                "best_ask": best_ask,
                                "spread_pct": round(spread_pct, 4),
                                "mid_price": round(mid_price, 6),
                                "depth_1c_bid": round(depth_1c_bid, 2),
                                "depth_1c_ask": round(depth_1c_ask, 2),
                                "depth_5c_bid": round(depth_5c_bid, 2),
                                "depth_5c_ask": round(depth_5c_ask, 2),
                                "depth_10c_bid": round(depth_10c_bid, 2),
                                "depth_10c_ask": round(depth_10c_ask, 2),
                                "num_bid_levels": num_bid_levels,
                                "num_ask_levels": num_ask_levels,
                            }
                        )

                    except Exception as exc:
                        log.debug(
                            "Book fetch failed for %s/%s: %s", slug, side, exc
                        )
                        continue
