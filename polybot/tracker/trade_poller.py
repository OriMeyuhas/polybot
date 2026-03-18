import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

from polybot.config import TrackerConfig
from polybot.tracker.state import TrackerState
from polybot.tracker.csv_writer import TrackerCSVWriter
from polybot.tracker.parsing import parse_slug, parse_title_fallback, TIMEFRAME_SECONDS
from polybot.tracker.strategy import classify_strategy

log = logging.getLogger(__name__)


def _effective_side(side_raw: str, outcome_raw: str) -> str:
    side_up = side_raw.upper()
    outcome_up = outcome_raw.strip().lower()
    if side_up == "BUY":
        return "UP" if outcome_up in ("up", "yes") else "DOWN"
    else:  # SELL
        return "EXIT_UP" if outcome_up in ("up", "yes") else "EXIT_DOWN"


async def run_trade_poller(
    cfg: TrackerConfig, state: TrackerState, writer: TrackerCSVWriter
) -> None:
    url = f"{cfg.polymarket_data_api}/activity?user={cfg.tracked_wallet}&limit=50"

    async with httpx.AsyncClient(timeout=15) as client:
        while True:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                trades = resp.json()
                if not isinstance(trades, list):
                    trades = trades.get("data", trades.get("results", []))

                for trade in trades:
                    # --- dedup ---
                    tx_hash = trade.get("transactionHash", "") or trade.get("tx_hash", "")
                    outcome_raw = trade.get("outcome", "")
                    dedup_key = f"{tx_hash}:{outcome_raw}"
                    if dedup_key in state.seen_trade_keys:
                        continue
                    state.seen_trade_keys.append(dedup_key)

                    # --- parse market ---
                    slug = trade.get("eventSlug", "") or trade.get("slug", "")
                    parsed = parse_slug(slug)
                    if parsed["asset"] == "UNKNOWN":
                        parsed = parse_title_fallback(trade.get("title", ""))

                    asset = parsed["asset"]
                    timeframe = parsed["timeframe"]
                    window_start = parsed["window_start_epoch"]
                    window_end = window_start + TIMEFRAME_SECONDS.get(timeframe, 0)

                    # --- trade details ---
                    trade_ts = trade.get("timestamp", 0)
                    side_raw = trade.get("side", "")
                    price = float(trade.get("price", 0))
                    size_shares = float(trade.get("size", 0))
                    size_usd = float(trade.get("usdcSize", 0)) or (price * size_shares)

                    eff_side = _effective_side(side_raw, outcome_raw)

                    # --- spot enrichment ---
                    spot_now = state.spot_buffer.get_price_now(asset)
                    spot_1m = state.spot_buffer.get_price_at(asset, 60)
                    spot_3m = state.spot_buffer.get_price_at(asset, 180)
                    delta_1m = ((spot_now - spot_1m) / spot_1m * 100) if spot_1m > 0 else 0.0
                    delta_3m = ((spot_now - spot_3m) / spot_3m * 100) if spot_3m > 0 else 0.0

                    # --- book query ---
                    token_id = trade.get("asset", "")
                    best_bid: object = "N/A"
                    best_ask: object = "N/A"
                    spread_pct: object = "N/A"
                    try:
                        book_resp = await client.get(
                            cfg.clob_book_poll_url, params={"token_id": token_id}
                        )
                        book_resp.raise_for_status()
                        book = book_resp.json()
                        bids = book.get("bids", [])
                        asks = book.get("asks", [])
                        if bids:
                            best_bid = float(bids[0].get("price", 0))
                        if asks:
                            best_ask = float(asks[0].get("price", 0))
                        if isinstance(best_bid, float) and isinstance(best_ask, float) and best_ask > 0:
                            spread_pct = round((best_ask - best_bid) / best_ask * 100, 4)
                    except Exception:
                        pass  # defaults stay "N/A"

                    # --- window timing ---
                    window_total = TIMEFRAME_SECONDS.get(timeframe, 0)
                    window_elapsed = (trade_ts - window_start) if window_start > 0 else 0
                    window_pct = window_elapsed / window_total if window_total > 0 else 0

                    # --- strategy ---
                    strategy = classify_strategy(
                        slug, eff_side, window_elapsed, window_total,
                        delta_1m, asset, state.market_sides,
                    )

                    # --- build CSV row ---
                    ts_iso = datetime.fromtimestamp(trade_ts, tz=timezone.utc).isoformat() if trade_ts else ""
                    row = {
                        "timestamp": ts_iso,
                        "tx_hash": tx_hash,
                        "asset": asset,
                        "timeframe": timeframe,
                        "market_slug": slug,
                        "side": eff_side,
                        "outcome": outcome_raw,
                        "price": price,
                        "size_usd": round(size_usd, 4),
                        "size_shares": round(size_shares, 4),
                        "spot_price_at_fill": spot_now,
                        "spot_1m_ago": spot_1m,
                        "spot_3m_ago": spot_3m,
                        "spot_delta_1m_pct": round(delta_1m, 4),
                        "spot_delta_3m_pct": round(delta_3m, 4),
                        "window_start_epoch": window_start,
                        "window_end_epoch": window_end,
                        "window_elapsed_sec": window_elapsed,
                        "window_total_sec": window_total,
                        "window_pct_elapsed": round(window_pct, 4),
                        "book_best_bid": best_bid,
                        "book_best_ask": best_ask,
                        "book_spread_pct": spread_pct,
                        "strategy_guess": strategy,
                    }
                    writer.write_trade(row)

                    # --- update state ---
                    condition_id = trade.get("conditionId", "") or trade.get("condition_id", "")
                    if slug not in state.active_markets:
                        state.active_markets[slug] = {
                            "condition_id": condition_id,
                            "token_id_up": "",
                            "token_id_dn": "",
                            "asset": asset,
                            "timeframe": timeframe,
                            "window_start_epoch": window_start,
                            "window_end_epoch": window_end,
                        }
                    # Fill in token IDs as we discover them
                    mkt = state.active_markets[slug]
                    if outcome_raw.strip().lower() in ("up", "yes"):
                        mkt["token_id_up"] = mkt["token_id_up"] or token_id
                    else:
                        mkt["token_id_dn"] = mkt["token_id_dn"] or token_id

                    if slug not in state.whale_trades:
                        state.whale_trades[slug] = []
                    state.whale_trades[slug].append({
                        "tx_hash": tx_hash,
                        "side": eff_side,
                        "price": price,
                        "size_usd": size_usd,
                        "size_shares": size_shares,
                        "ts": trade_ts,
                    })

                    if slug not in state.spot_at_discovery:
                        state.spot_at_discovery[slug] = spot_now

                    log.info(
                        "Whale trade | %s %s %.2f @ %.4f ($%.2f) | %s [%s] | strat=%s",
                        eff_side, asset, size_shares, price, size_usd,
                        slug, timeframe, strategy,
                    )

            except httpx.HTTPStatusError as exc:
                log.warning("Trade poller HTTP error %s: %s", exc.response.status_code, exc)
            except Exception:
                log.warning("Trade poller error", exc_info=True)

            await asyncio.sleep(cfg.trade_poll_interval_sec)
