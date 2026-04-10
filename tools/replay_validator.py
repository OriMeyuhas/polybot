"""Book replay validator — Proposal #45.

Validates paper fill realism by cross-checking each fill in order_log against
the book state at fill time, reconstructed from book_log events.

Usage:
    python tools/replay_validator.py --date 2026-04-09
    python tools/replay_validator.py --date 2026-04-09 --data-dir data

Reports:
    - Total fills examined
    - Fills with no matching book quote (warmup/WS gap)
    - Fills where fill price was outside the real book's spread
    - Fills where real book had insufficient size at that price level
    - Summary: "realistic_fills / total_fills (%)"

Read-only: does NOT modify any files.
"""

from __future__ import annotations

import argparse
import bisect
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


class BookQuote(NamedTuple):
    """Lightweight book state at a point in time.

    Produced from both 'book' (full depth) and 'price_change' (delta) events.
    For 'book' events, bids/asks are populated for optional size checking.
    For 'price_change' events, bids/asks are None — we rely on best_bid/best_ask only.
    """
    ts: float
    token_id: str
    best_bid: float
    best_ask: float
    bids: list | None   # None for price_change events
    asks: list | None   # None for price_change events


class ValidationResult(NamedTuple):
    fill: FillRecord
    token_id: str | None
    book: BookQuote | None
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


def load_book_quotes(
    book_log_path: pathlib.Path,
    token_ids: set[str],
) -> dict[str, list[BookQuote]]:
    """Load book quotes for the given token_ids from both 'book' and 'price_change' events.

    For 'book' events: one BookQuote per event, with full bids/asks depth.
    For 'price_change' events: one BookQuote per asset_id in price_changes array,
        using the best_bid/best_ask fields from that price_change entry.

    Returns: {token_id: [sorted list of BookQuote by ts]}
    """
    # Build a lookup for fast token matching (full_token_id -> canonical token_id)
    token_lookup: dict[str, str] = {}
    for tid in token_ids:
        token_lookup[tid] = tid

    def match_token(full_token_id: str) -> str | None:
        if full_token_id in token_lookup:
            return token_lookup[full_token_id]
        # Prefix matching for partial IDs
        for tid in token_ids:
            if full_token_id.startswith(tid) or tid.startswith(full_token_id):
                token_lookup[full_token_id] = tid  # cache for next time
                return tid
        return None

    index: dict[str, list[BookQuote]] = defaultdict(list)

    with open(book_log_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            event_type = rec.get("event_type")
            ts = float(rec.get("ts", 0))
            data = rec.get("data", {})

            if event_type == "book":
                # Full depth snapshot for a single asset_id
                full_token_id = data.get("asset_id", "")
                if not full_token_id:
                    continue
                matched = match_token(full_token_id)
                if matched is None:
                    continue
                bids = data.get("bids", [])
                asks = data.get("asks", [])
                if not bids and not asks:
                    continue
                # Compute best_bid/best_ask from the full depth
                try:
                    best_bid = max(float(e["price"]) for e in bids) if bids else 0.0
                    best_ask = min(float(e["price"]) for e in asks) if asks else 1.0
                except (KeyError, ValueError):
                    continue
                quote = BookQuote(
                    ts=ts,
                    token_id=full_token_id,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    bids=bids,
                    asks=asks,
                )
                index[matched].append(quote)

            elif event_type == "price_change":
                # Delta update — may contain multiple asset_ids
                price_changes = data.get("price_changes", [])
                for pc in price_changes:
                    full_token_id = pc.get("asset_id", "")
                    if not full_token_id:
                        continue
                    matched = match_token(full_token_id)
                    if matched is None:
                        continue
                    try:
                        best_bid = float(pc.get("best_bid", 0))
                        best_ask = float(pc.get("best_ask", 1))
                    except (TypeError, ValueError):
                        continue
                    quote = BookQuote(
                        ts=ts,
                        token_id=full_token_id,
                        best_bid=best_bid,
                        best_ask=best_ask,
                        bids=None,  # no depth from delta events
                        asks=None,
                    )
                    index[matched].append(quote)

    # Sort each token's list by ts
    for tid in index:
        index[tid].sort(key=lambda q: q.ts)
    return dict(index)


# Keep load_book_index as an alias / wrapper for backward compatibility with existing tests.
def load_book_index(
    book_log_path: pathlib.Path,
    token_ids: set[str],
) -> dict[str, list[BookQuote]]:
    """Backward-compatible alias for load_book_quotes.

    Existing tests pass BookSnapshot objects to check_fill_against_book; those still work
    because we access .bids/.asks which BookQuote also has (possibly None).
    """
    return load_book_quotes(book_log_path, token_ids)


# ---------------------------------------------------------------------------
# Core validation logic
# ---------------------------------------------------------------------------

def find_quote_at(
    quotes: list[BookQuote],
    fill_ts: float,
    stale_sec: float = 60.0,
) -> BookQuote | None:
    """Find the most recent quote with ts <= fill_ts.

    Uses binary search. Returns None if:
    - No quotes exist before fill_ts
    - The most recent quote is older than stale_sec before fill_ts

    Unlike the old find_nearest_book, this does NOT look at quotes AFTER fill_ts
    (a fill can't be validated against a future book state) and does NOT reject
    quotes just because they're a few seconds old — a stable book level set 30s ago
    is still the correct book state at fill time.
    """
    if not quotes:
        return None
    # Find rightmost quote with ts <= fill_ts using bisect
    # bisect_right gives insertion point after all entries with ts == fill_ts
    ts_list = [q.ts for q in quotes]
    idx = bisect.bisect_right(ts_list, fill_ts) - 1
    if idx < 0:
        return None  # all quotes are in the future
    quote = quotes[idx]
    if fill_ts - quote.ts > stale_sec:
        return None  # most recent quote is too old
    return quote


def find_nearest_book(
    snapshots: list[BookQuote],
    fill_ts: float,
    max_delta_sec: float = 2.0,
) -> BookQuote | None:
    """Find the most recent quote at or before fill_ts.

    The max_delta_sec parameter is kept for API compatibility but is no longer a
    strict ±2s window — we use it as a stale threshold (reject if older than
    max_delta_sec * 30 to give reasonable leeway, but cap at 60s).

    For the primary validator path, use find_quote_at() directly.
    """
    stale_threshold = min(max_delta_sec * 30, 60.0)
    return find_quote_at(snapshots, fill_ts, stale_sec=stale_threshold)


def check_fill_against_book(fill: FillRecord, book: BookQuote) -> str:
    """Validate a fill against a book quote.

    We place passive maker bids to BUY tokens (YES or NO). We fill when a taker
    sells into our bid. Validation:

    1. Price outside spread: fill price is more than 2c above the best ask
       (means we somehow paid more than the ask — impossible for a passive fill)
       OR more than 5c below the best bid (means the market moved dramatically
       against our fill direction).

    2. Insufficient size: only checked when we have full depth (from 'book' events).
       The nearest ask level to our fill price has very thin size AND book depth
       above fill_price is also thin — suggests the book was one-sided and unlikely
       to have had a counterparty at our price.

    Returns one of: "realistic", "price_outside_spread", "insufficient_size"
    """
    best_bid = book.best_bid
    best_ask = book.best_ask
    fill_price = fill.price

    # Check 1: fill price way above ask → impossible for passive maker buy
    if best_ask < 0.99 and fill_price > best_ask + 0.02:
        return "price_outside_spread"

    # Check 2: fill price way below bid → implies book has moved far away
    if best_bid > 0.01 and fill_price < best_bid - 0.05:
        return "price_outside_spread"

    # Check 3: Insufficient counterparty size (only when we have full depth).
    if book.bids is not None and book.asks is not None:
        try:
            asks = [(float(e["price"]), float(e["size"])) for e in book.asks]
            bids = [(float(e["price"]), float(e["size"])) for e in book.bids]
        except (KeyError, ValueError):
            return "insufficient_size"

        if not asks and not bids:
            return "price_outside_spread"

        # As a passive buyer at fill_price, a taker must be willing to SELL at or below
        # fill_price. Give ±2c tolerance to account for book movement between quote and fill.
        available_ask_size = sum(s for p, s in asks if fill_price - 0.02 <= p <= fill_price + 0.02)
        total_ask_size = sum(s for _, s in asks)

        if available_ask_size == 0 and total_ask_size < fill.size:
            return "insufficient_size"

    return "realistic"


def validate(
    fills: list[FillRecord],
    market_token_map: dict[str, dict],
    book_index: dict[str, list[BookQuote]],
    max_delta_sec: float = 2.0,
    stale_sec: float = 60.0,
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

        quotes = book_index.get(token_id, [])
        book = find_quote_at(quotes, fill.ts, stale_sec=stale_sec)
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
    print(f"  No book quote (stale) : {no_book}  ({100*no_book/total:.1f}%)")
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

def run(
    date: str,
    data_dir: pathlib.Path,
    max_delta_sec: float = 2.0,
    stale_sec: float = 60.0,
) -> list[ValidationResult]:
    """Run the replay validator for a given date.

    Args:
        date: ISO date string "YYYY-MM-DD"
        data_dir: path to the data/ directory
        max_delta_sec: kept for API compatibility; actual staleness threshold is stale_sec
        stale_sec: reject quotes older than this many seconds before the fill

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

    print(f"Loading book quotes from {book_log} (this may take a moment for large files) ...")
    book_index = load_book_quotes(book_log, all_tokens)
    total_quotes = sum(len(v) for v in book_index.values())
    print(f"  Loaded {total_quotes} book quotes for {len(book_index)} tokens.")
    print(f"  (includes both full 'book' snapshots and 'price_change' delta updates)")

    print("Validating fills ...")
    results = validate(fills, market_token_map, book_index, max_delta_sec, stale_sec=stale_sec)

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
        help="(Legacy) Max time delta parameter (default: 2.0)"
    )
    parser.add_argument(
        "--stale-sec", type=float, default=60.0,
        help="Reject book quotes older than this many seconds before the fill (default: 60)"
    )
    args = parser.parse_args()
    run(
        date=args.date,
        data_dir=pathlib.Path(args.data_dir),
        max_delta_sec=args.max_delta_sec,
        stale_sec=args.stale_sec,
    )
