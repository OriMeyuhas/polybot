"""Book replay validator — Proposal #45.

Validates paper fill realism by cross-checking each fill in order_log against
the nearest book_log snapshot within ±2 seconds.

Usage:
    python tools/replay_validator.py --date 2026-04-09
    python tools/replay_validator.py --date 2026-04-09 --data-dir data

Reports:
    - Total fills examined
    - Fills with no matching book snapshot (warmup/WS gap)
    - Fills where fill price was outside the real book's spread
    - Fills where real book had insufficient size at that price level
    - Summary: "realistic_fills / total_fills (%)"

Read-only: does NOT modify any files.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import defaultdict
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

class FillRecord(NamedTuple):
    ts: float
    market_id: str
    side: str           # "UP" or "DN"
    price: float
    size: float
    order_id: str


class BookSnapshot(NamedTuple):
    ts: float
    token_id: str       # full token ID from data.asset_id
    bids: list          # list of {"price": str, "size": str}
    asks: list          # list of {"price": str, "size": str}


class ValidationResult(NamedTuple):
    fill: FillRecord
    token_id: str | None
    book: BookSnapshot | None
    verdict: str        # "realistic", "no_book", "price_outside_spread", "insufficient_size"


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def load_fills(order_log_path: pathlib.Path) -> list[FillRecord]:
    """Load all fill events from order_log."""
    fills = []
    with open(order_log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "fill":
                fills.append(FillRecord(
                    ts=float(rec.get("ts", 0)),
                    market_id=rec.get("market_id", ""),
                    side=rec.get("side", ""),
                    price=float(rec.get("price", 0)),
                    size=float(rec.get("size", 0)),
                    order_id=rec.get("order_id", ""),
                ))
    return fills


def load_market_token_map(market_event_log_path: pathlib.Path) -> dict[str, dict]:
    """Load market_id -> {"up_token_id": ..., "dn_token_id": ...} from market_event_log.

    Supports both old format (no token_ids) and new format (with token_ids, Proposal #46).
    """
    market_tokens: dict[str, dict] = {}
    if not market_event_log_path.exists():
        return market_tokens
    with open(market_event_log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") not in ("discovered", "dropped"):
                continue
            mid = rec.get("market_id", "")
            if not mid:
                continue
            meta = rec.get("metadata", {})
            up_tok = meta.get("up_token_id", "")
            dn_tok = meta.get("dn_token_id", "")
            if up_tok and dn_tok:
                market_tokens[mid] = {"up_token_id": up_tok, "dn_token_id": dn_tok}
    return market_tokens


def load_book_index(
    book_log_path: pathlib.Path,
    token_ids: set[str],
) -> dict[str, list[BookSnapshot]]:
    """Load book snapshots for the given token_ids, indexed by token_id.

    Only loads 'book' type events (full depth snapshots). Skips 'price_change'
    events which don't have full bid/ask depth.

    Returns: {token_id: [sorted list of BookSnapshot by ts]}
    """
    index: dict[str, list[BookSnapshot]] = defaultdict(list)
    with open(book_log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_type") != "book":
                continue
            data = rec.get("data", {})
            full_token_id = data.get("asset_id", "")
            if not full_token_id:
                continue
            # Match against token_ids (which may be full or partial)
            matched = None
            for tid in token_ids:
                if full_token_id == tid or full_token_id.startswith(tid) or tid.startswith(full_token_id):
                    matched = tid
                    break
            if matched is None:
                continue
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids and not asks:
                continue
            snap = BookSnapshot(
                ts=float(rec.get("ts", 0)),
                token_id=full_token_id,
                bids=bids,
                asks=asks,
            )
            index[matched].append(snap)

    # Sort each token's list by ts
    for tid in index:
        index[tid].sort(key=lambda s: s.ts)
    return dict(index)


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------

def find_nearest_book(
    snapshots: list[BookSnapshot],
    fill_ts: float,
    max_delta_sec: float = 2.0,
) -> BookSnapshot | None:
    """Binary search for the snapshot closest in time to fill_ts within max_delta_sec."""
    if not snapshots:
        return None
    # Binary search for insertion point
    lo, hi = 0, len(snapshots) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if snapshots[mid].ts < fill_ts:
            lo = mid + 1
        else:
            hi = mid
    # Check candidates around lo
    best = None
    best_delta = float("inf")
    for idx in (lo - 1, lo, lo + 1):
        if 0 <= idx < len(snapshots):
            delta = abs(snapshots[idx].ts - fill_ts)
            if delta < best_delta:
                best_delta = delta
                best = snapshots[idx]
    if best_delta > max_delta_sec:
        return None
    return best


def check_fill_against_book(fill: FillRecord, book: BookSnapshot) -> str:
    """Validate a fill against a book snapshot.

    We place passive maker bids to BUY tokens (YES or NO). We fill when a taker
    sells into our bid. Validation:

    1. Price outside spread: fill price is more than 2c above the best ask
       (means we somehow paid more than the ask — impossible for a passive fill)
       OR more than 5c below the best bid (means the market moved dramatically
       against our fill direction).

    2. Insufficient size: the nearest ask level to our fill price has very thin
       size AND the book depth above fill_price is also thin — suggests the book
       was one-sided and unlikely to have had a counterparty at our price.

    Returns one of: "realistic", "price_outside_spread", "insufficient_size"
    """
    # Parse asks/bids to float
    try:
        asks = [(float(e["price"]), float(e["size"])) for e in book.asks]
        bids = [(float(e["price"]), float(e["size"])) for e in book.bids]
    except (KeyError, ValueError):
        return "insufficient_size"

    if not asks and not bids:
        return "price_outside_spread"

    fill_price = fill.price
    best_bid = max((p for p, _ in bids), default=0.0)
    best_ask = min((p for p, _ in asks), default=1.0)

    # Check 1: fill price way above ask → impossible for passive maker buy
    # (passive buy fills when ask comes DOWN to our price, never above ask)
    if best_ask < 0.99 and fill_price > best_ask + 0.02:
        return "price_outside_spread"

    # Check 2: fill price way below bid → implies book has moved far away
    if best_bid > 0.01 and fill_price < best_bid - 0.05:
        return "price_outside_spread"

    # Check 3: Insufficient counterparty size.
    # As a passive buyer at fill_price, a taker must be willing to SELL at or below
    # fill_price. The relevant book liquidity is asks at prices <= fill_price + small_tol
    # (asks that would be filled by a taker wanting to sell at market).
    # However, in binary markets, the "ask" on the book represents sellers —
    # so we look at ask levels that are AT OR NEAR fill_price.
    # Give ±2c tolerance to account for book movement between snapshot and fill.
    available_ask_size = sum(s for p, s in asks if fill_price - 0.02 <= p <= fill_price + 0.02)
    total_ask_size = sum(s for _, s in asks)

    # If no asks anywhere near our fill price AND total asks are thin, flag it.
    if available_ask_size == 0 and total_ask_size < fill.size:
        return "insufficient_size"

    return "realistic"


def validate(
    fills: list[FillRecord],
    market_token_map: dict[str, dict],
    book_index: dict[str, list[BookSnapshot]],
    max_delta_sec: float = 2.0,
) -> list[ValidationResult]:
    """Run fill validation. Returns one ValidationResult per fill."""
    results = []
    for fill in fills:
        tokens = market_token_map.get(fill.market_id, {})
        if not tokens:
            results.append(ValidationResult(fill=fill, token_id=None, book=None, verdict="no_book"))
            continue

        # Select token based on side
        side_upper = fill.side.upper()
        if "UP" in side_upper:
            token_id = tokens.get("up_token_id", "")
        else:
            token_id = tokens.get("dn_token_id", "")

        if not token_id:
            results.append(ValidationResult(fill=fill, token_id=None, book=None, verdict="no_book"))
            continue

        snapshots = book_index.get(token_id, [])
        book = find_nearest_book(snapshots, fill.ts, max_delta_sec)
        if book is None:
            results.append(ValidationResult(fill=fill, token_id=token_id, book=None, verdict="no_book"))
            continue

        verdict = check_fill_against_book(fill, book)
        results.append(ValidationResult(fill=fill, token_id=token_id, book=book, verdict=verdict))

    return results


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[ValidationResult]) -> None:
    total = len(results)
    if total == 0:
        print("No fills found.")
        return

    counts: dict[str, int] = defaultdict(int)
    for r in results:
        counts[r.verdict] += 1

    realistic = counts["realistic"]
    no_book = counts["no_book"]
    outside_spread = counts["price_outside_spread"]
    insufficient = counts["insufficient_size"]

    print("=" * 60)
    print("Book Replay Validator — Fill Realism Report")
    print("=" * 60)
    print(f"Total fills examined    : {total}")
    print(f"  No book snapshot (±2s): {no_book}  ({100*no_book/total:.1f}%)")
    print(f"  Price outside spread  : {outside_spread}  ({100*outside_spread/total:.1f}%)")
    print(f"  Insufficient book size: {insufficient}  ({100*insufficient/total:.1f}%)")
    print(f"  Realistic fills       : {realistic}  ({100*realistic/total:.1f}%)")
    print("-" * 60)
    print(f"SUMMARY: {realistic} / {total} fills realistic ({100*realistic/total:.1f}%)")
    print("=" * 60)

    # Phantom PnL estimate: unrealistic fills are phantom
    phantom_cost = sum(
        r.fill.price * r.fill.size
        for r in results
        if r.verdict != "realistic"
    )
    real_cost = sum(
        r.fill.price * r.fill.size
        for r in results
        if r.verdict == "realistic"
    )
    print(f"Estimated phantom capital deployed: ${phantom_cost:.2f}")
    print(f"Estimated realistic capital deployed: ${real_cost:.2f}")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(date: str, data_dir: pathlib.Path, max_delta_sec: float = 2.0) -> list[ValidationResult]:
    """Run the replay validator for a given date.

    Args:
        date: ISO date string "YYYY-MM-DD"
        data_dir: path to the data/ directory
        max_delta_sec: max time delta between fill and book snapshot

    Returns:
        List of ValidationResult objects (for programmatic use).
    """
    order_log = data_dir / f"order_log_{date}.jsonl"
    book_log = data_dir / f"book_log_{date}.jsonl"
    market_event_log = data_dir / f"market_event_log_{date}.jsonl"

    if not order_log.exists():
        print(f"ERROR: order_log not found: {order_log}", file=sys.stderr)
        return []

    if not book_log.exists():
        print(f"ERROR: book_log not found: {book_log}", file=sys.stderr)
        return []

    print(f"Loading fills from {order_log} ...")
    fills = load_fills(order_log)
    print(f"  Found {len(fills)} fills.")

    if not fills:
        print("No fills to validate.")
        return []

    print(f"Loading market token map from {market_event_log} ...")
    market_token_map = load_market_token_map(market_event_log)
    print(f"  Found {len(market_token_map)} markets with token IDs.")

    if not market_token_map:
        print(
            "WARNING: No token IDs found in market_event_log. "
            "This is expected for data before Proposal #46 was deployed (2026-04-10). "
            "All fills will report 'no_book'.",
            file=sys.stderr,
        )

    # Collect all token IDs needed
    all_tokens: set[str] = set()
    for tokens in market_token_map.values():
        if tokens.get("up_token_id"):
            all_tokens.add(tokens["up_token_id"])
        if tokens.get("dn_token_id"):
            all_tokens.add(tokens["dn_token_id"])

    print(f"Loading book snapshots from {book_log} (this may take a moment for large files) ...")
    book_index = load_book_index(book_log, all_tokens)
    total_snaps = sum(len(v) for v in book_index.values())
    print(f"  Loaded {total_snaps} book snapshots for {len(book_index)} tokens.")

    print("Validating fills ...")
    results = validate(fills, market_token_map, book_index, max_delta_sec)

    print()
    print_report(results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Book replay fill validator (Proposal #45)")
    parser.add_argument(
        "--date", required=True,
        help="Date to validate in YYYY-MM-DD format (e.g. 2026-04-09)"
    )
    parser.add_argument(
        "--data-dir", default="data",
        help="Path to the data directory (default: data/)"
    )
    parser.add_argument(
        "--max-delta-sec", type=float, default=2.0,
        help="Max time delta between fill and book snapshot in seconds (default: 2.0)"
    )
    args = parser.parse_args()
    run(
        date=args.date,
        data_dir=pathlib.Path(args.data_dir),
        max_delta_sec=args.max_delta_sec,
    )
