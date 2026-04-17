---
name: Cycle 7 Paper Fill Engine and Alpha Opportunities
description: Paper fill engine fills 100% at any depth — gives fake 0.82 pair cost when realistic would be 0.91. Rebalancing code has 3 call-site bugs that crash at runtime. price_to_beat is unused alpha signal.
type: project
---

## Cycle 7: Paper Fill Engine Realism + Alpha (2026-03-28)

### Critical Bug Found: _tighten_light_side() has 3 broken call sites
1. `build_ladder_rungs()` called with wrong kwargs (num_rungs, missing budget/spacing/fee_rate)
2. `place_batch_limit_buys()` called with (token_id=, prices=, sizes=) but signature takes (orders: list[dict])
3. `_filter_rungs_by_pair_cost()` called with 3 args but signature takes (self, market_id, side, rungs, other_side_ask, max_pair_cost)

**Result**: Active rebalancing (Cycle 6 design) crashes on every invocation. This MUST be fixed before any further strategy work.

### Paper Fill Engine Analysis
- Current: `if market_price <= order_price: fill 100%` — binary, no distance sensitivity
- Log evidence: fills at $0.23 (0.27 from ask) treated same as $0.48 (0.02 from ask)
- VWAP with current model: 0.41 → pair cost 0.82 (looks great)
- VWAP with realistic model: 0.455 → pair cost 0.91 (barely profitable)
- **Current paper mode overstates profitability by ~10 cents on pair cost**

### Proposed Fill Probability Model (3-tier exponential decay)
- Near market (distance <= 0.02): 80% fill per tick
- Mid-range (0.02-0.05): 40% fill per tick  
- Deep (0.05-0.10): 15% fill per tick
- Very deep (>0.10): 5% fill per tick

### Alpha Opportunity: price_to_beat (strike price)
- `price_to_beat` is available on every MarketWindow but UNUSED by strategy
- It's the Chainlink oracle price that determines settlement (UP wins if spot > price_to_beat)
- Current bot uses Binance open-price snapshot instead — potential oracle/exchange divergence
- **Alpha angle**: if current_spot is already significantly above/below price_to_beat, the UP/DN probabilities are not 50/50. Could bias ladder budget allocation.

**Why:** Paper results are not predictive of live performance without realistic fill simulation. The VWAP gap (0.82 vs 0.91) means strategies that look profitable in paper will lose money live.

**How to apply:** Implement fill probability in PaperClobClient.tick(). Fix _tighten_light_side() call sites. price_to_beat integration is lower priority but high-value.
