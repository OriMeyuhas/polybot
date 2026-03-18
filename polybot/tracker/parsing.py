import re
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Slug Parsing
# ---------------------------------------------------------------------------
# Map full names that appear in slugs to ticker symbols
_ASSET_NORMALIZE = {
    "bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL",
    "xrp": "XRP", "btc": "BTC", "eth": "ETH", "sol": "SOL",
    "ftse": "FTSE", "spx": "SPX", "ndx": "NDX",
}


def _normalize_asset(raw: str) -> str:
    return _ASSET_NORMALIZE.get(raw.lower(), raw.upper())


def parse_slug(slug: str) -> dict:
    """
    Parse a Polymarket event slug. Handles two known formats:

    Format 1 (intraday):  btc-updown-15m-1773756900
    Format 2 (hourly):    xrp-up-or-down-march-17-2026-10am-et
                          bitcoin-up-or-down-march-17-2026-10am-et

    Returns {'asset', 'timeframe', 'window_start_epoch'}.
    """
    # Format 1: {asset}-updown-{Nm|Nh}-{epoch}
    m = re.match(r"^([a-z0-9]+)-updown-(\d+[mh])-(\d+)$", slug)
    if m:
        return {
            "asset": _normalize_asset(m.group(1)),
            "timeframe": m.group(2),
            "window_start_epoch": int(m.group(3)),
        }

    # Format 2: {asset}-up-or-down-{month}-{day}-{year}-{hour}am/pm-et
    # e.g. xrp-up-or-down-march-17-2026-10am-et
    m2 = re.match(
        r"^([a-z0-9]+)-up-or-down-([a-z]+-\d+-\d+-\d+[ap]m(?:-\d+[ap]m)?)-et$",
        slug,
    )
    if m2:
        asset = m2.group(1).upper()
        time_part = m2.group(2)  # e.g. march-17-2026-10am or march-17-2026-10am-11am

        # Check if it's a range (contains two time tokens) → infer timeframe
        times = re.findall(r"(\d+)([ap]m)", time_part)
        if len(times) == 2:
            # Two times → compute window duration
            def to_minutes(hour_str, ampm):
                h = int(hour_str)
                if ampm == "pm" and h != 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                return h * 60
            start_min = to_minutes(*times[0])
            end_min = to_minutes(*times[1])
            diff = (end_min - start_min) % (24 * 60)
            if diff == 60:
                tf = "1h"
            elif diff == 30:
                tf = "30m"
            elif diff == 15:
                tf = "15m"
            else:
                tf = f"{diff}m"
        else:
            # Single hour token → assume 1h window
            tf = "1h"

        # Compute window_start_epoch from date+hour in slug
        # time_part format: "march-18-2026-4am" or "march-18-2026-10am-11am"
        date_parts = time_part.split("-")
        window_start = 0
        try:
            month_name = date_parts[0]
            day = int(date_parts[1])
            year = int(date_parts[2])
            # First time token is window start
            start_hour_raw = times[0] if times else re.findall(r"(\d+)([ap]m)", time_part)
            if start_hour_raw:
                h, ampm = start_hour_raw if isinstance(start_hour_raw, tuple) else start_hour_raw[0]
                h = int(h)
                if ampm == "pm" and h != 12:
                    h += 12
                elif ampm == "am" and h == 12:
                    h = 0
                month = _MONTH_NAMES.get(month_name.lower(), 0)
                if month:
                    window_start = _et_to_utc_epoch(year, month, day, h)
        except (IndexError, ValueError):
            window_start = 0

        return {"asset": _normalize_asset(asset), "timeframe": tf, "window_start_epoch": window_start}

    return {"asset": "UNKNOWN", "timeframe": "?", "window_start_epoch": 0}


def parse_title_fallback(title: str) -> dict:
    """Extract asset and timeframe from title when slug parsing fails."""
    result = {"asset": "UNKNOWN", "timeframe": "?", "window_start_epoch": 0}

    title_lower = title.lower()
    for keyword, symbol in _ASSET_NORMALIZE.items():
        if keyword in title_lower:
            result["asset"] = symbol
            break

    # "10:15AM-10:30AM" → 15m window
    range_match = re.search(
        r"(\d{1,2}):(\d{2})[AP]M\s*-\s*(\d{1,2}):(\d{2})[AP]M", title, re.IGNORECASE
    )
    if range_match:
        start_min = int(range_match.group(1)) * 60 + int(range_match.group(2))
        end_min = int(range_match.group(3)) * 60 + int(range_match.group(4))
        diff = (end_min - start_min) % (24 * 60)
        if diff > 0:
            result["timeframe"] = f"{diff}m" if diff < 60 else f"{diff // 60}h"
        return result

    # "10AM ET" with no range → 1h
    if re.search(r"\d{1,2}[AP]M\s+ET", title, re.IGNORECASE):
        result["timeframe"] = "1h"

    return result


_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _et_to_utc_epoch(year: int, month: int, day: int, hour: int) -> int:
    """Convert an Eastern Time hour to UTC epoch. Handles DST automatically."""
    # US DST: starts 2nd Sunday of March 2am, ends 1st Sunday of November 2am
    # Find 2nd Sunday of March
    march1 = datetime(year, 3, 1)
    first_sun_march = march1 + timedelta(days=(6 - march1.weekday()) % 7)
    dst_start = first_sun_march + timedelta(days=7)  # 2nd Sunday
    # Find 1st Sunday of November
    nov1 = datetime(year, 11, 1)
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)  # 1st Sunday

    dt_naive = datetime(year, month, day, hour, 0, 0)
    et_offset = timedelta(hours=-4) if dst_start <= dt_naive < dst_end else timedelta(hours=-5)
    dt_utc = dt_naive - et_offset
    return int(dt_utc.replace(tzinfo=timezone.utc).timestamp())


TIMEFRAME_SECONDS = {
    "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "2h": 7200,
}
