"""
Driver script to pull 14 days of historical Dome data for BTC 15m markets.

Calls dome_snapshot.run() for each date, sleeping between days, logging
per-day success/failure to data/dome_snapshots/_pull_log.jsonl, and writing
a final summary to data/dome_snapshots/_pull_summary.json.

Usage:
    python tools/dome_pull_14d.py
"""
from __future__ import annotations

import datetime
import json
import os
import pathlib
import sys
import time
import traceback

_TOOLS_DIR = pathlib.Path(__file__).parent
sys.path.insert(0, str(_TOOLS_DIR))

# Make sure DOME_API_KEY is loaded before importing dome_snapshot
if not os.environ.get("DOME_API_KEY"):
    _env_path = _TOOLS_DIR.parent / ".env"
    if _env_path.exists():
        for _line in _env_path.read_text().splitlines():
            if _line.startswith("DOME_API_KEY="):
                os.environ["DOME_API_KEY"] = _line.split("=", 1)[1].strip()
                break

import dome_snapshot  # noqa: E402

OUT_DIR = pathlib.Path("data/dome_snapshots")
LOG_FILE = OUT_DIR / "_pull_log.jsonl"
SUMMARY_FILE = OUT_DIR / "_pull_summary.json"

# Most recent first
DATES = [
    "2026-04-11",
    "2026-04-10",
    "2026-04-09",
    "2026-04-08",
    "2026-04-07",
    "2026-04-06",
    "2026-04-05",
    "2026-04-04",
    "2026-04-03",
    "2026-04-02",
    "2026-04-01",
    "2026-03-31",
    "2026-03-30",
    "2026-03-29",
]

ASSET = "BTC"
TIMEFRAME = "15m"
HOURS = 24
SLEEP_BETWEEN_DAYS = 10


def _log(entry: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _count_markets_for_date(date_str: str) -> int:
    """Count how many JSONL files exist for a given date."""
    date = datetime.date.fromisoformat(date_str)
    midnight = int(
        datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc).timestamp()
    )
    end = midnight + 24 * 3600
    n = 0
    for f in OUT_DIR.glob(f"btc-updown-15m-*.jsonl"):
        try:
            ts = int(f.stem.rsplit("-", 1)[-1])
        except ValueError:
            continue
        if midnight <= ts < end and f.stat().st_size > 0:
            n += 1
    return n


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 70)
    print(f"Dome 14-day pull starting at {datetime.datetime.utcnow().isoformat()}Z")
    print(f"Dates: {DATES[0]} -> {DATES[-1]} ({len(DATES)} days)")
    print("=" * 70)

    t_start = time.monotonic()
    results: list[dict] = []

    for i, date_str in enumerate(DATES, 1):
        date = datetime.date.fromisoformat(date_str)
        print()
        print("#" * 70)
        print(f"# Day {i}/{len(DATES)}: {date_str}")
        print("#" * 70)
        day_t0 = time.monotonic()
        status = "ok"
        err_msg = ""

        try:
            dome_snapshot.run(
                date=date,
                asset=ASSET,
                timeframe=TIMEFRAME,
                out_dir=OUT_DIR,
                hours=HOURS,
                force=False,
            )
        except KeyboardInterrupt:
            status = "interrupted"
            err_msg = "KeyboardInterrupt"
            _log({
                "ts": int(time.time()),
                "date": date_str,
                "status": status,
                "error": err_msg,
            })
            results.append({
                "date": date_str,
                "status": status,
                "markets": _count_markets_for_date(date_str),
                "elapsed_sec": time.monotonic() - day_t0,
            })
            break
        except Exception as exc:
            status = "error"
            err_msg = f"{type(exc).__name__}: {exc}"
            print(f"!! Day {date_str} failed: {err_msg}")
            traceback.print_exc()

        elapsed = time.monotonic() - day_t0
        n_markets = _count_markets_for_date(date_str)
        results.append({
            "date": date_str,
            "status": status,
            "markets": n_markets,
            "elapsed_sec": round(elapsed, 1),
        })
        _log({
            "ts": int(time.time()),
            "date": date_str,
            "status": status,
            "markets": n_markets,
            "elapsed_sec": round(elapsed, 1),
            "error": err_msg,
        })
        print(f"\n>> Day {date_str} done: status={status} markets={n_markets} elapsed={elapsed:.1f}s")

        if i < len(DATES):
            print(f">> Sleeping {SLEEP_BETWEEN_DAYS}s before next day...")
            time.sleep(SLEEP_BETWEEN_DAYS)

    total_elapsed = time.monotonic() - t_start

    # Aggregate summary
    total_markets = sum(r["markets"] for r in results)
    full_days = [r["date"] for r in results if r["markets"] >= 96]
    partial_days = [r for r in results if 0 < r["markets"] < 96]
    failed_days = [r["date"] for r in results if r["status"] != "ok"]

    # Disk usage
    total_bytes = 0
    for f in OUT_DIR.glob("btc-updown-15m-*.jsonl"):
        try:
            total_bytes += f.stat().st_size
        except OSError:
            pass

    summary = {
        "started_at": datetime.datetime.utcnow().isoformat() + "Z",
        "elapsed_sec": round(total_elapsed, 1),
        "elapsed_human": f"{int(total_elapsed // 60)}m{int(total_elapsed % 60)}s",
        "asset": ASSET,
        "timeframe": TIMEFRAME,
        "dates_requested": DATES,
        "results": results,
        "total_markets": total_markets,
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / 1024 / 1024, 2),
        "full_days": full_days,
        "partial_days": [{"date": p["date"], "markets": p["markets"]} for p in partial_days],
        "failed_days": failed_days,
    }

    SUMMARY_FILE.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print()
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(f"  Total elapsed   : {summary['elapsed_human']}")
    print(f"  Total markets   : {total_markets}")
    print(f"  Total disk      : {summary['total_mb']} MB")
    print(f"  Full days (96)  : {len(full_days)} -> {full_days}")
    print(f"  Partial days    : {len(partial_days)}")
    for p in partial_days:
        print(f"      {p['date']}: {p['markets']}/96")
    print(f"  Failed days     : {len(failed_days)} -> {failed_days}")
    print(f"  Summary written : {SUMMARY_FILE}")
    print("=" * 70)


if __name__ == "__main__":
    main()
