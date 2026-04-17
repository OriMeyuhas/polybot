---
name: Cycle 10 Whale Imbalance Behavior Deep Dive
description: 111K whale trades analyzed. Whale achieves 96.6% two-sided through persistent both-side ladders, NOT active rebalancing. One-side-cap triggers at 4.4% elapsed and would destroy 48.9% of winners. Exits are loss-cutting (72% sold below entry), not rebalancing.
type: project
---

## Cycle 10: Whale Imbalance Behavior (2026-03-31)

### Dataset
- 111,779 trades across 910 unique markets (4 days of data)
- 1,069 settlements with PnL data
- 97,628 entry trades + 14,151 exit trades

### Key Findings

1. **Whale achieves 96.6% two-sided fill rate** (879/910 markets)
   - Only 31 markets (3.4%) end one-sided
   - 15m has best rate: 98.9% two-sided; 1h worst at 93.9%

2. **Whale does NOT actively rebalance imbalanced positions**
   - Mid-window exits target heavy side only 47.1% of the time (basically random)
   - Exits actually WORSEN imbalance (0.254 -> 0.333 avg)
   - Exits are loss-cutting: 72.1% sell below entry VWAP, avg loss -$0.05/share

3. **ALL markets peak at near-100% imbalance early (avg 5.8% elapsed)** then naturally recover
   - Avg peak imbalance: 0.982 (essentially 100%)
   - Avg final imbalance: 0.320
   - 54.7% of markets recover from >0.80 peak to <0.30 final
   - Recovery mechanism: both-side resting orders naturally attract fills over time

4. **One-side-cap would trigger on 99% of whale markets at 4.4% elapsed**
   - Bot cancels heavy side at 3:1 ratio with qty > 5
   - The whale continues getting 55.6 MORE heavy-side fills after this point (99.7% of markets)
   - Heavy side matches settlement outcome 48.9% of the time
   - Cancelling heavy side destroys paired-position profit on half the markets

5. **Pair cost is the dominant profit driver, imbalance is secondary**
   - PC < 0.92 + Imbalance < 0.20: 51.5% WR, +$142.60/market
   - PC < 0.95 (all imbalance): 46.9% WR, +$92.80/market
   - PC >= 0.95: 26.8% WR, -$89.30/market
   - The pair_cost threshold of 0.92 captures most profit; below that, imbalance matters

6. **Fill pattern: both sides fill simultaneously throughout the window**
   - Both sides fill in 32% of time buckets (moderate interleaving)
   - First fills within 7-8% of window start on both sides
   - Fills span ~51% of window on average
   - UP and DN gaps average 18.9% of window (longest gap between same-side fills)

7. **Price dynamics: light side price drops LESS than heavy side at moderate imbalance**
   - At imbalance 0.1-0.3: light side drops 1.5c less per half (subtle aggressive pricing)
   - At imbalance >0.4: pattern reverses (natural time decay dominates)
   - Whale does NOT explicitly raise light-side bids; the effect is from ladder structure

### Whale's Actual Strategy for Imbalance
The whale's approach is **passive patience with loss-cutting exits**:
1. Post ladders on BOTH sides at market entry
2. Keep both sides active throughout the window (no cancellation of heavy side)
3. Exit (sell) tokens that have lost value mid-window (loss-cutting, not rebalancing)
4. Accept that imbalance peaks early and naturally recovers via resting orders on both sides

### Critical Bot Anti-Patterns (vs Whale)
1. **_check_one_side_cap** cancels heavy side at 3:1 ratio -- triggers at 4.4% elapsed, kills 99% of two-sided potential
2. **check_imbalance** cancels heavy side at max_imbalance_ratio (0.35) -- blocks natural recovery from peak
3. **LIGHT-SIDE TIGHTEN** cancels far orders but doesn't repost -- net negative effect
4. Bot treats imbalance as danger; whale treats it as temporary market-driven noise

**Why:** The bot's imbalance handlers assume the first few fills predict the final ratio. The data proves this is false -- 100% of markets peak at near-100% imbalance early, then naturally recover to 0.32 avg.

**How to apply:** Remove one-side-cap entirely. Raise max_imbalance_ratio to 0.60+ or remove it. Let both-side ladders persist and natural fill balance emerge. Only act on imbalance if pair cost exceeds 0.92 (the actual profit boundary).
