"""Gamma API market discovery — finds crypto up/down prediction markets."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from polybot.types import MarketWindow

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Slug patterns for crypto up/down markets (match polytrader format)
CRYPTO_SLUG_PATTERNS = [
    "btc-updown-5m-",
    "btc-updown-15m-",
    "eth-updown-5m-",
    "eth-updown-15m-",
    "sol-updown-5m-",
    "sol-updown-15m-",
    "xrp-updown-5m-",
    "xrp-updown-15m-",
]

# Maps slug prefix to canonical asset name
ASSET_FROM_SLUG = {
    "btc": "BTC",
    "eth": "ETH",
    "sol": "SOL",
    "xrp": "XRP",
}


@dataclass
class MarketInfo:
    condition_id: str
    question: str
    slug: str
    clob_token_ids: list[str]
    outcomes: list[str]
    event_start_iso: str
    end_date_iso: str
    price_to_beat: str
    active: bool
    liquidity: float


def _parse_iso(iso_str: str) -> int:
    """Parse ISO 8601 datetime to epoch seconds."""
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0


def _detect_asset(slug: str) -> str | None:
    """Detect crypto asset from slug prefix (e.g. 'btc-updown-5m-...' -> 'BTC')."""
    slug_lower = slug.lower()
    for prefix, asset in ASSET_FROM_SLUG.items():
        if slug_lower.startswith(prefix + "-"):
            return asset
    return None


# Timeframe string -> seconds
_TIMEFRAME_MAP = {"5m": 300, "15m": 900, "1h": 3600}

def parse_slug_timing(slug: str) -> tuple[str, int, int, int] | None:
    """Parse crypto up/down slug to extract (asset, timeframe_sec, open_epoch, close_epoch).

    Handles two slug formats:
      - btc-updown-5m-1773942300  (epoch suffix)
      - btc-updown-15m-2026-03-19 (date suffix)

    Returns None if slug doesn't match the crypto up/down pattern.
    """
    m = re.match(
        r"^([a-z]+)-updown-(\d+[mh])-(.+)$", slug.lower()
    )
    if not m:
        return None

    asset_lower, tf_str, suffix = m.groups()
    asset = ASSET_FROM_SLUG.get(asset_lower)
    if not asset:
        return None

    timeframe_sec = _TIMEFRAME_MAP.get(tf_str)
    if not timeframe_sec:
        return None

    # Try epoch first
    try:
        open_epoch = int(suffix)
        return (asset, timeframe_sec, open_epoch, open_epoch + timeframe_sec)
    except ValueError:
        pass

    # Try date format (YYYY-MM-DD)
    try:
        dt = datetime.strptime(suffix, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        open_epoch = int(dt.timestamp())
        return (asset, timeframe_sec, open_epoch, open_epoch + timeframe_sec)
    except ValueError:
        return None


def _parse_json_field(raw, default=None):
    """Parse a field that might be a JSON string or already a list."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default if default is not None else []
    return raw if raw is not None else (default if default is not None else [])


def to_market_window(info: MarketInfo, asset: str) -> MarketWindow:
    """Convert MarketInfo (Gamma format) to MarketWindow (PolyBot strategy format)."""
    # Map Up/Down (or Yes/No) outcomes to token IDs
    up_idx = -1
    dn_idx = -1
    for i, outcome in enumerate(info.outcomes):
        label = outcome.lower().strip()
        if label in ("up", "yes"):
            up_idx = i
        elif label in ("down", "no"):
            dn_idx = i

    # Fallback: first=Up, second=Down
    if up_idx == -1:
        up_idx = 0
    if dn_idx == -1:
        dn_idx = 1 if len(info.outcomes) > 1 else 0

    up_token = info.clob_token_ids[up_idx] if up_idx < len(info.clob_token_ids) else ""
    dn_token = info.clob_token_ids[dn_idx] if dn_idx < len(info.clob_token_ids) else ""

    open_epoch = _parse_iso(info.event_start_iso)
    close_epoch = _parse_iso(info.end_date_iso)
    timeframe_sec = close_epoch - open_epoch if close_epoch > open_epoch else 0

    return MarketWindow(
        market_id=info.slug,
        condition_id=info.condition_id,
        asset=asset,
        timeframe_sec=timeframe_sec,
        up_token_id=up_token,
        dn_token_id=dn_token,
        open_epoch=open_epoch,
        close_epoch=close_epoch,
    )


CLOB_API = "https://clob.polymarket.com"


def _filter_market_from_event(
    market: dict,
    event: dict,
    patterns: list[str],
    now_epoch: int,
    max_hours: float = 2.0,
    min_liquidity: float = 50.0,
) -> MarketInfo | None:
    """Validate a single market dict and return MarketInfo or None.

    Logs a DEBUG message for every rejection reason so operators can diagnose
    why specific markets are being skipped.
    """
    slug = market.get("slug", "") or market.get("conditionId", "")
    if not slug:
        logger.debug("skip market: no slug or conditionId")
        return None

    # Match against slug patterns
    matched = False
    for pattern in patterns:
        prefix = pattern.replace("*", "")
        if prefix in slug:
            matched = True
            break
    if not matched:
        logger.debug("skip %s: slug does not match any pattern", slug)
        return None

    # Validate asset
    asset = _detect_asset(slug)
    if not asset:
        logger.debug("skip %s: cannot detect asset from slug", slug)
        return None

    # Parse JSON string fields
    token_ids = _parse_json_field(
        market.get("clobTokenIds", market.get("clob_token_ids"))
    )
    outcomes = _parse_json_field(
        market.get("outcomes"), default=["Up", "Down"]
    )

    if len(token_ids) < 2:
        logger.debug("skip %s: insufficient token IDs (%d)", slug, len(token_ids))
        return None
    if len(outcomes) < 2:
        logger.debug("skip %s: insufficient outcomes (%d)", slug, len(outcomes))
        return None

    # Time filter: use slug-derived timing as PRIMARY, fall back to API endDate
    slug_timing = parse_slug_timing(slug)
    if slug_timing:
        _, _, _, close_epoch = slug_timing
        hours_left = (close_epoch - now_epoch) / 3600
        end_iso = datetime.fromtimestamp(close_epoch, tz=timezone.utc).isoformat()
    else:
        end_iso = market.get("endDate") or event.get("endDate") or ""
        if not end_iso:
            logger.debug("skip %s: no end date available (slug parse failed, no API endDate)", slug)
            return None
        close_epoch = _parse_iso(end_iso)
        if close_epoch == 0:
            logger.debug("skip %s: cannot parse endDate '%s'", slug, end_iso)
            return None
        hours_left = (close_epoch - now_epoch) / 3600

    if hours_left <= 0:
        logger.debug("skip %s: already expired (hours_left=%.2f)", slug, hours_left)
        return None
    if hours_left > max_hours:
        logger.debug("skip %s: too far out (hours_left=%.2f > max %.1f)", slug, hours_left, max_hours)
        return None

    # Liquidity check
    liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)
    if liquidity < min_liquidity:
        logger.debug("skip %s: low liquidity (%.1f < %.1f)", slug, liquidity, min_liquidity)
        return None

    start_iso = (
        market.get("eventStartTime")
        or event.get("startTime")
        or event.get("startDate")
        or ""
    )

    return MarketInfo(
        condition_id=market.get("conditionId", market.get("condition_id", "")),
        question=market.get("question", ""),
        slug=slug,
        clob_token_ids=[str(t) for t in token_ids],
        outcomes=[str(o) for o in outcomes],
        event_start_iso=start_iso,
        end_date_iso=end_iso,
        price_to_beat=str(market.get("priceToBeat", market.get("price_to_beat", "0"))),
        active=market.get("active", True),
        liquidity=liquidity,
    )


async def discover_crypto_updown_markets(
    gamma_host: str = GAMMA_API,
    clob_host: str = CLOB_API,
    slug_patterns: list[str] | None = None,
    max_hours_to_resolution: float = 2,
    min_liquidity: float = 50,
) -> list[tuple[MarketInfo, str]]:
    """Fetch active crypto up/down markets from Gamma API.

    Uses tag_slug=up-or-down to find the short-term crypto price markets,
    then filters by slug pattern and time to resolution.  Falls back to
    CLOB API if Gamma returns zero matching results.

    Returns list of (MarketInfo, asset) tuples.
    """
    patterns = slug_patterns or CRYPTO_SLUG_PATTERNS
    results: list[tuple[MarketInfo, str]] = []
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())

    # Build date window params (harmlessly ignored if API doesn't support them)
    window_start = now.isoformat()
    window_end = (now + timedelta(hours=max_hours_to_resolution)).isoformat()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{gamma_host}/events",
                params={
                    "tag_slug": "up-or-down",
                    "closed": "false",
                    "active": "true",
                    "limit": "500",
                    "end_date_window_start": window_start,
                    "end_date_window_end": window_end,
                },
            )
            if resp.status_code != 200:
                logger.error("Gamma API returned %d", resp.status_code)
                return results

            events = resp.json()
            seen_slugs: set[str] = set()

            for event in events:
                for market in event.get("markets", []):
                    slug = market.get("slug", "") or market.get("conditionId", "")
                    if slug in seen_slugs:
                        continue
                    seen_slugs.add(slug)

                    info = _filter_market_from_event(
                        market,
                        event=event,
                        patterns=patterns,
                        now_epoch=now_epoch,
                        max_hours=max_hours_to_resolution,
                        min_liquidity=min_liquidity,
                    )
                    if info is not None:
                        asset = _detect_asset(info.slug)
                        if asset:
                            results.append((info, asset))

            # CLOB API fallback when Gamma returns 0 matching results
            if not results:
                logger.info("Gamma returned 0 matches; trying CLOB API fallback")
                try:
                    clob_resp = await client.get(
                        f"{clob_host}/markets",
                        params={"limit": "500"},
                    )
                    if clob_resp.status_code == 200:
                        clob_markets = clob_resp.json()
                        if isinstance(clob_markets, dict):
                            clob_markets = clob_markets.get("data", clob_markets.get("markets", []))
                        for cm in clob_markets:
                            slug = cm.get("slug") or cm.get("market_slug") or cm.get("condition_id", "")
                            if slug in seen_slugs:
                                continue
                            seen_slugs.add(slug)

                            info = _filter_market_from_event(
                                cm,
                                event={},
                                patterns=patterns,
                                now_epoch=now_epoch,
                                max_hours=max_hours_to_resolution,
                                min_liquidity=min_liquidity,
                            )
                            if info is not None:
                                asset = _detect_asset(info.slug)
                                if asset:
                                    results.append((info, asset))
                    else:
                        logger.warning("CLOB API fallback returned %d", clob_resp.status_code)
                except Exception as clob_err:
                    logger.warning("CLOB API fallback failed: %s", clob_err)

    except Exception as e:
        logger.error("Gamma API discovery failed: %s", e)
        raise  # Don't swallow — spec says errors propagate

    # Sort by time to resolution (soonest first)
    def _hours_left(item: tuple[MarketInfo, str]) -> float:
        try:
            iso = item[0].end_date_iso
            if not iso:
                return float("inf")
            end_dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            return (end_dt - now).total_seconds() / 3600
        except (ValueError, TypeError):
            return float("inf")

    results.sort(key=_hours_left)
    logger.info("Discovery found %d crypto up/down markets", len(results))
    return results
