"""Settlement tracker — watches for market outcomes after windows close and computes PnL."""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from polybot.config import TrackerConfig
from polybot.settlement import resolve_via_clob, resolve_via_gamma, fetch_condition_id, try_resolve_once
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PnL computation
# ---------------------------------------------------------------------------

def _compute_pnl(settled_outcome: str, trades: list) -> dict:
    """Compute PnL for a settled market.

    ``trades`` is the list of whale trade dicts recorded by the trade poller.
    Each trade has at least: side (UP/DOWN/EXIT), price, size_usd, size_shares.
    """
    up_cost = 0.0
    up_shares = 0.0
    down_cost = 0.0
    down_shares = 0.0

    for t in trades:
        side = str(t.get("side", "")).upper()
        cost = float(t.get("size_usd", 0))
        shares = float(t.get("size_shares", 0))
        if side == "UP":
            up_cost += cost
            up_shares += shares
        elif side == "DOWN":
            down_cost += cost
            down_shares += shares
        # EXIT trades are ignored for PnL

    winning_pnl = 0.0
    losing_pnl = 0.0

    if settled_outcome == "UP":
        winning_pnl = up_shares * 1.0 - up_cost
        losing_pnl = -down_cost
    elif settled_outcome == "DOWN":
        winning_pnl = down_shares * 1.0 - down_cost
        losing_pnl = -up_cost

    total_pnl = winning_pnl + losing_pnl
    total_cost = up_cost + down_cost
    total_shares = up_shares + down_shares

    whale_avg_price = total_cost / total_shares if total_shares > 0 else 0.0

    # Whale side = whichever side has more USD invested
    if up_cost >= down_cost:
        whale_side = "UP"
    else:
        whale_side = "DOWN"

    roi = total_pnl / total_cost if total_cost > 0 else 0.0

    return {
        "whale_had_position": total_cost > 0,
        "whale_side": whale_side,
        "whale_avg_price": round(whale_avg_price, 6),
        "whale_total_usd": round(total_cost, 4),
        "whale_pnl_usd": round(total_pnl, 4),
        "whale_roi_pct": round(roi * 100, 4),
    }


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def run_settlement_tracker(
    cfg: TrackerConfig,
    state: TrackerState,
    writer: TrackerCSVWriter,
) -> None:
    """Continuously poll active markets and settle those whose windows have closed.

    Non-blocking: each loop tries to resolve expired markets once.  If not yet
    resolved on Polymarket's side, the market stays in active_markets and is
    retried next iteration (~30s).  Gives up after ``settlement_give_up_sec``
    (default 30 min) and records UNKNOWN.
    """

    log.info("Settlement tracker started")

    async with httpx.AsyncClient() as client:
        while True:
            try:
                current_time = time.time()
                snapshot = list(state.active_markets.items())

                for slug, market_info in snapshot:
                    window_end = market_info.get("window_end_epoch", 0)
                    if current_time <= window_end:
                        continue  # window still open

                    elapsed_since_end = current_time - window_end
                    condition_id = market_info.get("condition_id", slug)

                    # --- try to resolve (single non-blocking attempt) ---
                    resolution = await try_resolve_once(client, cfg.polymarket_host, slug, condition_id)

                    if resolution is not None:
                        settled_outcome = resolution["outcome"]
                        log.info("Market %s resolved: %s (%.0fs after window end)",
                                 slug, settled_outcome, elapsed_since_end)
                    elif elapsed_since_end < cfg.settlement_give_up_sec:
                        # Not resolved yet — wait and retry next loop
                        log.debug(
                            "Market %s not resolved yet (%.0fs / %.0fs) — will retry",
                            slug, elapsed_since_end, cfg.settlement_give_up_sec,
                        )
                        continue
                    else:
                        # Timed out waiting for resolution
                        log.warning(
                            "Market %s not resolved after %.0fs — recording as UNKNOWN",
                            slug, elapsed_since_end,
                        )
                        settled_outcome = "UNKNOWN"

                    # --- PnL computation ---
                    trades = state.whale_trades.get(slug, [])
                    pnl_info = _compute_pnl(settled_outcome, trades)

                    # --- Spot change ---
                    asset = market_info.get("asset", "")
                    spot_at_open = state.spot_at_discovery.get(slug, 0.0)
                    spot_at_close = state.spot_buffer.get_price_now(asset)
                    if spot_at_open > 0:
                        spot_change_pct = round(
                            ((spot_at_close - spot_at_open) / spot_at_open) * 100, 4,
                        )
                    else:
                        spot_change_pct = 0.0

                    # --- Write settlement row ---
                    row = {
                        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                        "market_slug": slug,
                        "asset": asset,
                        "timeframe": market_info.get("timeframe", ""),
                        "window_start_epoch": market_info.get("window_start_epoch", 0),
                        "window_end_epoch": window_end,
                        "settled_outcome": settled_outcome,
                        "settlement_price": resolution["settlement_price"] if resolution else 0.0,
                        "spot_at_open": spot_at_open,
                        "spot_at_close": spot_at_close,
                        "spot_change_pct": spot_change_pct,
                        **pnl_info,
                    }
                    writer.write_settlement(row)

                    # --- Cleanup state ---
                    state.active_markets.pop(slug, None)
                    state.whale_trades.pop(slug, None)
                    state.spot_at_discovery.pop(slug, None)

                    log.info(
                        "Settlement recorded for %s | outcome=%s pnl=%.4f roi=%.2f%%",
                        slug, settled_outcome, pnl_info["whale_pnl_usd"], pnl_info["whale_roi_pct"],
                    )

            except Exception:
                log.exception("Error in settlement tracker loop")

            await asyncio.sleep(30)
