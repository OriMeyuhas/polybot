"""Market discovery: find active crypto up/down markets and extract token IDs."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from polybot.types import MarketWindow

logger = logging.getLogger(__name__)

_ASSET_KEYWORDS = {
    "bitcoin": "BTC",
    "btc": "BTC",
    "ethereum": "ETH",
    "eth": "ETH",
    "solana": "SOL",
    "sol": "SOL",
    "xrp": "XRP",
}


def _extract_asset(text: str) -> str | None:
    text_lower = text.lower()
    for keyword, symbol in _ASSET_KEYWORDS.items():
        if keyword in text_lower:
            return symbol
    return None


def is_crypto_updown_market(market: dict, assets: tuple[str, ...]) -> bool:
    question = market.get("question", "").lower()
    if "up" not in question or "down" not in question:
        return False
    asset = _extract_asset(question)
    if asset is None or asset not in assets:
        return False
    tokens = market.get("tokens", [])
    outcomes = {t.get("outcome", "").lower() for t in tokens}
    return "up" in outcomes and "down" in outcomes


def _parse_iso_epoch(iso_str: str) -> int:
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (ValueError, AttributeError):
        return 0


def parse_market_to_window(market: dict, slug: str) -> MarketWindow | None:
    question = market.get("question", "")
    asset = _extract_asset(question)
    if asset is None:
        return None

    tokens = market.get("tokens", [])
    up_token = None
    dn_token = None
    for t in tokens:
        outcome = t.get("outcome", "").lower()
        if outcome == "up":
            up_token = t.get("token_id", "")
        elif outcome == "down":
            dn_token = t.get("token_id", "")

    if not up_token or not dn_token:
        return None

    open_epoch = _parse_iso_epoch(market.get("game_start_time", ""))
    close_epoch = _parse_iso_epoch(market.get("end_date_iso", ""))
    timeframe_sec = close_epoch - open_epoch if close_epoch > open_epoch else 900

    return MarketWindow(
        market_id=slug,
        condition_id=market.get("condition_id", ""),
        asset=asset,
        timeframe_sec=timeframe_sec,
        up_token_id=up_token,
        dn_token_id=dn_token,
        open_epoch=open_epoch,
        close_epoch=close_epoch,
    )


def _discover_sync(client, assets: tuple[str, ...]) -> list[MarketWindow]:
    """Synchronous helper — runs in a thread to avoid blocking the event loop."""
    windows = []
    markets_resp = client.get_markets()
    markets = markets_resp.get("data", []) if isinstance(markets_resp, dict) else []
    for m in markets:
        if not is_crypto_updown_market(m, assets):
            continue
        slug = m.get("question_id", m.get("condition_id", ""))
        mw = parse_market_to_window(m, slug)
        if mw is not None:
            windows.append(mw)
    return windows


async def discover_active_markets(
    client,
    assets: tuple[str, ...],
) -> list[MarketWindow]:
    import asyncio
    try:
        return await asyncio.to_thread(_discover_sync, client, assets)
    except Exception as e:
        logger.error("Market discovery failed: %s", e)
        return []
