"""Settlement tracker — watches for market outcomes after windows close and computes PnL."""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from polybot.config import TrackerConfig
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settlement resolution helpers
# ---------------------------------------------------------------------------

async def _resolve_via_clob(
    client: httpx.AsyncClient,
    cfg: TrackerConfig,
    condition_id: str,
) -> dict | None:
    """Try the CLOB API: GET /markets/{condition_id}.

    Returns a dict with ``outcome`` ("UP" or "DOWN") and ``settlement_price``
    if the market is resolved, otherwise *None*.
    """
    url = f"{cfg.polymarket_host}/markets/{condition_id}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # The CLOB response typically has a `tokens` array and a boolean
    # `resolved` (or the tokens carry a `winner` flag).
    if data.get("resolved") or data.get("closed"):
        tokens = data.get("tokens", [])
        for tok in tokens:
            if tok.get("winner") is True or str(tok.get("winner")).lower() == "true":
                outcome = tok.get("outcome", "").upper()
                if outcome in ("UP", "DOWN", "YES", "NO"):
                    return {"outcome": outcome, "settlement_price": 1.0}

        # Fallback: look for a top-level `winner` field
        winner = data.get("winner")
        if winner:
            return {"outcome": str(winner).upper(), "settlement_price": 1.0}

    return None


async def _resolve_via_data_api(
    client: httpx.AsyncClient,
    cfg: TrackerConfig,
    slug: str,
) -> dict | None:
    """Fallback: query the data-api events endpoint by slug."""
    url = f"{cfg.polymarket_data_api}/events?slug={slug}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    # payload may be a list or a single object
    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        markets = event.get("markets", [event])
        for mkt in markets:
            if mkt.get("resolved") or mkt.get("closed"):
                outcome = mkt.get("outcome", mkt.get("winner", ""))
                if outcome:
                    return {"outcome": str(outcome).upper(), "settlement_price": 1.0}

    return None


async def _resolve_settlement(
    client: httpx.AsyncClient,
    cfg: TrackerConfig,
    slug: str,
    condition_id: str,
) -> dict | None:
    """Attempt to resolve a market with retries + exponential backoff.

    Returns ``{"outcome": "UP"/"DOWN", "settlement_price": 1.0}`` on success
    or *None* if all retries are exhausted.
    """
    for attempt in range(cfg.settlement_retry_max):
        try:
            # --- primary: CLOB API ---
            result = await _resolve_via_clob(client, cfg, condition_id)
            if result is not None:
                return result

            # --- fallback: data API ---
            result = await _resolve_via_data_api(client, cfg, slug)
            if result is not None:
                return result

        except Exception as exc:
            log.warning(
                "Settlement resolve attempt %d/%d for %s failed: %s",
                attempt + 1, cfg.settlement_retry_max, slug, exc,
            )

        backoff = cfg.settlement_retry_backoff_sec * (2 ** attempt)
        log.debug("Retrying settlement for %s in %.1fs …", slug, backoff)
        await asyncio.sleep(backoff)

    log.error(
        "Failed to resolve settlement for %s after %d attempts — removing from active markets",
        slug, cfg.settlement_retry_max,
    )
    return None


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
    """Continuously poll active markets and settle those whose windows have closed."""

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

                    log.info("Window closed for %s — attempting settlement", slug)

                    condition_id = market_info.get("condition_id", slug)
                    resolution = await _resolve_settlement(client, cfg, slug, condition_id)

                    settled_outcome = None
                    if resolution is not None:
                        settled_outcome = resolution["outcome"]
                        log.info("Market %s resolved: %s", slug, settled_outcome)
                    else:
                        log.warning("Market %s could not be resolved — recording as UNKNOWN", slug)
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

            await asyncio.sleep(5)
