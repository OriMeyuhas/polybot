"""Cycle 22 certainty-threshold re-sweep on the full Dome corpus.

Re-runs the book_mid_gate certainty-threshold sweep over the full Dome
snapshot corpus with improved statistics (per-market outputs, CI bootstrap,
regime quartiles, max drawdown).

Plan-only: does not modify any strategy code or .env.

Run:
    python tools/certainty_resweep.py

Outputs:
    results/sweep/certainty_resweep_per_market.csv
    results/sweep/certainty_resweep_summary.json
    stdout summary
"""
from __future__ import annotations

import csv
import datetime
import json
import pathlib
import random
import sys
from statistics import mean

_PROJECT_ROOT = pathlib.Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools.backtester import (  # noqa: E402
    BacktestConfig,
    DomeMarketData,
    load_dome_snapshot,
    simulate_market_dome,
    MarketResult,
)
from tools.book_mid_gate_sweep import (  # noqa: E402
    apply_book_mid_gate,
    simulate_market_with_book_gate,
)


THRESHOLDS = [0.45, 0.50, 0.55, 0.60]
MAX_SPREAD = 0.05
BASELINE_THRESHOLD = 0.55
BOOTSTRAP_ITERS = 1000
BOOTSTRAP_SEED = 42


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------

def load_corpus(dome_dir: pathlib.Path) -> list[DomeMarketData]:
    files = sorted(dome_dir.glob("btc-updown-15m-*.jsonl"))
    loaded: list[DomeMarketData] = []
    for p in files:
        d = load_dome_snapshot(p)
        if d is None:
            continue
        if not d.has_orderbook:
            continue
        if d.outcome is None:
            continue
        loaded.append(d)
    return loaded


def _epoch_to_date(ep: int) -> str:
    return datetime.datetime.fromtimestamp(
        ep, datetime.timezone.utc
    ).strftime("%Y-%m-%d")


def abs_outcome_return(d: DomeMarketData) -> float | None:
    if d.ptb is None or d.binance_at_close is None:
        return None
    if d.ptb <= 0:
        return None
    return abs(d.binance_at_close - d.ptb) / d.ptb


# ---------------------------------------------------------------------------
# Per-threshold sweep producing per-market records
# ---------------------------------------------------------------------------

def sweep_threshold(
    corpus: list[DomeMarketData],
    cfg: BacktestConfig,
    threshold: float,
    max_spread: float,
) -> list[dict]:
    """Run the sweep at one threshold, returning one record per market."""
    records: list[dict] = []
    for d in corpus:
        result, fired, _side = simulate_market_with_book_gate(
            d, cfg, True, threshold, max_spread
        )
        records.append({
            "threshold": threshold,
            "market_id": d.market_slug,
            "window_start": int(d.window_start),
            "fired": bool(fired),
            "pnl": float(result.pnl),
            "outcome_correct": (
                None if result.outcome_correct is None
                else bool(result.outcome_correct)
            ),
            "paired": bool(result.paired),
            "outcome": result.outcome,
            "abs_outcome_return": abs_outcome_return(d),
        })
    return records


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def max_drawdown_from_fired(records: list[dict]) -> float:
    """Max drawdown over cumsum of fired-market PnLs ordered by window_start.

    Reported as a negative number (worst peak-to-trough). 0 if no fires.
    """
    fired = [r for r in records if r["fired"]]
    if not fired:
        return 0.0
    fired_sorted = sorted(fired, key=lambda r: r["window_start"])
    cum = 0.0
    peak = 0.0
    worst_dd = 0.0
    for r in fired_sorted:
        cum += r["pnl"]
        if cum > peak:
            peak = cum
        dd = cum - peak  # non-positive
        if dd < worst_dd:
            worst_dd = dd
    return worst_dd


def compute_threshold_metrics(records: list[dict]) -> dict:
    markets_total = len(records)
    fired = [r for r in records if r["fired"]]
    markets_fired = len(fired)
    fire_rate = markets_fired / markets_total if markets_total else 0.0

    fired_pnl = sum(r["pnl"] for r in fired)
    fired_pnl_per_market = (
        fired_pnl / markets_fired if markets_fired else None
    )
    correct_count = sum(
        1 for r in fired if r["outcome_correct"] is True
    )
    fired_correct_rate = (
        correct_count / markets_fired if markets_fired else 0.0
    )
    max_dd = max_drawdown_from_fired(records)

    return {
        "markets_total": markets_total,
        "markets_fired": markets_fired,
        "fire_rate": fire_rate,
        "fired_pnl": fired_pnl,
        "fired_pnl_per_market": fired_pnl_per_market,
        "fired_correct_rate": fired_correct_rate,
        "fired_correct_count": correct_count,
        "max_drawdown": max_dd,
    }


# ---------------------------------------------------------------------------
# Bootstrap delta vs baseline
# ---------------------------------------------------------------------------

def bootstrap_delta_mean(
    sample_t: list[float],
    sample_base: list[float],
    iters: int,
    rng: random.Random,
) -> tuple[float | None, float | None, float | None]:
    """Difference-of-means bootstrap: mean(t) - mean(base).

    Returns (mean_diff_point, ci_lo, ci_hi) or (None, None, None) if
    either sample is empty.
    """
    if not sample_t or not sample_base:
        return (None, None, None)
    point = mean(sample_t) - mean(sample_base)
    diffs: list[float] = []
    nt = len(sample_t)
    nb = len(sample_base)
    for _ in range(iters):
        rt = [sample_t[rng.randrange(nt)] for _ in range(nt)]
        rb = [sample_base[rng.randrange(nb)] for _ in range(nb)]
        diffs.append(mean(rt) - mean(rb))
    diffs.sort()
    lo_idx = int(0.025 * iters)
    hi_idx = int(0.975 * iters) - 1
    if hi_idx < 0:
        hi_idx = 0
    ci_lo = diffs[lo_idx]
    ci_hi = diffs[hi_idx]
    return (point, ci_lo, ci_hi)


# ---------------------------------------------------------------------------
# Regime quartiles on abs_outcome_return
# ---------------------------------------------------------------------------

def compute_quartile_cutoffs(values: list[float]) -> tuple[float, float, float]:
    """Return (q25, q50, q75) cutoffs. values must be non-empty."""
    xs = sorted(values)
    n = len(xs)

    def _pct(p: float) -> float:
        if n == 1:
            return xs[0]
        # Linear-interpolation percentile (numpy-default-like).
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return xs[lo] * (1 - frac) + xs[hi] * frac

    return (_pct(0.25), _pct(0.50), _pct(0.75))


def quartile_index(
    val: float | None, cuts: tuple[float, float, float]
) -> int | None:
    """Return 1..4 given val (None -> None). Q1 = smallest."""
    if val is None:
        return None
    q25, q50, q75 = cuts
    if val <= q25:
        return 1
    if val <= q50:
        return 2
    if val <= q75:
        return 3
    return 4


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    dome_dir = _PROJECT_ROOT / "data" / "dome_snapshots"
    print(f"Loading corpus from {dome_dir}...")
    corpus = load_corpus(dome_dir)
    n = len(corpus)
    print(f"Loaded {n} valid markets (has_orderbook=True, outcome present)")

    # Date coverage
    dates: dict[str, int] = {}
    for d in corpus:
        day = _epoch_to_date(int(d.window_start))
        dates[day] = dates.get(day, 0) + 1
    dates_sorted = sorted(dates.keys())
    if dates_sorted:
        print(
            f"Date coverage: {dates_sorted[0]} .. {dates_sorted[-1]} "
            f"({len(dates_sorted)} days)"
        )

    # Base config matching book_mid_gate_sweep main()
    base_cfg = BacktestConfig(
        bankroll=500.0,
        position_size_fraction=0.1,
        directional_budget_cap=18.0,
        rungs=10,
        spacing=0.01,
        width=0.10,
        size_skew=2.0,
        max_pair_cost=0.98,
        one_sided_abort_enabled=True,
        one_sided_abort_cost_pct=0.01,
        one_sided_abort_ratio=3.0,
        fv_gate_enabled=False,
    )

    # -----------------------------------------------------------------
    # Run sweep per threshold
    # -----------------------------------------------------------------
    all_records: list[dict] = []
    per_threshold_records: dict[float, list[dict]] = {}
    per_threshold_metrics: dict[float, dict] = {}

    for t in THRESHOLDS:
        print(f"\nSweeping threshold {t:.2f}...")
        recs = sweep_threshold(corpus, base_cfg, t, MAX_SPREAD)
        per_threshold_records[t] = recs
        all_records.extend(recs)
        metrics = compute_threshold_metrics(recs)
        per_threshold_metrics[t] = metrics
        ppm = metrics["fired_pnl_per_market"]
        ppm_s = f"${ppm:+.4f}" if ppm is not None else "N/A"
        print(
            f"  t={t:.2f}: fires={metrics['markets_fired']} "
            f"({metrics['fire_rate']:.2%})  "
            f"$/mkt(fired)={ppm_s}  "
            f"correct_rate={metrics['fired_correct_rate']:.4f}  "
            f"max_dd=${metrics['max_drawdown']:+.2f}"
        )

    # -----------------------------------------------------------------
    # Bootstrap delta vs baseline (0.55)
    # -----------------------------------------------------------------
    rng = random.Random(BOOTSTRAP_SEED)

    base_fired = [
        r["pnl"] for r in per_threshold_records[BASELINE_THRESHOLD]
        if r["fired"]
    ]
    bootstrap_deltas: dict[float, dict] = {}
    for t in THRESHOLDS:
        if t == BASELINE_THRESHOLD:
            bootstrap_deltas[t] = {
                "delta_point": 0.0,
                "ci_lo": 0.0,
                "ci_hi": 0.0,
                "excludes_zero": False,
                "n_t": len(base_fired),
                "n_base": len(base_fired),
            }
            continue
        sample_t = [
            r["pnl"] for r in per_threshold_records[t] if r["fired"]
        ]
        pt, lo, hi = bootstrap_delta_mean(
            sample_t, base_fired, BOOTSTRAP_ITERS, rng
        )
        excludes = None
        if lo is not None and hi is not None:
            excludes = (lo > 0.0) or (hi < 0.0)
        bootstrap_deltas[t] = {
            "delta_point": pt,
            "ci_lo": lo,
            "ci_hi": hi,
            "excludes_zero": excludes,
            "n_t": len(sample_t),
            "n_base": len(base_fired),
        }

    # -----------------------------------------------------------------
    # Regime quartiles
    # -----------------------------------------------------------------
    all_returns = [
        abs_outcome_return(d) for d in corpus
        if abs_outcome_return(d) is not None
    ]
    quartile_cuts = compute_quartile_cutoffs(all_returns) if all_returns else None
    quartile_breakdown: dict[float, dict[int, dict]] = {}
    if quartile_cuts is not None:
        for t in THRESHOLDS:
            per_q: dict[int, dict] = {
                1: {"fired_n": 0, "fired_pnl": 0.0, "total_n": 0},
                2: {"fired_n": 0, "fired_pnl": 0.0, "total_n": 0},
                3: {"fired_n": 0, "fired_pnl": 0.0, "total_n": 0},
                4: {"fired_n": 0, "fired_pnl": 0.0, "total_n": 0},
            }
            for r in per_threshold_records[t]:
                q = quartile_index(r["abs_outcome_return"], quartile_cuts)
                if q is None:
                    continue
                per_q[q]["total_n"] += 1
                if r["fired"]:
                    per_q[q]["fired_n"] += 1
                    per_q[q]["fired_pnl"] += r["pnl"]
            for q in per_q:
                fn = per_q[q]["fired_n"]
                per_q[q]["pnl_per_fired"] = (
                    per_q[q]["fired_pnl"] / fn if fn else None
                )
            quartile_breakdown[t] = per_q

    # -----------------------------------------------------------------
    # Write outputs
    # -----------------------------------------------------------------
    out_dir = _PROJECT_ROOT / "results" / "sweep"
    out_dir.mkdir(parents=True, exist_ok=True)

    csv_path = out_dir / "certainty_resweep_per_market.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "threshold", "market_id", "window_start", "fired",
            "pnl", "outcome_correct", "paired", "outcome",
            "abs_outcome_return",
        ])
        for r in all_records:
            writer.writerow([
                f"{r['threshold']:.2f}",
                r["market_id"],
                r["window_start"],
                int(r["fired"]),
                f"{r['pnl']:.6f}",
                "" if r["outcome_correct"] is None else int(r["outcome_correct"]),
                int(r["paired"]),
                r["outcome"] if r["outcome"] is not None else "",
                (
                    "" if r["abs_outcome_return"] is None
                    else f"{r['abs_outcome_return']:.8f}"
                ),
            ])
    print(f"\nWrote per-market CSV: {csv_path}")

    summary = {
        "corpus": {
            "markets_total": n,
            "date_start": dates_sorted[0] if dates_sorted else None,
            "date_end": dates_sorted[-1] if dates_sorted else None,
            "days_covered": len(dates_sorted),
        },
        "config": {
            "thresholds": THRESHOLDS,
            "max_spread": MAX_SPREAD,
            "baseline_threshold": BASELINE_THRESHOLD,
            "bootstrap_iters": BOOTSTRAP_ITERS,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "base_cfg": {
                "bankroll": 500.0,
                "position_size_fraction": 0.1,
                "directional_budget_cap": 18.0,
                "rungs": 10,
                "spacing": 0.01,
                "width": 0.10,
                "size_skew": 2.0,
                "max_pair_cost": 0.98,
                "one_sided_abort_enabled": True,
                "one_sided_abort_cost_pct": 0.01,
                "one_sided_abort_ratio": 3.0,
                "fv_gate_enabled": False,
            },
        },
        "per_threshold": {
            f"{t:.2f}": per_threshold_metrics[t] for t in THRESHOLDS
        },
        "bootstrap_vs_baseline": {
            f"{t:.2f}": bootstrap_deltas[t] for t in THRESHOLDS
        },
        "quartile_cutoffs": (
            {
                "q25": quartile_cuts[0],
                "q50": quartile_cuts[1],
                "q75": quartile_cuts[2],
                "n_with_data": len(all_returns),
            }
            if quartile_cuts is not None else None
        ),
        "quartile_breakdown": {
            f"{t:.2f}": {
                f"Q{q}": quartile_breakdown[t][q] for q in (1, 2, 3, 4)
            }
            for t in THRESHOLDS
        } if quartile_cuts is not None else None,
    }

    json_path = out_dir / "certainty_resweep_summary.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    print(f"Wrote summary JSON: {json_path}")

    # -----------------------------------------------------------------
    # Stdout summary
    # -----------------------------------------------------------------
    print("\n" + "=" * 78)
    print("SUMMARY: certainty-threshold re-sweep on full Dome corpus")
    print("=" * 78)
    print(
        f"Corpus: {n} markets, "
        f"{dates_sorted[0] if dates_sorted else '?'} .. "
        f"{dates_sorted[-1] if dates_sorted else '?'}"
    )
    print()
    print(
        f"{'thr':>5} {'total':>7} {'fired':>6} {'fire_rt':>8} "
        f"{'$/mkt_fired':>13} {'correct_rt':>11} {'max_dd':>10} "
        f"{'delta$/mkt':>11} {'95%CI':>22}"
    )
    for t in THRESHOLDS:
        m = per_threshold_metrics[t]
        b = bootstrap_deltas[t]
        ppm = m["fired_pnl_per_market"]
        ppm_s = f"{ppm:+.4f}" if ppm is not None else "N/A"
        if t == BASELINE_THRESHOLD:
            delta_s = "baseline"
            ci_s = "(baseline)"
        else:
            delta_s = (
                f"{b['delta_point']:+.4f}" if b["delta_point"] is not None
                else "N/A"
            )
            ci_s = (
                f"({b['ci_lo']:+.4f}, {b['ci_hi']:+.4f})"
                if b["ci_lo"] is not None else "N/A"
            )
        print(
            f"{t:>5.2f} {m['markets_total']:>7d} {m['markets_fired']:>6d} "
            f"{m['fire_rate']:>7.2%} {ppm_s:>13} "
            f"{m['fired_correct_rate']:>11.4f} "
            f"{m['max_drawdown']:>+10.2f} {delta_s:>11} {ci_s:>22}"
        )

    if quartile_cuts is not None:
        print()
        print(
            f"Quartile cutoffs on abs_outcome_return "
            f"(n={len(all_returns)} with data):"
        )
        print(
            f"  q25={quartile_cuts[0]:.6f}  "
            f"q50={quartile_cuts[1]:.6f}  "
            f"q75={quartile_cuts[2]:.6f}"
        )
        print()
        print(
            f"{'thr':>5} | "
            f"{'Q1 (rng)':>16} {'Q2':>16} {'Q3':>16} {'Q4 (trend)':>16}"
        )
        for t in THRESHOLDS:
            row = f"{t:>5.2f} | "
            for q in (1, 2, 3, 4):
                d = quartile_breakdown[t][q]
                ppm = d["pnl_per_fired"]
                ppm_s = f"{ppm:+.4f}" if ppm is not None else "N/A"
                row += f"{d['fired_n']:>4}/{ppm_s:>10} "
            print(row)


if __name__ == "__main__":
    main()
