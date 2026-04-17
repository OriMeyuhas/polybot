---
name: Cycle 2 Analysis 2026-03-28
description: Deep-dive on reprice churn, unwired risk guards, live-mode gaps, and cancel race conditions — 6 findings ranked by priority
type: project
---

## Findings (2026-03-28, Cycle 2)

### 1. Excessive Reprice Churn (HIGH — profitability)
run_v4.log: 40 reprices in ~13 minutes across 3 windows = 1 reprice every 20s.
reprice_threshold=0.05 triggers on any 5-cent ask move — too sensitive for volatile 5m markets.
Each reprice: cancel all resting on one side + repost. In live mode each cancel+post = 2 API calls per rung.
With 4 rungs per side, that's ~8 API calls per reprice. 40 reprices = ~320 API calls in 13 min.
Risk: rate limits (429s), order churn reduces queue priority, fills missed during cancel-repost gap.

### 2. exposure_factor() and check_capital_at_risk() Never Called (HIGH — risk)
Both methods exist in RiskManager but are never invoked.
exposure_factor() should scale budget down after 3+ consecutive losses.
check_capital_at_risk() should block new ladders when committed > 40% of bankroll.
These were added by cycle 1 but never wired into ladder_manager or bot.py.

### 3. Reprice Budget Ignores Already-filled Opposite Side (MEDIUM — profitability)
reprice_if_needed() sets budget_per_side = min(total_budget/2, available/2).
But it only subtracts already_filled_cost for the side being repriced.
If UP has 3 filled rungs and DN has 0, the UP reprice gets budget = total/2 - up_filled_cost,
but the DN reprice still gets full total/2. This over-allocates to the unfilled side after imbalance.

### 4. Cancel-then-Post Race in Live Mode (MEDIUM — order mgmt)
reprice_if_needed() does: cancel_batch(cancelled_ids) then place_batch_limit_buys(new_rungs).
In live mode, cancel is async — the exchange may still show old orders as "LIVE" for a few hundred ms.
If reconcile() runs between cancel and repost, it could detect the old orders as "disappeared" = filled.
Mitigation: the 10s MIN_REPRICE_INTERVAL helps but doesn't fully prevent if reconcile runs at 500ms.

### 5. Paper PnL All Slightly Negative in run30m_v3 (LOW — investigation)
3 settlements: -$0.02, -$0.01, -$0.06. All pair_costs were 0.88-0.89.
Fills: only 1-3 fills per side (budget too small at $4.95).
At $100 bankroll with 15% fraction = $15 per window, split = ~$5 per side budget.
With pair_cost ~0.88, expected profit per balanced pair = $0.12.
But with only 1-2 fills per side, fees (~1.56% * min(p,1-p)) eat the margin.
The 5m-at-$100 bankroll scenario is marginal by design (micro tier already blocks it in latest code).

### 6. Live Settlement Relies on condition_id from Gamma Discovery (LOW — live)
If Gamma returns stale/wrong condition_id, settlement will fail.
The fetch_condition_id fallback exists but only triggers if condition_id is missing or non-hex.
If Gamma returns a valid-looking but wrong hex condition_id, no fallback kicks in.
