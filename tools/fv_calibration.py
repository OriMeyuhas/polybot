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

    # Sample FV at each percentile of the window
    sample_points = []
    for pct in sample_times_pct:
        ts = open_ep + pct * window_dur
        spot = _get_price_at(prices, ts)
        if spot is None:
            continue
        secs_remaining = close_ep - ts
        fv = _p_fair_up(start_price, spot, max(secs_remaining, 0.1), vol_annual)
        cert = _fv_cert(fv)
        sample_points.append({
            "ts_pct": pct,
            "ts": ts,
            "spot": spot,
            "fv": round(fv, 4),
            "cert": round(cert, 4),
        })

    if not sample_points:
        return {"error": "no_sample_points", "outcome": stl.outcome}

    fv_at_open = sample_points[0]["fv"] if sample_points else 0.5
    cert_at_open = sample_points[0]["cert"] if sample_points else 0.5
    fv_at_close = sample_points[-1]["fv"] if len(sample_points) > 1 else fv_at_open
    cert_at_close = sample_points[-1]["cert"] if len(sample_points) > 1 else cert_at_open
    fv_at_mid = sample_points[len(sample_points)//2]["fv"]

    predicted_up = fv_at_open >= 0.5
    actual_up = stl.outcome == "UP"
    prediction_correct = predicted_up == actual_up

    # Prediction correct at close (stronger signal — closer to resolution)
    predicted_up_at_close = fv_at_close >= 0.5
    actual_up = stl.outcome == "UP"
    prediction_correct_at_close = predicted_up_at_close == actual_up

    # Max certainty seen during the window (peak signal)
    max_cert = max((sp["cert"] for sp in sample_points), default=cert_at_open)
    fv_at_max_cert = max(sample_points, key=lambda sp: sp["cert"], default={"fv": fv_at_open})["fv"]
    predicted_up_at_max = fv_at_max_cert >= 0.5
    prediction_correct_at_max = predicted_up_at_max == actual_up

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


def cert_bucket_fine(cert: float) -> str:
    for i, edge in enumerate(BUCKET_EDGES[1:]):
        if cert < edge:
            return BUCKET_LABELS[i]
    return BUCKET_LABELS[-1]


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

    # Process each settlement
    market_results: list[dict] = []
    no_data_count = 0
    dome_fetch_count = 0
    local_data_count = 0

    for i, stl in enumerate(settlements):
        if (i + 1) % 50 == 0:
            logger.info("Processing settlement %d/%d ...", i + 1, len(settlements))

        # Get prices for this window
        # First try local data
        window_prices = [
            (ts, price) for ts, price in local_prices
            if stl.open_epoch - 600 <= ts <= stl.close_epoch
        ]

        # Fallback to Dome for older markets
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

    # Build calibration table
    cal_table = compute_calibration_table(market_results)

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
    # Does FV predict correctly at > 50% rate in high-certainty buckets?
    high_cert_open = [r for r in valid_results if r.get("cert_at_open", 0) >= 0.80]
    high_cert_accuracy = (
        sum(1 for r in high_cert_open if r.get("prediction_correct", False)) / len(high_cert_open)
        if high_cert_open else None
    )
    # Also compute accuracy at close with high certainty (stronger signal)
    high_cert_close = [r for r in valid_results if r.get("cert_at_close", 0) >= 0.80]
    high_cert_close_accuracy = (
        sum(1 for r in high_cert_close if r.get("prediction_correct_at_close", False)) / len(high_cert_close)
        if high_cert_close else None
    )
    overall_accuracy_at_close = (
        sum(1 for r in valid_results if r.get("prediction_correct_at_close", False)) / n_valid
        if n_valid > 0 else None
    )

    output = {
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
        # Legacy key
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

    def _print_table(table_data: dict, title: str) -> None:
        print(f"\n{title}")
        print(f"  {'Bucket':12s}  {'N':>5}  {'Win%':>7}  {'CI 95%':>20}  {'Signal':>8}")
        print(f"  {'-'*12}  {'-'*5}  {'-'*7}  {'-'*20}  {'-'*8}")
        for label, vals in table_data.items():
            n = vals["n"]
            wr = vals.get("win_rate")
            cilo = vals.get("ci_lo_95")
            cihi = vals.get("ci_hi_95")
            sig = "YES ***" if vals.get("significant") else ""
            wr_str = f"{wr:.1%}" if wr is not None else "N/A"
            ci_str = f"[{cilo:.1%} - {cihi:.1%}]" if cilo is not None else ""
            print(f"  {label:12s}  {n:5d}  {wr_str:>7s}  {ci_str:>20s}  {sig:>8s}")

    print(f"\n{'='*65}")
    print("FV CALIBRATION RESULTS")
    print(f"{'='*65}")
    meta = result.get("metadata", {})
    print(f"Date range        : {args.start} to {args.end}")
    print(f"Total settlements : {meta.get('total_settlements', 0)}")
    print(f"Valid (with data) : {meta.get('valid_results', 0)}")
    print(f"No price data     : {meta.get('no_data_count', 0)} (Apr 2-9 before local price_log)")
    dist = meta.get("outcome_distribution", {})
    print(f"Outcomes          : UP={dist.get('UP', 0)}  DOWN={dist.get('DOWN', 0)}")
    oa_open = result.get("overall_accuracy_at_open") or result.get("overall_accuracy")
    oa_close = result.get("overall_accuracy_at_close")
    hca_open = result.get("high_cert_accuracy_at_open") or result.get("high_cert_accuracy")
    hca_close = result.get("high_cert_accuracy_at_close")
    print(f"FV accuracy (at open)    : {oa_open:.1%}" if oa_open is not None else "FV accuracy (at open)    : N/A")
    print(f"FV accuracy (at close)   : {oa_close:.1%}" if oa_close is not None else "FV accuracy (at close)   : N/A")
    print(f"High-cert accuracy (open): {hca_open:.1%}" if hca_open is not None else "High-cert accuracy (open): N/A")
    print(f"High-cert acc (close>=0.8): {hca_close:.1%}" if hca_close is not None else "High-cert acc (close>=0.8): N/A")

    cal = result.get("calibration_table", {})
    _print_table(cal.get("by_cert_at_close", {}).get("fine_grained", {}),
        "Calibration by FV at CLOSE (fine, 10 bins) — retrospective signal:")
    _print_table(cal.get("by_cert_at_close", {}).get("coarse", {}),
        "Calibration by FV at CLOSE (coarse, 5 bins):")
    _print_table(cal.get("by_cert_at_open", {}).get("coarse", {}),
        "Calibration by FV at OPEN (coarse, 5 bins) — entry signal:")
    print()


import os  # needed for dome_api_key env var lookup

if __name__ == "__main__":
    main()
