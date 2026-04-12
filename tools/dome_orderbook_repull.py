"""
dome_orderbook_repull.py — Surgical re-fetch of orderbook data only.

Walks existing data/dome_snapshots/*.jsonl files, strips the old
{"type": "orderbook", ...} lines (which only cover the first 40s of each
15m window), and re-fetches orderbooks for UP and DN tokens using the new
paginated get_orderbook_snapshots() that covers the full 900s window.

Candles, binance, chainlink, and header lines are preserved unchanged.

Progress is written to:
  data/dome_snapshots/_orderbook_repull_log.jsonl   — one JSON line per market
  data/dome_snapshots/_orderbook_repull_stdout.log  — stdout mirror (external)

Resume support: markets already logged as status="done" in the log file are
skipped on the next run.

Usage:
    python tools/dome_orderbook_repull.py [--snapshots-dir data/dome_snapshots]
                                          [--rate-limit 100]  # requests / 60s
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import sys
import time
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_TOOLS_DIR = pathlib.Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

# Load .env
_env_path = _TOOLS_DIR.parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if "=" in _line and not _line.startswith("#"):
            k, v = _line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k not in os.environ:
                os.environ[k] = v

from dome_client import DomeClient, DomeAPIError  # noqa: E402

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dome_repull")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_RATE_LIMIT_PAUSE_SEC = 60      # pause duration when 429 is received
_MIN_INTERVAL_SEC = 0.65        # ~92 req/min polite rate limit (below 100/min)
_LOG_FILENAME = "_orderbook_repull_log.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_done_set(log_path: pathlib.Path) -> set[str]:
    """Return slugs that completed successfully in a previous run."""
    done: set[str] = set()
    if not log_path.exists():
        return done
    with open(log_path) as fh:
        for line in fh:
            try:
                rec = json.loads(line)
                if rec.get("status") == "done":
                    done.add(rec["slug"])
            except Exception:
                pass
    return done


def _append_log(log_path: pathlib.Path, record: dict) -> None:
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def _rewrite_market_file(
    path: pathlib.Path,
    new_up_snaps: list[dict],
    new_dn_snaps: list[dict],
) -> None:
    """Replace orderbook lines in *path* with fresh paginated snapshots.

    Reads the existing file, keeps header/candle/binance/chainlink lines,
    replaces orderbook lines with the new data, writes back atomically.
    """
    kept_lines: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("type") == "orderbook":
                continue  # strip old orderbook data
            kept_lines.append(rec)

    # Find where to insert orderbook lines: after candles, before binance/chainlink
    # Strategy: insert after last candle line (or after header if no candles)
    insert_idx = 1  # default: right after header
    for i, rec in enumerate(kept_lines):
        if rec.get("type") == "candle":
            insert_idx = i + 1

    new_ob_lines = [
        {"type": "orderbook", "side": "UP", "data": s} for s in new_up_snaps
    ] + [
        {"type": "orderbook", "side": "DN", "data": s} for s in new_dn_snaps
    ]

    final_lines = kept_lines[:insert_idx] + new_ob_lines + kept_lines[insert_idx:]

    tmp_path = path.with_suffix(".jsonl.tmp")
    tmp_path.write_text(
        "\n".join(json.dumps(ln) for ln in final_lines), encoding="utf-8"
    )
    tmp_path.replace(path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(snapshots_dir: pathlib.Path, rate_limit: int) -> None:
    log_path = snapshots_dir / _LOG_FILENAME
    done_set = _load_done_set(log_path)
    logger.info("Loaded %d previously completed markets from log", len(done_set))

    # Collect market files (exclude control files starting with _)
    market_files = sorted(
        f for f in snapshots_dir.glob("*.jsonl")
        if not f.name.startswith("_")
    )
    total = len(market_files)
    logger.info("Found %d market files to process", total)

    cache_dir = snapshots_dir / "_cache"
    # Use a long TTL — historical data is immutable
    client = DomeClient(
        cache_dir=cache_dir,
        cache_ttl_sec=30 * 86_400,
        min_interval_sec=_MIN_INTERVAL_SEC,
    )

    processed = 0
    skipped = 0
    errors = 0
    t_start = time.monotonic()
    api_calls = 0  # track for rate estimation

    for idx, market_path in enumerate(market_files, 1):
        slug = market_path.stem

        if slug in done_set:
            logger.debug("[%d/%d] %s — skipping (already done)", idx, total, slug)
            skipped += 1
            continue

        # Read header to get token IDs and window times
        try:
            with open(market_path, encoding="utf-8") as fh:
                header_raw = fh.readline()
            if not header_raw.strip():
                logger.warning("[%d/%d] %s — empty file, skipping", idx, total, slug)
                skipped += 1
                continue
            header = json.loads(header_raw)
        except Exception as exc:
            logger.error("[%d/%d] %s — header parse error: %s", idx, total, slug, exc)
            _append_log(log_path, {"slug": slug, "status": "error", "error": str(exc), "ts": time.time()})
            errors += 1
            continue

        if header.get("type") != "header":
            logger.warning("[%d/%d] %s — first line is not header, skipping", idx, total, slug)
            skipped += 1
            continue

        up_token_id: str = header.get("up_token_id", "")
        dn_token_id: str = header.get("dn_token_id", "")
        window_start: int = header.get("window_start", 0)
        window_end: int = header.get("window_end", 0)

        if not up_token_id or not window_start:
            logger.warning("[%d/%d] %s — missing token_id or window times", idx, total, slug)
            _append_log(log_path, {"slug": slug, "status": "skip_no_token", "ts": time.time()})
            skipped += 1
            continue

        t0 = time.monotonic()
        retry_after_pause = False

        try:
            up_snaps = client.get_orderbook_snapshots(up_token_id, window_start, window_end)
            api_calls += 1
            dn_snaps: list[dict] = []
            if dn_token_id:
                dn_snaps = client.get_orderbook_snapshots(dn_token_id, window_start, window_end)
                api_calls += 1

        except DomeAPIError as exc:
            if exc.status_code == 429:
                logger.warning(
                    "[%d/%d] %s — RATE LIMITED, pausing %ds then retrying",
                    idx, total, slug, _RATE_LIMIT_PAUSE_SEC,
                )
                time.sleep(_RATE_LIMIT_PAUSE_SEC)
                retry_after_pause = True
            else:
                logger.error(
                    "[%d/%d] %s — API error %d: %s",
                    idx, total, slug, exc.status_code, exc.body[:100],
                )
                _append_log(log_path, {
                    "slug": slug, "status": "error",
                    "error": f"HTTP {exc.status_code}", "ts": time.time(),
                })
                errors += 1
                continue

        if retry_after_pause:
            try:
                up_snaps = client.get_orderbook_snapshots(up_token_id, window_start, window_end)
                api_calls += 1
                dn_snaps = []
                if dn_token_id:
                    dn_snaps = client.get_orderbook_snapshots(dn_token_id, window_start, window_end)
                    api_calls += 1
            except Exception as exc2:
                logger.error("[%d/%d] %s — retry failed: %s", idx, total, slug, exc2)
                _append_log(log_path, {
                    "slug": slug, "status": "error", "error": str(exc2), "ts": time.time(),
                })
                errors += 1
                continue

        # Compute window coverage for the log
        up_span = 0.0
        if up_snaps:
            up_ts = [s.get("timestamp", 0) / 1000 for s in up_snaps]
            up_span = max(up_ts) - min(up_ts) if len(up_ts) > 1 else 0.0

        try:
            _rewrite_market_file(market_path, up_snaps, dn_snaps)
        except Exception as exc:
            logger.error("[%d/%d] %s — file write error: %s", idx, total, slug, exc)
            _append_log(log_path, {
                "slug": slug, "status": "error", "error": str(exc), "ts": time.time(),
            })
            errors += 1
            continue

        elapsed = time.monotonic() - t0
        processed += 1

        _append_log(log_path, {
            "slug": slug,
            "status": "done",
            "up_snaps": len(up_snaps),
            "dn_snaps": len(dn_snaps),
            "up_span_sec": round(up_span, 1),
            "elapsed_sec": round(elapsed, 2),
            "ts": time.time(),
        })

        # Rate estimation: report after first 10 markets
        if processed == 10:
            elapsed_total = time.monotonic() - t_start
            rate = processed / elapsed_total * 60  # markets / min
            remaining = total - processed - skipped
            eta_min = remaining / rate if rate > 0 else float("inf")
            logger.info(
                "Rate after first 10: %.1f markets/min — ETA %.0f min (%.1f h) for %d remaining",
                rate, eta_min, eta_min / 60, remaining,
            )

        if (processed + errors) % 50 == 0 and (processed + errors) > 0:
            elapsed_total = time.monotonic() - t_start
            rate = (processed + errors) / elapsed_total * 60
            remaining = total - idx
            eta_min = remaining / rate if rate > 0 else float("inf")
            logger.info(
                "[%d/%d] done=%d err=%d skip=%d | %.1f mkts/min | ETA %.0f min",
                idx, total, processed, errors, skipped, rate, eta_min,
            )
        else:
            logger.info(
                "[%d/%d] %s — UP=%d DN=%d span=%.0fs (%.2fs)",
                idx, total, slug, len(up_snaps), len(dn_snaps), up_span, elapsed,
            )

    client.close()

    total_elapsed = time.monotonic() - t_start
    logger.info("=" * 60)
    logger.info("Repull complete in %.0f min", total_elapsed / 60)
    logger.info("  Processed: %d", processed)
    logger.info("  Skipped:   %d", skipped)
    logger.info("  Errors:    %d", errors)
    logger.info("  API calls: %d (approx, not counting pagination sub-requests)", api_calls)
    logger.info("=" * 60)

    # Write final summary
    summary_path = snapshots_dir / "_orderbook_repull_summary.json"
    summary_path.write_text(json.dumps({
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "total_elapsed_min": round(total_elapsed / 60, 1),
        "api_calls_approx": api_calls,
        "completed_at": time.time(),
    }, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Re-fetch orderbook data for existing dome_snapshots files (surgical upgrade)"
    )
    parser.add_argument(
        "--snapshots-dir",
        default="data/dome_snapshots",
        help="Directory containing .jsonl snapshot files (default: data/dome_snapshots)",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=100,
        help="Max API requests per 60s (default: 100; script uses 92 to stay safe)",
    )
    args = parser.parse_args(argv)

    snapshots_dir = pathlib.Path(args.snapshots_dir)
    if not snapshots_dir.is_dir():
        logger.error("snapshots-dir does not exist: %s", snapshots_dir)
        sys.exit(1)

    logger.info("Starting orderbook repull from %s", snapshots_dir.resolve())
    run(snapshots_dir, args.rate_limit)


if __name__ == "__main__":
    main()
