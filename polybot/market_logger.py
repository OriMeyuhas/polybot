"""Market data logger — captures real Polymarket state for paper mode verification.

Logs to data/market_log.jsonl with real order book snapshots, midpoints,
and settlement verification data for every market the bot trades.
"""

import json
import logging
import pathlib
import time

import httpx

logger = logging.getLogger(__name__)

LOG_PATH = pathlib.Path("data/market_log.jsonl")


def _append(record: dict):
    """Append a record to the market log."""
    try:
        LOG_PATH.parent.mkdir(exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def log_fill(market_id: str, asset: str, side: str, price: float, size: float,
             token_id: str):
    """Log a fill with real order book snapshot for verification.

    Captures what the real book looks like at the moment our paper engine says
    we got a fill — so we can verify: was there actually liquidity at this price?
    """
    book = _fetch_book_top(token_id)
    _append({
        "ts": time.time(),
        "event": "fill",
        "market_id": market_id,
        "asset": asset,
        "side": side,
        "fill_price": price,
        "fill_size": size,
        "real_best_bid": book.get("best_bid"),
        "real_best_ask": book.get("best_ask"),
        "real_spread": book.get("spread"),
        "book_levels": book.get("bid_levels", 0) + book.get("ask_levels", 0),
        "fill_vs_bid": round(price - book["best_bid"], 4) if book.get("best_bid") else None,
    })


def log_force_buy(market_id: str, asset: str, side: str, price: float,
                  size: float, pair_cost: float, token_id: str):
    """Log a force-buy with real book state."""
    book = _fetch_book_top(token_id)
    _append({
        "ts": time.time(),
        "event": "force_buy",
        "market_id": market_id,
        "asset": asset,
        "side": side,
        "buy_price": price,
        "buy_size": size,
        "pair_cost": pair_cost,
        "real_best_bid": book.get("best_bid"),
        "real_best_ask": book.get("best_ask"),
        "real_spread": book.get("spread"),
        "book_levels": book.get("bid_levels", 0) + book.get("ask_levels", 0),
    })


def log_ladder_post(market_id: str, asset: str, timeframe_sec: int,
                    up_token_id: str, dn_token_id: str,
                    up_midpoint: float, dn_midpoint: float,
                    spot_price: float, open_price: float,
                    up_rungs: int, dn_rungs: int, pair_cost: float):
    """Log market state when a ladder is posted."""
    _append({
        "ts": time.time(),
        "event": "ladder_post",
        "market_id": market_id,
        "asset": asset,
        "timeframe_sec": timeframe_sec,
        "up_midpoint": up_midpoint,
        "dn_midpoint": dn_midpoint,
        "mid_sum": round(up_midpoint + dn_midpoint, 4),
        "spot_price": spot_price,
        "open_price": open_price,
        "spot_delta_pct": round((spot_price - open_price) / open_price * 100, 4) if open_price > 0 else 0,
        "up_rungs": up_rungs,
        "dn_rungs": dn_rungs,
        "pair_cost": pair_cost,
    })


def log_settlement(market_id: str, asset: str, timeframe_sec: int,
                   up_token_id: str, dn_token_id: str,
                   paper_outcome: str, spot_price: float, open_price: float,
                   pnl: float, pair_cost: float | None,
                   up_qty: float, dn_qty: float):
    """Log settlement with real market data for verification."""
    # Fetch real CLOB midpoints at settlement time
    up_mid = _fetch_midpoint(up_token_id)
    dn_mid = _fetch_midpoint(dn_token_id)

    spot_delta = (spot_price - open_price) / open_price if open_price > 0 else 0
    real_outcome = "UP" if spot_delta > 0 else "DOWN" if spot_delta < 0 else "UNKNOWN"

    # Check if CLOB agrees: UP token > 0.5 means market thinks UP
    clob_outcome = None
    if up_mid is not None and dn_mid is not None:
        if up_mid > 0.5:
            clob_outcome = "UP"
        elif dn_mid > 0.5:
            clob_outcome = "DOWN"

    match = paper_outcome == real_outcome
    clob_match = paper_outcome == clob_outcome if clob_outcome else None

    record = {
        "ts": time.time(),
        "event": "settlement",
        "market_id": market_id,
        "asset": asset,
        "timeframe_sec": timeframe_sec,
        "paper_outcome": paper_outcome,
        "spot_delta_pct": round(spot_delta * 100, 4),
        "real_outcome_spot": real_outcome,
        "spot_price": spot_price,
        "open_price": open_price,
        "up_midpoint": up_mid,
        "dn_midpoint": dn_mid,
        "clob_outcome": clob_outcome,
        "paper_matches_spot": match,
        "paper_matches_clob": clob_match,
        "pnl": round(pnl, 4),
        "pair_cost": pair_cost,
        "up_qty": round(up_qty, 1),
        "dn_qty": round(dn_qty, 1),
    }

    _append(record)

    if not match:
        logger.warning(
            "SETTLEMENT MISMATCH: %s paper=%s but spot says %s (delta=%.4f%%)",
            market_id, paper_outcome, real_outcome, spot_delta * 100,
        )
    if clob_match is False:
        logger.warning(
            "CLOB DISAGREES: %s paper=%s but CLOB says %s (UP_mid=%.3f DN_mid=%.3f)",
            market_id, paper_outcome, clob_outcome, up_mid or 0, dn_mid or 0,
        )


def log_book_snapshot(market_id: str, asset: str,
                      up_token_id: str, dn_token_id: str,
                      reason: str = "periodic"):
    """Log order book top-of-book for both sides."""
    up_book = _fetch_book_top(up_token_id)
    dn_book = _fetch_book_top(dn_token_id)

    _append({
        "ts": time.time(),
        "event": "book_snapshot",
        "market_id": market_id,
        "asset": asset,
        "reason": reason,
        "up_best_bid": up_book.get("best_bid"),
        "up_best_ask": up_book.get("best_ask"),
        "up_spread": up_book.get("spread"),
        "up_levels": up_book.get("bid_levels", 0) + up_book.get("ask_levels", 0),
        "dn_best_bid": dn_book.get("best_bid"),
        "dn_best_ask": dn_book.get("best_ask"),
        "dn_spread": dn_book.get("spread"),
        "dn_levels": dn_book.get("bid_levels", 0) + dn_book.get("ask_levels", 0),
    })


def _fetch_midpoint(token_id: str) -> float | None:
    """Fetch current CLOB midpoint for a token."""
    try:
        resp = httpx.get(
            f"https://clob.polymarket.com/midpoint?token_id={token_id}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            return float(data.get("mid", 0))
    except Exception:
        pass
    return None


def _fetch_book_top(token_id: str) -> dict:
    """Fetch top-of-book (best bid/ask) for a token."""
    try:
        resp = httpx.get(
            f"https://clob.polymarket.com/book?token_id={token_id}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            # Polymarket returns bids lowest-first, asks lowest-first
            # Best bid = highest bid (last element), best ask = lowest ask (first element)
            best_bid = float(bids[-1]["price"]) if bids else None
            best_ask = float(asks[0]["price"]) if asks else None
            spread = round(best_ask - best_bid, 4) if best_bid and best_ask else None
            bid_depth = len(bids)
            ask_depth = len(asks)
            return {"best_bid": best_bid, "best_ask": best_ask, "spread": spread,
                    "bid_levels": bid_depth, "ask_levels": ask_depth}
    except Exception:
        pass
    return {}
