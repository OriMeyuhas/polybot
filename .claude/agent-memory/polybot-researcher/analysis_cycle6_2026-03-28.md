---
name: Cycle 6 Active Rebalancing Design
description: Detailed design for two-tier active imbalance rebalancing — tighten light side at 0.20, cluster at 0.35. Includes reprice conflict prevention and cooldown logic.
type: project
---

## Cycle 6: Active Rebalancing Design (2026-03-28)

### Problem
check_imbalance() only cancels heavy side, never helps light side fill. Whale data: balanced fills are 6x more profitable than imbalanced.

### Design: Two-Tier Rebalancing

1. **Moderate (imb 0.20-0.35):** Cancel light side's resting rungs, repost at HALF width (closer to market). Cooldown: 15s per market.
2. **Severe (imb > 0.35):** Cancel heavy side (existing) + tighten light side to 2-3 rungs clustered within 0.03 of best ask.

### Key Implementation Details
- New config: `rebalance_moderate_threshold=0.20`, `rebalance_cooldown_sec=15.0`
- New LadderState fields: `last_rebalance_at`, `rebalanced_side`, `end_epoch`
- `_tighten_light_side()` helper handles both modes via `width_factor` or `cluster_width` params
- Reprice conflict prevented by updating `state.anchor_*` and `state.last_reprice_at` after tightening
- `_filter_rungs_by_pair_cost` applied to tightened rungs — safety valve preserved
- OrderTracker needs NO changes — TrackedOrder is identical for tightened rungs

### Risks Identified
- Churn without cooldown (mitigated by 15s cooldown matching MIN_REPRICE_INTERVAL)
- Pair cost degradation from closer-to-market rungs (mitigated by pair cost filter)
- Paper mode may overestimate fill rate on tightened rungs (monitor)

**Why:** Imbalance is the #1 PnL predictor (Cycle 5). Active rebalancing converts the current passive cancel-and-wait approach into an active fill-seeking mechanism.

**How to apply:** Implement in ladder_manager.py. The Coder agent should modify check_imbalance(), add _tighten_light_side(), and update LadderState/BotConfig. Tests needed for both tiers, cooldown, and reprice non-interference.
