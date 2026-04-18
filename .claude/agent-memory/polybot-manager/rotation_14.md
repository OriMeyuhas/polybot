---
name: Rotation 14 Ship — POSITION_SIZE_FRACTION 0.01 -> 0.05
description: 2026-04-18 dome-sweep winner shipped, bot restarted at bankroll $540
type: project
---

# Rotation 14 Ship — 2026-04-18

**Change shipped**: `POSITION_SIZE_FRACTION=0.01 -> 0.05` in `.env`.
**Commit**: aff7c60 `feat(strategy): position_size_fraction 0.01->0.05 (dome-sweep winner)`.
**Plan**: `docs/plans/dome-sweep-2026-04-18.md`.
**Tests**: 1004/1004 passing.
**Bot state after restart**: PID 28704, $540 bankroll, 2 active markets, `Trading started via UI`.

## Why: dome-sweep evidence

14-day (actually 19-day) Dome snapshot sweep across 11 configs + 3 custom configs
mirroring live `.env`. Single-knob change from current live showed:
- Train $/mkt +$2.30 -> +$5.54 (Sharpe 0.82 -> 0.64)
- Holdout $/mkt +$3.01 -> +$7.97 (Sharpe 1.04 -> 0.87) — **+165% uplift preserved OOS**
- Max loss -$5.24 -> -$18.04 (bounded by DIRECTIONAL_BUDGET_CAP=$18).

## How to apply / rollback

Rollback trigger: if 10-settlement rolling PnL after restart is < -$30, or any
single-market loss exceeds $25, revert `.env` `POSITION_SIZE_FRACTION` to 0.01 and restart.

## Exit criteria per user mandate

Rolling PnL > +$20 across 20 consecutive settlements AND 0 errors AND tests green.
Until then, keep monitoring. Next scheduled research dispatch at +10 settlements.
