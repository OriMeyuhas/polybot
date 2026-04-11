"""
Dome API client for historical Polymarket / Binance / Chainlink data.

All caller-facing timestamps are EPOCH SECONDS.
The client converts to milliseconds for endpoints that require it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import time
from typing import Any

logger = logging.getLogger("dome_client")

# ---------------------------------------------------------------------------
# Optional HTTP backend — prefer httpx, fall back to requests, then urllib
# ---------------------------------------------------------------------------
try:
    import httpx as _httpx  # type: ignore
    _BACKEND = "httpx"
except ImportError:
    try:
        import requests as _requests  # type: ignore
        _BACKEND = "requests"
    except ImportError:
        import urllib.request as _urllib_request  # type: ignore
        import urllib.error as _urllib_error
        _BACKEND = "urllib"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class DomeAPIError(Exception):
    """Raised on non-2xx responses from the Dome API."""

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        super().__init__(f"DomeAPI {status_code} for {url!r}: {body[:200]}")


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------
_RETRYABLE = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_CAP_SLEEP = 8.0


def _backoff(attempt: int) -> float:
    """Exponential back-off capped at _CAP_SLEEP seconds."""
    return min(2 ** attempt, _CAP_SLEEP)


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------
class DomeClient:
    """HTTP client for the Dome historical-data API.

    Parameters
    ----------
    api_key:
        Bearer token.  Reads ``DOME_API_KEY`` env var when *None*.
    base_url:
        API root (no trailing slash).
    min_interval_sec:
        Minimum seconds between requests (polite rate limiting).
    cache_dir:
        If given, JSON responses are cached to disk by URL hash with *cache_ttl_sec* TTL.
    cache_ttl_sec:
        How long (seconds) a cached response is considered fresh. Default 24 h.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://api.domeapi.io",
        min_interval_sec: float = 0.10,
        cache_dir: pathlib.Path | None = None,
        cache_ttl_sec: int = 86_400,
    ) -> None:
        resolved_key = api_key or os.environ.get("DOME_API_KEY")
        if not resolved_key:
            raise ValueError(
                "Dome API key not found. Pass api_key= or set DOME_API_KEY env var."
            )
        self._api_key = resolved_key
        self._base_url = base_url.rstrip("/")
        self._min_interval_sec = min_interval_sec
        self._cache_dir = cache_dir
        self._cache_ttl_sec = cache_ttl_sec
        self._last_request_time: float = 0.0

        if cache_dir is not None:
            cache_dir.mkdir(parents=True, exist_ok=True)

        # Build a reusable session/client if possible
        if _BACKEND == "httpx":
            self._client = _httpx.Client(
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=30.0,
            )
        else:
            self._client = None  # per-request

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_market(self, market_slug: str) -> dict:
        """Return market metadata for the given slug."""
        return self._get("/v1/polymarket/markets", {"market_slug": market_slug})

    def get_candlesticks(
        self,
        condition_id: str,
        start_sec: int,
        end_sec: int,
        interval: str = "1m",
    ) -> list[dict]:
        """Return parsed candlestick list for *condition_id*.

        Dome's candlesticks endpoint uses epoch SECONDS — passed through as-is.
        The raw response is ``{"candlesticks": [[[{...}]]]}`` — we return the
        flat list of candle dicts for the first token (YES side by default).
        """
        raw = self._get(
            f"/v1/polymarket/candlesticks/{condition_id}",
            {"start_time": start_sec, "end_time": end_sec, "interval": interval},
        )
        # Shape: {"candlesticks": [ [[ candle, ... ]] ]}
        # Outer list = per-token, inner nested list = per-candle
        candles_per_token: list = raw.get("candlesticks", [])
        if not candles_per_token:
            return []
        # Flatten the innermost list for the first token (YES)
        yes_outer = candles_per_token[0]  # e.g. [[c1, c2, ...]]
        if yes_outer and isinstance(yes_outer[0], list):
            return yes_outer[0]
        return yes_outer

    def get_orderbook_snapshots(
        self,
        token_id: str,
        start_sec: int,
        end_sec: int,
    ) -> list[dict]:
        """Return orderbook snapshots for *token_id*.

        Dome's orderbooks endpoint uses epoch MILLISECONDS — converted internally.
        """
        raw = self._get(
            "/v1/polymarket/orderbooks",
            {
                "token_id": token_id,
                "start_time": start_sec * 1000,
                "end_time": end_sec * 1000,
            },
        )
        return raw.get("snapshots", [])

    def get_binance_prices(
        self,
        currency: str,
        start_sec: int,
        end_sec: int,
    ) -> list[dict]:
        """Return Binance price ticks for *currency* (e.g. ``btcusdt``).

        Endpoint uses epoch MILLISECONDS — converted internally.
        """
        raw = self._get(
            "/v1/crypto-prices/binance",
            {
                "currency": currency,
                "start_time": start_sec * 1000,
                "end_time": end_sec * 1000,
            },
        )
        return raw.get("prices", [])

    def get_chainlink_prices(
        self,
        currency: str,
        start_sec: int,
        end_sec: int,
    ) -> list[dict]:
        """Return Chainlink price ticks for *currency* (e.g. ``btc/usd``).

        Endpoint uses epoch MILLISECONDS — converted internally.
        """
        raw = self._get(
            "/v1/crypto-prices/chainlink",
            {
                "currency": currency,
                "start_time": start_sec * 1000,
                "end_time": end_sec * 1000,
            },
        )
        return raw.get("prices", [])

    def get_wallet_pnl(
        self,
        wallet_address: str,
        start_sec: int | None = None,
        end_sec: int | None = None,
    ) -> dict:
        """Return realized PnL for a wallet address."""
        params: dict[str, Any] = {}
        if start_sec is not None:
            params["start_time"] = start_sec * 1000
        if end_sec is not None:
            params["end_time"] = end_sec * 1000
        return self._get(f"/v1/polymarket/wallet/pnl/{wallet_address}", params)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> dict:
        """Execute a GET, with retry, cache, and rate-limit guard."""
        url = self._build_url(path, params or {})

        # --- cache check ---
        if self._cache_dir is not None:
            cached = self._cache_load(url)
            if cached is not None:
                logger.debug("cache hit: %s", url)
                return cached

        # --- rate-limit sleep ---
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < self._min_interval_sec:
            time.sleep(self._min_interval_sec - elapsed)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            logger.debug("GET %s (attempt %d)", url, attempt)
            try:
                status, body, _ = self._http_get(url)
            except Exception as exc:
                logger.warning("Request error %s: %s", url, exc)
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    time.sleep(_backoff(attempt))
                continue

            self._last_request_time = time.monotonic()

            if status == 200:
                data = json.loads(body)
                if self._cache_dir is not None:
                    self._cache_save(url, data)
                return data

            if status in _RETRYABLE and attempt < _MAX_RETRIES:
                sleep_for = _backoff(attempt)
                logger.warning(
                    "Dome API %d on %s — retrying in %.1fs (attempt %d/%d)",
                    status, url, sleep_for, attempt + 1, _MAX_RETRIES,
                )
                time.sleep(sleep_for)
                continue

            raise DomeAPIError(status, body, url)

        if last_exc is not None:
            raise last_exc
        raise DomeAPIError(0, "Max retries exceeded", url)

    def _http_get(self, url: str) -> tuple[int, str, dict]:
        """Return (status_code, body_text, headers) using the available backend."""
        if _BACKEND == "httpx":
            resp = self._client.get(url)  # type: ignore[union-attr]
            return resp.status_code, resp.text, dict(resp.headers)

        if _BACKEND == "requests":
            resp = _requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=30)
            return resp.status_code, resp.text, dict(resp.headers)

        # urllib fallback
        req = _urllib_request.Request(
            url, headers={"Authorization": f"Bearer {self._api_key}"}
        )
        try:
            with _urllib_request.urlopen(req, timeout=30) as r:
                body = r.read().decode()
                return r.status, body, dict(r.headers)
        except _urllib_error.HTTPError as exc:
            body = exc.read().decode()
            return exc.code, body, {}

    def _build_url(self, path: str, params: dict) -> str:
        from urllib.parse import urlencode
        base = f"{self._base_url}{path}"
        if params:
            return f"{base}?{urlencode(params)}"
        return base

    # ------------------------------------------------------------------
    # Disk cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, url: str) -> pathlib.Path:
        digest = hashlib.sha256(url.encode()).hexdigest()[:16]
        return self._cache_dir / f"{digest}.json"  # type: ignore[operator]

    def _cache_load(self, url: str) -> dict | None:
        p = self._cache_key(url)
        if not p.exists():
            return None
        try:
            payload = json.loads(p.read_text())
            if time.time() - payload["_saved_at"] > self._cache_ttl_sec:
                return None
            return payload["data"]
        except Exception:
            return None

    def _cache_save(self, url: str, data: dict) -> None:
        p = self._cache_key(url)
        try:
            p.write_text(json.dumps({"_saved_at": time.time(), "data": data}))
        except Exception as exc:
            logger.debug("Cache write failed: %s", exc)

    def close(self) -> None:
        """Close the underlying HTTP session (if any)."""
        if _BACKEND == "httpx" and self._client is not None:
            self._client.close()

    def __enter__(self) -> "DomeClient":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
