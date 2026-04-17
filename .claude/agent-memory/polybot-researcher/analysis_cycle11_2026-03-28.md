---
name: Cycle 11 Live Mode Gap Analysis
description: 7 critical findings for DRY_RUN=false transition — phantom fills from auto-cancel, get_orders pagination bottleneck, redemption not implemented, balance param missing asset_type, no credential pre-check, rate limit budget, and no order-level cancellation on settlement
type: project
---

## Cycle 11: Live Mode Gap Analysis (2026-03-28)

### FINDING 1 (CRITICAL): Phantom fills from exchange-side auto-cancellation
- reconcile() line 214-219: if order is "resting" and disappears from get_orders, it's treated as filled
- In live mode, Polymarket auto-cancels all resting orders when a market resolves/settles
- Bot's no_trade_final_sec=60 cancels rungs 60s before expiry, but market resolution can be INSTANT after close_epoch
- Race window: between close_epoch and our cancel_ladder(), if Polymarket resolves first, ALL resting orders disappear
- reconcile() treats those as fills -> phantom position -> incorrect PnL -> overpays on losing side
- Fix: Before reconcile, check if market is expired/settled. Filter out orders on expired markets from the "disappeared=filled" logic. Or use get_order() per disappeared order to check actual status.

### FINDING 2 (HIGH): get_orders() pagination bottleneck at 500ms polling
- Live ClobClient.get_orders() hits /data/orders and paginates through ALL open orders
- With 8 concurrent markets * 2 sides * ~20 rungs = ~320 orders, pagination = multiple HTTP calls per tick
- At 500ms polling, this blocks the event loop thread for potentially 1-3 seconds
- Rate limit: 900 req/10s for GET /data/orders — at 4 pages per poll * 2 polls/sec = 8 req/sec = OK but tight
- Fix: Add OpenOrderParams(asset_id=token_id) filtering to scope queries per-market, or increase poll_interval_ms for live mode

### FINDING 3 (CRITICAL): Redemption is NOT implemented for live mode
- _redeem_tokens() at bot.py:1062-1075 raises NotImplementedError for live mode
- Redeemer.run() catches this as an Exception, logs warning, retries with exponential backoff
- After max_retries (10), moves to failed dict — capital stays locked forever
- The bot settles positions and records PnL correctly, but USDC.e is never actually redeemed
- Winning tokens sit in the wallet until manually redeemed via Polymarket UI
- Impact: bankroll (from get_balance_allowance) won't reflect settlement proceeds until manual redemption
- Mitigation: Not a blocker for going live — PnL tracking works, but capital recycling is manual

### FINDING 4 (LOW): BalanceAllowanceParams missing asset_type
- _fetch_live_balance() passes BalanceAllowanceParams() with asset_type=None
- The SDK add_balance_allowance_params_to_url skips asset_type query param when None
- API likely defaults to collateral, but explicitly pass AssetType.COLLATERAL for safety
- signature_type=-1 is correctly handled by SDK (overwritten to builder.sig_type)

### FINDING 5 (HIGH): No credential validation before going live
- ui_start_full() in bot.py does NOT validate API credentials before attempting trades
- If PRIVATE_KEY, API_KEY, API_SECRET, or API_PASSPHRASE are wrong/missing:
  - create_clob_client() succeeds (just creates the object)
  - First call to create_order() triggers assert_level_1_auth() which raises
  - First call to post_order() triggers assert_level_2_auth() which raises
  - Error is caught by _make_clob_error() but user gets a cryptic "Batch order rejected" warning
- Fix: Add a pre-flight credential check — call client.get_ok() or client.get_balance_allowance() BEFORE entering trading loop. If it fails, surface clear error on dashboard.

### FINDING 6 (MEDIUM): Rate limit budget analysis
- Per-tick API calls in live mode:
  - get_orders() = 1-4 calls (paginated) every 500ms = 2-8 req/sec (limit: 90/sec)
  - place_limit_buy() = 1 call per order, batch of ~20 per new market = burst of 20 (limit: 350/sec burst)
  - cancel_batch() = 1 call per order = up to 20 per reprice (limit: 300/sec burst)
  - get_order_book() = called per best_ask check = 2 per market per tick (limit: 150/sec)
  - get_midpoint() = polled separately by ClobMidpointPoller every 3s per token
  - get_balance_allowance() = every 60s (limit: 20/sec)
- Total worst-case burst: ~60 req/sec during ladder posting, well under 900/10s general limit
- Risk area: get_order_book calls accumulate — 8 markets * 2 tokens * 2 calls/sec = 32/sec (limit 150/sec OK)
- Verdict: rate limits are NOT a concern at current scale (1-2 assets, 3-5 markets)

### FINDING 7 (HIGH): No per-market order cancellation on settlement
- When a market settles, cancel_ladder() cancels orders by calling cancel_batch with tracked order IDs
- But the SDK has cancel_market_orders() which cancels ALL orders for a specific market in one call
- Using individual cancel_batch is fine functionally but slower and more API calls
- More importantly: if cancel_ladder() fails mid-batch, some orders remain on exchange
- These orphaned orders could get filled AFTER settlement was recorded
- Fix: Use cancel_market_orders() for settlement cancellation as atomic operation

**Why:** Findings 1, 3, and 5 are the three that will cause real money loss or confusion in live mode. Finding 2 degrades performance. Findings 4, 6, 7 are defensive hardening.

**How to apply:** Fix 1 and 5 are pre-live blockers. Fix 3 is acceptable as manual workaround initially. Fix 2 should increase live poll interval to 1-2 seconds. Fix 7 is optimization.
