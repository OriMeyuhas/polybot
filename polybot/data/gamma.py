"""Gamma API market discovery — finds crypto up/down prediction markets."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from polybot.types import MarketWindow

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"

# Slug patterns for crypto up/down markets
CRYPTO_SLUG_PATTERNS = [
    "btc", "bitcoin",
    "eth", "ethereum",
    "sol", "solana",
    "xrp", "ripple",
]

# Maps slug substrings to canonical asset names
ASSET_FROM_SLUG = {
    "btc": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL",
    "xrp": "XRP", "ripple": "XRP",
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
    """Detect crypto asset from slug string."""
    slug_lower = slug.lower()
    for pattern, asset in ASSET_FROM_SLUG.items():
        if pattern in slug_lower:
            return asset
    return None


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


def _is_crypto_updown(slug: str) -> bool:
    """Check if slug matches a crypto up/down market."""
    slug_lower = slug.lower()
    return any(p in slug_lower for p in CRYPTO_SLUG_PATTERNS) and (
        "up" in slug_lower or "down" in slug_lower or "updown" in slug_lower
    )


async def discover_crypto_updown_markets(
    gamma_host: str = GAMMA_API,
) -> list[tuple[MarketInfo, str]]:
    """Fetch active crypto up/down markets from Gamma API.

    Returns list of (MarketInfo, asset) tuples.
    """
    results: list[tuple[MarketInfo, str]] = []
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{gamma_host}/events",
                params={"active": "true", "closed": "false", "limit": "100"},
            )
            if resp.status_code != 200:
                logger.error("Gamma API returned %d", resp.status_code)
                return results

            events = resp.json()
            for event in events:
                for market in event.get("markets", []):
                    slug = market.get("slug", "") or market.get("conditionId", "")
                    if not _is_crypto_updown(slug):
                        continue

                    asset = _detect_asset(slug)
                    if not asset:
                        continue

                    token_ids = []
                    outcomes = []
                    for token in market.get("clobTokenIds", market.get("clob_token_ids", [])):
                        token_ids.append(str(token))
                    for outcome in market.get("outcomes", []):
                        outcomes.append(str(outcome))

                    if len(token_ids) < 2 or len(outcomes) < 2:
                        continue

                    info = MarketInfo(
                        condition_id=market.get("conditionId", market.get("condition_id", "")),
                        question=market.get("question", ""),
                        slug=slug,
                        clob_token_ids=token_ids,
                        outcomes=outcomes,
                        event_start_iso=event.get("startDate", event.get("start_date", "")),
                        end_date_iso=event.get("endDate", market.get("endDate", market.get("end_date_iso", ""))),
                        price_to_beat=str(market.get("priceToBeat", market.get("price_to_beat", "0"))),
                        active=market.get("active", True),
                        liquidity=float(market.get("liquidity", 0)),
                    )
                    results.append((info, asset))

    except Exception as e:
        logger.error("Gamma API discovery failed: %s", e)
        raise  # Don't swallow — spec says errors propagate

    return results
