# Whale-Calibrated Ladder + Dynamic Balance + Discovery Fix

**Date:** 2026-03-20
**Status:** Approved
**Approach:** A (Fix-First, Then Layer Features)

## Problem Statement

PolyBot starts but never places orders. Additionally, the ladder strategy parameters are hand-tuned rather than derived from actual whale (0x8dxd) trading data, and the bot ignores real wallet balance when sizing positions.

Three issues, three phases:

1. **Market discovery is broken** — after initial discovery, subsequent polls return 0 markets, so no ladders are ever posted on new windows.
2. **Balance is disconnected from sizing** — `_wallet_balance` is polled but never fed back into `position_manager.bankroll`, and `get_ladder_params()` uses the initial config bankroll, not the live balance. Users with different wallet sizes get wrong position sizes.
3. **Ladder parameters are not whale-calibrated** — the config defaults were hand-estimated. The tracker CSVs (67k+ trades, 618+ settlements) contain the actual data needed to derive optimal parameters.

## Phase 1: Fix Market Discovery

### Root Cause

`polybot/data/gamma.py:117-255` — The `discover_crypto_updown_markets()` function queries:

```
GET https://gamma-api.polymarket.com/events?tag_slug=up-or-down&closed=false&limit=500
```

Then filters client-side by `hours_left > max_hours_to_resolution` (default 2h, line 195). This fails because:

- No server-side date filtering is used, relying entirely on client-side time checks.
- The `endDate` fallback chain (`market.get("endDate") or event.get("endDate")`) may return the event-level end date rather than the individual market window close time.
- New rolling 5m/15m windows may not appear immediately in the Gamma API response after prior windows expire.
- When all initially-discovered windows close (5-15 minutes later), discovery returns 0 markets and stays that way.

### Fix

1. **Server-side date filtering.** Use Gamma API's `end_date_window_start` and `end_date_window_end` query parameters to request only markets ending between `now` and `now + max_hours_to_resolution`. Combined with `active=true` status filter.

2. **Slug-epoch cross-check.** Crypto up/down slugs embed the window start epoch: `btc-updown-5m-{epoch}`. Parse this to derive `open_epoch` and `close_epoch` (open + timeframe) independently of the API's `endDate` field. Use the slug-derived times as the primary source, with API `endDate` as validation.

3. **CLOB API fallback.** If Gamma returns 0 results, query `GET https://clob.polymarket.com/markets` filtered by known condition IDs or token patterns. This provides a second source of active markets.

4. **Diagnostic logging.** Log the reason each market is filtered out (time, slug mismatch, liquidity, missing tokens) so "Discovered 0 active markets" is always diagnosable.

### Files Changed

- `polybot/data/gamma.py` — Server-side filters, slug-epoch parsing, CLOB fallback, logging.

## Phase 2: Dynamic Balance Integration

### Current State

- `bot.py:564-577` — `_poll_wallet_balance()` polls every 60s but only stores the result in `self._wallet_balance` (display only).
- `position_manager.bankroll` is initialized from `cfg.bankroll` and only updated by `+= pnl` on settlement (bot.py:471).
- `config.py:133-167` — `get_ladder_params()` reads `self.bankroll` (the initial config value) for auto-scaling formulas.
- Live wallet balance and position manager bankroll diverge after the first settlement or failed redemption.

### Fix

1. **Balance sync in `_poll_wallet_balance()`.** After polling:
   - **Live mode:** Set `position_manager.bankroll = on-chain USDC balance` (from `get_balance_allowance` with `AssetType.COLLATERAL`). On-chain balance is the source of truth.
   - **Paper mode:** `position_manager.bankroll` already tracks `initial + cumulative_pnl`. Set `_wallet_balance = position_manager.bankroll` (existing behavior, no change needed).

2. **Dynamic `get_ladder_params()`.** Change signature to accept `current_bankroll: float` parameter. Every `post_ladder()` call passes the live `position_manager.bankroll`. The auto-scaling formulas then adapt:
   - `auto_fraction = max(0.02, min(0.30, 25.0 / current_bankroll))`
   - `auto_rungs = max(8, min(60, int(12 * log10(current_bankroll))))`

3. **Minimum capital guard.** Before posting a ladder, verify:
   ```
   available = position_manager.bankroll - total_committed()
   min_required = MIN_ORDER_SIZE * avg_price * 2 * min_rungs_per_side
   if available < min_required: skip this window
   ```
   Prevents posting ladders with sub-minimum rung sizes.

4. **Overleverage protection.** If `_wallet_balance < total_committed()` in live mode, pause new ladder posting and log a warning. Resume when balance recovers (e.g., after redemptions complete).

### Files Changed

- `polybot/bot.py` — Balance-to-bankroll sync in `_poll_wallet_balance()`, overleverage check before ladder posting.
- `polybot/config.py` — `get_ladder_params(current_bankroll)` accepts dynamic bankroll argument.
- `polybot/strategy/ladder_manager.py` — Pass current bankroll to `get_ladder_params()`, add minimum capital guard.
- `polybot/strategy/position_manager.py` — Add `update_bankroll(balance: float)` method with logging.

## Phase 3: Whale-Calibrated Parameters

### Approach

Analyze the tracker CSVs directly (no script needed — one-time analysis) and update `config.py` defaults with whale-derived optimal parameters.

### Data Sources

| File | Rows | Content |
|------|------|---------|
| `data/tracker/trades_20260318.csv` | 52,452 | Whale trades with price, size, timing, strategy classification |
| `data/tracker/trades_20260319.csv` | 14,965 | Same, second day |
| `data/tracker/settlements_20260318.csv` | 484 | Position outcomes with PnL, avg prices, win/loss |
| `data/tracker/settlements_20260319.csv` | 134 | Same, second day |

### Parameters to Derive

| Parameter | Derivation Method |
|-----------|------------------|
| `LADDER_RUNGS` / `LADDER_RUNGS_5M` | Count distinct price levels per window, take median across windows |
| `LADDER_SPACING` / `LADDER_SPACING_5M` | Median gap between adjacent whale price levels per window |
| `LADDER_WIDTH` / `LADDER_WIDTH_5M` | Median range (max price - min price) of entries per window |
| `LADDER_SIZE_SKEW` / `LADDER_SIZE_SKEW_5M` | Ratio: median size at most expensive rung / median size at cheapest rung |
| `POSITION_SIZE_FRACTION` | Median (position_usd / running_bankroll) from settlements |
| `MAX_PAIR_COST` | 95th percentile of (whale_avg_up_price + whale_avg_dn_price) on winning trades |
| Entry timing gate | Median `window_pct_elapsed` at first trade per window — informs the 10% entry gate |

Segmented by timeframe (5m vs 15m) since the whale trades them differently.

### Output

Updated defaults in `polybot/config.py` for both 15m and 5m ladder parameters. The `.env` override mechanism continues to work for manual tuning.

### Files Changed

- `polybot/config.py` — Updated default values for ladder parameters based on analysis.

## What Does NOT Change

- `polybot/oms/order_executor.py` — Works correctly when called.
- `polybot/oms/clob_client.py` — Paper and live clients work correctly.
- `polybot/web/` — Dashboard unaffected.
- `polybot/tracker/` — Tracker pipeline unaffected.
- `polybot/types.py` — Data types unchanged.
- `polybot/strategy/order_tracker.py` — Fill detection unchanged.

## File Change Summary

| File | Phase | Nature of Change |
|------|-------|-----------------|
| `polybot/data/gamma.py` | 1 | Server-side date filters, slug-epoch parsing, CLOB fallback, diagnostic logging |
| `polybot/bot.py` | 1, 2 | Balance-to-bankroll sync, overleverage check in trading loop |
| `polybot/config.py` | 2, 3 | Dynamic `get_ladder_params(current_bankroll)`, whale-calibrated defaults |
| `polybot/strategy/ladder_manager.py` | 2 | Pass current bankroll, minimum capital guard |
| `polybot/strategy/position_manager.py` | 2 | `update_bankroll()` method |

## Testing Strategy

- **Phase 1:** Run bot in paper mode, verify markets are continuously discovered every 60s across multiple 5m/15m window rollovers. Check logs show non-zero market count.
- **Phase 2:** Start with $100 paper bankroll, run through several windows, verify ladder sizes scale down appropriately. Then $10,000, verify they scale up. Verify bankroll updates after settlement.
- **Phase 3:** Compare whale-derived params against current defaults. Run paper mode with new params and verify ladders post at reasonable prices/sizes.

## Success Criteria

1. Bot continuously discovers and trades new market windows without gaps.
2. Ladder sizing adapts to current wallet balance on every window.
3. Users with $50 and $50,000 both get sensible ladder configurations without manual config.
4. Parameters are grounded in actual whale trading data, not guesswork.
