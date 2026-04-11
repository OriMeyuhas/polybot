"""FV Brain calibration tool for PolyBot.

Uses all 771 settlements from settlement_log.jsonl (Apr 2-11) to build a
calibration table: for each FV certainty bucket, what is the realized win rate?

If FV has a real signal, high-certainty predictions should win more often than
low-certainty ones. This tool measures that with statistical significance over
the full settlement sample.

Usage:
    python tools/fv_calibration.py \\
        --start 2026-04-02 --end 2026-04-11 \\
        --out results/fv_calibration.json

Data sources:
    - settlement_log.jsonl: market windows + outcomes
    - price_log_*.jsonl: Binance prices (primary FV input)
    - Dome Binance/Chainlink API (cached, for markets before local data)
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import math
import pathlib
import sys
import time
from dataclasses import dataclass, asdict
from typing import Any

# ---------------------------------------------------------------------------
# Add project root to path
# ---------------------------------------------------------------------------
_TOOLS_DIR = pathlib.Path(__file__).parent
_PROJECT_ROOT = _TOOLS_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from polybot.strategy.fair_value import p_fair_up, certainty as fv_certainty
    from polybot.strategy.vol_estimator import VolEstimator
    _POLYBOT_IMPORTED = True
except ImportError:
    _POLYBOT_IMPORTED = False

try:
    from tools.dome_client import DomeClient
    _DOME_IMPORTED = True
except ImportError:
    _DOME_IMPORTED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fv_calibration")


# ---------------------------------------------------------------------------
# Fallback FV implementation
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _p_fair_up_fallback(
    start_price: float | None,
    current_price: float | None,
    seconds_to_resolution: float,
    vol_annualized: float | None = 0.5,
) -> float:
    if start_price is None or current_price is None:
        return 0.5
    s, c = float(start_price), float(current_price)
    if s <= 0 or c <= 0:
        return 0.5
    if seconds_to_resolution <= 0:
        return 0.99 if c >= s else 0.01
    v = vol_annualized or 0.5
    t_years = seconds_to_resolution / (365.25 * 24 * 3600)
    denom = v * math.sqrt(t_years)
    if denom < 1e-15:
        return 0.99 if c >= s else 0.01
    d = math.log(c / s) / denom
    d = max(-6.0, min(6.0, d))
    return max(0.01, min(0.99, _norm_cdf(d)))


def _fv_certainty_fallback(p_up: float) -> float:
    return max(p_up, 1.0 - p_up)


if _POLYBOT_IMPORTED:
    _p_fair_up = p_fair_up
    _fv_cert = fv_certainty
else:
    _p_fair_up = _p_fair_up_fallback
    _fv_cert = _fv_certainty_fallback


# ---------------------------------------------------------------------------
# Settlement record
# ---------------------------------------------------------------------------

@dataclass
class Settlement:
    market_id: str
    ts: float
    outcome: str
    open_epoch: int
    close_epoch: int
    timeframe_sec: int
    asset: str


# ---------------------------------------------------------------------------
# Price loading from local files
# ---------------------------------------------------------------------------

def load_local_prices(
    data_dir: pathlib.Path,
    start_ts: float,
    end_ts: float,
    asset: str = "BTC",
    source: str = "binance",
) -> list[tuple[float, float]]:
    """Load (ts, price) tuples from local price_log files."""
    result: list[tuple[float, float]] = []
    start_dt = datetime.datetime.fromtimestamp(start_ts, datetime.timezone.utc).date()
    end_dt = datetime.datetime.fromtimestamp(end_ts, datetime.timezone.utc).date()

    d = start_dt
    while d <= end_dt:
        price_log = data_dir / f"price_log_{d}.jsonl"
        if price_log.exists():
            with open(price_log, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = obj.get("ts", 0.0)
                    if start_ts - 600 <= ts <= end_ts + 60:
                        if obj.get("asset", "") == asset and obj.get("source", "") == source:
                            price = obj.get("price")
                            if price is not None:
                                result.append((ts, float(price)))
        d += datetime.timedelta(days=1)

    result.sort(key=lambda x: x[0])
    return result


def _get_price_at(prices: list[tuple[float, float]], ts: float) -> float | None:
    """Binary search for price at or just before ts."""
    if not prices:
        return None
    lo, hi = 0, len(prices) - 1
    result_idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if prices[mid][0] <= ts:
            result_idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if result_idx < 0:
        return prices[0][1] if prices else None
    return prices[result_idx][1]


# ---------------------------------------------------------------------------
# Dome price fetching (with caching)
# ---------------------------------------------------------------------------

def fetch_dome_prices_cached(
    dome: Any,
    market_id: str,
    open_epoch: int,
    close_epoch: int,
    cache_dir: pathlib.Path,
) -> list[tuple[float, float]]:
    """Fetch Binance prices from Dome, cached to disk."""
    cache_key = f"dome_prices_{market_id}_{open_epoch}.json"
    cache_path = cache_dir / cache_key
    if cache_path.exists():
        try:
            with open(cache_path) as f:
                data = json.load(f)
            return [(float(x["ts"]), float(x["price"])) for x in data]
        except Exception:
            pass

    if dome is None:
        return []

    try:
        resp = dome.binance_prices(
            token="BTC",
            start_epoch=open_epoch - 600,  # buffer for vol
            end_epoch=close_epoch,
        )
        prices = []
        for pt in resp:
            ts = pt.get("timestamp_ms", pt.get("timestamp", 0))
            if ts > 1e12:
                ts /= 1000.0
            val = pt.get("value", pt.get("price", 0))
            if ts > 0 and val > 0:
                prices.append((float(ts), float(val)))
        prices.sort(key=lambda x: x[0])

        # Cache
        cache_dir.mkdir(parents=True, exist_ok=True)
        with open(cache_path, "w") as f:
            json.dump([{"ts": t, "price": p} for t, p in prices], f)

        return prices
    except Exception as e:
        logger.debug("Dome fetch failed for %s: %s", market_id, e)
        return []


# ---------------------------------------------------------------------------
# Dome cache bulk loader — reads all Binance prices from the DomeClient cache
# ---------------------------------------------------------------------------

def load_dome_cache_prices(
    cache_dir: pathlib.Path,
    source: str = "binance",
) -> list[tuple[float, float]]:
    """Load all Binance (or Chainlink) price points from DomeClient disk cache.

    The DomeClient stores responses as {digest}.json files.  Each file has the
    shape {"_saved_at": float, "data": {"prices": [{"symbol": …, "value": …,
    "timestamp": <ms>}, …]}}.

    Returns a deduplicated, sorted list of (epoch_sec, price) tuples.
    """
    if not cache_dir.exists():
        return []

    source_lower = source.lower()
    # Keywords that identify the desired price source in the symbol field
    if source_lower == "binance":
        symbol_keys = ("btcusdt",)
    else:  # chainlink
        symbol_keys = ("btc/usd",)

    seen_ts: dict[float, float] = {}  # ts_sec -> price (dedup by timestamp)
    n_files = 0

    for fpath in cache_dir.glob("*.json"):
        try:
            payload = json.loads(fpath.read_text())
            data = payload.get("data", {})
            if not isinstance(data, dict):
                continue
            prices_raw = data.get("prices", [])
            if not prices_raw:
                continue
            # Check symbol to confirm source
            sym = str(prices_raw[0].get("symbol", "")).lower()
            if not any(k in sym for k in symbol_keys):
                continue
            n_files += 1
            for pt in prices_raw:
                ts_ms = pt.get("timestamp", 0)
                val = pt.get("value", pt.get("price", 0))
                if ts_ms > 0 and val > 0:
                    ts_sec = ts_ms / 1000.0
                    # Keep the latest value for a given second
                    seen_ts[ts_sec] = float(val)
        except Exception:
            continue

    logger.info(
        "Dome cache: loaded %d %s price points from %d files in %s",
        len(seen_ts), source, n_files, cache_dir,
    )
    result = sorted(seen_ts.items())
    return result


# ---------------------------------------------------------------------------
# Per-market FV trajectory computation
# ---------------------------------------------------------------------------

def compute_market_fv_trajectory(
    stl: Settlement,
    prices: list[tuple[float, float]],
    vol_window_sec: int = 300,
    vol_fallback_annual: float = 0.50,
    vol_min_samples: int = 30,
    sample_times_pct: tuple = (0.0, 0.25, 0.5, 0.75, 1.0),
) -> dict:
    """Compute FV at several points during the market window.

    Returns a dict with:
        fv_at_open: float
        fv_at_midpoint: float
        fv_at_close: float (entry-weighted average)
        cert_at_open: float
        cert_at_close: float
        win_rate_prediction: bool (True if FV predicted correctly at open)
        outcome: "UP" or "DOWN"
        sample_points: list of {ts_pct, fv, cert}
    """
    open_ep = float(stl.open_epoch)
    close_ep = float(stl.close_epoch)
    window_dur = close_ep - open_ep

    # Price to beat = price at open (start price for this window)
    start_price = _get_price_at(prices, open_ep)
    if start_price is None:
        return {"error": "no_prices", "outcome": stl.outcome}

    # Vol estimation
    vol_annual = vol_fallback_annual
    if _POLYBOT_IMPORTED:
        try:
            vol_est = VolEstimator(
                min_samples=vol_min_samples,
                fallback_vol_annual=vol_fallback_annual,
            )
            pre_start = open_ep - vol_window_sec * 3
            for ts, price in prices:
                if pre_start <= ts <= open_ep:
                    vol_est.push(ts, price)
            if vol_est.is_ready:
                vol_annual = vol_est.vol_annualized(vol_window_sec)
        except Exception:
            pass
    else:
        # Manual vol from price series
        pre_prices = [p for ts, p in prices if open_ep - vol_window_sec <= ts <= open_ep and p > 0]
        if len(pre_prices) >= 2:
            log_rets = [math.log(pre_prices[i] / pre_prices[i-1]) for i in range(1, len(pre_prices))]
            if log_rets:
                var = sum(r**2 for r in log_rets) / len(log_rets)
                vol_annual = math.sqrt(max(var, 0)) * math.sqrt(365.25 * 24 * 3600)

    # Sample FV at each percentile of the window (legacy sample_times_pct + the
    # 7 ELAPSED_BINS needed for the 2D calibration table).
    # We compute all unique fractions in one pass, then split the results.
    all_fracs = sorted(set(list(sample_times_pct) + ELAPSED_BINS))
    actual_up = stl.outcome == "UP"

    all_sp: dict[float, dict] = {}
    for pct in all_fracs:
        ts = open_ep + pct * window_dur
        spot = _get_price_at(prices, ts)
        if spot is None:
            continue
        secs_remaining = close_ep - ts
        fv = _p_fair_up(start_price, spot, max(secs_remaining, 0.1), vol_annual)
        cert = _fv_cert(fv)
        all_sp[pct] = {
            "ts_pct": pct,
            "ts": ts,
            "spot": spot,
            "fv": round(fv, 4),
            "cert": round(cert, 4),
        }

    # Legacy sample_points list (only the originally requested fractions)
    sample_points = [all_sp[pct] for pct in sample_times_pct if pct in all_sp]

    if not sample_points:
        return {"error": "no_sample_points", "outcome": stl.outcome}

    fv_at_open = sample_points[0]["fv"] if sample_points else 0.5
    cert_at_open = sample_points[0]["cert"] if sample_points else 0.5
    fv_at_close = sample_points[-1]["fv"] if len(sample_points) > 1 else fv_at_open
    cert_at_close = sample_points[-1]["cert"] if len(sample_points) > 1 else cert_at_open
    fv_at_mid = sample_points[len(sample_points)//2]["fv"]

    predicted_up = fv_at_open >= 0.5
    prediction_correct = predicted_up == actual_up

    # Prediction correct at close (stronger signal — closer to resolution)
    predicted_up_at_close = fv_at_close >= 0.5
    prediction_correct_at_close = predicted_up_at_close == actual_up

    # Max certainty seen during the window (peak signal)
    max_cert = max((sp["cert"] for sp in sample_points), default=cert_at_open)
    fv_at_max_cert = max(sample_points, key=lambda sp: sp["cert"], default={"fv": fv_at_open})["fv"]
    predicted_up_at_max = fv_at_max_cert >= 0.5
    prediction_correct_at_max = predicted_up_at_max == actual_up

    # Build elapsed_predictions dict for 2D calibration table
    elapsed_predictions: dict[str, dict] = {}
    for frac in ELAPSED_BINS:
        pk = _pred_key(frac)
        if frac not in all_sp:
            continue
        sp = all_sp[frac]
        fv_at_t = sp["fv"]
        cert_at_t = sp["cert"]
        pred_up_at_t = fv_at_t >= 0.5
        correct_at_t = pred_up_at_t == actual_up
        elapsed_predictions[pk] = {
            "p_up": fv_at_t,
            "cert": cert_at_t,
            "correct": correct_at_t,
            "ts_pct": frac,
            "spot": sp["spot"],
        }

    return {
        "market_id": stl.market_id,
        "open_epoch": stl.open_epoch,
        "outcome": stl.outcome,
        "fv_at_open": fv_at_open,
        "cert_at_open": cert_at_open,
        "fv_at_midpoint": fv_at_mid,
        "fv_at_close": fv_at_close,
        "cert_at_close": cert_at_close,
        "max_cert": round(max_cert, 4),
        "vol_annual": round(vol_annual, 4),
        "start_price": start_price,
        "prediction_correct": prediction_correct,
        "prediction_correct_at_close": prediction_correct_at_close,
        "prediction_correct_at_max": prediction_correct_at_max,
        "sample_points": sample_points,
        "elapsed_predictions": elapsed_predictions,
        "data_source": "local",
    }


# ---------------------------------------------------------------------------
# Calibration table computation
# ---------------------------------------------------------------------------

BUCKET_EDGES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95, 1.01]
BUCKET_LABELS = [
    "0.50-0.55", "0.55-0.60", "0.60-0.65", "0.65-0.70",
    "0.70-0.75", "0.75-0.80", "0.80-0.85", "0.85-0.90",
    "0.90-0.95", "0.95-1.00",
]

# ---------------------------------------------------------------------------
# 2D Calibration constants (elapsed × confidence)
# ---------------------------------------------------------------------------

# Fractions of the window at which we sample FV
ELAPSED_BINS: list[float] = [0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.99]

# 5 coarse confidence buckets (10-point bands)
CONF_BUCKET_EDGES: list[float] = [0.50, 0.60, 0.70, 0.80, 0.90, 1.01]
CONF_BUCKET_LABELS: list[str] = [
    "0.50-0.60",
    "0.60-0.70",
    "0.70-0.80",
    "0.80-0.90",
    "0.90-1.00",
]

# Key: e.g. "5pct_elapsed", "10pct_elapsed", ...
def _elapsed_key(frac: float) -> str:
    return f"{int(round(frac * 100))}pct_elapsed"

# Map fraction -> elapsed key
ELAPSED_KEY_MAP: dict[float, str] = {f: _elapsed_key(f) for f in ELAPSED_BINS}

# Map elapsed key -> fraction
ELAPSED_FRAC_MAP: dict[str, float] = {v: k for k, v in ELAPSED_KEY_MAP.items()}

# "elapsed% label" as used in elapsed_predictions dict inside records
# e.g. 0.05 -> "5pct", 0.10 -> "10pct", etc.
def _pred_key(frac: float) -> str:
    return f"{int(round(frac * 100))}pct"


def cert_bucket_fine(cert: float) -> str:
    for i, edge in enumerate(BUCKET_EDGES[1:]):
        if cert < edge:
            return BUCKET_LABELS[i]
    return BUCKET_LABELS[-1]


def _conf_bucket(cert: float) -> str:
    """Return the coarse 5-bin confidence bucket label for `cert`."""
    for i, edge in enumerate(CONF_BUCKET_EDGES[1:]):
        if cert < edge:
            return CONF_BUCKET_LABELS[i]
    return CONF_BUCKET_LABELS[-1]


# ---------------------------------------------------------------------------
# Wilson CI helper
# ---------------------------------------------------------------------------

def wilson_ci(
    p: float,
    n: int,
    z: float = 1.96,
) -> tuple[float | None, float | None]:
    """Compute Wilson 95% confidence interval.

    Returns (lo, hi) floats, or (None, None) when n == 0.

    Reference: Wilson (1927) interval, implemented without scipy.
    """
    if n == 0:
        return None, None
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    lo = max(0.0, centre - half)
    hi = min(1.0, centre + half)
    return lo, hi


# ---------------------------------------------------------------------------
# 2D Calibration table: elapsed × confidence
# ---------------------------------------------------------------------------

def compute_fv_2d_table(
    market_results: list[dict],
    min_n_significant: int = 30,
    significance_ci_lo_threshold: float = 0.55,
) -> dict:
    """Build the 2D calibration table: elapsed-bin × confidence-bucket.

    Each cell:
        {"n": int, "wins": int, "win_rate": float|None,
         "ci_lo": float|None, "ci_hi": float|None, "significant": bool}

    `significant` = True when n >= min_n_significant AND CI lower bound
    (at 95%) exceeds significance_ci_lo_threshold.

    Records must contain an `elapsed_predictions` dict keyed by pred_key
    (e.g. "5pct", "10pct", ...) whose values are:
        {"p_up": float, "cert": float, "correct": bool}

    Records with an "error" key are skipped.
    """
    # Initialize empty table: {elapsed_key: {conf_label: {"n":0,"wins":0}}}
    table: dict[str, dict[str, dict]] = {}
    for frac in ELAPSED_BINS:
        ek = ELAPSED_KEY_MAP[frac]
        table[ek] = {
            label: {"n": 0, "wins": 0}
            for label in CONF_BUCKET_LABELS
        }

    for record in market_results:
        if record.get("error"):
            continue
        ep_dict = record.get("elapsed_predictions", {})
        for frac in ELAPSED_BINS:
            pk = _pred_key(frac)
            ek = ELAPSED_KEY_MAP[frac]
            if pk not in ep_dict:
                continue
            pred = ep_dict[pk]
            cert = pred.get("cert", 0.5)
            correct = bool(pred.get("correct", False))
            cb = _conf_bucket(cert)
            table[ek][cb]["n"] += 1
            if correct:
                table[ek][cb]["wins"] += 1

    # Compute win_rate, CI, significant
    for ek in table:
        for cb in table[ek]:
            cell = table[ek][cb]
            n = cell["n"]
            wins = cell["wins"]
            wr = wins / n if n > 0 else None
            if wr is not None:
                ci_lo, ci_hi = wilson_ci(wr, n)
            else:
                ci_lo = ci_hi = None
            sig = (
                n >= min_n_significant
                and ci_lo is not None
                and ci_lo > significance_ci_lo_threshold
            )
            cell["win_rate"] = round(wr, 4) if wr is not None else None
            cell["ci_lo"] = round(ci_lo, 4) if ci_lo is not None else None
            cell["ci_hi"] = round(ci_hi, 4) if ci_hi is not None else None
            cell["significant"] = sig

    return table


# ---------------------------------------------------------------------------
# Actionable threshold finder
# ---------------------------------------------------------------------------

def find_actionable_threshold(
    table: dict,
    cert_threshold: float = 0.80,
    win_rate_threshold: float = 0.65,
    min_n: int = 30,
) -> dict | None:
    """Find the earliest elapsed bin where FV is reliably predictive.

    Searches for the earliest elapsed_key (ordered by ELAPSED_BINS) where
    any confidence bucket >= cert_threshold has:
        win_rate > win_rate_threshold  AND  n >= min_n

    Returns a dict with the headline result, or None if no bucket qualifies.
    """
    # Build ordered list of elapsed keys
    ordered_elapsed = [ELAPSED_KEY_MAP[f] for f in ELAPSED_BINS]

    for ek in ordered_elapsed:
        frac = ELAPSED_FRAC_MAP[ek]
        bins_at_elapsed = table.get(ek, {})
        for label in CONF_BUCKET_LABELS:
            # Only look at confidence buckets >= cert_threshold
            # The bucket label "0.80-0.90" starts at 0.80
            try:
                bucket_lo = float(label.split("-")[0])
            except (ValueError, IndexError):
                continue
            if bucket_lo < cert_threshold:
                continue
            cell = bins_at_elapsed.get(label, {})
            n = cell.get("n", 0)
            wr = cell.get("win_rate")
            if wr is None:
                continue
            if n >= min_n and wr > win_rate_threshold:
                return {
                    "elapsed_pct": int(round(frac * 100)),
                    "certainty_threshold": bucket_lo,
                    "certainty_bucket": label,
                    "realized_win_rate": wr,
                    "n": n,
                    "ci_lo": cell.get("ci_lo"),
                    "ci_hi": cell.get("ci_hi"),
                    "significant": cell.get("significant", False),
                    "description": (
                        f"At {int(round(frac * 100))}% elapsed with cert>={bucket_lo:.2f}, "
                        f"realized win rate = {wr:.1%} (n={n})"
                    ),
                }
    return None


def compute_calibration_table(market_results: list[dict]) -> dict:
    """Compute calibration table from per-market FV results.

    Produces three views:
    1. By cert_at_open: FV signal at window start (entry signal for ladder posting)
    2. By cert_at_close: FV signal at window end (retrospective — shows max signal)
    3. By max_cert: Highest certainty seen during the window (upper bound)
    """
    def _make_buckets():
        fine = {label: [] for label in BUCKET_LABELS}
        coarse = {"0.50-0.60": [], "0.60-0.70": [], "0.70-0.80": [], "0.80-0.90": [], "0.90-1.00": []}
        return fine, coarse

    fine_open, coarse_open = _make_buckets()
    fine_close, coarse_close = _make_buckets()
    fine_max, coarse_max = _make_buckets()

    for r in market_results:
        if r.get("error"):
            continue

        def _add(fine_b, coarse_b, cert, correct):
            fb = cert_bucket_fine(cert)
            if fb in fine_b:
                fine_b[fb].append(correct)
            if cert < 0.60:
                coarse_b["0.50-0.60"].append(correct)
            elif cert < 0.70:
                coarse_b["0.60-0.70"].append(correct)
            elif cert < 0.80:
                coarse_b["0.70-0.80"].append(correct)
            elif cert < 0.90:
                coarse_b["0.80-0.90"].append(correct)
            else:
                coarse_b["0.90-1.00"].append(correct)

        _add(fine_open, coarse_open,
             r.get("cert_at_open", 0.5),
             r.get("prediction_correct", False))
        _add(fine_close, coarse_close,
             r.get("cert_at_close", 0.5),
             r.get("prediction_correct_at_close", r.get("prediction_correct", False)))
        _add(fine_max, coarse_max,
             r.get("max_cert", r.get("cert_at_open", 0.5)),
             r.get("prediction_correct_at_max", r.get("prediction_correct", False)))

    def _summarize(buckets: dict) -> dict:
        out = {}
        for label, outcomes in buckets.items():
            n = len(outcomes)
            win_rate = sum(outcomes) / n if n > 0 else None
            # Wilson confidence interval at 95%
            if n > 0 and win_rate is not None:
                z = 1.96
                p = win_rate
                ci_lo = (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)
                ci_hi = (p + z*z/(2*n) + z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)
            else:
                ci_lo = ci_hi = None
            out[label] = {
                "n": n,
                "wins": sum(outcomes) if outcomes else 0,
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "ci_lo_95": round(ci_lo, 4) if ci_lo is not None else None,
                "ci_hi_95": round(ci_hi, 4) if ci_hi is not None else None,
                "significant": (
                    ci_lo is not None and ci_lo > 0.5
                ) if ci_lo is not None else False,
            }
        return out

    return {
        "by_cert_at_open": {
            "fine_grained": _summarize(fine_open),
            "coarse": _summarize(coarse_open),
            "description": "FV signal at window open (entry signal for ladder posting)",
        },
        "by_cert_at_close": {
            "fine_grained": _summarize(fine_close),
            "coarse": _summarize(coarse_close),
            "description": "FV signal at window close (retrospective maximum signal)",
        },
        "by_max_cert": {
            "fine_grained": _summarize(fine_max),
            "coarse": _summarize(coarse_max),
            "description": "Peak FV certainty seen during the window",
        },
        # Legacy keys for compatibility
        "fine_grained": _summarize(fine_open),
        "coarse": _summarize(coarse_open),
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def run_fv_calibration(
    data_dir: pathlib.Path,
    start_date: str,
    end_date: str,
    output_path: pathlib.Path | None = None,
    dome_api_key: str | None = None,
    cache_dir: pathlib.Path | None = None,
) -> dict:
    """Run FV calibration over all settlements in the date range."""
    start_dt = datetime.date.fromisoformat(start_date)
    end_dt = datetime.date.fromisoformat(end_date)
    start_epoch = int(datetime.datetime(
        start_dt.year, start_dt.month, start_dt.day, tzinfo=datetime.timezone.utc
    ).timestamp())
    end_epoch = int(datetime.datetime(
        end_dt.year, end_dt.month, end_dt.day, tzinfo=datetime.timezone.utc
    ).timestamp()) + 86400

    # Load settlements
    settlement_log = data_dir / "settlement_log.jsonl"
    settlements: list[Settlement] = []
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
                if not (start_epoch <= ts < end_epoch):
                    continue
                market_id = obj.get("market_id", "")
                outcome = obj.get("outcome")
                tf = obj.get("timeframe_sec", 900)
                asset = obj.get("asset", "BTC")
                if not market_id or not outcome:
                    continue
                # Compute open/close epoch from market_id or tf + ts
                # market_id format: btc-updown-15m-{open_epoch}
                open_epoch = 0
                try:
                    open_epoch = int(market_id.split("-")[-1])
                except (ValueError, IndexError):
                    pass
                if open_epoch == 0:
                    # Fallback: settlement ts - timeframe_sec
                    open_epoch = int(ts) - tf
                close_epoch = open_epoch + tf

                settlements.append(Settlement(
                    market_id=market_id,
                    ts=ts,
                    outcome=outcome,
                    open_epoch=open_epoch,
                    close_epoch=close_epoch,
                    timeframe_sec=tf,
                    asset=asset,
                ))

    logger.info("Loaded %d settlements for calibration (%s to %s)",
        len(settlements), start_date, end_date)

    # Set up Dome client (optional)
    dome = None
    if _DOME_IMPORTED and dome_api_key:
        try:
            dome = DomeClient(
                api_key=dome_api_key,
                cache_dir=cache_dir or (data_dir / "dome_snapshots" / "_cache"),
            )
            logger.info("Dome client initialized")
        except Exception as e:
            logger.warning("Dome client init failed: %s", e)

    dome_cache = cache_dir or (data_dir / "dome_snapshots" / "_cache")

    # Load local prices (bulk load for efficiency)
    logger.info("Loading local price data...")
    local_prices = load_local_prices(
        data_dir, float(start_epoch) - 3600, float(end_epoch), "BTC", "binance"
    )
    logger.info("Loaded %d local price points", len(local_prices))

    # Also load prices from the DomeClient disk cache (covers pre-Apr-10 markets)
    logger.info("Loading Binance prices from Dome disk cache...")
    dome_bulk_prices = load_dome_cache_prices(dome_cache, source="binance")
    logger.info("Loaded %d Binance price points from Dome cache", len(dome_bulk_prices))

    # Merge local + dome bulk prices (deduplicate by rounding to nearest second)
    all_prices_dict: dict[float, float] = {}
    for ts, price in dome_bulk_prices:
        all_prices_dict[round(ts, 1)] = price
    for ts, price in local_prices:
        all_prices_dict[round(ts, 1)] = price  # local overrides dome
    all_prices = sorted(all_prices_dict.items())
    logger.info("Combined price dataset: %d unique points", len(all_prices))

    # Process each settlement
    market_results: list[dict] = []
    no_data_count = 0
    dome_fetch_count = 0
    local_data_count = 0

    for i, stl in enumerate(settlements):
        if (i + 1) % 50 == 0:
            logger.info("Processing settlement %d/%d ...", i + 1, len(settlements))

        # Get prices for this window from combined dataset
        window_prices = [
            (ts, price) for ts, price in all_prices
            if stl.open_epoch - 600 <= ts <= stl.close_epoch
        ]

        # Fallback to Dome API for markets still missing data
        if len(window_prices) < 5 and dome is not None:
            dome_prices = fetch_dome_prices_cached(
                dome, stl.market_id, stl.open_epoch, stl.close_epoch, dome_cache
            )
            if dome_prices:
                window_prices = dome_prices
                dome_fetch_count += 1

        if len(window_prices) < 2:
            no_data_count += 1
            market_results.append({
                "market_id": stl.market_id,
                "error": "no_prices",
                "outcome": stl.outcome,
            })
            continue

        local_data_count += 1
        result = compute_market_fv_trajectory(
            stl, window_prices,
            vol_window_sec=300,
            vol_fallback_annual=0.50,
            vol_min_samples=30,
        )
        market_results.append(result)

    logger.info(
        "Processed %d settlements: %d with data, %d no data (%d from Dome)",
        len(settlements), local_data_count, no_data_count, dome_fetch_count,
    )

    # Build calibration tables
    cal_table = compute_calibration_table(market_results)
    cal_2d = compute_fv_2d_table(market_results)
    actionable = find_actionable_threshold(cal_2d)

    # Summary stats
    valid_results = [r for r in market_results if not r.get("error")]
    n_valid = len(valid_results)
    overall_accuracy = (
        sum(1 for r in valid_results if r.get("prediction_correct", False)) / n_valid
        if n_valid > 0 else None
    )

    # Outcome distribution
    outcomes = [r.get("outcome") for r in market_results if r.get("outcome")]
    n_up = sum(1 for o in outcomes if o == "UP")
    n_dn = sum(1 for o in outcomes if o == "DOWN")

    # Per-certainty-bucket profitability signal
    high_cert_open = [r for r in valid_results if r.get("cert_at_open", 0) >= 0.80]
    high_cert_accuracy = (
        sum(1 for r in high_cert_open if r.get("prediction_correct", False)) / len(high_cert_open)
        if high_cert_open else None
    )
    high_cert_close = [r for r in valid_results if r.get("cert_at_close", 0) >= 0.80]
    high_cert_close_accuracy = (
        sum(1 for r in high_cert_close if r.get("prediction_correct_at_close", False)) / len(high_cert_close)
        if high_cert_close else None
    )
    overall_accuracy_at_close = (
        sum(1 for r in valid_results if r.get("prediction_correct_at_close", False)) / n_valid
        if n_valid > 0 else None
    )

    # Overall accuracy at 50% elapsed (the middle of the window)
    overall_accuracy_at_50pct = None
    n_50pct = sum(1 for r in valid_results if "50pct" in r.get("elapsed_predictions", {}))
    n_50pct_correct = sum(
        1 for r in valid_results
        if r.get("elapsed_predictions", {}).get("50pct", {}).get("correct", False)
    )
    if n_50pct > 0:
        overall_accuracy_at_50pct = round(n_50pct_correct / n_50pct, 4)

    # Per-bucket significance for per_bucket_significance output key
    per_bucket_significance: dict[str, dict] = {}
    for ek, bins in cal_2d.items():
        for label, cell in bins.items():
            if cell["n"] >= 30:
                key = f"{ek}_{label}"
                per_bucket_significance[key] = {
                    "n": cell["n"],
                    "win_rate": cell["win_rate"],
                    "ci_lower": cell["ci_lo"],
                    "ci_upper": cell["ci_hi"],
                    "significant": cell["significant"],
                }

    output = {
        "sample_size": len(settlements),
        "settlements_with_data": n_valid,
        "overall_accuracy_at_50pct": overall_accuracy_at_50pct,
        "calibration_by_time_and_confidence": cal_2d,
        "per_bucket_significance": per_bucket_significance,
        "actionable_threshold": actionable,
        "metadata": {
            "start_date": start_date,
            "end_date": end_date,
            "total_settlements": len(settlements),
            "valid_results": n_valid,
            "no_data_count": no_data_count,
            "dome_fetch_count": dome_fetch_count,
            "local_data_count": local_data_count,
            "outcome_distribution": {"UP": n_up, "DOWN": n_dn},
        },
        "overall_accuracy_at_open": round(overall_accuracy, 4) if overall_accuracy is not None else None,
        "overall_accuracy_at_close": round(overall_accuracy_at_close, 4) if overall_accuracy_at_close is not None else None,
        "high_cert_accuracy_at_open": round(high_cert_accuracy, 4) if high_cert_accuracy is not None else None,
        "high_cert_accuracy_at_close": round(high_cert_close_accuracy, 4) if high_cert_close_accuracy is not None else None,
        # Legacy keys
        "overall_accuracy": round(overall_accuracy, 4) if overall_accuracy is not None else None,
        "high_cert_accuracy": round(high_cert_accuracy, 4) if high_cert_accuracy is not None else None,
        "calibration_table": cal_table,
        "per_market": market_results,
    }

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Write summary without per_market details (too verbose)
        summary = {k: v for k, v in output.items() if k != "per_market"}
        with open(output_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info("Results written to %s", output_path)

        # Also write per-market data to separate file
        per_market_path = output_path.with_suffix("") / "per_market.jsonl"
        per_market_path.parent.mkdir(parents=True, exist_ok=True)
        with open(per_market_path, "w") as f:
            for r in market_results:
                f.write(json.dumps(r) + "\n")

    return output


def _print_2d_table(cal_2d: dict) -> None:
    """Pretty-print the 2D calibration table to stdout."""
    col_labels = CONF_BUCKET_LABELS
    elapsed_keys = [ELAPSED_KEY_MAP[f] for f in ELAPSED_BINS]

    # Header
    hdr = f"  {'Elapsed':14s}"
    for label in col_labels:
        hdr += f"  {label:>20s}"
    print(hdr)
    print("  " + "-" * 14 + ("  " + "-" * 20) * len(col_labels))

    for ek in elapsed_keys:
        bins = cal_2d.get(ek, {})
        row = f"  {ek:14s}"
        for label in col_labels:
            cell = bins.get(label, {})
            n = cell.get("n", 0)
            wr = cell.get("win_rate")
            sig = "*" if cell.get("significant") else " "
            if wr is not None:
                cell_str = f"{wr:.1%}/{n}n{sig}"
            else:
                cell_str = f"-/{n}n "
            row += f"  {cell_str:>20s}"
        print(row)
    print()
    print("  Format: win_rate/N (* = significant, CI_lo > 0.55, n >= 30)")


def _print_legacy_table(table_data: dict, title: str, ci_key_lo: str = "ci_lo_95") -> None:
    print(f"\n{title}")
    print(f"  {'Bucket':12s}  {'N':>5}  {'Win%':>7}  {'CI 95%':>20}  {'Signal':>8}")
    print(f"  {'-'*12}  {'-'*5}  {'-'*7}  {'-'*20}  {'-'*8}")
    for label, vals in table_data.items():
        n = vals["n"]
        wr = vals.get("win_rate")
        cilo = vals.get(ci_key_lo) or vals.get("ci_lo")
        cihi = vals.get("ci_hi_95") or vals.get("ci_hi")
        sig = "YES ***" if vals.get("significant") else ""
        wr_str = f"{wr:.1%}" if wr is not None else "N/A"
        ci_str = f"[{cilo:.1%} - {cihi:.1%}]" if cilo is not None else ""
        print(f"  {label:12s}  {n:5d}  {wr_str:>7s}  {ci_str:>20s}  {sig:>8s}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="FV brain calibration over settlement history"
    )
    parser.add_argument("--start", default="2026-04-02",
        help="Start date (YYYY-MM-DD), inclusive")
    parser.add_argument("--end", default="2026-04-11",
        help="End date (YYYY-MM-DD), inclusive")
    parser.add_argument("--data-dir", default="data",
        help="Data directory with price_log, settlement_log files")
    parser.add_argument("--out", default="results/fv_calibration.json",
        help="Output JSON file")
    parser.add_argument("--dome-key", default=None,
        help="Dome API key for fetching older price data")
    parser.add_argument("--cache-dir", default=None,
        help="Cache directory for Dome responses")
    parser.add_argument("--print", dest="do_print", action="store_true",
        help="Pretty-print the 2D calibration table to stdout")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir)
    output_path = pathlib.Path(args.out)
    cache_dir = pathlib.Path(args.cache_dir) if args.cache_dir else None
    dome_api_key = args.dome_key or os.environ.get("DOME_API_KEY")

    result = run_fv_calibration(
        data_dir=data_dir,
        start_date=args.start,
        end_date=args.end,
        output_path=output_path,
        dome_api_key=dome_api_key,
        cache_dir=cache_dir,
    )

    print(f"\n{'='*80}")
    print("FV CALIBRATION RESULTS")
    print(f"{'='*80}")
    meta = result.get("metadata", {})
    print(f"Date range        : {args.start} to {args.end}")
    print(f"Total settlements : {meta.get('total_settlements', 0)}")
    print(f"Valid (with data) : {meta.get('valid_results', 0)}")
    print(f"No price data     : {meta.get('no_data_count', 0)}")
    dist = meta.get("outcome_distribution", {})
    print(f"Outcomes          : UP={dist.get('UP', 0)}  DOWN={dist.get('DOWN', 0)}")
    oa_open = result.get("overall_accuracy_at_open") or result.get("overall_accuracy")
    oa_close = result.get("overall_accuracy_at_close")
    oa_50 = result.get("overall_accuracy_at_50pct")
    hca_open = result.get("high_cert_accuracy_at_open") or result.get("high_cert_accuracy")
    hca_close = result.get("high_cert_accuracy_at_close")
    print(f"FV accuracy (at open)     : {oa_open:.1%}" if oa_open is not None else "FV accuracy (at open)     : N/A")
    print(f"FV accuracy (at 50% elap) : {oa_50:.1%}" if oa_50 is not None else "FV accuracy (at 50% elap) : N/A")
    print(f"FV accuracy (at close)    : {oa_close:.1%}" if oa_close is not None else "FV accuracy (at close)    : N/A")
    print(f"High-cert acc (open >=0.8): {hca_open:.1%}" if hca_open is not None else "High-cert acc (open >=0.8): N/A")
    print(f"High-cert acc (close>=0.8): {hca_close:.1%}" if hca_close is not None else "High-cert acc (close>=0.8): N/A")

    # Actionable threshold headline
    at = result.get("actionable_threshold")
    print(f"\n{'='*80}")
    print("ACTIONABLE THRESHOLD")
    print(f"{'='*80}")
    if at is not None:
        print(f"  {at['description']}")
        print(f"  n={at['n']}, CI_lo={at['ci_lo']}, significant={at['significant']}")
    else:
        print("  NO ACTIONABLE FV SIGNAL FOUND")
        print("  (No elapsed×confidence bucket has win_rate>0.65 with n>=30)")

    if args.do_print:
        # 2D calibration table
        cal_2d = result.get("calibration_by_time_and_confidence", {})
        print(f"\n{'='*80}")
        print("2D CALIBRATION TABLE  (elapsed% × certainty bucket)")
        print("Columns = confidence bucket, Rows = elapsed fraction of 15m window")
        print(f"{'='*80}")
        _print_2d_table(cal_2d)

        # Legacy tables
        cal = result.get("calibration_table", {})
        _print_legacy_table(cal.get("by_cert_at_close", {}).get("coarse", {}),
            "Calibration by FV at CLOSE (coarse, 5 bins) — retrospective:")
        _print_legacy_table(cal.get("by_cert_at_open", {}).get("coarse", {}),
            "Calibration by FV at OPEN (coarse, 5 bins) — entry signal:")
    print()


import os  # needed for dome_api_key env var lookup

if __name__ == "__main__":
    main()
