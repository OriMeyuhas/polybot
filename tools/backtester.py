"""Full-fidelity paired MM backtester for PolyBot.

Supports two data source modes:
  - local: Uses local book_log_YYYY-MM-DD.jsonl files (high-fidelity, Apr 10-11 only)
  - dome:  Uses Dome snapshot files in data/dome_snapshots/ (14 days, 1,344 markets)
  - auto:  Tries dome first, falls back to local for dates without Dome coverage

Usage:
    # Dome dataset (1,344 markets, 14 days):
    python tools/backtester.py \\
        --data-source dome \\
        --config experiments/paired_only.yaml \\
        --output results/paired_only_dome.json

    # Local book_log (122 markets, Apr 10-11 only):
    python tools/backtester.py \\
        --data-source local \\
        --config experiments/paired_only.yaml \\
        --output results/paired_only.json \\
        --start 2026-04-10 --end 2026-04-11

Architecture (local mode):
  1. Build a market window index from market_event_log_*.jsonl
  2. Build a token->market mapping by scanning book_log for condition_ids
     and matching them to market windows via time proximity
  3. For each settled market, replay best_bid/best_ask from book_log
  4. Simulate fills, safety nets, and compute PnL

Architecture (dome mode):
  1. Scan data/dome_snapshots/*.jsonl for all market files
  2. For each file: read header (metadata + outcome), orderbook entries (book at open),
     Binance prices (last 100s before window_end), Chainlink prices
  3. Simulate fills at window open using book state from dome snapshots
  4. Aggregate into full result set with pnl_per_day breakdown
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import os
import pathlib
import pickle
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Add project root to path so we can import from polybot
# ---------------------------------------------------------------------------
_TOOLS_DIR = pathlib.Path(__file__).parent
_PROJECT_ROOT = _TOOLS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Import FV brain from polybot (read-only — no modifications)
try:
    from polybot.strategy.fair_value import p_fair_up, certainty as fv_certainty
    from polybot.strategy.vol_estimator import VolEstimator
    from polybot.strategy.ladder_manager import build_ladder_rungs
    _POLYBOT_IMPORTED = True
except ImportError:
    _POLYBOT_IMPORTED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backtester")


# ---------------------------------------------------------------------------
# Fallback implementations (used if polybot import fails)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _p_fair_up_fallback(
    start_price: float | None,
    current_price: float | None,
    seconds_to_resolution: float,
    vol_annualized: float | None = None,
) -> float:
    if start_price is None or current_price is None:
        return 0.5
    s, c = float(start_price), float(current_price)
    if s <= 0 or c <= 0:
        return 0.5
    if seconds_to_resolution <= 0:
        return 0.99 if c >= s else 0.01
    if vol_annualized is None or vol_annualized <= 0:
        return 0.5
    t_years = seconds_to_resolution / (365.25 * 24 * 3600)
    denom = vol_annualized * math.sqrt(t_years)
    if denom < 1e-15:
        return 0.99 if c >= s else 0.01
    d = math.log(c / s) / denom
    d = max(-6.0, min(6.0, d))
    return max(0.01, min(0.99, _norm_cdf(d)))


def _fv_certainty_fallback(p_up: float) -> float:
    return max(p_up, 1.0 - p_up)


def _build_ladder_rungs_fallback(
    best_ask: float,
    budget: float,
    rungs: int,
    spacing: float,
    width: float,
    size_skew: float,
    tick_size: float = 0.01,
    fee_rate: float = 0.0,
    max_rung_price: float = 1.0,
) -> list[tuple[float, float]]:
    """Simplified ladder rung builder (mirrors polybot's version)."""
    MIN_ORDER_SIZE = 5.0
    if best_ask <= 0 or budget <= 0:
        return []
    avg_price = max(tick_size, best_ask - width / 2)
    min_cost_per_rung = MIN_ORDER_SIZE * avg_price
    max_affordable = max(1, int(budget / min_cost_per_rung))
    effective_rungs = min(rungs, max_affordable)

    anchor = max(tick_size, best_ask - width - tick_size)
    effective_spacing = spacing if effective_rungs == rungs else width / max(effective_rungs, 1)

    prices = []
    for i in range(effective_rungs):
        p = anchor + i * effective_spacing
        p = round(round(p / tick_size) * tick_size, 10)
        p = max(tick_size, min(min(1.0 - tick_size, max_rung_price), p))
        prices.append(p)

    weights = [
        1.0 + (size_skew - 1.0) * (i / max(effective_rungs - 1, 1))
        for i in range(effective_rungs)
    ]
    total_weighted_cost = sum(w * p for w, p in zip(weights, prices))
    if total_weighted_cost <= 0:
        return []

    scale = budget / total_weighted_cost
    result = []
    for price, weight in zip(prices, weights):
        size = scale * weight
        if size >= MIN_ORDER_SIZE:
            result.append((price, round(size, 1)))
    return result


# Resolve which implementations to use
if _POLYBOT_IMPORTED:
    _p_fair_up = p_fair_up
    _fv_certainty = fv_certainty
    _build_ladder = build_ladder_rungs
    logger.info("Using polybot strategy imports")
else:
    _p_fair_up = _p_fair_up_fallback
    _fv_certainty = _fv_certainty_fallback
    _build_ladder = _build_ladder_rungs_fallback
    logger.warning("polybot import failed — using fallback implementations")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class BacktestConfig:
    # Strategy toggles
    fv_gate_enabled: bool = False
    fv_gate_certainty_threshold: float = 0.80
    directional_budget_cap: float = 20.0
    fv_cancel_enabled: bool = True
    fv_cancel_certainty_threshold: float = 0.75
    one_sided_abort_enabled: bool = True
    one_sided_abort_cost_pct: float = 0.01
    one_sided_abort_ratio: float = 3.0

    # Ladder params
    rungs: int = 10
    spacing: float = 0.01
    width: float = 0.10
    position_size_fraction: float = 0.05
    max_pair_cost: float = 0.98
    size_skew: float = 2.0

    # Entry filter
    trend_filter_enabled: bool = False
    trend_filter_window_sec: int = 300
    trend_filter_threshold_pct: float = 0.004

    # Simulation params
    bankroll: float = 500.0
    maker_fee_rate: float = 0.0
    vol_window_sec: int = 300
    vol_fallback_annual: float = 0.50
    vol_min_samples: int = 30

    # Name (auto-set from config file, not loaded from YAML)
    name: str = "unnamed"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BacktestConfig":
        valid = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in valid}
        return cls(**filtered)

    @classmethod
    def from_yaml(cls, path: pathlib.Path) -> "BacktestConfig":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        cfg = cls.from_dict(data)
        cfg.name = path.stem
        return cfg


# ---------------------------------------------------------------------------
# Book state reconstruction
# ---------------------------------------------------------------------------

class BookState:
    """Tracks best_bid/best_ask for a single token, updated from book_log events."""

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id  # 20-char short form
        self.best_bid: float = 0.0
        self.best_ask: float = 1.0
        self.last_update_ts: float = 0.0

    def apply_book_event(self, event: dict) -> None:
        """Apply a 'book' event (full snapshot): compute best_bid/best_ask from bids/asks arrays."""
        data = event.get("data", {})
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        ts = event.get("ts", 0.0)
        if bids:
            self.best_bid = max(float(b["price"]) for b in bids)
        if asks:
            self.best_ask = min(float(a["price"]) for a in asks)
        self.last_update_ts = ts

    def apply_price_change(self, price_change: dict, ts: float) -> None:
        """Apply one price_change entry (from inside a price_change event's price_changes array)."""
        bb = price_change.get("best_bid")
        ba = price_change.get("best_ask")
        if bb is not None:
            self.best_bid = float(bb)
        if ba is not None:
            self.best_ask = float(ba)
        self.last_update_ts = ts


# ---------------------------------------------------------------------------
# Market window data class
# ---------------------------------------------------------------------------

@dataclass
class MarketWindow:
    market_id: str
    open_epoch: int
    close_epoch: int
    outcome: str | None  # "UP" or "DOWN" from settlement_log
    up_token_id: str | None  # 20-char short form
    dn_token_id: str | None
    pnl_actual: float  # real PnL from settlement_log (for comparison)


# ---------------------------------------------------------------------------
# Book log indexer
# ---------------------------------------------------------------------------

def build_book_index(
    data_dir: pathlib.Path,
    dates: list[str],
    cache_dir: pathlib.Path | None = None,
) -> dict[str, list[tuple[float, float, float]]]:
    """Build an index of book states per token.

    Returns:
        dict mapping token_id (20-char) -> sorted list of (ts, best_bid, best_ask)

    The list is sorted ascending by ts so we can binary-search for
    "best state before time T".
    """
    cache_key = "_".join(dates)
    cache_path = (cache_dir or data_dir) / f"book_index_{cache_key}.pkl"

    if cache_path.exists():
        try:
            logger.info("Loading book index from cache: %s", cache_path)
            with open(cache_path, "rb") as f:
                return pickle.load(f)
        except Exception as e:
            logger.warning("Cache load failed (%s), rebuilding...", e)

    logger.info("Building book index for dates: %s", dates)
    start = time.time()

    # token_id -> [(ts, best_bid, best_ask), ...]
    # Use lists for accumulation; we'll convert to tuples later
    index: dict[str, list[tuple[float, float, float]]] = {}
    current_state: dict[str, tuple[float, float]] = {}  # token -> (bid, ask)

    total_lines = 0
    total_events = 0

    for date in dates:
        book_log = data_dir / f"book_log_{date}.jsonl"
        if not book_log.exists():
            logger.warning("Book log not found: %s", book_log)
            continue

        logger.info("Scanning %s ...", book_log)
        with open(book_log, encoding="utf-8") as f:
            for line in f:
                total_lines += 1
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                event_type = obj.get("event_type", "")
                ts = obj.get("ts", 0.0)

                if event_type == "book":
                    token_id = obj.get("token_id", "")
                    if not token_id:
                        # Try to extract from data.asset_id
                        token_id = str(obj.get("data", {}).get("asset_id", ""))[:20]
                    if not token_id:
                        continue
                    data = obj.get("data", {})
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    if not bids and not asks:
                        continue
                    best_bid = max((float(b["price"]) for b in bids), default=0.0)
                    best_ask = min((float(a["price"]) for a in asks), default=1.0)
                    current_state[token_id] = (best_bid, best_ask)
                    if token_id not in index:
                        index[token_id] = []
                    index[token_id].append((ts, best_bid, best_ask))
                    total_events += 1

                elif event_type == "price_change":
                    data = obj.get("data", {})
                    for pc in data.get("price_changes", []):
                        asset_id = str(pc.get("asset_id", ""))[:20]
                        if not asset_id:
                            continue
                        bb = pc.get("best_bid")
                        ba = pc.get("best_ask")
                        if bb is None and ba is None:
                            continue
                        prev_bid, prev_ask = current_state.get(asset_id, (0.0, 1.0))
                        best_bid = float(bb) if bb is not None else prev_bid
                        best_ask = float(ba) if ba is not None else prev_ask
                        current_state[asset_id] = (best_bid, best_ask)
                        if asset_id not in index:
                            index[asset_id] = []
                        index[asset_id].append((ts, best_bid, best_ask))
                        total_events += 1

    elapsed = time.time() - start
    logger.info(
        "Indexed %d events from %d lines in %.1fs. Tokens: %d",
        total_events, total_lines, elapsed, len(index),
    )

    # Save cache
    try:
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "wb") as f:
            pickle.dump(index, f, protocol=4)
        logger.info("Book index cached to %s", cache_path)
    except Exception as e:
        logger.warning("Failed to cache book index: %s", e)

    return index


def lookup_book_state(
    index: dict[str, list[tuple[float, float, float]]],
    token_id: str,
    ts: float,
) -> tuple[float, float] | None:
    """Binary search for the most recent (best_bid, best_ask) at or before ts.

    Returns (best_bid, best_ask) or None if no data for this token.
    """
    entries = index.get(token_id)
    if not entries:
        return None

    # Binary search: find rightmost entry with ts <= target
    lo, hi = 0, len(entries) - 1
    result_idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if entries[mid][0] <= ts:
            result_idx = mid
            lo = mid + 1
        else:
            hi = mid - 1

    if result_idx < 0:
        # ts before all entries — use first entry
        if entries:
            return entries[0][1], entries[0][2]
        return None

    return entries[result_idx][1], entries[result_idx][2]


# ---------------------------------------------------------------------------
# Market window loading and token mapping
# ---------------------------------------------------------------------------

def load_market_windows(
    data_dir: pathlib.Path,
    dates: list[str],
    settlement_log: pathlib.Path,
    start_epoch: int,
    end_epoch: int,
) -> list[MarketWindow]:
    """Load market windows for the given date range.

    Combines:
    - market_event_log for open/close epochs AND token IDs (preferred)
    - settlement_log for outcomes
    """
    # Step 1: Build market window registry from market_event_log
    market_meta: dict[str, dict] = {}  # market_id -> {open, close, up_token, dn_token}
    for date in dates:
        event_log = data_dir / f"market_event_log_{date}.jsonl"
        if not event_log.exists():
            continue
        with open(event_log, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in ("discovered", "settled"):
                    continue
                mid = obj.get("market_id", "")
                if not mid:
                    continue
                meta = obj.get("metadata", {})
                open_ep = meta.get("open_epoch", 0)
                close_ep = meta.get("close_epoch", 0)
                up_tok_full = meta.get("up_token_id", "")
                dn_tok_full = meta.get("dn_token_id", "")
                if mid not in market_meta:
                    market_meta[mid] = {
                        "open": open_ep,
                        "close": close_ep,
                        "up_token": str(up_tok_full)[:20] if up_tok_full else None,
                        "dn_token": str(dn_tok_full)[:20] if dn_tok_full else None,
                    }
                elif up_tok_full and not market_meta[mid].get("up_token"):
                    # Update token IDs if discovered event had them
                    market_meta[mid]["up_token"] = str(up_tok_full)[:20]
                    market_meta[mid]["dn_token"] = str(dn_tok_full)[:20]

    # Step 2: Load outcomes from settlement_log
    outcomes: dict[str, str] = {}
    actual_pnls: dict[str, float] = {}
    if settlement_log.exists():
        with open(settlement_log, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = obj.get("ts", 0)
                if start_epoch <= ts < end_epoch:
                    mid = obj.get("market_id", "")
                    outcome = obj.get("outcome")
                    pnl = obj.get("pnl", 0.0)
                    if mid and outcome:
                        outcomes[mid] = outcome
                        actual_pnls[mid] = pnl

    # Step 3: Build MarketWindow objects for markets in date range
    windows: list[MarketWindow] = []
    for mid, meta in market_meta.items():
        open_ep = meta["open"]
        close_ep = meta["close"]
        # Only include markets that settled in our date range
        if mid not in outcomes:
            continue
        # Use token IDs from market_event_log metadata if available (ground truth)
        up_tok = meta.get("up_token")
        dn_tok = meta.get("dn_token")
        w = MarketWindow(
            market_id=mid,
            open_epoch=open_ep,
            close_epoch=close_ep,
            outcome=outcomes.get(mid),
            up_token_id=up_tok,  # may be None; map_tokens_to_markets fills rest
            dn_token_id=dn_tok,
            pnl_actual=actual_pnls.get(mid, 0.0),
        )
        windows.append(w)

    logger.info("Loaded %d market windows with outcomes", len(windows))
    return windows


def map_tokens_to_markets(
    index: dict[str, list[tuple[float, float, float]]],
    windows: list[MarketWindow],
    data_dir: pathlib.Path,
    dates: list[str],
    cache_dir: pathlib.Path | None = None,
) -> None:
    """Assign up_token_id and dn_token_id to each MarketWindow.

    Strategy:
    1. Scan book_log to build condition_id -> {token_ids} mapping
    2. For each condition_id, find the median timestamp of its token activity
    3. Match condition to the market window whose close_epoch is closest to
       when the WS subscription ended (last book event for those tokens)
    4. Determine UP vs DN by price: UP token trades near 0.5 at start of window;
       the token with lower initial price is the UP token (conservative) or
       we use the fact that UP+DN prices sum to ~1.0 and the one with higher
       best_ask at window open is the DN token (since DN wins when price falls).
    """
    cache_key = "_".join(dates) + "_token_map"
    cache_path = (cache_dir or data_dir) / f"token_map_{cache_key}.pkl"

    if cache_path.exists():
        try:
            with open(cache_path, "rb") as f:
                cached = pickle.load(f)
            if isinstance(cached, dict) and "token_map" in cached:
                token_map = cached["token_map"]
                ctwk = cached.get("condition_to_window_keys", {})
                logger.info("Token map loaded from cache: %d condition_ids", len(token_map))
                # Rebuild condition_to_window from market_id keys
                mid_to_window = {w.market_id: w for w in windows}
                condition_to_window: dict[str, MarketWindow] = {}
                for cond, mid in ctwk.items():
                    w = mid_to_window.get(mid)
                    if w is not None:
                        condition_to_window[cond] = w
                _apply_token_map_with_window_map(token_map, condition_to_window, windows)
                return
        except Exception as e:
            logger.warning("Token map cache load failed: %s", e)

    # Count markets that already have tokens from metadata (ground truth)
    already_mapped = sum(1 for w in windows if w.up_token_id and w.dn_token_id)
    needs_mapping = [w for w in windows if not w.up_token_id or not w.dn_token_id]
    logger.info(
        "Token mapping: %d already have tokens from metadata, %d need heuristic mapping",
        already_mapped, len(needs_mapping),
    )
    if not needs_mapping:
        logger.info("All markets have token IDs from metadata — skipping book_log scan")
        return

    logger.info("Building token->market mapping by scanning book_log condition_ids...")

    # Build: condition_id -> set of token_ids (short form)
    condition_tokens: dict[str, set] = {}
    condition_ts_last: dict[str, float] = {}  # last timestamp for tokens in this condition

    for date in dates:
        book_log = data_dir / f"book_log_{date}.jsonl"
        if not book_log.exists():
            continue
        with open(book_log, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_type = obj.get("event_type", "")
                ts = obj.get("ts", 0.0)
                data = obj.get("data", {})
                mkt_hash = data.get("market", "")
                if not mkt_hash:
                    continue

                if event_type == "book":
                    token_id = obj.get("token_id", "")
                    if not token_id:
                        token_id = str(data.get("asset_id", ""))[:20]
                    if token_id:
                        if mkt_hash not in condition_tokens:
                            condition_tokens[mkt_hash] = set()
                        condition_tokens[mkt_hash].add(token_id)
                        condition_ts_last[mkt_hash] = max(
                            condition_ts_last.get(mkt_hash, 0.0), ts
                        )

                elif event_type == "price_change":
                    for pc in data.get("price_changes", []):
                        asset_id = str(pc.get("asset_id", ""))[:20]
                        if asset_id and mkt_hash:
                            if mkt_hash not in condition_tokens:
                                condition_tokens[mkt_hash] = set()
                            condition_tokens[mkt_hash].add(asset_id)
                            condition_ts_last[mkt_hash] = max(
                                condition_ts_last.get(mkt_hash, 0.0), ts
                            )

    logger.info("Found %d unique condition_ids in book_log", len(condition_tokens))

    # Build: market close_epoch -> market window
    close_to_window: dict[int, MarketWindow] = {}
    for w in windows:
        close_to_window[w.close_epoch] = w

    # Match condition_id to market window:
    # The last book event for a condition's tokens should be ~60-200s after close_epoch
    # (bot unsubscribes shortly after settlement)
    # Find the market window whose close_epoch is closest to (last_ts - ~120s)
    all_close_epochs = sorted(close_to_window.keys())

    # condition_id -> (up_token, dn_token) or None
    condition_to_up_dn: dict[str, tuple[str, str] | None] = {}

    for cond_hash, tokens in condition_tokens.items():
        last_ts = condition_ts_last.get(cond_hash, 0.0)
        token_list = list(tokens)

        if len(token_list) != 2:
            # Unexpected token count — skip
            logger.debug("condition %s has %d tokens (expected 2), skipping", cond_hash, len(token_list))
            condition_to_up_dn[cond_hash] = None
            continue

        # Find which market window this condition belongs to
        # The last book event should be shortly after close_epoch (when bot drops the market)
        # Estimate close_epoch = last_ts - ~100s (rough heuristic)
        estimated_close = last_ts - 100.0
        best_window = None
        best_dist = float("inf")
        for close_ep in all_close_epochs:
            dist = abs(close_ep - estimated_close)
            if dist < best_dist:
                best_dist = dist
                best_window = close_to_window[close_ep]

        if best_window is None or best_dist > 900:  # more than 15m off = bad match
            logger.debug(
                "condition %s: no matching window (last_ts=%.0f, est_close=%.0f, best_dist=%.0f)",
                cond_hash, last_ts, estimated_close, best_dist,
            )
            condition_to_up_dn[cond_hash] = None
            continue

        # Determine UP vs DN token
        # Strategy: the UP token (priced as P(UP)) should be the one trading lower
        # early in the window if price is drifting down, but at open they're symmetric
        # Better: look at mid-window best_ask for each token
        # The DOWN token has the complement price: if UP=0.45, DN=0.55
        # In a binary market: UP.best_ask + DN.best_ask ≈ 1.0 at equilibrium
        # We identify UP token by: it's the one where best_ask < 0.5 means P(UP) < 0.5
        # Actually: use the initial book state at open_epoch
        t0 = best_window.open_epoch
        t1 = best_window.close_epoch
        mid_ts = t0 + (t1 - t0) / 4  # use first quarter of window

        asks_at_open = {}
        for tok in token_list:
            state = lookup_book_state(index, tok, mid_ts)
            if state is not None:
                asks_at_open[tok] = state[1]  # best_ask
            else:
                # Try any time in the window
                state = lookup_book_state(index, tok, t1)
                if state is not None:
                    asks_at_open[tok] = state[1]
                else:
                    asks_at_open[tok] = 0.5  # unknown

        # In a CLOB binary market:
        # UP token: if market is near 50%, both trade ~0.5
        # The trick is which is UP vs DN. We can use:
        # The bot logs UP=high price (near 1.0) when near resolved,
        # or we can use the fact that the market_id contains the open_epoch
        # and compare with the actual outcome recorded in the window
        # If outcome=UP: UP token settled at 1.0 (was bid high), DN at 0.0
        # If outcome=DOWN: DN token settled at 1.0 (was bid high), UP at 0.0

        # Use the LAST snapshot (near settlement) to determine which token went to 1.0
        # The winning token should have best_bid close to 1.0 just before settlement
        final_asks = {}
        for tok in token_list:
            state = lookup_book_state(index, tok, t1 - 60)  # 60s before close
            if state is not None:
                final_asks[tok] = state[1]  # best_ask near 0.0 for winner (no one selling cheap)
            else:
                final_asks[tok] = 0.5

        # The WINNING token should have best_ask near 0.01 (seller doesn't want to sell at discount)
        # The LOSING token should have best_ask near 0.99 (nobody wants to buy it)
        # So: the token with LOWER final best_ask is likely the winner
        tok_a, tok_b = token_list[0], token_list[1]
        ask_a = final_asks.get(tok_a, 0.5)
        ask_b = final_asks.get(tok_b, 0.5)

        outcome = best_window.outcome
        if outcome == "UP":
            # UP token won -> UP token has lower final ask (people don't sell cheaply)
            if ask_a <= ask_b:
                up_token = tok_a
                dn_token = tok_b
            else:
                up_token = tok_b
                dn_token = tok_a
        elif outcome == "DOWN":
            # DN token won -> DN token has lower final ask
            if ask_a <= ask_b:
                dn_token = tok_a
                up_token = tok_b
            else:
                dn_token = tok_b
                up_token = tok_a
        else:
            # No outcome — use price at open (lower ask = UP)
            if ask_a <= ask_b:
                up_token = tok_a
                dn_token = tok_b
            else:
                up_token = tok_b
                dn_token = tok_a

        condition_to_up_dn[cond_hash] = (up_token, dn_token)
        logger.debug(
            "Matched condition %s -> market %s (up=%s, dn=%s)",
            cond_hash[:20], best_window.market_id, up_token, dn_token,
        )

    # Apply mapping to windows
    # We need a second pass: condition_hash -> window already done above
    # Build reverse: (estimated) close_epoch from condition -> window
    # And we already matched them in the loop above

    # Re-do the assignment directly (build condition -> window map)
    condition_to_window: dict[str, MarketWindow] = {}
    for cond_hash, tokens in condition_tokens.items():
        last_ts = condition_ts_last.get(cond_hash, 0.0)
        estimated_close = last_ts - 100.0
        best_window = None
        best_dist = float("inf")
        for close_ep in all_close_epochs:
            dist = abs(close_ep - estimated_close)
            if dist < best_dist:
                best_dist = dist
                best_window = close_to_window[close_ep]
        if best_window is not None and best_dist <= 900:
            condition_to_window[cond_hash] = best_window

    # token_map: for caching
    token_map: dict[str, tuple[str, str] | None] = condition_to_up_dn

    # Apply to windows
    _apply_token_map_with_window_map(token_map, condition_to_window, windows)

    # Cache
    try:
        save_data = {
            "token_map": token_map,
            "condition_to_window_keys": {
                k: v.market_id for k, v in condition_to_window.items()
            },
        }
        with open(cache_path, "wb") as f:
            pickle.dump(save_data, f, protocol=4)
        logger.info("Token map cached to %s", cache_path)
    except Exception as e:
        logger.warning("Failed to cache token map: %s", e)


def _apply_token_map(
    cached: dict,
    windows: list[MarketWindow],
) -> None:
    """Apply cached token map (simple version from first cache format)."""
    # This handles the case where cache is just {condition: (up, dn)}
    # We don't have condition_to_window in old cache format
    pass  # Fall through to rebuild


def _apply_token_map_with_window_map(
    token_map: dict[str, tuple[str, str] | None],
    condition_to_window: dict[str, MarketWindow],
    windows: list[MarketWindow],
) -> None:
    """Apply the condition->up_dn mapping to actual MarketWindow objects."""
    mid_to_window = {w.market_id: w for w in windows}

    for cond_hash, up_dn in token_map.items():
        window = condition_to_window.get(cond_hash)
        if window is None:
            continue
        w = mid_to_window.get(window.market_id)
        if w is None:
            continue
        if up_dn is not None and not w.up_token_id:
            # Don't overwrite ground-truth tokens from metadata
            up_token, dn_token = up_dn
            w.up_token_id = up_token
            w.dn_token_id = dn_token


# ---------------------------------------------------------------------------
# Price series loading (for FV computation)
# ---------------------------------------------------------------------------

def load_price_series(
    data_dir: pathlib.Path,
    dates: list[str],
    start_epoch: float,
    end_epoch: float,
    asset: str = "BTC",
    source: str = "binance",
) -> list[tuple[float, float]]:
    """Load (ts, price) pairs from price_log files for the given window."""
    result: list[tuple[float, float]] = []
    for date in dates:
        price_log = data_dir / f"price_log_{date}.jsonl"
        if not price_log.exists():
            continue
        with open(price_log, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = obj.get("ts", 0.0)
                if start_epoch <= ts <= end_epoch:
                    if obj.get("asset", "") == asset and obj.get("source", "") == source:
                        price = obj.get("price")
                        if price is not None:
                            result.append((ts, float(price)))
    result.sort(key=lambda x: x[0])
    return result


def _get_price_at(price_series: list[tuple[float, float]], ts: float) -> float | None:
    """Binary search for most recent price at or before ts."""
    if not price_series:
        return None
    lo, hi = 0, len(price_series) - 1
    result_idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if price_series[mid][0] <= ts:
            result_idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if result_idx < 0:
        return price_series[0][1]
    return price_series[result_idx][1]


# ---------------------------------------------------------------------------
# Core event types
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    side: str        # "UP" or "DN"
    price: float
    qty: float
    epoch_sec: float
    cost: float


@dataclass
class Event:
    epoch_sec: float
    kind: str
    detail: str


@dataclass
class MarketResult:
    market_id: str
    outcome: str | None
    outcome_correct: bool | None
    fills: list[Fill]
    events: list[Event]
    pnl: float
    paired: bool
    up_cost: float
    dn_cost: float
    pair_cost: float
    up_qty: float
    dn_qty: float
    fv_at_entry: float
    certainty_at_entry: float
    aborted: bool
    fv_blocked: bool
    cert_bucket: str
    market_hour: int
    has_book_data: bool  # True if we had local book data for this market


# ---------------------------------------------------------------------------
# Main simulation function
# ---------------------------------------------------------------------------

def simulate_market(
    window: MarketWindow,
    book_index: dict[str, list[tuple[float, float, float]]],
    all_prices: list[tuple[float, float]],
    cfg: BacktestConfig,
) -> MarketResult:
    """Simulate the strategy over one market window using local book_log data."""
    budget = cfg.bankroll * cfg.position_size_fraction

    events: list[Event] = []
    fills: list[Fill] = []
    tick_size = 0.01

    open_ep = float(window.open_epoch)
    close_ep = float(window.close_epoch)
    window_dur = close_ep - open_ep

    # -------------------------------------------------------------------
    # Check if we have book data for this market
    # -------------------------------------------------------------------
    has_book_data = False
    if window.up_token_id and window.dn_token_id:
        up_state = lookup_book_state(book_index, window.up_token_id, open_ep)
        dn_state = lookup_book_state(book_index, window.dn_token_id, open_ep)
        has_book_data = up_state is not None or dn_state is not None

    # -------------------------------------------------------------------
    # Get Binance prices for this window (+/- buffer for vol estimation)
    # -------------------------------------------------------------------
    pre_window_start = open_ep - cfg.vol_window_sec * 2
    window_prices = [
        (ts, price) for ts, price in all_prices
        if pre_window_start <= ts <= close_ep
    ]

    # Price at open (for start_price / PTB)
    start_price = _get_price_at(window_prices, open_ep)

    if start_price is None:
        # No price data — use 0.5 FV
        fv_up = 0.5
        cert = 0.5
        events.append(Event(open_ep, "NO_PRICE_DATA", "No Binance prices available"))
        return _empty_result(window, fv_up, cert, open_ep, fv_blocked=False, events=events)

    # -------------------------------------------------------------------
    # Vol estimation over pre-window period
    # -------------------------------------------------------------------
    vol_annual = cfg.vol_fallback_annual
    vol_est = None
    if _POLYBOT_IMPORTED:
        try:
            vol_est = VolEstimator(
                min_samples=cfg.vol_min_samples,
                fallback_vol_annual=cfg.vol_fallback_annual,
            )
            for ts, price in window_prices:
                vol_est.push(ts, price)
            if vol_est.is_ready:
                vol_annual = vol_est.vol_annualized(cfg.vol_window_sec)
        except Exception:
            pass

    if vol_est is None or not (hasattr(vol_est, 'is_ready') and vol_est.is_ready):
        prices = [p for _, p in window_prices if p > 0]
        if len(prices) >= 2:
            log_rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
            if log_rets:
                mean_r = sum(log_rets) / len(log_rets)
                var = sum((r - mean_r) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
                vol_per_sec = math.sqrt(max(var, 0))
                vol_annual = vol_per_sec * math.sqrt(365.25 * 24 * 3600)

    # -------------------------------------------------------------------
    # Entry decision (at window open)
    # -------------------------------------------------------------------
    entry_spot = _get_price_at(window_prices, open_ep) or start_price
    time_remaining_at_entry = window_dur

    fv_up = _p_fair_up(start_price, entry_spot, time_remaining_at_entry, vol_annual)
    cert = _fv_certainty(fv_up)

    fv_blocked = False

    # -------------------------------------------------------------------
    # Trend filter
    # -------------------------------------------------------------------
    if cfg.trend_filter_enabled:
        lookback_price = _get_price_at(window_prices, open_ep - cfg.trend_filter_window_sec)
        if lookback_price and entry_spot:
            pct_change = abs(entry_spot - lookback_price) / lookback_price
            if pct_change < cfg.trend_filter_threshold_pct:
                events.append(Event(open_ep, "TREND_FILTER_SKIP",
                    f"pct_change={pct_change:.4f}"))
                return _empty_result(window, fv_up, cert, open_ep, fv_blocked=False, events=events)

    # -------------------------------------------------------------------
    # FV gate
    # -------------------------------------------------------------------
    if cfg.fv_gate_enabled and cert >= cfg.fv_gate_certainty_threshold:
        fv_blocked = True
        events.append(Event(open_ep, "FV_GATE_BLOCK",
            f"cert={cert:.3f} >= threshold={cfg.fv_gate_certainty_threshold}"))

    # -------------------------------------------------------------------
    # Get initial book state for UP and DN tokens
    # -------------------------------------------------------------------
    if window.up_token_id:
        up_state = lookup_book_state(book_index, window.up_token_id, open_ep)
        up_ask_initial = up_state[1] if up_state else 0.50
    else:
        up_ask_initial = 0.50

    if window.dn_token_id:
        dn_state = lookup_book_state(book_index, window.dn_token_id, open_ep)
        dn_ask_initial = dn_state[1] if dn_state else 0.50
    else:
        dn_ask_initial = 0.50

    # -------------------------------------------------------------------
    # Build ladders
    # -------------------------------------------------------------------
    if fv_blocked:
        winning_side = "UP" if fv_up >= 0.5 else "DN"
        if winning_side == "UP":
            up_budget = min(budget, cfg.directional_budget_cap)
            dn_budget = 0.0
        else:
            dn_budget = min(budget, cfg.directional_budget_cap)
            up_budget = 0.0
    else:
        up_budget = budget / 2.0
        dn_budget = budget / 2.0

    up_rungs = _build_ladder(
        best_ask=up_ask_initial,
        budget=up_budget,
        rungs=cfg.rungs,
        spacing=cfg.spacing,
        width=cfg.width,
        size_skew=cfg.size_skew,
        tick_size=tick_size,
        fee_rate=cfg.maker_fee_rate,
        max_rung_price=1.0 - tick_size,
    ) if up_budget > 0 else []

    dn_rungs = _build_ladder(
        best_ask=dn_ask_initial,
        budget=dn_budget,
        rungs=cfg.rungs,
        spacing=cfg.spacing,
        width=cfg.width,
        size_skew=cfg.size_skew,
        tick_size=tick_size,
        fee_rate=cfg.maker_fee_rate,
        max_rung_price=1.0 - tick_size,
    ) if dn_budget > 0 else []

    events.append(Event(open_ep, "POST",
        f"up_rungs={len(up_rungs)} dn_rungs={len(dn_rungs)} "
        f"fv={fv_up:.3f} cert={cert:.3f} "
        f"up_ask={up_ask_initial:.2f} dn_ask={dn_ask_initial:.2f}"))

    # -------------------------------------------------------------------
    # Walk through window at 30-second intervals, checking fills
    # -------------------------------------------------------------------
    up_orders: list[tuple[float, float]] = list(up_rungs)
    dn_orders: list[tuple[float, float]] = list(dn_rungs)
    up_filled: list[Fill] = []
    dn_filled: list[Fill] = []

    fv_cancelled_up = False
    fv_cancelled_dn = False
    up_cost_accum = 0.0
    dn_cost_accum = 0.0
    aborted = False

    # Generate time steps (every 30 seconds)
    STEP_SEC = 30.0
    tick_ts = open_ep
    while tick_ts <= close_ep and (up_orders or dn_orders):
        # Get current spot price
        spot_now = _get_price_at(window_prices, tick_ts)
        if spot_now is None:
            tick_ts += STEP_SEC
            continue

        secs_remaining = close_ep - tick_ts
        fv_now = _p_fair_up(start_price, spot_now, secs_remaining, vol_annual)
        cert_now = _fv_certainty(fv_now)

        # Get current book state
        cur_up_ask = 0.50
        cur_dn_ask = 0.50
        if window.up_token_id:
            state = lookup_book_state(book_index, window.up_token_id, tick_ts)
            if state is not None:
                cur_up_ask = state[1]
        if window.dn_token_id:
            state = lookup_book_state(book_index, window.dn_token_id, tick_ts)
            if state is not None:
                cur_dn_ask = state[1]

        # FV cancel
        if cfg.fv_cancel_enabled and cert_now >= cfg.fv_cancel_certainty_threshold:
            losing_side = "DN" if fv_now >= 0.5 else "UP"
            if losing_side == "UP" and not fv_cancelled_up and up_orders:
                up_orders = []
                fv_cancelled_up = True
                events.append(Event(tick_ts, "CANCEL",
                    f"FV cancel UP: fv={fv_now:.3f} cert={cert_now:.3f}"))
            elif losing_side == "DN" and not fv_cancelled_dn and dn_orders:
                dn_orders = []
                fv_cancelled_dn = True
                events.append(Event(tick_ts, "CANCEL",
                    f"FV cancel DN: fv={fv_now:.3f} cert={cert_now:.3f}"))

        # Check UP fills: our resting buy fills when market ask <= our bid price
        for order in list(up_orders):
            price, qty = order
            if price >= cur_up_ask:  # our bid >= market ask -> fill
                cost = price * qty
                up_cost_accum += cost
                f = Fill("UP", price, qty, tick_ts, cost)
                up_filled.append(f)
                fills.append(f)
                up_orders.remove(order)
                events.append(Event(tick_ts, "FILL",
                    f"UP fill: price={price:.2f} qty={qty:.1f} ask={cur_up_ask:.2f}"))

        # Check DN fills
        for order in list(dn_orders):
            price, qty = order
            if price >= cur_dn_ask:
                cost = price * qty
                dn_cost_accum += cost
                f = Fill("DN", price, qty, tick_ts, cost)
                dn_filled.append(f)
                fills.append(f)
                dn_orders.remove(order)
                events.append(Event(tick_ts, "FILL",
                    f"DN fill: price={price:.2f} qty={qty:.1f} ask={cur_dn_ask:.2f}"))

        # One-sided abort
        if cfg.one_sided_abort_enabled and not aborted:
            total_cost = up_cost_accum + dn_cost_accum
            committed_pct = total_cost / max(budget, 0.01)
            if committed_pct >= cfg.one_sided_abort_cost_pct:
                up_q = sum(f.qty for f in up_filled)
                dn_q = sum(f.qty for f in dn_filled)
                if up_q > 0 or dn_q > 0:
                    heavy = max(up_q, dn_q)
                    light = min(up_q, dn_q)
                    if light == 0 and heavy > 0:
                        if up_q > dn_q:
                            cancelled = len(up_orders)
                            up_orders = []
                            events.append(Event(tick_ts, "ABORT",
                                f"UP heavy ({up_q:.1f} vs {dn_q:.1f}), cancelled {cancelled}"))
                        else:
                            cancelled = len(dn_orders)
                            dn_orders = []
                            events.append(Event(tick_ts, "ABORT",
                                f"DN heavy ({dn_q:.1f} vs {up_q:.1f}), cancelled {cancelled}"))
                        aborted = True

        tick_ts += STEP_SEC

    # -------------------------------------------------------------------
    # PnL computation
    # -------------------------------------------------------------------
    up_qty = sum(f.qty for f in up_filled)
    dn_qty = sum(f.qty for f in dn_filled)
    up_cost = sum(f.cost for f in up_filled)
    dn_cost = sum(f.cost for f in dn_filled)

    pnl = 0.0
    paired = False
    pair_cost = 0.0

    if window.outcome is None:
        pnl = 0.0
    elif up_qty > 0 and dn_qty > 0:
        avg_up_price = up_cost / max(up_qty, 0.001)
        avg_dn_price = dn_cost / max(dn_qty, 0.001)
        pair_cost = avg_up_price + avg_dn_price
        paired_qty = min(up_qty, dn_qty)

        if pair_cost <= cfg.max_pair_cost:
            paired = True
            paired_gain = paired_qty * 1.0
            paired_spend = paired_qty * pair_cost
            paired_pnl = paired_gain - paired_spend
        else:
            paired_pnl = 0.0

        # One-sided excess
        if window.outcome == "UP":
            winner_excess = max(0, up_qty - dn_qty)
            loser_excess = max(0, dn_qty - up_qty)
            winner_avg = avg_up_price
            loser_avg = avg_dn_price
        else:
            winner_excess = max(0, dn_qty - up_qty)
            loser_excess = max(0, up_qty - dn_qty)
            winner_avg = avg_dn_price
            loser_avg = avg_up_price

        one_sided_pnl = winner_excess * (1.0 - winner_avg) - loser_excess * loser_avg
        pnl = paired_pnl + one_sided_pnl

    elif up_qty > 0:
        pnl = up_qty * (1.0 - 1.0) if window.outcome == "UP" else -up_cost
        if window.outcome == "UP":
            pnl = up_qty * 1.0 - up_cost
        else:
            pnl = -up_cost
    elif dn_qty > 0:
        if window.outcome == "DOWN":
            pnl = dn_qty * 1.0 - dn_cost
        else:
            pnl = -dn_cost
    else:
        pnl = 0.0

    # FV prediction correctness
    outcome_correct = None
    if window.outcome is not None:
        fv_predicted_up = fv_up >= 0.5
        actual_up = window.outcome == "UP"
        outcome_correct = fv_predicted_up == actual_up

    cert_bucket = _cert_bucket(cert)
    market_hour = datetime.datetime.fromtimestamp(
        window.open_epoch, datetime.timezone.utc
    ).hour

    return MarketResult(
        market_id=window.market_id,
        outcome=window.outcome,
        outcome_correct=outcome_correct,
        fills=fills,
        events=events,
        pnl=round(pnl, 4),
        paired=paired,
        up_cost=round(up_cost, 4),
        dn_cost=round(dn_cost, 4),
        pair_cost=round(pair_cost, 4),
        up_qty=round(up_qty, 2),
        dn_qty=round(dn_qty, 2),
        fv_at_entry=round(fv_up, 4),
        certainty_at_entry=round(cert, 4),
        aborted=aborted,
        fv_blocked=fv_blocked,
        cert_bucket=cert_bucket,
        market_hour=market_hour,
        has_book_data=has_book_data,
    )


def _empty_result(
    window: MarketWindow,
    fv_up: float,
    cert: float,
    entry_sec: float,
    fv_blocked: bool,
    events: list[Event],
) -> MarketResult:
    market_hour = datetime.datetime.fromtimestamp(
        window.open_epoch, datetime.timezone.utc
    ).hour
    return MarketResult(
        market_id=window.market_id,
        outcome=window.outcome,
        outcome_correct=None,
        fills=[],
        events=events,
        pnl=0.0,
        paired=False,
        up_cost=0.0,
        dn_cost=0.0,
        pair_cost=0.0,
        up_qty=0.0,
        dn_qty=0.0,
        fv_at_entry=round(fv_up, 4),
        certainty_at_entry=round(cert, 4),
        aborted=False,
        fv_blocked=fv_blocked,
        cert_bucket=_cert_bucket(cert),
        market_hour=market_hour,
        has_book_data=False,
    )


def _cert_bucket(cert: float) -> str:
    if cert < 0.60:
        return "0.50-0.60"
    elif cert < 0.70:
        return "0.60-0.70"
    elif cert < 0.80:
        return "0.70-0.80"
    elif cert < 0.90:
        return "0.80-0.90"
    else:
        return "0.90-1.00"


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def aggregate_results(results: list[MarketResult], cfg: BacktestConfig) -> dict:
    if not results:
        return {"error": "no results"}

    total_pnl = sum(r.pnl for r in results)
    n = len(results)
    pnl_per_market = total_pnl / n

    wins = sum(1 for r in results if r.pnl > 0)
    win_rate = wins / n

    paired_count = sum(1 for r in results if r.paired)
    paired_rate = paired_count / n

    one_sided = sum(1 for r in results if (r.up_qty > 0) != (r.dn_qty > 0))
    one_sided_rate = one_sided / n

    no_fill = sum(1 for r in results if r.up_qty == 0 and r.dn_qty == 0)
    no_fill_rate = no_fill / n

    fv_blocked_count = sum(1 for r in results if r.fv_blocked)
    fv_blocked_rate = fv_blocked_count / n

    has_book_data_count = sum(1 for r in results if r.has_book_data)
    book_coverage_rate = has_book_data_count / n

    max_loss = min(r.pnl for r in results)
    max_gain = max(r.pnl for r in results)

    cumulative = []
    running = 0.0
    for r in results:
        running += r.pnl
        cumulative.append(running)
    peak = cumulative[0] if cumulative else 0.0
    max_drawdown = 0.0
    for c in cumulative:
        if c > peak:
            peak = c
        drawdown = (peak - c) / max(abs(peak), 1.0)
        max_drawdown = max(max_drawdown, drawdown)

    pnls = [r.pnl for r in results]
    mean_pnl = sum(pnls) / len(pnls)
    if len(pnls) > 1:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(var)
        sharpe_like = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    else:
        sharpe_like = 0.0

    buckets: dict[str, list] = {}
    for r in results:
        b = r.cert_bucket
        if b not in buckets:
            buckets[b] = []
        if r.outcome_correct is not None:
            buckets[b].append(r.outcome_correct)

    calibration_table = {}
    for bucket_name in ["0.50-0.60", "0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00"]:
        outcomes = buckets.get(bucket_name, [])
        win_r = sum(1 for x in outcomes if x) / len(outcomes) if outcomes else None
        calibration_table[bucket_name] = {
            "n": len(outcomes),
            "win_rate": round(win_r, 4) if win_r is not None else None,
        }

    hour_pnl: dict[int, float] = {}
    hour_count: dict[int, int] = {}
    for r in results:
        h = r.market_hour
        hour_pnl[h] = hour_pnl.get(h, 0.0) + r.pnl
        hour_count[h] = hour_count.get(h, 0) + 1
    per_hour_pnl = {str(h): round(v, 4) for h, v in sorted(hour_pnl.items())}
    per_hour_count = {str(h): c for h, c in sorted(hour_count.items())}

    sorted_results = sorted(results, key=lambda r: r.pnl)
    worst_markets = [
        {
            "market_id": r.market_id,
            "pnl": r.pnl,
            "outcome": r.outcome,
            "paired": r.paired,
            "up_qty": r.up_qty,
            "dn_qty": r.dn_qty,
            "has_book_data": r.has_book_data,
            "reason": (
                "no_book_data" if not r.has_book_data else
                "fv_blocked" if r.fv_blocked else
                "aborted" if r.aborted else
                "no_fills" if (r.up_qty == 0 and r.dn_qty == 0) else
                "one_sided" if ((r.up_qty > 0) != (r.dn_qty > 0)) else
                "paired_loss"
            ),
        }
        for r in sorted_results[:5]
    ]

    fv_correct = [r for r in results if r.outcome_correct is not None]
    fv_accuracy = sum(1 for r in fv_correct if r.outcome_correct) / len(fv_correct) if fv_correct else None

    return {
        "config_name": cfg.name,
        "config": cfg.to_dict(),
        "markets_simulated": n,
        "markets_with_outcome": sum(1 for r in results if r.outcome is not None),
        "book_coverage_rate": round(book_coverage_rate, 4),
        "total_pnl": round(total_pnl, 4),
        "mean_pnl_per_market": round(pnl_per_market, 4),
        "win_rate": round(win_rate, 4),
        "paired_rate": round(paired_rate, 4),
        "one_sided_rate": round(one_sided_rate, 4),
        "no_fill_rate": round(no_fill_rate, 4),
        "fv_blocked_rate": round(fv_blocked_rate, 4),
        "fv_accuracy": round(fv_accuracy, 4) if fv_accuracy is not None else None,
        "max_loss": round(max_loss, 4),
        "max_gain": round(max_gain, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        "sharpe_like": round(sharpe_like, 4),
        "calibration_table": calibration_table,
        "per_hour_pnl": per_hour_pnl,
        "per_hour_count": per_hour_count,
        "worst_markets": worst_markets,
    }


# ---------------------------------------------------------------------------
# Dome snapshot file loader
# ---------------------------------------------------------------------------

@dataclass
class DomeMarketData:
    """Parsed data from a single Dome snapshot file."""
    market_slug: str
    condition_id: str
    up_token_id: str
    dn_token_id: str
    window_start: int          # epoch seconds
    window_end: int            # epoch seconds
    outcome: str | None        # "UP" or "DOWN" from winning_side.label

    # Book state at window open (median of all snapshots, which are all at open)
    up_best_bid: float
    up_best_ask: float
    dn_best_bid: float
    dn_best_ask: float

    # FV-related prices
    ptb: float | None          # Chainlink price-to-beat (at window start, from extra_fields)
    binance_at_close: float | None   # last Binance price at window_end
    chainlink_at_close: float | None # last Chainlink price at window_end

    # Binance time series during last 100s (ts_sec, price)
    binance_series: list[tuple[float, float]]

    # Whether this market has orderbook data
    has_orderbook: bool

    # Raw entry count for diagnostics
    ob_up_count: int
    ob_dn_count: int

    # Full orderbook time series: list of (ts_sec, best_bid, best_ask) sorted by ts
    ob_up_series: list[tuple[float, float, float]] = field(default_factory=list)
    ob_dn_series: list[tuple[float, float, float]] = field(default_factory=list)


def load_dome_snapshot(path: pathlib.Path) -> DomeMarketData | None:
    """Parse a single Dome snapshot JSONL file.

    Returns DomeMarketData or None if the file is malformed/missing header.
    Markets with zero orderbook entries are returned with has_orderbook=False
    (caller decides whether to skip them).
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip()]
    except OSError as e:
        logger.warning("Cannot open dome file %s: %s", path, e)
        return None

    if not lines:
        return None

    # Parse header (must be first line)
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError:
        logger.warning("Bad header in %s", path)
        return None

    if header.get("type") != "header":
        logger.warning("First line is not header in %s", path)
        return None

    market_slug = header.get("market_slug", "")
    condition_id = header.get("condition_id", "")
    up_token_id = header.get("up_token_id", "")
    dn_token_id = header.get("dn_token_id", "")
    window_start = int(header.get("window_start", 0))
    window_end = int(header.get("window_end", 0))

    raw_market = header.get("raw_market", {}) or {}
    winning_side = raw_market.get("winning_side") or {}
    winning_label = winning_side.get("label", "")
    winning_id = winning_side.get("id", "")

    # Determine outcome
    outcome: str | None = None
    if winning_label == "Up":
        outcome = "UP"
    elif winning_label == "Down":
        outcome = "DOWN"
    elif winning_id:
        # Fallback: compare by token ID
        if winning_id == up_token_id:
            outcome = "UP"
        elif winning_id == dn_token_id:
            outcome = "DOWN"

    # extra_fields for PTB
    extra = raw_market.get("extra_fields", {})
    ptb: float | None = None
    ptb_raw = extra.get("price_to_beat")
    if ptb_raw is not None:
        try:
            ptb = float(ptb_raw)
        except (ValueError, TypeError):
            pass

    # Parse orderbook entries (UP and DN)
    ob_up_asks: list[float] = []
    ob_up_bids: list[float] = []
    ob_dn_asks: list[float] = []
    ob_dn_bids: list[float] = []

    # Full time series: (ts_sec, best_bid, best_ask)
    ob_up_series_raw: list[tuple[float, float, float]] = []
    ob_dn_series_raw: list[tuple[float, float, float]] = []

    binance_series: list[tuple[float, float]] = []
    chainlink_at_close: float | None = None
    binance_at_close: float | None = None
    max_chainlink_ts: float = 0.0
    max_binance_ts: float = 0.0

    for line in lines[1:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = obj.get("type", "")
        data = obj.get("data", {})

        if t == "orderbook":
            side = obj.get("side", "")
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            best_bid = max((float(b["price"]) for b in bids), default=0.0) if bids else 0.0
            best_ask = min((float(a["price"]) for a in asks), default=1.0) if asks else 1.0
            # Timestamp in milliseconds from Dome API
            ts_ms = data.get("timestamp", 0) or data.get("indexedAt", 0)
            ts_sec = ts_ms / 1000.0
            if side == "UP":
                ob_up_bids.append(best_bid)
                ob_up_asks.append(best_ask)
                ob_up_series_raw.append((ts_sec, best_bid, best_ask))
            elif side == "DN":
                ob_dn_bids.append(best_bid)
                ob_dn_asks.append(best_ask)
                ob_dn_series_raw.append((ts_sec, best_bid, best_ask))

        elif t == "binance":
            ts_ms = data.get("timestamp", 0)
            value = data.get("value")
            if value is not None:
                ts_sec = ts_ms / 1000.0
                binance_series.append((ts_sec, float(value)))
                if ts_ms > max_binance_ts:
                    max_binance_ts = ts_ms
                    binance_at_close = float(value)

        elif t == "chainlink":
            ts_ms = data.get("timestamp", 0)
            value = data.get("value")
            if value is not None and ts_ms > max_chainlink_ts:
                max_chainlink_ts = ts_ms
                chainlink_at_close = float(value)

    # Compute median best_bid/best_ask for UP and DN
    def _median(vals: list[float]) -> float:
        if not vals:
            return 0.5
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    up_best_bid = _median(ob_up_bids) if ob_up_bids else 0.0
    up_best_ask = _median(ob_up_asks) if ob_up_asks else 1.0
    dn_best_bid = _median(ob_dn_bids) if ob_dn_bids else 0.0
    dn_best_ask = _median(ob_dn_asks) if ob_dn_asks else 1.0

    binance_series.sort(key=lambda x: x[0])

    # Sort orderbook series by timestamp
    ob_up_series_raw.sort(key=lambda x: x[0])
    ob_dn_series_raw.sort(key=lambda x: x[0])

    return DomeMarketData(
        market_slug=market_slug,
        condition_id=condition_id,
        up_token_id=up_token_id,
        dn_token_id=dn_token_id,
        window_start=window_start,
        window_end=window_end,
        outcome=outcome,
        up_best_bid=up_best_bid,
        up_best_ask=up_best_ask,
        dn_best_bid=dn_best_bid,
        dn_best_ask=dn_best_ask,
        ptb=ptb,
        binance_at_close=binance_at_close,
        chainlink_at_close=chainlink_at_close,
        binance_series=binance_series,
        has_orderbook=len(ob_up_asks) > 0 or len(ob_dn_asks) > 0,
        ob_up_count=len(ob_up_asks),
        ob_dn_count=len(ob_dn_asks),
        ob_up_series=ob_up_series_raw,
        ob_dn_series=ob_dn_series_raw,
    )


def simulate_market_dome(
    dome: DomeMarketData,
    cfg: BacktestConfig,
) -> MarketResult:
    """Simulate the paired MM strategy for a single market using Dome snapshot data.

    Key differences from local book_log simulation:
    - Book state comes from a batch snapshot at window open (all ~100 UP + 100 DN snapshots
      are within the first ~16 seconds of the window). We use the median best_ask.
    - Fill simulation: A resting buy order fills if our bid price >= market best_ask.
      Since all dome book data is at window open, we can only assess fills at that moment.
      We conservatively fill all rungs where our bid >= open ask.
    - FV computation: Use PTB (Chainlink price-to-beat) as start_price, and Binance
      near window_end as a proxy for "current" price at ~T-100s. This approximates
      what the FV model would have computed near the end of the window.
    - For FV at entry (t=0), we set FV=0.5 (neutral) since we have no price history
      before window open in dome data. This means fv_gate and fv_cancel fire at most
      once (at the entry tick), based on price data from the close period.
    """
    budget = cfg.bankroll * cfg.position_size_fraction
    events: list[Event] = []
    fills: list[Fill] = []
    tick_size = 0.01

    open_ep = float(dome.window_start)
    close_ep = float(dome.window_end)
    window_dur = close_ep - open_ep

    has_book_data = dome.has_orderbook
    market_hour = datetime.datetime.fromtimestamp(
        dome.window_start, datetime.timezone.utc
    ).hour

    # -------------------------------------------------------------------
    # FV computation
    #
    # At entry (window open): We have the book state (UP ask, DN ask).
    # Market-implied probability for UP ≈ mid-price of UP token.
    # If UP ask=0.55 and UP bid=0.50, UP mid ≈ 0.525 → FV_up ≈ 0.525.
    # This is the correct "no-look-ahead" entry FV.
    #
    # Near window close (~100s before end): Dome has Binance/Chainlink prices.
    # We compute FV from PTB vs binance_at_close to simulate fv_cancel.
    # This is valid because we could have retrieved price data during the window
    # from Binance WS (which is available in real trading).
    # -------------------------------------------------------------------
    # FV at entry: use market-implied probability from book mid-price
    up_mid = (dome.up_best_bid + dome.up_best_ask) / 2.0
    dn_mid = (dome.dn_best_bid + dome.dn_best_ask) / 2.0
    # Normalize: market-implied P(UP) = UP_mid / (UP_mid + DN_mid)
    # But simpler: use UP_mid directly as proxy for P(UP)
    fv_up_entry = max(0.01, min(0.99, up_mid))
    cert_entry = _fv_certainty(fv_up_entry)

    # FV near window close: use Binance drift from PTB
    start_price = dome.ptb
    end_price = dome.binance_at_close  # price ~100s before window_end
    fv_up_close = 0.5
    cert_close = 0.5
    if start_price and end_price and start_price > 0 and end_price > 0:
        # At close, remaining seconds is ~100
        secs_remaining_at_close = 100.0
        fv_up_close = _p_fair_up(start_price, end_price, secs_remaining_at_close, cfg.vol_fallback_annual)
        cert_close = _fv_certainty(fv_up_close)

    fv_up = fv_up_entry
    cert = cert_entry

    # -------------------------------------------------------------------
    # FV gate (at entry)
    # -------------------------------------------------------------------
    fv_blocked = False
    if cfg.fv_gate_enabled and cert >= cfg.fv_gate_certainty_threshold:
        fv_blocked = True
        events.append(Event(open_ep, "FV_GATE_BLOCK",
            f"cert={cert:.3f} >= threshold={cfg.fv_gate_certainty_threshold}"))

    # -------------------------------------------------------------------
    # Build ladders from book state at open
    # -------------------------------------------------------------------
    up_ask_initial = dome.up_best_ask
    dn_ask_initial = dome.dn_best_ask

    if fv_blocked:
        winning_side = "UP" if fv_up >= 0.5 else "DN"
        if winning_side == "UP":
            up_budget = min(budget, cfg.directional_budget_cap)
            dn_budget = 0.0
        else:
            dn_budget = min(budget, cfg.directional_budget_cap)
            up_budget = 0.0
    else:
        up_budget = budget / 2.0
        dn_budget = budget / 2.0

    up_rungs = _build_ladder(
        best_ask=up_ask_initial,
        budget=up_budget,
        rungs=cfg.rungs,
        spacing=cfg.spacing,
        width=cfg.width,
        size_skew=cfg.size_skew,
        tick_size=tick_size,
        fee_rate=cfg.maker_fee_rate,
        max_rung_price=1.0 - tick_size,
    ) if up_budget > 0 else []

    dn_rungs = _build_ladder(
        best_ask=dn_ask_initial,
        budget=dn_budget,
        rungs=cfg.rungs,
        spacing=cfg.spacing,
        width=cfg.width,
        size_skew=cfg.size_skew,
        tick_size=tick_size,
        fee_rate=cfg.maker_fee_rate,
        max_rung_price=1.0 - tick_size,
    ) if dn_budget > 0 else []

    events.append(Event(open_ep, "POST",
        f"up_rungs={len(up_rungs)} dn_rungs={len(dn_rungs)} "
        f"fv={fv_up:.3f} cert={cert:.3f} "
        f"up_ask={up_ask_initial:.2f} dn_ask={dn_ask_initial:.2f}"))

    # -------------------------------------------------------------------
    # Fill simulation using real dome orderbook time series
    #
    # We have ~1785 orderbook snapshots per side spanning ~880s of the
    # 900s window. Walk through at configurable tick intervals (default
    # 30s). At each tick, binary-search for the latest book snapshot
    # before that time. Fill rule: resting buy at price P fills if
    # P >= market best_ask at that tick.
    # -------------------------------------------------------------------
    import bisect as _bisect

    up_orders: list[tuple[float, float]] = list(up_rungs)
    dn_orders: list[tuple[float, float]] = list(dn_rungs)
    up_filled: list[Fill] = []
    dn_filled: list[Fill] = []

    fv_cancelled_up = False
    fv_cancelled_dn = False
    up_cost_accum = 0.0
    dn_cost_accum = 0.0
    aborted = False

    # Pre-extract timestamp arrays for binary search
    up_series = dome.ob_up_series  # sorted (ts_sec, best_bid, best_ask)
    dn_series = dome.ob_dn_series

    def _book_at_idx(series: list[tuple[float, float, float]], ts_arr: list[float], t: float) -> tuple[float, float]:
        """Return (best_bid, best_ask) from latest snapshot at or before time t."""
        idx = _bisect.bisect_right(ts_arr, t) - 1
        if idx < 0:
            return (0.0, 1.0)  # no data yet
        return (series[idx][1], series[idx][2])

    # Build merged timeline of all unique snapshot timestamps from both sides
    # This is more efficient than fixed-interval ticking: we only check at
    # times when the book actually changed.
    all_ts_set: set[float] = set()
    up_ts_arr = [s[0] for s in up_series]
    dn_ts_arr = [s[0] for s in dn_series]
    all_ts_set.update(up_ts_arr)
    all_ts_set.update(dn_ts_arr)
    # Also add tick2 for FV cancel check
    tick2_ts = close_ep - 100.0
    all_ts_set.add(tick2_ts)
    all_timestamps = sorted(all_ts_set)

    fv_cancel_done = False

    for t in all_timestamps:
        if not up_orders and not dn_orders:
            break

        # Get current book state via binary search
        up_bb, up_ba = _book_at_idx(up_series, up_ts_arr, t)
        dn_bb, dn_ba = _book_at_idx(dn_series, dn_ts_arr, t)

        # Check UP fills: our bid >= market best_ask
        if up_orders:
            remaining_up = []
            for price, qty in up_orders:
                if price >= up_ba:
                    cost = price * qty
                    up_cost_accum += cost
                    f = Fill("UP", price, qty, t, cost)
                    up_filled.append(f)
                    fills.append(f)
                    events.append(Event(t, "FILL",
                        f"UP fill: price={price:.2f} qty={qty:.1f} ask={up_ba:.2f}"))
                else:
                    remaining_up.append((price, qty))
            up_orders = remaining_up

        # Check DN fills: our bid >= market best_ask
        if dn_orders:
            remaining_dn = []
            for price, qty in dn_orders:
                if price >= dn_ba:
                    cost = price * qty
                    dn_cost_accum += cost
                    f = Fill("DN", price, qty, t, cost)
                    dn_filled.append(f)
                    fills.append(f)
                    events.append(Event(t, "FILL",
                        f"DN fill: price={price:.2f} qty={qty:.1f} ask={dn_ba:.2f}"))
                else:
                    remaining_dn.append((price, qty))
            dn_orders = remaining_dn

        # FV cancel check at tick2 (~100s before end)
        if not fv_cancel_done and t >= tick2_ts:
            fv_cancel_done = True
            if cfg.fv_cancel_enabled and cert_close >= cfg.fv_cancel_certainty_threshold:
                predicted_loser = "DN" if fv_up_close >= 0.5 else "UP"
                if predicted_loser == "UP" and not fv_cancelled_up and up_orders:
                    up_orders = []
                    fv_cancelled_up = True
                    events.append(Event(t, "CANCEL",
                        f"FV cancel UP: fv={fv_up_close:.3f} cert={cert_close:.3f}"))
                elif predicted_loser == "DN" and not fv_cancelled_dn and dn_orders:
                    dn_orders = []
                    fv_cancelled_dn = True
                    events.append(Event(t, "CANCEL",
                        f"FV cancel DN: fv={fv_up_close:.3f} cert={cert_close:.3f}"))

        # One-sided abort check (run once after first fills)
        if cfg.one_sided_abort_enabled and not aborted:
            total_cost = up_cost_accum + dn_cost_accum
            committed_pct = total_cost / max(budget, 0.01)
            if committed_pct >= cfg.one_sided_abort_cost_pct:
                up_q = sum(f.qty for f in up_filled)
                dn_q = sum(f.qty for f in dn_filled)
                if up_q > 0 or dn_q > 0:
                    heavy = max(up_q, dn_q)
                    light = min(up_q, dn_q)
                    if light == 0 and heavy > 0:
                        if up_q > dn_q:
                            cancelled = len(up_orders)
                            up_orders = []
                            events.append(Event(t, "ABORT",
                                f"UP heavy ({up_q:.1f} vs {dn_q:.1f}), cancelled {cancelled}"))
                        else:
                            cancelled = len(dn_orders)
                            dn_orders = []
                            events.append(Event(t, "ABORT",
                                f"DN heavy ({dn_q:.1f} vs {up_q:.1f}), cancelled {cancelled}"))
                        aborted = True

    # -------------------------------------------------------------------
    # PnL computation (same logic as simulate_market)
    # -------------------------------------------------------------------
    up_qty = sum(f.qty for f in up_filled)
    dn_qty = sum(f.qty for f in dn_filled)
    up_cost = sum(f.cost for f in up_filled)
    dn_cost = sum(f.cost for f in dn_filled)

    pnl = 0.0
    paired = False
    pair_cost = 0.0

    if dome.outcome is None:
        pnl = 0.0
    elif up_qty > 0 and dn_qty > 0:
        avg_up_price = up_cost / max(up_qty, 0.001)
        avg_dn_price = dn_cost / max(dn_qty, 0.001)
        pair_cost = avg_up_price + avg_dn_price
        paired_qty = min(up_qty, dn_qty)

        if pair_cost <= cfg.max_pair_cost:
            paired = True
            paired_gain = paired_qty * 1.0
            paired_spend = paired_qty * pair_cost
            paired_pnl = paired_gain - paired_spend
        else:
            paired_pnl = 0.0

        if dome.outcome == "UP":
            winner_excess = max(0, up_qty - dn_qty)
            loser_excess = max(0, dn_qty - up_qty)
            winner_avg = avg_up_price
            loser_avg = avg_dn_price
        else:
            winner_excess = max(0, dn_qty - up_qty)
            loser_excess = max(0, up_qty - dn_qty)
            winner_avg = avg_dn_price
            loser_avg = avg_up_price

        one_sided_pnl = winner_excess * (1.0 - winner_avg) - loser_excess * loser_avg
        pnl = paired_pnl + one_sided_pnl

    elif up_qty > 0:
        if dome.outcome == "UP":
            pnl = up_qty * 1.0 - up_cost
        else:
            pnl = -up_cost
    elif dn_qty > 0:
        if dome.outcome == "DOWN":
            pnl = dn_qty * 1.0 - dn_cost
        else:
            pnl = -dn_cost
    else:
        pnl = 0.0

    # FV prediction correctness
    outcome_correct = None
    if dome.outcome is not None:
        fv_predicted_up = fv_up >= 0.5
        actual_up = dome.outcome == "UP"
        outcome_correct = fv_predicted_up == actual_up

    cert_bucket = _cert_bucket(cert)

    return MarketResult(
        market_id=dome.market_slug,
        outcome=dome.outcome,
        outcome_correct=outcome_correct,
        fills=fills,
        events=events,
        pnl=round(pnl, 4),
        paired=paired,
        up_cost=round(up_cost, 4),
        dn_cost=round(dn_cost, 4),
        pair_cost=round(pair_cost, 4),
        up_qty=round(up_qty, 2),
        dn_qty=round(dn_qty, 2),
        fv_at_entry=round(fv_up, 4),
        certainty_at_entry=round(cert, 4),
        aborted=aborted,
        fv_blocked=fv_blocked,
        cert_bucket=cert_bucket,
        market_hour=market_hour,
        has_book_data=has_book_data,
    )


def run_backtest_dome(
    dome_dir: pathlib.Path,
    cfg: BacktestConfig,
    output_path: pathlib.Path | None = None,
    verbose: bool = False,
    limit: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Run backtest using Dome snapshot files.

    Reads all btc-updown-15m-*.jsonl files from dome_dir.
    Skips markets with no orderbook data (e.g. 2026-04-05).
    Skips future markets (no outcome in raw_market.winning_side).

    If start_date / end_date are given (YYYY-MM-DD, inclusive), filter by
    the window_start epoch encoded in the filename.
    """
    if not dome_dir.exists():
        logger.error("Dome snapshot directory not found: %s", dome_dir)
        return {"error": f"dome directory not found: {dome_dir}"}

    all_files = sorted(dome_dir.glob("btc-updown-15m-*.jsonl"))
    logger.info("Found %d dome snapshot files in %s", len(all_files), dome_dir)

    # Optional date-range filter (by epoch in filename)
    start_ep: int | None = None
    end_ep: int | None = None
    if start_date:
        sd = datetime.date.fromisoformat(start_date)
        start_ep = int(datetime.datetime(
            sd.year, sd.month, sd.day, tzinfo=datetime.timezone.utc
        ).timestamp())
    if end_date:
        ed = datetime.date.fromisoformat(end_date)
        end_ep = int(datetime.datetime(
            ed.year, ed.month, ed.day, tzinfo=datetime.timezone.utc
        ).timestamp()) + 86400  # inclusive end-of-day

    if start_ep is not None or end_ep is not None:
        filtered: list[pathlib.Path] = []
        for p in all_files:
            stem = p.stem  # btc-updown-15m-<epoch>
            parts = stem.rsplit("-", 1)
            if len(parts) != 2:
                continue
            try:
                ep = int(parts[1])
            except ValueError:
                continue
            if start_ep is not None and ep < start_ep:
                continue
            if end_ep is not None and ep >= end_ep:
                continue
            filtered.append(p)
        logger.info(
            "Date filter %s..%s: %d files -> %d",
            start_date or "-", end_date or "-", len(all_files), len(filtered),
        )
        all_files = filtered

    results: list[MarketResult] = []
    skipped_no_book = 0
    skipped_no_outcome = 0
    skipped_other = 0
    loaded_count = 0

    for path in all_files:
        dome = load_dome_snapshot(path)
        if dome is None:
            skipped_other += 1
            continue

        if not dome.has_orderbook:
            skipped_no_book += 1
            if verbose:
                logger.debug("Skip (no book): %s", dome.market_slug)
            continue

        if dome.outcome is None:
            skipped_no_outcome += 1
            if verbose:
                logger.debug("Skip (no outcome): %s", dome.market_slug)
            continue

        loaded_count += 1
        result = simulate_market_dome(dome, cfg)
        results.append(result)

        if limit is not None and loaded_count >= limit:
            logger.info("Reached --limit=%d, stopping", limit)
            break

        if verbose and loaded_count % 100 == 0:
            logger.info(
                "Processed %d markets... latest: %s pnl=%+.2f paired=%s",
                loaded_count, dome.market_slug, result.pnl, result.paired,
            )

    logger.info(
        "Dome backtest: %d simulated, %d skipped_no_book, %d skipped_no_outcome, %d other",
        len(results), skipped_no_book, skipped_no_outcome, skipped_other,
    )

    if not results:
        return {
            "error": "no markets simulated",
            "markets_skipped": {
                "no_book": skipped_no_book,
                "no_settlement": skipped_no_outcome,
                "other": skipped_other,
            }
        }

    agg = aggregate_results(results, cfg)

    # Add dome-specific fields
    agg["data_source"] = "dome"
    agg["markets_skipped"] = {
        "no_book": skipped_no_book,
        "no_settlement": skipped_no_outcome,
        "other": skipped_other,
    }

    # pnl_per_day breakdown (sorted by date string)
    pnl_per_day: dict[str, float] = {}
    count_per_day: dict[str, int] = {}
    for r in results:
        # Extract date from market_id: btc-updown-15m-<epoch>
        parts = r.market_id.rsplit("-", 1)
        if len(parts) == 2:
            try:
                ep = int(parts[1])
                day = datetime.datetime.fromtimestamp(ep, datetime.timezone.utc).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                day = "unknown"
        else:
            day = "unknown"
        pnl_per_day[day] = pnl_per_day.get(day, 0.0) + r.pnl
        count_per_day[day] = count_per_day.get(day, 0) + 1

    agg["pnl_per_day"] = {k: round(v, 4) for k, v in sorted(pnl_per_day.items())}
    agg["count_per_day"] = {k: v for k, v in sorted(count_per_day.items())}

    # Extend worst_markets to top 10
    sorted_results = sorted(results, key=lambda r: r.pnl)
    agg["worst_markets"] = [
        {
            "market_id": r.market_id,
            "pnl": r.pnl,
            "outcome": r.outcome,
            "paired": r.paired,
            "up_qty": r.up_qty,
            "dn_qty": r.dn_qty,
            "has_book_data": r.has_book_data,
            "market_hour": r.market_hour,
            "reason": (
                "no_book_data" if not r.has_book_data else
                "fv_blocked" if r.fv_blocked else
                "aborted" if r.aborted else
                "no_fills" if (r.up_qty == 0 and r.dn_qty == 0) else
                "one_sided" if ((r.up_qty > 0) != (r.dn_qty > 0)) else
                "paired_loss"
            ),
        }
        for r in sorted_results[:10]
    ]

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(agg, f, indent=2)
        logger.info("Dome results written to %s", output_path)

    return agg


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------

def run_backtest(
    data_dir: pathlib.Path,
    cfg: BacktestConfig,
    start_date: str,
    end_date: str,
    output_path: pathlib.Path | None = None,
    verbose: bool = False,
    cache_dir: pathlib.Path | None = None,
) -> dict:
    """Run backtest using local book_log data."""
    # Compute date range
    start_dt = datetime.date.fromisoformat(start_date)
    end_dt = datetime.date.fromisoformat(end_date)
    dates = []
    d = start_dt
    while d <= end_dt:
        dates.append(str(d))
        d += datetime.timedelta(days=1)

    start_epoch = int(datetime.datetime(
        start_dt.year, start_dt.month, start_dt.day,
        tzinfo=datetime.timezone.utc
    ).timestamp())
    end_epoch = int(datetime.datetime(
        end_dt.year, end_dt.month, end_dt.day,
        tzinfo=datetime.timezone.utc
    ).timestamp()) + 86400  # include all of end_date

    settlement_log = data_dir / "settlement_log.jsonl"

    # Build book index (cached)
    book_index = build_book_index(data_dir, dates, cache_dir=cache_dir or data_dir)

    # Load market windows
    windows = load_market_windows(data_dir, dates, settlement_log, start_epoch, end_epoch)
    if not windows:
        logger.error("No market windows found for date range %s to %s", start_date, end_date)
        return {"error": "no market windows found"}

    # Map tokens to markets
    map_tokens_to_markets(book_index, windows, data_dir, dates, cache_dir=cache_dir or data_dir)

    no_token_count = sum(1 for w in windows if w.up_token_id is None)
    logger.info(
        "Markets: %d total, %d without token mapping",
        len(windows), no_token_count,
    )

    # Load price series for all dates
    logger.info("Loading price series for FV computation...")
    all_prices: list[tuple[float, float]] = []
    for date in dates:
        all_prices.extend(
            load_price_series(data_dir, [date], start_epoch - 3600, end_epoch, "BTC", "binance")
        )
    all_prices.sort(key=lambda x: x[0])
    logger.info("Loaded %d BTC price points", len(all_prices))

    # Simulate each market
    results: list[MarketResult] = []
    for w in sorted(windows, key=lambda x: x.open_epoch):
        result = simulate_market(w, book_index, all_prices, cfg)
        results.append(result)

        if verbose:
            logger.info(
                "%-45s  outcome=%-4s  pnl=%+7.2f  paired=%-5s  book=%-5s  fv=%.3f",
                w.market_id[-30:], w.outcome or "?", result.pnl,
                str(result.paired), str(result.has_book_data), result.fv_at_entry,
            )

    logger.info("Simulated %d markets", len(results))

    agg = aggregate_results(results, cfg)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(agg, f, indent=2)
        logger.info("Results written to %s", output_path)

    return agg


def _print_results(agg: dict, cfg_name: str, data_source: str) -> None:
    """Print backtest results summary to stdout."""
    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS: {cfg_name}  [{data_source}]")
    print(f"{'='*60}")
    skipped = agg.get("markets_skipped", {})
    if skipped:
        print(f"Skipped (no book) : {skipped.get('no_book', 0)}")
        print(f"Skipped (no stl)  : {skipped.get('no_settlement', 0)}")
    print(f"Markets simulated : {agg.get('markets_simulated', 0)}")
    print(f"Book data coverage: {agg.get('book_coverage_rate', 0):.1%}")
    print(f"Total PnL         : ${agg.get('total_pnl', 0):.2f}")
    print(f"PnL / market      : ${agg.get('mean_pnl_per_market', 0):.4f}")
    print(f"Win rate          : {agg.get('win_rate', 0):.1%}")
    print(f"Paired rate       : {agg.get('paired_rate', 0):.1%}")
    print(f"One-sided rate    : {agg.get('one_sided_rate', 0):.1%}")
    print(f"No-fill rate      : {agg.get('no_fill_rate', 0):.1%}")
    if agg.get("fv_accuracy") is not None:
        print(f"FV accuracy       : {agg.get('fv_accuracy', 0):.1%}")
    print(f"Max loss          : ${agg.get('max_loss', 0):.2f}")
    print(f"Max drawdown      : {agg.get('max_drawdown_pct', 0):.1%}")
    print(f"Sharpe-like       : {agg.get('sharpe_like', 0):.3f}")
    print(f"{'='*60}")
    print(f"\nCalibration table:")
    for bucket, vals in agg.get("calibration_table", {}).items():
        n = vals["n"]
        wr = vals["win_rate"]
        wr_str = f"{wr:.1%}" if wr is not None else "N/A"
        print(f"  {bucket}: n={n:4d}  win_rate={wr_str}")

    pnl_per_day = agg.get("pnl_per_day")
    if pnl_per_day:
        print(f"\nPnL per day:")
        count_per_day = agg.get("count_per_day", {})
        for day, dpnl in sorted(pnl_per_day.items()):
            cnt = count_per_day.get(day, "?")
            print(f"  {day}: ${dpnl:+9.2f}  (n={cnt})")

    worst = agg.get("worst_markets", [])
    if worst:
        print(f"\nWorst markets (top {len(worst)}):")
        for w in worst:
            print(
                f"  {w['market_id'][-35:]:35s}  pnl={w['pnl']:+7.2f}"
                f"  outcome={w.get('outcome','?'):4s}"
                f"  reason={w.get('reason','?')}"
            )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PolyBot paired MM backtester (local book_log or Dome snapshots)"
    )
    parser.add_argument(
        "--data-source", default="auto",
        choices=["local", "dome", "auto"],
        help=(
            "Data source: 'dome' = use data/dome_snapshots/ (1,344 markets, 14 days); "
            "'local' = use book_log/*.jsonl (122 markets, Apr 10-11); "
            "'auto' = dome if available, else local (default)"
        ),
    )
    parser.add_argument("--data-dir", default="data",
        help="Directory containing book_log, settlement_log, price_log, dome_snapshots/")
    parser.add_argument("--dome-dir", default=None,
        help="Override path to dome snapshot directory (default: data-dir/dome_snapshots)")
    parser.add_argument("--config", default=None,
        help="Path to YAML config file")
    parser.add_argument("--output", default=None, help="Output JSON file path")
    parser.add_argument("--start", default="2026-04-10",
        help="Start date (YYYY-MM-DD), inclusive (both local and dome mode)")
    parser.add_argument("--end", default="2026-04-11",
        help="End date (YYYY-MM-DD), inclusive (both local and dome mode)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--cache-dir", default=None,
        help="Directory for index caches (default: data_dir) [local mode only]")
    parser.add_argument("--limit", type=int, default=None,
        help="Limit number of markets to simulate (for testing)")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.exists():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    if args.config:
        cfg = BacktestConfig.from_yaml(pathlib.Path(args.config))
    else:
        cfg = BacktestConfig()
        cfg.name = "default"

    # Resolve dome directory
    dome_dir = pathlib.Path(args.dome_dir) if args.dome_dir else data_dir / "dome_snapshots"

    # Determine actual data source
    data_source = args.data_source
    if data_source == "auto":
        if dome_dir.exists() and any(dome_dir.glob("btc-updown-15m-*.jsonl")):
            data_source = "dome"
            logger.info("Auto mode: dome snapshots found at %s -> using dome", dome_dir)
        else:
            data_source = "local"
            logger.info("Auto mode: no dome snapshots found -> using local book_log")

    # Default output path includes data source suffix
    if args.output:
        output_path = pathlib.Path(args.output)
    else:
        suffix = "_dome" if data_source == "dome" else ""
        output_path = pathlib.Path("results") / f"{cfg.name}{suffix}.json"

    if data_source == "dome":
        logger.info("Running DOME backtest: %s -> %s", cfg.name, output_path)
        # Only pass date filter in dome mode if user explicitly provided
        # different values than the local-mode defaults (2026-04-10 / 11).
        # This preserves "dome without --start/--end runs full corpus" behavior.
        import sys as _sys
        argv_set = set(_sys.argv)
        dome_start = args.start if "--start" in argv_set else None
        dome_end = args.end if "--end" in argv_set else None
        agg = run_backtest_dome(
            dome_dir=dome_dir,
            cfg=cfg,
            output_path=output_path,
            verbose=args.verbose,
            limit=args.limit,
            start_date=dome_start,
            end_date=dome_end,
        )
        _print_results(agg, cfg.name, "dome")

    else:
        # local book_log mode
        logger.info("Running LOCAL backtest: %s, %s to %s -> %s",
                    cfg.name, args.start, args.end, output_path)
        cache_dir = pathlib.Path(args.cache_dir) if args.cache_dir else data_dir
        agg = run_backtest(
            data_dir=data_dir,
            cfg=cfg,
            start_date=args.start,
            end_date=args.end,
            output_path=output_path,
            verbose=args.verbose,
            cache_dir=cache_dir,
        )
        # Add data_source field for consistency
        agg["data_source"] = "local"
        # Patch output (already written by run_backtest, re-write with data_source)
        if output_path is not None:
            with open(output_path, "w") as f:
                json.dump(agg, f, indent=2)

        print(f"\n{'='*60}")
        print(f"BACKTEST RESULTS: {cfg.name}  [local]")
        print(f"{'='*60}")
        print(f"Date range        : {args.start} to {args.end}")
        print(f"Markets simulated : {agg.get('markets_simulated', 0)}")
        print(f"Book data coverage: {agg.get('book_coverage_rate', 0):.1%}")
        print(f"Total PnL         : ${agg.get('total_pnl', 0):.2f}")
        print(f"PnL / market      : ${agg.get('mean_pnl_per_market', 0):.4f}")
        print(f"Win rate          : {agg.get('win_rate', 0):.1%}")
        print(f"Paired rate       : {agg.get('paired_rate', 0):.1%}")
        print(f"One-sided rate    : {agg.get('one_sided_rate', 0):.1%}")
        print(f"No-fill rate      : {agg.get('no_fill_rate', 0):.1%}")
        if agg.get("fv_accuracy") is not None:
            print(f"FV accuracy       : {agg.get('fv_accuracy', 0):.1%}")
        print(f"Max loss          : ${agg.get('max_loss', 0):.2f}")
        print(f"Max drawdown      : {agg.get('max_drawdown_pct', 0):.1%}")
        print(f"Sharpe-like       : {agg.get('sharpe_like', 0):.3f}")
        print(f"{'='*60}")
        print(f"\nCalibration table:")
        for bucket, vals in agg.get("calibration_table", {}).items():
            n = vals["n"]
            wr = vals["win_rate"]
            wr_str = f"{wr:.1%}" if wr is not None else "N/A"
            print(f"  {bucket}: n={n:4d}  win_rate={wr_str}")
        print()


if __name__ == "__main__":
    main()
