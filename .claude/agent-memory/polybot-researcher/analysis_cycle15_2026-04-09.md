---
name: Cycle 15 Pair Cost Guard Analysis
description: MAX_PAIR_COST lowered from 0.98→0.95 destroyed paired rate (64%→29%). Paired rate directly predicts profitability. One-sided trades are net positive (+$1843 over 316 trades). Single fix: raise MAX_PAIR_COST back to 0.98.
type: project
---

## Key findings (2026-04-09, 621 settlements)

- Paired rate >50% = avg +$773/50 settlements. Paired rate <30% = avg -$98/50 settlements
- Current paired rate: 29% (stl 600-620) — lowest since the dark ages (stl 200-249 at 8%)
- Root cause: MAX_PAIR_COST lowered from 0.98 to 0.95
- At 0.98 the guard NEVER triggered (cycle 11). At 0.95 it fires 237 times, blocks 5-10 min per 15m window
- Top-3 VWAP = 0.9973 (what the guard checks) vs realized pair cost = 0.930 (what actually matters)
- One-sided trades: +$1843 over 316 trades (net positive, DO NOT cap budget)
- 1h markets: +$3208 historical, issue is same guard blocking. Keep enabled.
- Pair cost trend: 0.65→0.93 over full history (margin compression but still profitable at $0.07/pair)

**Why:** Routine proactive analysis at 621 settlements. Session PnL -$14.25, identified pair cost guard as root cause.
**How to apply:** Raise MAX_PAIR_COST from 0.95 to 0.98 in .env. No other changes needed.
