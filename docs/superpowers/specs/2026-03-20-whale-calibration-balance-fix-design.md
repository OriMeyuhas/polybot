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

1. **Server-side date filtering.** Try Gamma API `end_date_window_start` and `end_date_window_end` query parameters to request only markets ending between `now` and `now + max_hours_to_resolution`, combined with `active=true` status filter. **Fallback:** If those params are not supported by the Gamma API, keep client-side filtering but improve it with slug-derived timestamps (see #2) as the primary time source instead of the unreliable `endDate` field.

2. **Slug-based time derivation (primary time source).** Crypto up/down slugs embed timing info (e.g., `btc-updown-5m-1773942300` with epoch or `btc-updown-15m-2026-03-19` with date). Parse the slug to extract: asset, timeframe (5m/15m), and window identifier. Derive `open_epoch` and `close_epoch` (open + timeframe_seconds) from the slug. Use these as the primary time source for `is_active()` checks, with API `endDate` as a secondary validation. Handle both epoch and date formats in the slug suffix.

3. **CLOB API fallback.** If Gamma returns 0 results, query `GET https://clob.polymarket.com/markets` filtered by known condition IDs or token patterns. This provides a second source of active markets.

4. **Preserve markets on total discovery failure.** If both Gamma and CLOB return 0 results, keep the previous `_active_markets` dict unchanged and log at ERROR level. This prevents wiping all active ladders/positions when the API is temporarily unavailable.

5. **Diagnostic logging.** Log the reason each market is filtered out (time, slug mismatch, liquidity, missing tokens) so "Discovered 0 active markets" is always diagnosable.

### Files Changed

- `polybot/data/gamma.py` — Server-side filters (with fallback), slug-based time derivation, CLOB fallback, logging.
- `polybot/bot.py` — Preserve previous markets on total discovery failure.

## Phase 2: Dynamic Balance Integration

### Current State

- `bot.py:564-577` — `_poll_wallet_balance()` polls every 60s but only stores the result in `self._wallet_balance` (display only).
- `position_manager.bankroll` is initialized from `cfg.bankroll` and only updated by `+= pnl` on settlement (bot.py:471).
- `config.py:133-167` — `get_ladder_params()` reads `self.bankroll` (the initial config value) for auto-scaling formulas.
- Live wallet balance and position manager bankroll diverge after the first settlement or failed redemption.

### Fix

1. **Initial balance fetch on startup.** In `Bot.start()` (or top of `_run_trading_loop()` before first tick), do one synchronous balance fetch so the first ladder is sized correctly. This eliminates the 60s stale-bankroll window after restart.

2. **Balance sync in `_poll_wallet_balance()`.** After polling:
   - **Live mode:** Set `position_manager.bankroll = on-chain USDC balance` (via `clob_client.get_balance_allowance()`). On-chain balance is the source of truth. **Remove the `bankroll += pnl` line in `_settle_position()` for live mode** — PnL is reflected in the on-chain balance after redemption, so adding it locally would double-count.
   - **Paper mode:** `position_manager.bankroll` already tracks `initial + cumulative_pnl` via `+= pnl` on settlement. Set `_wallet_balance = position_manager.bankroll` (existing behavior, no change needed).
   - **On poll failure:** Keep the last known bankroll value, log at WARNING level (not DEBUG).

3. **Dynamic `get_ladder_params()`.** Change signature to accept `current_bankroll: float` parameter. All internal formulas use `current_bankroll` instead of `self.bankroll`. Every call site passes the live `position_manager.bankroll`:
   - `ladder_manager.py` — `post_ladder()` and `reprice_if_needed()`
   - `bot.py` — `build_state_snapshot()` (dashboard display)

   The auto-scaling formulas then adapt:
   - `auto_fraction = max(0.02, min(0.30, 25.0 / current_bankroll))`
   - `auto_rungs = max(8, min(60, int(12 * log10(current_bankroll))))`

4. **Minimum capital guard.** Before posting a ladder, verify:
   ```
   available = position_manager.bankroll - total_committed()
   min_required = MIN_ORDER_SIZE * avg_price * 2 * min_rungs_per_side
   if available < min_required: skip this window
   ```
   Prevents posting ladders with sub-minimum rung sizes.

5. **Overleverage protection.** If `_wallet_balance < total_committed()` in live mode, skip `post_ladder()` calls for new markets and log at WARNING level. Existing resting orders are NOT cancelled — they may still fill profitably. Resume posting when balance recovers (e.g., after redemptions complete).

### Files Changed

- `polybot/bot.py` — Initial balance fetch in `start()`, balance-to-bankroll sync in `_poll_wallet_balance()`, conditional `+= pnl` (paper only), overleverage check before ladder posting, `build_state_snapshot()` updated for new `get_ladder_params` signature.
- `polybot/config.py` — `get_ladder_params(current_bankroll)` accepts dynamic bankroll argument, all internal references use the parameter.
- `polybot/strategy/ladder_manager.py` — Pass current bankroll to `get_ladder_params()`, add minimum capital guard.
- `polybot/strategy/position_manager.py` — Enhance existing `update_bankroll(balance: float)` method with logging.

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

- `polybot/config.py` — Updated default values for ladder parameters (both field defaults and `load_bot_config()` env var defaults).

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
| `polybot/data/gamma.py` | 1 | Server-side date filters (with fallback), slug-based time derivation, CLOB fallback, diagnostic logging |
| `polybot/bot.py` | 1, 2 | Preserve markets on discovery failure, initial balance fetch, balance-to-bankroll sync, conditional `+= pnl`, overleverage check, `build_state_snapshot()` signature update |
| `polybot/config.py` | 2, 3 | Dynamic `get_ladder_params(current_bankroll)`, whale-calibrated defaults (both field and env defaults) |
| `polybot/strategy/ladder_manager.py` | 2 | Pass current bankroll to all `get_ladder_params()` calls, minimum capital guard |
| `polybot/strategy/position_manager.py` | 2 | Enhance `update_bankroll()` with logging |

## Testing Strategy

### Unit Tests

- **`get_ladder_params(current_bankroll)` signature change:** Test auto-scaling at $50, $500, $5,000, $50,000 bankrolls for both 5m and 15m timeframes. Update existing tests in `tests/test_config_new_fields.py`.
- **Balance sync logic:** Test that `_poll_wallet_balance` updates `position_manager.bankroll` in live mode and does NOT overwrite in paper mode. Test failure path (keeps last known value).
- **Minimum capital guard:** Test that ladders are skipped when available capital is below `MIN_ORDER_SIZE * avg_price * 2 * min_rungs`.
- **Overleverage protection:** Test that `post_ladder()` is skipped when wallet balance < total committed, and resumes when balance recovers.
- **PnL accounting:** Test that `bankroll += pnl` only fires in paper mode, not live mode.

### Integration Tests

- **Discovery continuity:** Mock Gamma API to return rolling 5m windows. Verify bot discovers new markets across 3+ window rollovers without gaps.
- **CLOB fallback path:** Mock Gamma to return 0, verify CLOB fallback is attempted and produces valid markets.
- **Total failure preservation:** Mock both Gamma and CLOB to return 0, verify `_active_markets` is preserved (not emptied).

### Manual Verification

- **Phase 1:** Run bot in paper mode, verify continuous discovery across multiple window rollovers. Check logs.
- **Phase 2:** Run with $100 and $10,000 paper bankrolls, verify ladder sizes scale appropriately.
- **Phase 3:** Compare whale-derived params against current defaults. Verify reasonable ladder configurations.

## Success Criteria

1. Bot continuously discovers and trades new market windows without gaps.
2. Ladder sizing adapts to current wallet balance on every window.
3. Users with $50 and $50,000 both get sensible ladder configurations without manual config.
4. Parameters are grounded in actual whale trading data, not guesswork.
