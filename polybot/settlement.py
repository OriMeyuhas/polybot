"""Shared settlement resolution helpers.

Used by both the tracker and the live bot to determine whether a
Polymarket up/down market has been resolved (and to which outcome).
"""

import json
import logging

import httpx

log = logging.getLogger(__name__)


async def resolve_via_clob(
    client: httpx.AsyncClient,
    clob_host: str,
    condition_id: str,
) -> dict | None:
    """Try the CLOB API: GET /markets/{condition_id}.

    Returns a dict with ``outcome`` ("UP" or "DOWN") and ``settlement_price``
    if the market is resolved, otherwise *None*.
    """
    url = f"{clob_host}/markets/{condition_id}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    # The CLOB response typically has a `tokens` array and a boolean
    # `resolved` (or the tokens carry a `winner` flag).
    if data.get("resolved") or data.get("closed"):
        tokens = data.get("tokens", [])
        for tok in tokens:
            if tok.get("winner") is True or str(tok.get("winner")).lower() == "true":
                outcome = tok.get("outcome", "").upper()
                if outcome in ("UP", "DOWN", "YES", "NO"):
                    return {"outcome": outcome, "settlement_price": 1.0}

        # Fallback: look for a top-level `winner` field
        winner = data.get("winner")
        if winner:
            return {"outcome": str(winner).upper(), "settlement_price": 1.0}

    return None


async def resolve_via_gamma(
    client: httpx.AsyncClient,
    slug: str,
) -> dict | None:
    """Fallback: query gamma-api events endpoint by slug.

    Returns ``{"outcome": "UP"/"DOWN", "settlement_price": 1.0}`` if resolved,
    otherwise *None*.  Also works as a condition_id lookup.
    """
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    resp = await client.get(url, timeout=10)
    resp.raise_for_status()
    payload = resp.json()

    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        markets = event.get("markets", [event])
        for mkt in markets:
            if not (mkt.get("resolved") or mkt.get("closed")):
                continue

            # Method 1: outcomePrices + outcomes (gamma format)
            outcomes_raw = mkt.get("outcomes", "[]")
            prices_raw = mkt.get("outcomePrices", "[]")
            if isinstance(outcomes_raw, str):
                try:
                    outcomes_list = json.loads(outcomes_raw)
                    prices_list = json.loads(prices_raw)
                    for outcome, price in zip(outcomes_list, prices_list):
                        if str(price) == "1":
                            return {"outcome": str(outcome).upper(), "settlement_price": 1.0}
                except (ValueError, TypeError):
                    pass

            # Method 2: winner field
            winner = mkt.get("winner") or mkt.get("outcome")
            if winner:
                return {"outcome": str(winner).upper(), "settlement_price": 1.0}

    return None


async def fetch_condition_id(
    client: httpx.AsyncClient,
    slug: str,
) -> str:
    """Look up the real condition_id from gamma-api by slug. Returns empty string on failure."""
    try:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        resp = await client.get(url, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        events = payload if isinstance(payload, list) else [payload]
        for event in events:
            markets = event.get("markets", [event])
            for mkt in markets:
                cid = mkt.get("conditionId", mkt.get("condition_id", ""))
                if cid:
                    return cid
    except Exception as exc:
        log.debug("Failed to fetch condition_id from gamma for %s: %s", slug, exc)
    return ""


async def try_resolve_once(
    client: httpx.AsyncClient,
    clob_host: str,
    slug: str,
    condition_id: str,
) -> dict | None:
    """Single non-blocking resolution attempt.

    Returns ``{"outcome": "UP"/"DOWN", "settlement_price": 1.0}`` on success
    or *None* if the market hasn't been resolved yet.
    """
    # If condition_id is missing or looks like a slug (not a hex hash), fetch from gamma
    if not condition_id or not condition_id.startswith("0x"):
        fetched = await fetch_condition_id(client, slug)
        if fetched:
            log.info("Fetched condition_id from gamma for %s: %s", slug, fetched)
            condition_id = fetched

    # --- primary: CLOB API (only if we have a real condition_id) ---
    if condition_id and condition_id.startswith("0x"):
        try:
            result = await resolve_via_clob(client, clob_host, condition_id)
            if result is not None:
                if condition_id:
                    result["condition_id"] = condition_id
                return result
        except Exception as exc:
            log.warning("CLOB resolution failed for %s, falling back to Gamma: %s", slug, exc)

    # --- fallback: Gamma API ---
    try:
        result = await resolve_via_gamma(client, slug)
        if result is not None:
            if condition_id:
                result["condition_id"] = condition_id
            return result
    except Exception as exc:
        log.warning("Gamma resolution also failed for %s: %s", slug, exc)

    return None
