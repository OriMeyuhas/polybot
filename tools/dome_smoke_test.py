"""
Dome API smoke test script.

Checks:
1. Basic connectivity: fetch a known market
2. Rate limit probe: 10 back-to-back requests, report throttling
3. Rate limit headers: extract X-RateLimit-* from responses
4. Historical depth: Binance BTC prices at 7d, 30d, 90d, 180d ago

Run:
    python tools/dome_smoke_test.py
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import datetime
import logging

# Make tools/ importable
_TOOLS_DIR = pathlib.Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))

from dome_client import DomeClient, DomeAPIError

# Show INFO logs from the client during smoke test
logging.basicConfig(level=logging.WARNING)

# Known good slug for probing
_PROBE_SLUG = "btc-updown-15m-1775924100"

SEP = "-" * 60


def _now_sec() -> int:
    return int(time.time())


def _days_ago(n: int) -> int:
    return _now_sec() - n * 86_400


def section(title: str) -> None:
    print()
    print(SEP)
    print(f"  {title}")
    print(SEP)


# ---------------------------------------------------------------------------
# 1. Basic connectivity
# ---------------------------------------------------------------------------
def test_basic_connectivity(client: DomeClient) -> str | None:
    section("1. Basic connectivity — GET known market")
    try:
        t0 = time.monotonic()
        data = client.get_market(_PROBE_SLUG)
        elapsed_ms = (time.monotonic() - t0) * 1000

        # Extract condition_id regardless of nesting
        market_obj = data
        if "markets" in data:
            # Shape: {"markets": [...], "pagination": {...}}
            markets_list = data["markets"]
            market_obj = markets_list[0] if markets_list else {}
        elif "market" in data:
            market_obj = data["market"]
        elif isinstance(data, list) and data:
            market_obj = data[0]

        condition_id = market_obj.get("condition_id", "(not found)")
        print(f"  OK  slug={_PROBE_SLUG}")
        print(f"      condition_id={condition_id}")
        print(f"      latency={elapsed_ms:.0f}ms")
        print(f"      response_keys={list(market_obj.keys())[:8]}")
        return condition_id
    except DomeAPIError as exc:
        print(f"  FAIL  HTTP {exc.status_code}: {exc.body[:200]}")
        return None
    except Exception as exc:
        print(f"  FAIL  {exc}")
        return None


# ---------------------------------------------------------------------------
# 2. Rate limit probe — 10 back-to-back requests
# ---------------------------------------------------------------------------
def test_rate_limit_probe(client: DomeClient) -> dict:
    section("2. Rate limit probe — 10 back-to-back requests")
    results = []
    throttled = 0
    rate_limit_headers: dict = {}
    original_min_interval = client._min_interval_sec
    client._min_interval_sec = 0  # fire as fast as possible

    for i in range(10):
        try:
            t0 = time.monotonic()
            # Call _http_get directly so we can see headers
            url = client._build_url("/v1/polymarket/markets", {"market_slug": _PROBE_SLUG})
            status, body, headers = client._http_get(url)
            elapsed_ms = (time.monotonic() - t0) * 1000

            # Capture rate limit headers on first response with them
            if not rate_limit_headers:
                for h in headers:
                    if h.lower().startswith("x-ratelimit"):
                        rate_limit_headers[h] = headers[h]

            if status == 429:
                throttled += 1
                results.append(f"  req {i+1:2d}: THROTTLED (429)  {elapsed_ms:.0f}ms")
            else:
                results.append(f"  req {i+1:2d}: OK ({status})  {elapsed_ms:.0f}ms")
        except Exception as exc:
            results.append(f"  req {i+1:2d}: ERROR {exc}")

    client._min_interval_sec = original_min_interval

    for r in results:
        print(r)
    print()
    print(f"  Throttled: {throttled}/10")
    return rate_limit_headers


# ---------------------------------------------------------------------------
# 3. Rate limit headers
# ---------------------------------------------------------------------------
def test_rate_limit_headers(client: DomeClient, captured_headers: dict) -> None:
    section("3. Rate limit headers from responses")
    if not captured_headers:
        # Try one fresh request and look at headers directly
        url = client._build_url("/v1/polymarket/markets", {"market_slug": _PROBE_SLUG})
        try:
            _, _, headers = client._http_get(url)
            for h in headers:
                if h.lower().startswith("x-ratelimit") or h.lower().startswith("ratelimit"):
                    captured_headers[h] = headers[h]
        except Exception:
            pass

    KNOWN_HEADERS = [
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "RateLimit-Reset",
        "Retry-After",
    ]
    found_any = False
    # Case-insensitive lookup
    headers_lower = {k.lower(): v for k, v in captured_headers.items()}
    for h in KNOWN_HEADERS:
        val = headers_lower.get(h.lower())
        if val is not None:
            print(f"  {h}: {val}")
            found_any = True
    if not found_any:
        print("  (no rate-limit headers found in responses)")
        # Print all headers for debugging
        if captured_headers:
            print("  All response headers captured:")
            for k, v in captured_headers.items():
                print(f"    {k}: {v}")


# ---------------------------------------------------------------------------
# 4. Historical depth test
# ---------------------------------------------------------------------------
def test_historical_depth(client: DomeClient) -> None:
    section("4. Historical depth — Binance BTC prices")
    depths = [
        (7, "7 days ago"),
        (30, "30 days ago"),
        (90, "90 days ago"),
        (180, "180 days ago"),
    ]
    for days, label in depths:
        start_sec = _days_ago(days)
        end_sec = start_sec + 60  # 1-minute window
        try:
            t0 = time.monotonic()
            prices = client.get_binance_prices("btcusdt", start_sec, end_sec)
            elapsed_ms = (time.monotonic() - t0) * 1000
            dt_str = datetime.datetime.utcfromtimestamp(start_sec).strftime("%Y-%m-%d %H:%M")
            if prices:
                first_price = prices[0].get("value", "?")
                print(
                    f"  OK   {label:12s}  ({dt_str} UTC)  "
                    f"ticks={len(prices)}  first_price={first_price}  {elapsed_ms:.0f}ms"
                )
            else:
                print(
                    f"  EMPTY {label:12s}  ({dt_str} UTC)  "
                    f"(0 ticks)  {elapsed_ms:.0f}ms"
                )
        except DomeAPIError as exc:
            dt_str = datetime.datetime.utcfromtimestamp(start_sec).strftime("%Y-%m-%d")
            print(
                f"  FAIL  {label:12s}  ({dt_str})  HTTP {exc.status_code}: {exc.body[:100]}"
            )
        except Exception as exc:
            print(f"  FAIL  {label:12s}  {exc}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    print()
    print("=" * 60)
    print("  Dome API Smoke Test")
    print(f"  Date: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print("=" * 60)

    api_key = os.environ.get("DOME_API_KEY")
    if not api_key:
        # Try loading from .env in the project root
        env_path = pathlib.Path(__file__).parent.parent / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DOME_API_KEY="):
                    api_key = line.split("=", 1)[1].strip()
                    os.environ["DOME_API_KEY"] = api_key
                    break

    if not api_key:
        print("ERROR: DOME_API_KEY not set. Set env var or add to .env")
        sys.exit(1)

    print(f"  API key: {api_key[:8]}...{api_key[-4:]}")

    with DomeClient(min_interval_sec=0.05) as client:
        condition_id = test_basic_connectivity(client)
        rl_headers = test_rate_limit_probe(client)
        test_rate_limit_headers(client, rl_headers)
        test_historical_depth(client)

    print()
    print(SEP)
    print("  Smoke test complete")
    print(SEP)


if __name__ == "__main__":
    main()
