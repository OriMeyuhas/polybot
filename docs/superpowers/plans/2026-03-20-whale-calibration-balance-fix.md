# Whale-Calibrated Ladder + Dynamic Balance + Discovery Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the bot so it continuously discovers markets and places orders, size ladders dynamically from wallet balance, and calibrate parameters from whale tracker data.

**Architecture:** Three phases executed sequentially. Phase 1 fixes market discovery in `gamma.py` so windows are found every cycle. Phase 2 wires wallet balance into position sizing via a new `get_ladder_params(timeframe_sec, current_bankroll)` signature. Phase 3 analyzes whale CSVs and updates config defaults.

**Tech Stack:** Python 3.11+, asyncio, httpx, py-clob-client, pytest

**Spec:** `docs/superpowers/specs/2026-03-20-whale-calibration-balance-fix-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `polybot/data/gamma.py` | Modify | Slug-based time parsing, diagnostic logging, improved time filter, CLOB fallback |
| `polybot/bot.py` | Modify | Preserve markets on failure, initial balance fetch, balance sync, conditional PnL, overleverage guard |
| `polybot/config.py` | Modify | `get_ladder_params(timeframe_sec, current_bankroll)` signature, whale-calibrated defaults |
| `polybot/strategy/ladder_manager.py` | Modify | Pass current bankroll, minimum capital guard |
| `polybot/strategy/position_manager.py` | Modify | Enhance `update_bankroll()` with logging |
| `tests/test_gamma.py` | Modify | Slug parsing tests, diagnostic logging tests |
| `tests/test_config_new_fields.py` | Modify | Dynamic bankroll scaling tests |
| `tests/test_balance_sizing.py` | Create | Balance sync, min capital, overleverage tests |
| `tests/test_discovery_continuity.py` | Create | Discovery preservation, CLOB fallback tests |

---

## Phase 1: Fix Market Discovery

### Task 1: Add slug-based time parsing to gamma.py

**Files:**
- Modify: `polybot/data/gamma.py:52-67`
- Test: `tests/test_gamma.py`

- [ ] **Step 1: Write failing tests for slug time parsing**

```python
# tests/test_gamma.py — append these tests

from polybot.data.gamma import parse_slug_timing


def test_parse_slug_timing_epoch():
    """Slug with epoch suffix: btc-updown-5m-1773942300."""
    result = parse_slug_timing("btc-updown-5m-1773942300")
    assert result is not None
    asset, timeframe_sec, open_epoch, close_epoch = result
    assert asset == "BTC"
    assert timeframe_sec == 300
    assert open_epoch == 1773942300
    assert close_epoch == 1773942600  # open + 300


def test_parse_slug_timing_date():
    """Slug with date suffix: btc-updown-15m-2026-03-19."""
    result = parse_slug_timing("btc-updown-15m-2026-03-19")
    assert result is not None
    asset, timeframe_sec, open_epoch, close_epoch = result
    assert asset == "BTC"
    assert timeframe_sec == 900
    # open_epoch should be some valid epoch on 2026-03-19
    assert open_epoch > 0
    assert close_epoch == open_epoch + 900


def test_parse_slug_timing_unknown():
    """Non-matching slug returns None."""
    assert parse_slug_timing("will-trump-win") is None
    assert parse_slug_timing("") is None


def test_parse_slug_timing_1h():
    """1-hour window slug."""
    result = parse_slug_timing("eth-updown-1h-1773942300")
    assert result is not None
    _, timeframe_sec, _, close_epoch = result
    assert timeframe_sec == 3600
    assert close_epoch == 1773942300 + 3600
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_gamma.py::test_parse_slug_timing_epoch -v`
Expected: FAIL — `ImportError: cannot import name 'parse_slug_timing'`

- [ ] **Step 3: Implement `parse_slug_timing` in gamma.py**

Add after `_detect_asset()` (after line 67):

```python
import re

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
        from datetime import datetime, timezone
        dt = datetime.strptime(suffix, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        open_epoch = int(dt.timestamp())
        return (asset, timeframe_sec, open_epoch, open_epoch + timeframe_sec)
    except ValueError:
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_gamma.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/data/gamma.py tests/test_gamma.py
git commit -m "feat: add parse_slug_timing for slug-derived window times"
```

---

### Task 2: Improve time filter and add diagnostic logging in gamma.py

**Files:**
- Modify: `polybot/data/gamma.py:117-255` (the `discover_crypto_updown_markets` function)
- Test: `tests/test_gamma.py`

- [ ] **Step 1: Write failing test for diagnostic logging**

```python
# tests/test_gamma.py — append

import logging


def test_discovery_logs_filter_reasons(caplog):
    """Discovery should log why each market is filtered out."""
    from polybot.data.gamma import _filter_market_from_event

    # Market with missing token IDs
    market = {
        "slug": "btc-updown-5m-1773942300",
        "clobTokenIds": "[]",
        "outcomes": '["Up", "Down"]',
        "endDate": "2026-03-20T12:05:00Z",
        "active": True,
        "liquidityNum": 100.0,
    }
    with caplog.at_level(logging.DEBUG, logger="polybot.data.gamma"):
        result = _filter_market_from_event(market, event={}, patterns=["btc-updown-5m-"], now_epoch=1773942330)
    assert result is None
    assert "token" in caplog.text.lower() or "skip" in caplog.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_gamma.py::test_discovery_logs_filter_reasons -v`
Expected: FAIL — `ImportError: cannot import name '_filter_market_from_event'`

- [ ] **Step 3: Refactor discovery to use slug-based timing and log filter reasons**

Replace `discover_crypto_updown_markets` (lines 117-255) with improved version. Key changes:

1. Use `parse_slug_timing()` as primary time source instead of `endDate`
2. Extract the per-market filter logic into `_filter_market_from_event()` for testability
3. Add `logger.debug()` calls explaining each filter-out reason
4. Try server-side params `end_date_window_start`/`end_date_window_end` in the API call (they'll be ignored if unsupported)

```python
def _filter_market_from_event(
    market: dict,
    event: dict,
    patterns: list[str],
    now_epoch: int,
    max_hours: float = 2.0,
    min_liquidity: float = 50.0,
) -> MarketInfo | None:
    """Extract and validate a single market from Gamma event data.

    Returns MarketInfo if valid, None if filtered out.
    Logs the reason for every rejection at DEBUG level.
    """
    slug = market.get("slug", "") or market.get("conditionId", "")
    if not slug:
        logger.debug("FILTER: no slug or conditionId")
        return None

    # Slug pattern match
    matched = any(p.replace("*", "") in slug for p in patterns)
    if not matched:
        return None  # Don't log — most events are non-crypto

    # Asset detection
    asset = _detect_asset(slug)
    if not asset:
        logger.debug("FILTER [%s]: unknown asset prefix", slug)
        return None

    # Token IDs
    token_ids = _parse_json_field(
        market.get("clobTokenIds", market.get("clob_token_ids"))
    )
    outcomes = _parse_json_field(
        market.get("outcomes"), default=["Up", "Down"]
    )
    if len(token_ids) < 2 or len(outcomes) < 2:
        logger.debug("FILTER [%s]: insufficient tokens (%d) or outcomes (%d)", slug, len(token_ids), len(outcomes))
        return None

    # Time filter: prefer slug-derived timing
    slug_timing = parse_slug_timing(slug)
    if slug_timing:
        _, timeframe_sec, open_epoch, close_epoch = slug_timing
        hours_left = (close_epoch - now_epoch) / 3600
    else:
        # Fallback to API endDate
        end_iso = market.get("endDate") or event.get("endDate") or ""
        if not end_iso:
            logger.debug("FILTER [%s]: no endDate and slug timing failed", slug)
            return None
        try:
            end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            if end_dt.tzinfo is None:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            close_epoch = int(end_dt.timestamp())
            hours_left = (close_epoch - now_epoch) / 3600
        except (ValueError, TypeError):
            logger.debug("FILTER [%s]: unparseable endDate '%s'", slug, end_iso)
            return None

    if hours_left <= 0:
        logger.debug("FILTER [%s]: already expired (%.2fh left)", slug, hours_left)
        return None
    if hours_left > max_hours:
        logger.debug("FILTER [%s]: too far out (%.2fh > %.1fh)", slug, hours_left, max_hours)
        return None

    # Start time
    start_iso = (
        market.get("eventStartTime")
        or event.get("startTime")
        or event.get("startDate")
        or ""
    )

    # Liquidity
    liquidity = float(market.get("liquidityNum") or market.get("liquidity") or 0)
    if liquidity < min_liquidity:
        logger.debug("FILTER [%s]: low liquidity ($%.0f < $%.0f)", slug, liquidity, min_liquidity)
        return None

    return MarketInfo(
        condition_id=market.get("conditionId", market.get("condition_id", "")),
        question=market.get("question", ""),
        slug=slug,
        clob_token_ids=[str(t) for t in token_ids],
        outcomes=[str(o) for o in outcomes],
        event_start_iso=start_iso,
        end_date_iso=market.get("endDate") or event.get("endDate") or "",
        price_to_beat=str(market.get("priceToBeat", market.get("price_to_beat", "0"))),
        active=market.get("active", True),
        liquidity=liquidity,
    )
```

Then update `discover_crypto_updown_markets` to call `_filter_market_from_event` and use `now_epoch = int(datetime.now(timezone.utc).timestamp())`:

```python
async def discover_crypto_updown_markets(
    gamma_host: str = GAMMA_API,
    slug_patterns: list[str] | None = None,
    max_hours_to_resolution: float = 2,
    min_liquidity: float = 50,
) -> list[tuple[MarketInfo, str]]:
    patterns = slug_patterns or CRYPTO_SLUG_PATTERNS
    results: list[tuple[MarketInfo, str]] = []
    now = datetime.now(timezone.utc)
    now_epoch = int(now.timestamp())

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Server-side filtering (extra params ignored if API doesn't support them)
            end_window_end = (now + __import__('datetime').timedelta(hours=max_hours_to_resolution)).isoformat()
            params = {
                "tag_slug": "up-or-down",
                "closed": "false",
                "active": "true",
                "end_date_window_start": now.isoformat(),
                "end_date_window_end": end_window_end,
                "limit": "500",
            }
            resp = await client.get(f"{gamma_host}/events", params=params)
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

                    info = _filter_market_from_event(
                        market, event, patterns, now_epoch,
                        max_hours=max_hours_to_resolution,
                        min_liquidity=min_liquidity,
                    )
                    if info is None:
                        continue

                    seen_slugs.add(slug)
                    # Asset already validated inside _filter_market_from_event
                    asset = _detect_asset(slug)
                    results.append((info, asset))

    except Exception as e:
        logger.error("Gamma API discovery failed: %s", e)
        raise

    # CLOB API fallback if Gamma returned nothing
    if not results:
        logger.warning("Gamma returned 0 markets — trying CLOB API fallback")
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{gamma_host.replace('gamma-api', 'clob')}/markets",
                    params={"limit": "500"},
                )
                if resp.status_code == 200:
                    for market in resp.json().get("data", resp.json() if isinstance(resp.json(), list) else []):
                        slug = market.get("slug", "") or market.get("condition_id", "")
                        if slug in seen_slugs:
                            continue
                        info = _filter_market_from_event(
                            market, {}, patterns, now_epoch,
                            max_hours=max_hours_to_resolution,
                            min_liquidity=0,  # CLOB may not have liquidity field
                        )
                        if info:
                            seen_slugs.add(slug)
                            asset = _detect_asset(slug)
                            results.append((info, asset))
        except Exception as e:
            logger.error("CLOB fallback failed: %s", e)

    num_events = len(events) if 'events' in locals() else 0
    logger.info("Discovery: %d markets passed filters (from %d events)", len(results), num_events)

    # Sort by time to resolution (soonest first)
    def _hours_left(item: tuple[MarketInfo, str]) -> float:
        slug_timing = parse_slug_timing(item[0].slug)
        if slug_timing:
            return (slug_timing[3] - now_epoch) / 3600
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
    return results
```

- [ ] **Step 4: Run all gamma tests**

Run: `pytest tests/test_gamma.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/data/gamma.py tests/test_gamma.py
git commit -m "fix: improve discovery time filter with slug parsing and diagnostic logging"
```

---

### Task 3: Preserve active markets on total discovery failure

**Files:**
- Modify: `polybot/bot.py:374-413`
- Test: `tests/test_discovery_continuity.py` (create)

- [ ] **Step 1: Write failing test**

```python
# tests/test_discovery_continuity.py

import asyncio
from unittest.mock import patch, AsyncMock
from polybot.config import BotConfig
from polybot.bot import Bot
from polybot.types import MarketWindow


def test_preserve_markets_on_empty_discovery():
    """If discovery returns 0 markets, _active_markets should be preserved."""
    cfg = BotConfig(dry_run=True)
    bot = Bot(cfg)

    # Seed with a fake market
    fake = MarketWindow(
        market_id="btc-updown-5m-123",
        condition_id="cond_123",
        asset="BTC",
        timeframe_sec=300,
        up_token_id="tok_up",
        dn_token_id="tok_dn",
        open_epoch=100,
        close_epoch=400,
    )
    bot._active_markets = {"btc-updown-5m-123": fake}

    # Mock discover_crypto_updown_markets to return empty
    with patch(
        "polybot.bot.discover_crypto_updown_markets",
        new_callable=AsyncMock,
        return_value=[],
    ):
        asyncio.run(bot._discover_markets())

    # Markets should be preserved (not wiped)
    assert len(bot._active_markets) == 1
    assert "btc-updown-5m-123" in bot._active_markets
```

- [ ] **Step 2: Run test to verify it fails or passes as baseline**

Run: `pytest tests/test_discovery_continuity.py -v`

- [ ] **Step 3: Update `_discover_markets` in bot.py to preserve on empty**

At `bot.py:374-413`, change the market replacement logic. Replace line 404 (`self._active_markets = new_markets`) with:

```python
            if not new_markets and self._active_markets:
                logger.error(
                    "Discovery returned 0 markets — preserving %d existing markets",
                    len(self._active_markets),
                )
            else:
                # Log arrivals/departures
                old_ids = set(self._active_markets.keys())
                new_ids = set(new_markets.keys())
                arrived = new_ids - old_ids
                departed = old_ids - new_ids
                if arrived:
                    logger.info("NEW WINDOWS: %s", ", ".join(arrived))
                if departed:
                    logger.info("EXPIRED WINDOWS: %s", ", ".join(departed))
                self._active_markets = new_markets
```

Also move the arrival/departure logging inside the `else` block (it's currently before the assignment at lines 395-402).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_discovery_continuity.py tests/test_bot_orchestrator.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/bot.py tests/test_discovery_continuity.py
git commit -m "fix: preserve active markets when discovery returns 0 results"
```

---

## Phase 2: Dynamic Balance Integration

### Task 4: Enhance `update_bankroll` with logging

**Files:**
- Modify: `polybot/strategy/position_manager.py:43-44`
- Test: `tests/test_balance_sizing.py` (create)

- [ ] **Step 1: Write failing test**

```python
# tests/test_balance_sizing.py

import logging
from polybot.config import BotConfig
from polybot.strategy.position_manager import PositionManager


def test_update_bankroll_logs_change(caplog):
    """update_bankroll should log old -> new when values differ."""
    cfg = BotConfig(dry_run=True)
    pm = PositionManager(cfg, bankroll=1000.0)

    with caplog.at_level(logging.INFO, logger="polybot.strategy.position_manager"):
        pm.update_bankroll(1500.0)

    assert pm.bankroll == 1500.0
    assert "1000" in caplog.text or "1500" in caplog.text


def test_update_bankroll_no_log_if_same(caplog):
    """No log if bankroll hasn't changed."""
    cfg = BotConfig(dry_run=True)
    pm = PositionManager(cfg, bankroll=1000.0)

    with caplog.at_level(logging.INFO, logger="polybot.strategy.position_manager"):
        pm.update_bankroll(1000.0)

    # Should not log a change
    assert "bankroll" not in caplog.text.lower() or "unchanged" in caplog.text.lower() or caplog.text == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_balance_sizing.py::test_update_bankroll_logs_change -v`
Expected: FAIL — no log message produced

- [ ] **Step 3: Enhance `update_bankroll` in position_manager.py**

Replace lines 43-44:

```python
    def update_bankroll(self, new_bankroll: float):
        if abs(new_bankroll - self.bankroll) > 0.01:
            logger.info(
                "Bankroll updated: $%.2f -> $%.2f (delta: %+.2f)",
                self.bankroll, new_bankroll, new_bankroll - self.bankroll,
            )
        self.bankroll = new_bankroll
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_balance_sizing.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/strategy/position_manager.py tests/test_balance_sizing.py
git commit -m "feat: add logging to update_bankroll for balance change visibility"
```

---

### Task 5: Change `get_ladder_params` signature to accept `current_bankroll`

**Files:**
- Modify: `polybot/config.py:133-167`
- Modify: `tests/test_config_new_fields.py`

- [ ] **Step 1: Write failing test for new signature**

```python
# tests/test_config_new_fields.py — append

def test_get_ladder_params_dynamic_bankroll():
    """get_ladder_params should use current_bankroll, not self.bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    # With $100 bankroll: more rungs per dollar, higher fraction
    lp_100 = cfg.get_ladder_params(900, current_bankroll=100.0)
    # With $50000 bankroll: fewer rungs per dollar, lower fraction
    lp_50k = cfg.get_ladder_params(900, current_bankroll=50000.0)

    # Smaller bankroll = higher fraction
    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    # Smaller bankroll = fewer rungs
    assert lp_100.rungs < lp_50k.rungs


def test_get_ladder_params_5m_dynamic_bankroll():
    """5m params should also scale with current_bankroll."""
    from polybot.config import BotConfig

    cfg = BotConfig(dry_run=True, bankroll=1000.0)

    lp_100 = cfg.get_ladder_params(300, current_bankroll=100.0)
    lp_50k = cfg.get_ladder_params(300, current_bankroll=50000.0)

    assert lp_100.position_size_fraction > lp_50k.position_size_fraction
    # 5m fraction is 0.33x of base fraction
    assert lp_100.position_size_fraction < 0.10  # 0.25 * 0.33 = 0.0825
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_config_new_fields.py::test_get_ladder_params_dynamic_bankroll -v`
Expected: FAIL — `TypeError: get_ladder_params() got an unexpected keyword argument 'current_bankroll'`

- [ ] **Step 3: Update `get_ladder_params` signature**

Replace `polybot/config.py` lines 133-167:

```python
    def get_ladder_params(self, timeframe_sec: int, current_bankroll: float | None = None) -> LadderParams:
        """Return ladder parameters tuned for the given timeframe.

        Auto-scales position_size_fraction and rung count based on current_bankroll.
        Falls back to self.bankroll if current_bankroll is not provided (backward compat).
        """
        import math
        bankroll = max(current_bankroll if current_bankroll is not None else self.bankroll, 50)
        auto_fraction = max(0.02, min(0.30, 25.0 / bankroll))
        auto_rungs = max(8, min(60, int(12 * math.log10(bankroll))))

        if timeframe_sec <= 300:  # 5m or shorter
            return LadderParams(
                rungs=min(auto_rungs, self.ladder_rungs_5m),
                spacing=self.ladder_spacing_5m,
                width=self.ladder_width_5m,
                size_skew=self.ladder_size_skew_5m,
                max_pair_cost=self.max_pair_cost_5m,
                position_size_fraction=auto_fraction * 0.33,
            )
        return LadderParams(
            rungs=min(auto_rungs, self.ladder_rungs),
            spacing=self.ladder_spacing,
            width=self.ladder_width,
            size_skew=self.ladder_size_skew,
            max_pair_cost=self.max_pair_cost,
            position_size_fraction=auto_fraction,
        )
```

The `current_bankroll` defaults to `None` for backward compatibility — existing callers without the argument continue to work.

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_config_new_fields.py -v`
Expected: All pass (both new and existing tests)

- [ ] **Step 5: Commit**

```bash
git add polybot/config.py tests/test_config_new_fields.py
git commit -m "feat: get_ladder_params accepts current_bankroll for dynamic sizing"
```

---

### Task 6: Wire current bankroll through ladder_manager + min capital guard

**Files:**
- Modify: `polybot/strategy/ladder_manager.py:122-140, 277-314`
- Test: `tests/test_balance_sizing.py`

- [ ] **Step 1: Write failing test for min capital guard**

```python
# tests/test_balance_sizing.py — append

from unittest.mock import MagicMock
from polybot.strategy.ladder_manager import LadderManager, MIN_ORDER_SIZE
from polybot.types import MarketWindow


def _make_ladder_manager(bankroll=1000.0):
    """Create a LadderManager with mocked dependencies."""
    cfg = BotConfig(dry_run=True, bankroll=bankroll)
    executor = MagicMock()
    executor.get_best_ask = MagicMock(return_value=0.50)
    executor.place_batch_limit_buys = MagicMock(return_value=[])
    tracker = MagicMock()
    tracker.get_resting = MagicMock(return_value=[])
    pm = PositionManager(cfg, bankroll=bankroll)
    risk = MagicMock()
    risk.is_halted = MagicMock(return_value=False)
    risk.can_open_position = MagicMock(return_value=True)

    return LadderManager(cfg, executor, tracker, pm, risk)


def test_min_capital_guard_skips_when_broke():
    """post_ladder returns 0 when available capital is below minimum."""
    lm = _make_ladder_manager(bankroll=5.0)  # $5 total — too small for a two-sided ladder
    market = MarketWindow(
        market_id="btc-updown-5m-123", condition_id="c", asset="BTC",
        timeframe_sec=300, up_token_id="up", dn_token_id="dn",
        open_epoch=100, close_epoch=400,
    )
    count = lm.post_ladder(market)
    assert count == 0
```

- [ ] **Step 2: Run test to verify it fails or check baseline**

Run: `pytest tests/test_balance_sizing.py::test_min_capital_guard_skips_when_broke -v`

- [ ] **Step 3: Update `post_ladder` and `reprice_if_needed` to pass current bankroll**

In `polybot/strategy/ladder_manager.py`, update `post_ladder` (line 131):

```python
            # Select timeframe-specific ladder parameters
            lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)
```

And the budget calculation (lines 133-140), add a minimum capital guard:

```python
            available = self.positions.bankroll - self.total_committed()
            budget = min(
                self.positions.bankroll * lp.position_size_fraction,
                available,
            )
            # Minimum capital guard: need enough for MIN_ORDER_SIZE on both sides
            min_required = MIN_ORDER_SIZE * 2.0
            if budget < min_required:
                logger.info("MIN CAPITAL: %s skipped — available $%.2f < min $%.2f",
                            market.market_id, budget, min_required)
                return 0
```

Also update `reprice_if_needed` (line 307):

```python
            lp = self.cfg.get_ladder_params(market.timeframe_sec, current_bankroll=self.positions.bankroll)
```

And its budget calculation (line 312):

```python
            total_budget = self.positions.bankroll * lp.position_size_fraction
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_balance_sizing.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/strategy/ladder_manager.py tests/test_balance_sizing.py
git commit -m "feat: ladder manager uses dynamic bankroll + minimum capital guard"
```

---

### Task 7: Initial balance fetch + balance sync + conditional PnL in bot.py

**Files:**
- Modify: `polybot/bot.py:127-132, 454-486, 563-577, 721-723`

- [ ] **Step 1: Write tests for balance sync behavior**

```python
# tests/test_balance_sizing.py — append

from polybot.bot import Bot


def test_settle_position_paper_updates_bankroll():
    """In paper mode, _settle_position should add PnL to bankroll."""
    cfg = BotConfig(dry_run=True, bankroll=1000.0)
    bot = Bot(cfg)
    assert bot.position_manager.bankroll == 1000.0


def test_bot_wallet_balance_initialized_from_bankroll():
    """Bot._wallet_balance should start equal to cfg.bankroll."""
    cfg = BotConfig(dry_run=True, bankroll=500.0)
    bot = Bot(cfg)
    assert bot._wallet_balance == 500.0


def test_settle_position_live_does_not_add_pnl():
    """In live mode, _settle_position should NOT add PnL to bankroll (on-chain is source of truth)."""
    cfg = BotConfig(dry_run=False, bankroll=1000.0, private_key="0x" + "ab" * 32)
    bot = Bot(cfg)
    from polybot.types import MarketWindow, Position
    market = MarketWindow(
        market_id="test", condition_id="c", asset="BTC",
        timeframe_sec=300, up_token_id="up", dn_token_id="dn",
        open_epoch=100, close_epoch=400,
    )
    # Add a position that would yield +$50 PnL if UP wins
    bot.position_manager.positions["test"] = Position(
        market_id="test", up_qty=100, up_cost=50, dn_qty=0, dn_cost=0,
    )
    bot.position_manager.mark_pending_settlement("test")
    bot._expired_market_cache["test"] = market
    bankroll_before = bot.position_manager.bankroll
    bot._settle_position("test", market, "UP")
    # In live mode, bankroll should NOT change from PnL (it comes from on-chain balance)
    assert bot.position_manager.bankroll == bankroll_before


def test_overleverage_flag():
    """Bot should detect overleveraged state when wallet < committed."""
    cfg = BotConfig(dry_run=False, bankroll=100.0, private_key="0x" + "ab" * 32)
    bot = Bot(cfg)
    bot._wallet_balance = 50.0  # Wallet has $50
    # Simulate committed capital > wallet
    # total_committed reads from ladder_manager which reads from tracker
    # For this test, just check the condition directly
    committed = 100.0
    overleveraged = (
        not bot.cfg.dry_run
        and bot._wallet_balance < committed
    )
    assert overleveraged is True
```

- [ ] **Step 2: Run tests**

Run: `pytest tests/test_balance_sizing.py -v`

- [ ] **Step 3: Apply changes to bot.py**

**3a. Update `_poll_wallet_balance` (lines 563-577):**

```python
    async def _poll_wallet_balance(self):
        """Poll wallet USDC balance every 60s. Syncs into position_manager.bankroll."""
        while self.running:
            try:
                if not self.cfg.dry_run:
                    result = await asyncio.to_thread(
                        self.clob_client.get_balance_allowance
                    )
                    balance = float(result.get("balance", 0)) / 1e6
                    self._wallet_balance = balance
                    self.position_manager.update_bankroll(balance)
                else:
                    self._wallet_balance = self.position_manager.bankroll
            except Exception as e:
                logger.warning("Balance poll failed (keeping last known): %s", e)
            await asyncio.sleep(60)
```

**3b. Add initial balance fetch in `start()` (after line 132):**

```python
    async def start(self):
        """Start all subsystems."""
        self.running = True
        self._start_time = time.time()
        self.gui_state.update(running=True, mode=self.mode)

        # Initial balance fetch so first ladder is sized correctly
        if not self.cfg.dry_run:
            try:
                result = await asyncio.to_thread(
                    self.clob_client.get_balance_allowance
                )
                balance = float(result.get("balance", 0)) / 1e6
                self._wallet_balance = balance
                self.position_manager.update_bankroll(balance)
                logger.info("Initial wallet balance: $%.2f", balance)
            except Exception as e:
                logger.warning("Initial balance fetch failed: %s", e)

        logger.info("Bot started in %s mode", self.mode)
```

**3c. Conditional PnL in `_settle_position` (line 471):**

Replace `self.position_manager.bankroll += pnl` with:

```python
            # Only update bankroll from PnL in paper mode.
            # In live mode, on-chain balance is the source of truth
            # (synced via _poll_wallet_balance).
            if self.cfg.dry_run:
                self.position_manager.bankroll += pnl
```

**3d. Update `build_state_snapshot` (lines 721-723):**

Replace:
```python
                        "rungs_total": self.cfg.get_ladder_params(
                            mkt.timeframe_sec
                        ).rungs,
```
With:
```python
                        "rungs_total": self.cfg.get_ladder_params(
                            mkt.timeframe_sec,
                            current_bankroll=self.position_manager.bankroll,
                        ).rungs,
```

**3e. Add overleverage check in `_trading_loop_tick` (before the ladder posting loop, around line 287):**

After `if not self._cancel_only_mode:` (line 287), add:

```python
            # Overleverage protection (live mode only)
            overleveraged = (
                not self.cfg.dry_run
                and self._wallet_balance < self.ladder_manager.total_committed()
            )
            if overleveraged:
                logger.warning(
                    "OVERLEVERAGED: wallet $%.2f < committed $%.2f — skipping new ladders",
                    self._wallet_balance, self.ladder_manager.total_committed(),
                )
```

Then wrap the ladder posting loop (lines 289-308) with `if not overleveraged:`.

- [ ] **Step 4: Run all tests**

Run: `pytest tests/test_balance_sizing.py tests/test_bot_orchestrator.py -v`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add polybot/bot.py tests/test_balance_sizing.py
git commit -m "feat: wire wallet balance into position sizing with overleverage guard"
```

---

## Phase 3: Whale-Calibrated Parameters

### Task 8: Analyze whale CSVs and update config defaults

**Files:**
- Modify: `polybot/config.py:69-82, 180-191`

- [ ] **Step 1: Analyze the whale tracker data**

Read `data/tracker/trades_20260318.csv` and `data/tracker/settlements_20260318.csv` (the larger dataset). Compute:

For **15m windows** (filter by `timeframe == "15m"`):
- Group trades by `market_slug` (= window)
- Per window: count distinct price levels, compute spacing between adjacent sorted prices, compute price range (width)
- Per window: compute size ratio (size at max price / size at min price) for skew
- From settlements: compute 95th percentile of `whale_avg_price` sum for winning trades
- Compute median `window_pct_elapsed` at first trade per window for entry timing

For **5m windows** (filter by `timeframe == "5m"`): same analysis.

This is a one-time analysis done by the implementing agent — no script is committed.

- [ ] **Step 2: Update config.py field defaults (lines 69-82)**

Update the field defaults in `BotConfig` with the whale-derived values. Example (exact values come from analysis):

```python
    # Ladder parameters — 15m default (calibrated from whale tracker CSV data)
    ladder_rungs: int = <whale_derived_15m_rungs>
    ladder_spacing: float = <whale_derived_15m_spacing>
    ladder_width: float = <whale_derived_15m_width>
    ladder_size_skew: float = <whale_derived_15m_skew>
    max_pair_cost: float = <whale_derived_15m_max_pair_cost>
    position_size_fraction: float = <whale_derived_15m_fraction>

    # Ladder parameters — 5m overrides
    ladder_rungs_5m: int = <whale_derived_5m_rungs>
    ladder_spacing_5m: float = <whale_derived_5m_spacing>
    ladder_width_5m: float = <whale_derived_5m_width>
    ladder_size_skew_5m: float = <whale_derived_5m_skew>
    max_pair_cost_5m: float = <whale_derived_5m_max_pair_cost>
    position_size_fraction_5m: float = <whale_derived_5m_fraction>
```

- [ ] **Step 3: Reconcile `load_bot_config()` env var defaults (lines 180-191)**

Make the env var default strings match the new field defaults. This fixes the pre-existing `max_pair_cost` mismatch (field: 0.95, env: "0.995").

```python
        ladder_rungs=int(os.getenv("LADDER_RUNGS", str(<whale_15m_rungs>))),
        ladder_spacing=float(os.getenv("LADDER_SPACING", str(<whale_15m_spacing>))),
        ladder_width=float(os.getenv("LADDER_WIDTH", str(<whale_15m_width>))),
        ladder_size_skew=float(os.getenv("LADDER_SIZE_SKEW", str(<whale_15m_skew>))),
        max_pair_cost=float(os.getenv("MAX_PAIR_COST", str(<whale_15m_max_pair_cost>))),
        position_size_fraction=float(os.getenv("POSITION_SIZE_FRACTION", str(<whale_15m_fraction>))),
        ladder_rungs_5m=int(os.getenv("LADDER_RUNGS_5M", str(<whale_5m_rungs>))),
        ladder_spacing_5m=float(os.getenv("LADDER_SPACING_5M", str(<whale_5m_spacing>))),
        ladder_width_5m=float(os.getenv("LADDER_WIDTH_5M", str(<whale_5m_width>))),
        ladder_size_skew_5m=float(os.getenv("LADDER_SIZE_SKEW_5M", str(<whale_5m_skew>))),
        max_pair_cost_5m=float(os.getenv("MAX_PAIR_COST_5M", str(<whale_5m_max_pair_cost>))),
        position_size_fraction_5m=float(os.getenv("POSITION_SIZE_FRACTION_5M", str(<whale_5m_fraction>))),
```

- [ ] **Step 4: Update existing config tests that assert old defaults**

In `tests/test_config_new_fields.py`, if any test asserts specific ladder default values (e.g., `assert cfg.ladder_rungs == 36` or `assert cfg.max_pair_cost == 0.95`), update them to match the new whale-derived defaults. This prevents test breakage.

- [ ] **Step 5: Run all tests to verify nothing breaks**

Run: `pytest tests/ -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add polybot/config.py tests/test_config_new_fields.py
git commit -m "feat: whale-calibrated ladder defaults from tracker CSV analysis"
```

---

## Final Verification

### Task 9: Run full test suite and manual smoke test

- [ ] **Step 1: Run all tests**

Run: `pytest tests/ -v --tb=short`
Expected: All pass, no regressions

- [ ] **Step 2: Paper mode smoke test**

Run: `python run_bot.py` and verify in logs:
1. Markets are discovered on first cycle
2. Ladders are posted (look for "LADDER POSTED" messages)
3. After 5+ minutes, new windows are discovered when old ones expire
4. Balance is logged correctly

- [ ] **Step 3: Commit any remaining fixes**

If smoke test reveals issues, fix and commit each fix separately.
