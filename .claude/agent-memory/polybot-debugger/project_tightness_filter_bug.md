---
name: Tightness filter threshold bug (0 ladders posted)
description: market_tightness > 0.98 check on best_ask_up + best_ask_dn blocks all real markets because binary asks always sum > 1.0
type: project
---

**Bug pattern:** Bot discovers markets but posts 0 ladders. All guards are silent (LOG_LEVEL=ERROR). No LADDER POSTED log entries.

**Root cause:** `polybot/strategy/ladder_manager.py` — `_post_ladder_core()` had a tightness filter:

```python
market_tightness = best_ask_up + best_ask_dn
if market_tightness > 0.98:
    return 0
```

In any real binary market: up_mid + dn_mid ≈ 1.0, and best asks are above mid, so asks sum > 1.0 > 0.98 always. This filter fires on every real market.

**Fix:** Remove the filter entirely (or set threshold > 1.0). The `max_pair_cost` VWAP guard already handles expensive markets. The tightness filter was not in the committed baseline.

**Fix applied 2026-03-30:** Removed the 9-line block (lines 250-259 in the broken version).

**Why:** Binary market property — asks on both sides must exceed 0.50 each to create a valid binary book. Any spread above the midpoint pushes the ask sum above 1.0.
