"""Book-mid gate threshold sweep on 14 days of Dome data.

Validates the production BOOK_MID_GATE_* settings by running a train/holdout
split over dome_snapshots/. Applies the gate BEFORE the existing backtester's
FV gate, mirroring ladder_manager.py:987-1029.

Outputs threshold sweep results to stdout (no file writes except optional).

Run:
    python tools/book_mid_gate_sweep.py
"""
from __future__ import annotations

import datetime
import json
import pathlib
import sys
from dataclasses import replace

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


# ---------------------------------------------------------------------------
# Book-mid gate wrapper
# ---------------------------------------------------------------------------

def apply_book_mid_gate(
    dome: DomeMarketData,
    certainty_threshold: float,
    max_spread: float,
) -> tuple[bool, str | None, float, float]:
    """Mirror of ladder_manager.py:987-1029. Returns:
       (fired, winning_side, book_mid_up_normalized, cert).

    If fired, caller should force directional-only posting on `winning_side`.
    If not fired, gate falls through silently.
    """
    up_bid = dome.up_best_bid
    up_ask = dome.up_best_ask
    dn_bid = dome.dn_best_bid
    dn_ask = dome.dn_best_ask
    up_mid = (up_bid + up_ask) / 2.0
    dn_mid = (dn_bid + dn_ask) / 2.0

    # Gate guards
    if (up_ask - up_bid) > max_spread:
        return (False, None, 0.0, 0.0)
    if (dn_ask - dn_bid) > max_spread:
        return (False, None, 0.0, 0.0)
    if (up_mid + dn_mid) <= 0.0:
        return (False, None, 0.0, 0.0)

    book_mid_up = up_mid / (up_mid + dn_mid)
    cert = 2.0 * abs(book_mid_up - 0.5)
    if cert < certainty_threshold:
        return (False, None, book_mid_up, cert)

    winning_side = "UP" if book_mid_up > 0.5 else "DN"
    return (True, winning_side, book_mid_up, cert)


def simulate_market_with_book_gate(
    dome: DomeMarketData,
    cfg: BacktestConfig,
    book_gate_enabled: bool,
    book_cert_threshold: float,
    book_max_spread: float,
) -> tuple[MarketResult, bool, str | None]:
    """Run simulate_market_dome with the book-mid gate applied as a pre-step.

    Implementation trick: the existing simulate_market_dome already supports
    `fv_gate_enabled` which forces directional on the winning side. We hijack
    that mechanism when the book-mid gate fires:
      - When book-mid gate fires UP winning: set cfg.fv_gate_enabled=True with
        cert_threshold=0.0 so FV will block, then ensure the sim's FV direction
        picks UP. Easier: we monkey-patch the dome mid prices to force fv_up
        alignment.

    Cleaner approach: recompute what simulate_market_dome does by directly
    biasing the dome.up_best_bid/ask to align with the gate decision. But
    that changes the fill behavior. Simpler still: just replicate the piece
    of logic we need from simulate_market_dome here.

    To keep this script tight and production-faithful, we instead do:
      - If book-mid gate fires: emulate the "directional-only budget" branch
        manually using the same sim, but with a modified config where
        fv_gate_enabled=True, fv_gate_certainty_threshold is met, AND we
        set dome.up_best_bid/ask to force fv_up_entry > 0.5 (for UP winning)
        or < 0.5 (for DN winning).
    """
    if not book_gate_enabled:
        return (simulate_market_dome(dome, cfg), False, None)

    fired, winning_side, _book_mid_up, _cert = apply_book_mid_gate(
        dome, book_cert_threshold, book_max_spread
    )
    if not fired:
        return (simulate_market_dome(dome, cfg), False, None)

    # Gate fired: force directional-only posting on winning_side by using the
    # existing fv_gate mechanism. simulate_market_dome computes fv_up_entry
    # from book mids; if fv_gate_enabled AND cert >= threshold, it blocks.
    # We set cfg.fv_gate_enabled=True + threshold=0.0 so it always fires,
    # and the sim auto-picks direction from fv_up_entry (which IS the
    # book-mid direction — same signal we computed).
    gated_cfg = replace(
        cfg,
        fv_gate_enabled=True,
        fv_gate_certainty_threshold=0.0,
    )
    result = simulate_market_dome(dome, gated_cfg)
    return (result, True, winning_side)


# ---------------------------------------------------------------------------
# Corpus loader
# ---------------------------------------------------------------------------

def _epoch_to_date(ep: int) -> str:
    return datetime.datetime.fromtimestamp(ep, datetime.timezone.utc).strftime("%Y-%m-%d")


def load_corpus(dome_dir: pathlib.Path) -> list[DomeMarketData]:
    """Load all valid dome snapshots with book + outcome."""
    files = sorted(dome_dir.glob("btc-updown-15m-*.jsonl"))
    loaded: list[DomeMarketData] = []
    for p in files:
        d = load_dome_snapshot(p)
        if d is None or not d.has_orderbook or d.outcome is None:
            continue
        loaded.append(d)
    return loaded


def partition_by_date(
    corpus: list[DomeMarketData],
    train_start: str, train_end: str,  # inclusive
    holdout_start: str, holdout_end: str,  # inclusive
) -> tuple[list[DomeMarketData], list[DomeMarketData]]:
    """Split corpus into train/holdout by window_start date."""
    def _in_range(d: DomeMarketData, lo: str, hi: str) -> bool:
        day = _epoch_to_date(int(d.window_start))
        return lo <= day <= hi
    train = [d for d in corpus if _in_range(d, train_start, train_end)]
    holdout = [d for d in corpus if _in_range(d, holdout_start, holdout_end)]
    return train, holdout


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def run_sweep(
    corpus: list[DomeMarketData],
    cfg: BacktestConfig,
    book_gate_enabled: bool,
    book_cert_threshold: float,
    book_max_spread: float,
) -> dict:
    fired_results: list[MarketResult] = []
    nofire_results: list[MarketResult] = []
    fired_winners: list[str] = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    for d in corpus:
        result, fired, side = simulate_market_with_book_gate(
            d, cfg, book_gate_enabled, book_cert_threshold, book_max_spread
        )
        total_pnl += result.pnl
        if result.pnl > 0:
            wins += 1
        elif result.pnl < 0:
            losses += 1
        if fired:
            fired_results.append(result)
            if side is not None:
                fired_winners.append(side)
        else:
            nofire_results.append(result)

    n = len(corpus)
    fire_rate = len(fired_results) / n if n else 0.0
    win_rate = wins / n if n else 0.0
    fired_pnl = sum(r.pnl for r in fired_results)
    fired_wins = sum(1 for r in fired_results if r.pnl > 0)
    fired_correct = sum(
        1 for r in fired_results
        if r.outcome_correct is True
    )
    fired_wr = fired_wins / len(fired_results) if fired_results else 0.0

    return {
        "n": n,
        "total_pnl": total_pnl,
        "pnl_per_market": total_pnl / n if n else 0.0,
        "win_rate": win_rate,
        "fire_rate": fire_rate,
        "fired_count": len(fired_results),
        "fired_pnl": fired_pnl,
        "fired_pnl_per_market": fired_pnl / len(fired_results) if fired_results else 0.0,
        "fired_win_rate": fired_wr,
        "fired_correct_side": fired_correct,
        "nofire_pnl": sum(r.pnl for r in nofire_results),
        "nofire_pnl_per_market": (
            sum(r.pnl for r in nofire_results) / len(nofire_results)
            if nofire_results else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    dome_dir = _PROJECT_ROOT / "data" / "dome_snapshots"
    print(f"Loading corpus from {dome_dir}...")
    corpus = load_corpus(dome_dir)
    print(f"Loaded {len(corpus)} valid markets")

    # Print date coverage
    dates: dict[str, int] = {}
    for d in corpus:
        day = _epoch_to_date(int(d.window_start))
        dates[day] = dates.get(day, 0) + 1
    print("Date coverage:")
    for day in sorted(dates.keys()):
        print(f"  {day}: {dates[day]} markets")

    # Train: 9 days early, Holdout: 5 days later
    train, holdout = partition_by_date(
        corpus,
        train_start="2026-03-29", train_end="2026-04-06",
        holdout_start="2026-04-07", holdout_end="2026-04-11",
    )
    print(f"\nTrain: {len(train)}, Holdout: {len(holdout)}")

    # Production config (matches .env defaults)
    base_cfg = BacktestConfig(
        bankroll=500.0,
        position_size_fraction=0.1,  # matches current trading config
        directional_budget_cap=18.0,
        rungs=10,
        spacing=0.01,
        width=0.10,
        size_skew=2.0,
        max_pair_cost=0.98,
        one_sided_abort_enabled=True,
        one_sided_abort_cost_pct=0.01,
        one_sided_abort_ratio=3.0,
        fv_gate_enabled=False,  # We disable the native FV gate and use book-mid instead
    )

    # --- Full-corpus characterization at production settings (0.65, 0.05) ---
    print("\n" + "=" * 72)
    print("FULL-CORPUS characterization at PRODUCTION settings (0.65, 0.05)")
    print("=" * 72)
    full_prod = run_sweep(corpus, base_cfg, True, 0.65, 0.05)
    full_off = run_sweep(corpus, base_cfg, False, 0.0, 0.0)
    print(f"  gate OFF  : n={full_off['n']:4d} total=${full_off['total_pnl']:+.2f} "
          f"pnl/mkt=${full_off['pnl_per_market']:+.4f} WR={full_off['win_rate']:.1%}")
    print(f"  gate 0.65 : n={full_prod['n']:4d} total=${full_prod['total_pnl']:+.2f} "
          f"pnl/mkt=${full_prod['pnl_per_market']:+.4f} WR={full_prod['win_rate']:.1%}")
    print(f"            fires={full_prod['fired_count']} ({full_prod['fire_rate']:.1%}) "
          f"fired_pnl=${full_prod['fired_pnl']:+.2f} "
          f"fired_pnl/mkt=${full_prod['fired_pnl_per_market']:+.4f} "
          f"fired_WR={full_prod['fired_win_rate']:.1%} "
          f"correct_side={full_prod['fired_correct_side']}")

    # --- Train sweep over thresholds ---
    thresholds = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
    print("\n" + "=" * 72)
    print(f"TRAIN SWEEP (2026-03-29..2026-04-06, n={len(train)}) max_spread=0.05")
    print("=" * 72)
    print(f"  baseline (gate OFF):")
    train_off = run_sweep(train, base_cfg, False, 0.0, 0.0)
    print(f"    total=${train_off['total_pnl']:+.2f} pnl/mkt=${train_off['pnl_per_market']:+.4f} "
          f"WR={train_off['win_rate']:.1%}")
    train_results: dict[float, dict] = {}
    for t in thresholds:
        r = run_sweep(train, base_cfg, True, t, 0.05)
        train_results[t] = r
        print(f"  t={t:.2f}: total=${r['total_pnl']:+8.2f} pnl/mkt=${r['pnl_per_market']:+.4f} "
              f"WR={r['win_rate']:.1%} fires={r['fired_count']:4d} ({r['fire_rate']:.1%}) "
              f"fired_pnl/mkt=${r['fired_pnl_per_market']:+.4f} fired_WR={r['fired_win_rate']:.1%}")

    # Pick best threshold by total pnl
    best_t = max(train_results.keys(), key=lambda t: train_results[t]["total_pnl"])
    best_pnl = train_results[best_t]["total_pnl"]
    print(f"\n  BEST train threshold: {best_t} (total=${best_pnl:+.2f})")

    # --- Holdout evaluation ---
    print("\n" + "=" * 72)
    print(f"HOLDOUT EVALUATION (2026-04-07..2026-04-11, n={len(holdout)})")
    print("=" * 72)
    holdout_off = run_sweep(holdout, base_cfg, False, 0.0, 0.0)
    print(f"  gate OFF   : total=${holdout_off['total_pnl']:+.2f} "
          f"pnl/mkt=${holdout_off['pnl_per_market']:+.4f} WR={holdout_off['win_rate']:.1%}")
    for t in thresholds:
        r = run_sweep(holdout, base_cfg, True, t, 0.05)
        marker = " <- train-best" if t == best_t else (" <- current" if t == 0.65 else "")
        print(f"  t={t:.2f}    : total=${r['total_pnl']:+8.2f} "
              f"pnl/mkt=${r['pnl_per_market']:+.4f} WR={r['win_rate']:.1%} "
              f"fires={r['fired_count']:3d} ({r['fire_rate']:.1%}){marker}")

    # --- max_spread sweep at best threshold ---
    print("\n" + "=" * 72)
    print(f"MAX_SPREAD SWEEP at t={best_t} on HOLDOUT")
    print("=" * 72)
    for ms in [0.03, 0.05, 0.08]:
        r = run_sweep(holdout, base_cfg, True, best_t, ms)
        print(f"  max_spread={ms:.2f}: total=${r['total_pnl']:+.2f} "
              f"pnl/mkt=${r['pnl_per_market']:+.4f} fires={r['fired_count']} "
              f"({r['fire_rate']:.1%})")


if __name__ == "__main__":
    main()
