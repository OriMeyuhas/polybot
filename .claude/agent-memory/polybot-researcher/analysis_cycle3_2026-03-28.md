---
name: Cycle 3 Analysis 2026-03-28
description: Pre-live audit — event loop blocking in heartbeat, missing daily reset in standby, redeemer false success, 5 findings
type: project
---

## Findings (2026-03-28, Cycle 3)

### 1. Event Loop Blocking in Heartbeat + Connection Callbacks (HIGH — live-only)
Heartbeat.run() calls client.post_heartbeat() synchronously (no to_thread).
In paper mode this is instant (returns dict). In live mode with py-clob-client,
this is a blocking HTTP call that freezes the event loop for 100-500ms per tick.
_on_connection_lost() calls order_executor.cancel_all() synchronously from heartbeat context.
_on_connection_recovered() calls clob_client.get_order() in a sync loop for unknown orders.
**Impact:** In live mode, dashboard freezes, fill detection delayed, settlement poller blocked.

### 2. _run_daily_reset Missing from run_standby() (MEDIUM — both modes)
run_bot.py uses run_standby(), but _run_daily_reset is only in run().
Daily PnL and consecutive_losses are never reset. After 24h, stale daily_pnl
accumulates and drawdown circuit breaker fires on wrong data.

### 3. Redeemer Records False Success (MEDIUM — live-only)
_redeem_tokens() returns 0.0 without raising. Redeemer.run() sees no exception,
calls _record_success(), removes from pending. Log says "Redeemed: $0.00 USDC".
Tokens sit unredeemed, capital locked, operator thinks redemption worked.

### 4. All Tests Pass (556), No Import Errors (POSITIVE)
Clean test suite, clean imports, config loads correctly.
Bankroll=$200 in .env puts bot in "Small" tier (BTC only, 5m+15m, 3 concurrent, 10%).

### 5. Paper PnL Evidence from run_v4.log
3 settlements in ~13 min: $0.77, $1.01, $0.04 = $1.82 total on $100 bankroll.
Pair costs: 0.807, 0.862, 0.864. All below 0.90 guard.
Fill pattern healthy: 4-6 rungs per side, both sides filling.
One-sided ladder bug (window 1: 15 UP + 0 DN) was from pre-fix code.
