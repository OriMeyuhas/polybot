"""Calibration backtester for PolyBot strategy.

Reads Dome historical snapshots from data/dome_snapshots/*.jsonl and simulates
the strategy against them, producing a calibration report.

Usage:
    python tools/backtester.py \
        --data-dir data/dome_snapshots/ \
        --config experiments/baseline_current.yaml \
        --output results/baseline_current.json

Each JSONL file has:
  Line 1: header (market metadata)
  Rest:   candle, orderbook, binance, chainlink records

Simulation model (simplified):
  - Walk through the market window tick-by-tick using Binance price series
  - At window start, compute FV and decide whether to post ladder
  - Determine fill prices from the orderbook snapshots (best ask on the UP/DN token)
  - At close, compute PnL based on whether UP or DOWN won
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import pathlib
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

    # Entry filter (trend filter — test whether requiring alignment helps)
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
# Data loading
# ---------------------------------------------------------------------------

@dataclass
class MarketSnapshot:
    slug: str
    condition_id: str
    up_token_id: str
    window_start: int   # epoch seconds
    window_end: int     # epoch seconds
    price_to_beat: float
    final_price: float | None
    outcome: str | None  # "UP", "DOWN", or None if unresolved

    # Time series (sorted ascending by timestamp)
    binance: list[dict]    # [{timestamp_ms, value}]
    chainlink: list[dict]  # [{timestamp_ms, value}]
    orderbooks: list[dict] # [{timestamp_ms, asks, bids, tick_size}]
    candles: list[dict]    # [{end_period_ts, yes_ask, yes_bid, price}]


def load_snapshot(path: pathlib.Path) -> MarketSnapshot | None:
    """Parse a dome snapshot JSONL file into a MarketSnapshot."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        logger.warning("Failed to read %s: %s", path, e)
        return None

    header = None
    candles = []
    orderbooks = []
    binance = []
    chainlink = []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue

        t = obj.get("type")
        if t == "header":
            header = obj
        elif t == "candle":
            candles.append(obj["data"])
        elif t == "orderbook":
            d = obj["data"]
            orderbooks.append({
                "timestamp_ms": d.get("timestamp", 0),
                "asks": d.get("asks", []),
                "bids": d.get("bids", []),
                "tick_size": float(d.get("tickSize", 0.01)),
                "asset_id": d.get("assetId", ""),
                # "side" is present in new-schema files ("UP" or "DN"); absent in old files.
                "side": obj.get("side"),
            })
        elif t == "binance":
            d = obj["data"]
            binance.append({
                "timestamp_ms": d["timestamp"],
                "value": float(d["value"]),
            })
        elif t == "chainlink":
            d = obj["data"]
            chainlink.append({
                "timestamp_ms": d["timestamp"],
                "value": float(d["value"]),
            })

    if header is None:
        logger.warning("No header in %s", path)
        return None

    raw = header.get("raw_market", {})
    extra = raw.get("extra_fields", {})
    price_to_beat = extra.get("price_to_beat", 0.0)
    final_price_raw = extra.get("final_price")

    if final_price_raw is not None:
        final_price = float(final_price_raw)
        if final_price > 0 and price_to_beat > 0:
            outcome = "UP" if final_price >= price_to_beat else "DOWN"
        else:
            outcome = None
    else:
        winning_side = raw.get("winning_side")
        if winning_side == "up" or winning_side == "UP":
            outcome = "UP"
            final_price = None
        elif winning_side == "down" or winning_side == "DOWN":
            outcome = "DOWN"
            final_price = None
        else:
            outcome = None
            final_price = None

    # Sort time series ascending
    binance.sort(key=lambda x: x["timestamp_ms"])
    chainlink.sort(key=lambda x: x["timestamp_ms"])
    orderbooks.sort(key=lambda x: x["timestamp_ms"])
    candles.sort(key=lambda x: x["end_period_ts"])

    return MarketSnapshot(
        slug=header.get("market_slug", path.stem),
        condition_id=header.get("condition_id", ""),
        up_token_id=header.get("up_token_id", ""),
        window_start=header.get("window_start", 0),
        window_end=header.get("window_end", 0),
        price_to_beat=float(price_to_beat) if price_to_beat else 0.0,
        final_price=final_price,
        outcome=outcome,
        binance=binance,
        chainlink=chainlink,
        orderbooks=orderbooks,
        candles=candles,
    )


# ---------------------------------------------------------------------------
# Fill simulation helpers
# ---------------------------------------------------------------------------

def best_ask_at(snapshot: MarketSnapshot, epoch_sec: int) -> float | None:
    """Return the best ask price (UP token) at or just before epoch_sec.

    Prefers entries tagged with side="UP". Falls back to entries with no side tag
    (old-schema files). Ignores DN-side entries.
    """
    target_ms = epoch_sec * 1000

    # Separate UP-side and untagged orderbooks (old-schema compatibility)
    up_obs = [ob for ob in snapshot.orderbooks if ob.get("side") in ("UP", None)]
    candidates = up_obs if up_obs else snapshot.orderbooks

    # Find last entry at or before target_ms
    best = None
    for ob in candidates:
        if ob["timestamp_ms"] <= target_ms:
            best = ob
        else:
            break
    if best is None and candidates:
        best = candidates[0]
    if best is None:
        return None
    asks = best["asks"]
    if not asks:
        return None
    # We want the lowest ask = best ask for a buyer
    min_ask = min(float(a["price"]) for a in asks)
    return min_ask


def best_dn_ask_at(snapshot: MarketSnapshot, epoch_sec: int) -> float | None:
    """Return the best ask price for the DOWN token at or just before epoch_sec.

    Priority:
      1. Real DN-side orderbook entries (side="DN") — present in new-schema files.
      2. Approximation from UP bid: dn_ask ≈ 1.0 - best_up_bid — fallback for old
         files that only contain UP orderbook data.
    """
    target_ms = epoch_sec * 1000

    # --- Priority 1: real DN-side orderbooks (new schema) ---
    dn_obs = [ob for ob in snapshot.orderbooks if ob.get("side") == "DN"]
    if dn_obs:
        best_dn = None
        for ob in dn_obs:
            if ob["timestamp_ms"] <= target_ms:
                best_dn = ob
            else:
                break
        if best_dn is None:
            best_dn = dn_obs[0]
        asks = best_dn["asks"]
        if asks:
            tick_size = best_dn.get("tick_size", 0.01)
            min_ask = min(float(a["price"]) for a in asks)
            return max(tick_size, min(1.0 - tick_size, min_ask))

    # --- Priority 2: approximate from UP bid (old schema fallback) ---
    up_obs = [ob for ob in snapshot.orderbooks if ob.get("side") in ("UP", None)]
    candidates = up_obs if up_obs else snapshot.orderbooks

    best = None
    for ob in candidates:
        if ob["timestamp_ms"] <= target_ms:
            best = ob
        else:
            break
    if best is None and candidates:
        best = candidates[0]
    if best is None:
        return None

    bids = best["bids"]
    if not bids:
        return None
    tick_size = best.get("tick_size", 0.01)
    # Best bid on UP token → DN ask ≈ 1 - UP_bid
    max_bid = max(float(b["price"]) for b in bids)
    dn_ask = round(1.0 - max_bid, 10)
    return max(tick_size, min(1.0 - tick_size, dn_ask))


def order_would_fill(order_price: float, best_ask: float | None) -> bool:
    """Check if a passive buy order at order_price would fill.

    In a CLOB, a passive buy (limit order) fills when the market ask drops to
    or below our bid price. Since we're posting bids, we fill when:
    our_price >= current_best_ask

    This is a simplification — we ignore queue position.
    """
    if best_ask is None:
        return False
    return order_price >= best_ask


def candle_midpoint_at(snapshot: MarketSnapshot, epoch_sec: int) -> float | None:
    """Get UP token midpoint price from candles at epoch_sec (used as fallback)."""
    # Find the candle whose window contains epoch_sec
    # Candles are 1-minute intervals, end_period_ts is the end of the candle
    for c in snapshot.candles:
        end_ts = c["end_period_ts"]
        start_ts = end_ts - 60
        if start_ts <= epoch_sec <= end_ts:
            close_dollars = c["price"].get("close_dollars", "0.5")
            try:
                return float(close_dollars)
            except (ValueError, TypeError):
                return None
    return None


# ---------------------------------------------------------------------------
# Core event types
# ---------------------------------------------------------------------------

@dataclass
class Fill:
    side: str        # "UP" or "DN"
    price: float
    qty: float
    epoch_sec: int
    cost: float      # price * qty


@dataclass
class Event:
    epoch_sec: int
    kind: str        # "FILL", "CANCEL", "ABORT", "POST", "FV_GATE_BLOCK"
    detail: str


@dataclass
class MarketResult:
    slug: str
    outcome: str | None
    outcome_correct: bool | None  # did our FV prediction agree with outcome?
    fills: list[Fill]
    events: list[Event]
    pnl: float
    paired: bool
    up_cost: float
    dn_cost: float
    pair_cost: float
    up_qty: float
    dn_qty: float
    fv_at_entry: float   # FV p_up when we first posted
    certainty_at_entry: float
    aborted: bool
    fv_blocked: bool     # True if FV gate blocked posting
    # Confidence bucket for calibration (based on certainty at entry)
    cert_bucket: str
    # For per-hour breakdown
    market_hour: int     # hour of day (UTC) when window opened


# ---------------------------------------------------------------------------
# Main simulation function
# ---------------------------------------------------------------------------

def simulate_market(snapshot: MarketSnapshot, cfg: BacktestConfig) -> MarketResult:
    """Simulate the strategy over one historical market window."""
    window_dur = snapshot.window_end - snapshot.window_start
    budget = cfg.bankroll * cfg.position_size_fraction

    events: list[Event] = []
    fills: list[Fill] = []

    # -------------------------------------------------------------------
    # Step 1: Build Binance price series for FV computation
    # -------------------------------------------------------------------
    # Start price = Chainlink PTB (price_to_beat) if available, else first Binance tick
    start_price: float | None = None
    if snapshot.price_to_beat and snapshot.price_to_beat > 0:
        start_price = snapshot.price_to_beat

    # Build price-time mapping for vol estimation
    vol_est = None
    if _POLYBOT_IMPORTED:
        try:
            vol_est = VolEstimator(
                min_samples=cfg.vol_min_samples,
                fallback_vol_annual=cfg.vol_fallback_annual,
            )
        except Exception:
            pass

    # Feed all binance prices into vol estimator in order
    for bp in snapshot.binance:
        ts_sec = bp["timestamp_ms"] / 1000.0
        if vol_est is not None:
            vol_est.push(ts_sec, bp["value"])

    # Use the mid-window vol as representative
    if vol_est is not None and vol_est.is_ready:
        vol_annual = vol_est.vol_annualized(cfg.vol_window_sec)
    else:
        # Simple fallback: compute realized vol from price series
        prices = [bp["value"] for bp in snapshot.binance if bp["value"] > 0]
        if len(prices) >= 2:
            log_rets = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices))]
            mean_r = sum(log_rets) / len(log_rets)
            var = sum((r - mean_r) ** 2 for r in log_rets) / max(len(log_rets) - 1, 1)
            vol_per_sec = math.sqrt(var)
            vol_annual = vol_per_sec * math.sqrt(365.25 * 24 * 3600)
        else:
            vol_annual = cfg.vol_fallback_annual

    # -------------------------------------------------------------------
    # Step 2: Entry decision (at window start)
    # -------------------------------------------------------------------
    entry_sec = snapshot.window_start
    time_remaining_at_entry = snapshot.window_end - entry_sec

    # Get current spot price at entry
    entry_binance = next(
        (bp for bp in snapshot.binance if bp["timestamp_ms"] / 1000.0 >= entry_sec),
        snapshot.binance[0] if snapshot.binance else None,
    )
    entry_spot = float(entry_binance["value"]) if entry_binance else start_price

    if start_price is None:
        start_price = entry_spot

    # Compute FV at entry
    fv_up = _p_fair_up(start_price, entry_spot, time_remaining_at_entry, vol_annual)
    cert = _fv_certainty(fv_up)

    fv_blocked = False

    # -------------------------------------------------------------------
    # Step 3: Trend filter (optional)
    # -------------------------------------------------------------------
    if cfg.trend_filter_enabled and snapshot.binance:
        # Look at price change over trend_filter_window_sec before entry
        lookback_ms = entry_sec * 1000 - cfg.trend_filter_window_sec * 1000
        lookback_price = None
        for bp in snapshot.binance:
            if bp["timestamp_ms"] >= lookback_ms:
                lookback_price = bp["value"]
                break
        if lookback_price and entry_spot:
            pct_change = abs(entry_spot - lookback_price) / lookback_price
            if pct_change < cfg.trend_filter_threshold_pct:
                # Market is ranging — skip entry
                events.append(Event(entry_sec, "TREND_FILTER_SKIP",
                    f"pct_change={pct_change:.4f} < threshold={cfg.trend_filter_threshold_pct}"))
                return _empty_result(snapshot, fv_up, cert, entry_sec, fv_blocked=False, events=events)

    # -------------------------------------------------------------------
    # Step 4: FV gate (optional)
    # -------------------------------------------------------------------
    if cfg.fv_gate_enabled and cert >= cfg.fv_gate_certainty_threshold:
        # Block posting the losing side; only post directional
        fv_blocked = True
        events.append(Event(entry_sec, "FV_GATE_BLOCK",
            f"cert={cert:.3f} >= threshold={cfg.fv_gate_certainty_threshold}"))

    # -------------------------------------------------------------------
    # Step 5: Get best asks at entry for UP and DN tokens
    # -------------------------------------------------------------------
    up_ask = best_ask_at(snapshot, entry_sec)
    dn_ask = best_dn_ask_at(snapshot, entry_sec)

    if up_ask is None:
        up_ask = 0.50  # default midpoint
    if dn_ask is None:
        dn_ask = 0.50

    # -------------------------------------------------------------------
    # Step 6: Build ladders for UP and DN sides
    # -------------------------------------------------------------------
    tick_size = 0.01  # standard Polymarket tick

    if fv_blocked:
        # Only post the side FV says is winning (within directional cap)
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
        best_ask=up_ask,
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
        best_ask=dn_ask,
        budget=dn_budget,
        rungs=cfg.rungs,
        spacing=cfg.spacing,
        width=cfg.width,
        size_skew=cfg.size_skew,
        tick_size=tick_size,
        fee_rate=cfg.maker_fee_rate,
        max_rung_price=1.0 - tick_size,
    ) if dn_budget > 0 else []

    events.append(Event(entry_sec, "POST",
        f"up_rungs={len(up_rungs)} dn_rungs={len(dn_rungs)} fv={fv_up:.3f} cert={cert:.3f}"))

    # -------------------------------------------------------------------
    # Step 7: Walk through the window tick-by-tick, checking fills
    # -------------------------------------------------------------------
    # We walk second by second through the Binance price series
    # For each tick we check if any of our resting orders would fill

    # Track fill state
    up_orders: list[tuple[float, float]] = list(up_rungs)  # (price, qty) still unfilled
    dn_orders: list[tuple[float, float]] = list(dn_rungs)
    up_filled: list[Fill] = []
    dn_filled: list[Fill] = []

    # Track FV cancel
    fv_cancelled_up = False
    fv_cancelled_dn = False

    # Track running costs (for abort check)
    up_cost_accum = 0.0
    dn_cost_accum = 0.0

    aborted = False

    prev_up_ask = up_ask
    prev_dn_ask = dn_ask

    # Build a time index of binance ticks in the window
    window_ticks = [
        bp for bp in snapshot.binance
        if snapshot.window_start * 1000 <= bp["timestamp_ms"] <= snapshot.window_end * 1000
    ]

    for bp in window_ticks:
        tick_sec = int(bp["timestamp_ms"] / 1000)
        spot_now = bp["value"]

        # Update vol estimator (already done above, but for completeness)
        # Recompute FV at this tick for cancel/abort decisions
        secs_remaining = snapshot.window_end - tick_sec
        fv_now = _p_fair_up(start_price, spot_now, secs_remaining, vol_annual)
        cert_now = _fv_certainty(fv_now)

        # Get current book state
        cur_up_ask = best_ask_at(snapshot, tick_sec) or prev_up_ask
        cur_dn_ask = best_dn_ask_at(snapshot, tick_sec) or prev_dn_ask
        prev_up_ask = cur_up_ask
        prev_dn_ask = cur_dn_ask

        # --- FV cancel ---
        if cfg.fv_cancel_enabled and cert_now >= cfg.fv_cancel_certainty_threshold:
            losing_side = "DN" if fv_now >= 0.5 else "UP"
            if losing_side == "UP" and not fv_cancelled_up and up_orders:
                up_orders = []
                fv_cancelled_up = True
                events.append(Event(tick_sec, "CANCEL",
                    f"FV cancel UP side: fv={fv_now:.3f} cert={cert_now:.3f}"))
            elif losing_side == "DN" and not fv_cancelled_dn and dn_orders:
                dn_orders = []
                fv_cancelled_dn = True
                events.append(Event(tick_sec, "CANCEL",
                    f"FV cancel DN side: fv={fv_now:.3f} cert={cert_now:.3f}"))

        # --- Check UP fills ---
        for order in list(up_orders):
            price, qty = order
            if order_would_fill(price, cur_up_ask):
                cost = price * qty
                up_cost_accum += cost
                f = Fill("UP", price, qty, tick_sec, cost)
                up_filled.append(f)
                fills.append(f)
                up_orders.remove(order)
                events.append(Event(tick_sec, "FILL", f"UP fill: price={price:.2f} qty={qty:.1f}"))

        # --- Check DN fills ---
        for order in list(dn_orders):
            price, qty = order
            if order_would_fill(price, cur_dn_ask):
                cost = price * qty
                dn_cost_accum += cost
                f = Fill("DN", price, qty, tick_sec, cost)
                dn_filled.append(f)
                fills.append(f)
                dn_orders.remove(order)
                events.append(Event(tick_sec, "FILL", f"DN fill: price={price:.2f} qty={qty:.1f}"))

        # --- One-sided abort check ---
        if cfg.one_sided_abort_enabled and not aborted:
            total_cost = up_cost_accum + dn_cost_accum
            committed_pct = total_cost / max(budget, 0.01)
            if committed_pct >= cfg.one_sided_abort_cost_pct:
                up_q = sum(f.qty for f in up_filled)
                dn_q = sum(f.qty for f in dn_filled)
                heavy = max(up_q, dn_q)
                light = min(up_q, dn_q)
                if heavy > 0 and light == 0:
                    ratio = heavy / max(light, 0.001)
                else:
                    ratio = heavy / max(light, 0.001) if light > 0 else 0
                if light == 0 and heavy > 0 and ratio >= cfg.one_sided_abort_ratio:
                    # Cancel unfilled orders on the filled side (stop adding to imbalance)
                    if up_q > dn_q:
                        cancelled = len(up_orders)
                        up_orders = []
                        events.append(Event(tick_sec, "ABORT",
                            f"One-sided abort: UP heavy ({up_q:.1f} vs {dn_q:.1f}), cancelled {cancelled} UP orders"))
                    else:
                        cancelled = len(dn_orders)
                        dn_orders = []
                        events.append(Event(tick_sec, "ABORT",
                            f"One-sided abort: DN heavy ({dn_q:.1f} vs {up_q:.1f}), cancelled {cancelled} DN orders"))
                    aborted = True

    # -------------------------------------------------------------------
    # Step 8: Compute PnL at settlement
    # -------------------------------------------------------------------
    up_qty = sum(f.qty for f in up_filled)
    dn_qty = sum(f.qty for f in dn_filled)
    up_cost = sum(f.cost for f in up_filled)
    dn_cost = sum(f.cost for f in dn_filled)
    total_cost = up_cost + dn_cost

    pnl = 0.0
    paired = False
    pair_cost = total_cost / max(min(up_qty, dn_qty), 0.001) if (up_qty > 0 and dn_qty > 0) else 0.0

    if snapshot.outcome is None:
        # Market unresolved — can't compute PnL
        pnl = 0.0
    else:
        # Pair cost guard: did we pass the guard?
        if up_qty > 0 and dn_qty > 0:
            paired_qty = min(up_qty, dn_qty)
            # Avg cost per paired share
            avg_up_price = up_cost / max(up_qty, 0.001)
            avg_dn_price = dn_cost / max(dn_qty, 0.001)
            implied_pair_cost = avg_up_price + avg_dn_price
            pair_cost = implied_pair_cost

            if pair_cost <= cfg.max_pair_cost:
                paired = True
                # Paired gain: $1.00 per pair regardless of outcome
                paired_gain = paired_qty * 1.0
                paired_spend = paired_qty * pair_cost
                paired_pnl = paired_gain - paired_spend
            else:
                paired = False
                paired_pnl = 0.0

            # Unpaired excess shares (one-sided PnL)
            if snapshot.outcome == "UP":
                winner_qty = up_qty - dn_qty if up_qty > dn_qty else 0
                loser_qty = dn_qty - up_qty if dn_qty > up_qty else 0
                loser_cost = dn_cost - (dn_cost / max(dn_qty, 0.001)) * min(up_qty, dn_qty) if dn_qty > up_qty else 0
                winner_cost = up_cost - (up_cost / max(up_qty, 0.001)) * min(up_qty, dn_qty) if up_qty > dn_qty else 0
            else:  # DOWN
                winner_qty = dn_qty - up_qty if dn_qty > up_qty else 0
                loser_qty = up_qty - dn_qty if up_qty > dn_qty else 0
                loser_cost = up_cost - (up_cost / max(up_qty, 0.001)) * min(up_qty, dn_qty) if up_qty > dn_qty else 0
                winner_cost = dn_cost - (dn_cost / max(dn_qty, 0.001)) * min(up_qty, dn_qty) if dn_qty > up_qty else 0

            one_sided_gain = winner_qty * 1.0 - winner_cost - loser_cost

            pnl = paired_pnl + one_sided_gain if paired else one_sided_gain

        elif up_qty > 0:
            # UP-only position
            if snapshot.outcome == "UP":
                pnl = up_qty * 1.0 - up_cost
            else:
                pnl = -up_cost
        elif dn_qty > 0:
            # DN-only position
            if snapshot.outcome == "DOWN":
                pnl = dn_qty * 1.0 - dn_cost
            else:
                pnl = -dn_cost
        else:
            pnl = 0.0

    # FV prediction correctness
    if snapshot.outcome is not None:
        fv_predicted_up = fv_up >= 0.5
        actual_up = snapshot.outcome == "UP"
        outcome_correct = fv_predicted_up == actual_up
    else:
        outcome_correct = None

    # Confidence bucket
    cert_bucket = _cert_bucket(cert)

    # Market hour (UTC)
    import datetime as dt_mod
    market_hour = dt_mod.datetime.fromtimestamp(snapshot.window_start, dt_mod.timezone.utc).hour

    return MarketResult(
        slug=snapshot.slug,
        outcome=snapshot.outcome,
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
    )


def _empty_result(
    snapshot: MarketSnapshot,
    fv_up: float,
    cert: float,
    entry_sec: int,
    fv_blocked: bool,
    events: list[Event],
) -> MarketResult:
    """Return a no-trade result."""
    import datetime as dt_mod
    return MarketResult(
        slug=snapshot.slug,
        outcome=snapshot.outcome,
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
        market_hour=dt_mod.datetime.fromtimestamp(snapshot.window_start, dt_mod.timezone.utc).hour,
    )


def _cert_bucket(cert: float) -> str:
    """Return calibration bucket label for a certainty value."""
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
    """Compute aggregate metrics from per-market results."""
    if not results:
        return {"error": "no results"}

    total_pnl = sum(r.pnl for r in results)
    n = len(results)
    pnl_per_market = total_pnl / n

    # Win rate (markets with pnl > 0)
    wins = sum(1 for r in results if r.pnl > 0)
    win_rate = wins / n

    # Paired rate (markets where both sides filled)
    paired_count = sum(1 for r in results if r.paired)
    paired_rate = paired_count / n

    # One-sided rate (only one side filled)
    one_sided = sum(1 for r in results if (r.up_qty > 0) != (r.dn_qty > 0))
    one_sided_rate = one_sided / n

    # No-fill rate
    no_fill = sum(1 for r in results if r.up_qty == 0 and r.dn_qty == 0)
    no_fill_rate = no_fill / n

    # FV gate blocked rate
    fv_blocked_count = sum(1 for r in results if r.fv_blocked)
    fv_blocked_rate = fv_blocked_count / n

    # Worst loss
    max_loss = min(r.pnl for r in results)
    max_gain = max(r.pnl for r in results)

    # Max drawdown (cumulative)
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

    # Sharpe-like: mean/std of per-market PnL
    pnls = [r.pnl for r in results]
    mean_pnl = sum(pnls) / len(pnls)
    if len(pnls) > 1:
        var = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(var)
        sharpe_like = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    else:
        sharpe_like = 0.0

    # Calibration table
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

    # Per-hour PnL
    hour_pnl: dict[int, float] = {}
    hour_count: dict[int, int] = {}
    for r in results:
        h = r.market_hour
        hour_pnl[h] = hour_pnl.get(h, 0.0) + r.pnl
        hour_count[h] = hour_count.get(h, 0) + 1
    per_hour_pnl = {str(h): round(v, 4) for h, v in sorted(hour_pnl.items())}
    per_hour_count = {str(h): c for h, c in sorted(hour_count.items())}

    # Worst markets (bottom 5)
    sorted_results = sorted(results, key=lambda r: r.pnl)
    worst_markets = [
        {
            "market_id": r.slug,
            "pnl": r.pnl,
            "outcome": r.outcome,
            "paired": r.paired,
            "up_qty": r.up_qty,
            "dn_qty": r.dn_qty,
            "reason": (
                "fv_blocked" if r.fv_blocked else
                "aborted" if r.aborted else
                "no_fills" if (r.up_qty == 0 and r.dn_qty == 0) else
                "one_sided" if ((r.up_qty > 0) != (r.dn_qty > 0)) else
                "paired_loss"
            ),
        }
        for r in sorted_results[:5]
    ]

    # FV accuracy (over markets where we have an outcome)
    fv_correct = [r for r in results if r.outcome_correct is not None]
    fv_accuracy = sum(1 for r in fv_correct if r.outcome_correct) / len(fv_correct) if fv_correct else None

    return {
        "config_name": cfg.name,
        "config": cfg.to_dict(),
        "markets_simulated": n,
        "markets_with_outcome": sum(1 for r in results if r.outcome is not None),
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
# CLI runner
# ---------------------------------------------------------------------------

def run_backtest(
    data_dir: pathlib.Path,
    cfg: BacktestConfig,
    output_path: pathlib.Path | None = None,
    verbose: bool = False,
) -> dict:
    """Run backtest over all JSONL files in data_dir."""
    jsonl_files = sorted(data_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("No JSONL files found in %s", data_dir)
        return {"error": "no data files found", "data_dir": str(data_dir)}

    logger.info("Found %d snapshot files in %s", len(jsonl_files), data_dir)

    results: list[MarketResult] = []
    skipped = 0

    for fpath in jsonl_files:
        snapshot = load_snapshot(fpath)
        if snapshot is None:
            skipped += 1
            continue

        result = simulate_market(snapshot, cfg)
        results.append(result)

        if verbose:
            outcome_str = snapshot.outcome or "?"
            logger.info(
                "%-45s  outcome=%-4s  pnl=%+7.2f  paired=%-5s  fv=%.3f  cert=%.3f",
                snapshot.slug, outcome_str, result.pnl,
                str(result.paired), result.fv_at_entry, result.certainty_at_entry,
            )

    logger.info("Simulated %d markets, skipped %d", len(results), skipped)

    agg = aggregate_results(results, cfg)
    agg["skipped_files"] = skipped

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(agg, f, indent=2)
        logger.info("Results written to %s", output_path)

    return agg


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backtester for PolyBot strategy against Dome historical snapshots"
    )
    parser.add_argument("--data-dir", required=True, help="Directory with .jsonl snapshot files")
    parser.add_argument(
        "--config", default=None,
        help="Path to YAML config file (BacktestConfig fields). Defaults to baseline_current.yaml"
    )
    parser.add_argument("--output", default=None, help="Output JSON file path")
    parser.add_argument("--verbose", "-v", action="store_true", help="Log per-market results")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir)
    if not data_dir.exists():
        logger.error("Data directory does not exist: %s", data_dir)
        sys.exit(1)

    if args.config:
        config_path = pathlib.Path(args.config)
        cfg = BacktestConfig.from_yaml(config_path)
    else:
        cfg = BacktestConfig()
        cfg.name = "default"

    if args.output:
        output_path = pathlib.Path(args.output)
    else:
        output_path = pathlib.Path("results") / f"{cfg.name}.json"

    agg = run_backtest(data_dir, cfg, output_path, verbose=args.verbose)

    # Print summary to stdout
    print(f"\n{'='*60}")
    print(f"BACKTEST RESULTS: {cfg.name}")
    print(f"{'='*60}")
    print(f"Markets simulated : {agg.get('markets_simulated', 0)}")
    print(f"Total PnL         : ${agg.get('total_pnl', 0):.2f}")
    print(f"PnL / market      : ${agg.get('mean_pnl_per_market', 0):.4f}")
    print(f"Win rate          : {agg.get('win_rate', 0):.1%}")
    print(f"Paired rate       : {agg.get('paired_rate', 0):.1%}")
    print(f"One-sided rate    : {agg.get('one_sided_rate', 0):.1%}")
    print(f"No-fill rate      : {agg.get('no_fill_rate', 0):.1%}")
    if agg.get('fv_accuracy') is not None:
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
