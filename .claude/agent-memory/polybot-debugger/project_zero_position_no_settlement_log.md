---
name: Zero-position markets do not emit "Settled" log lines
description: A market that expires with zero filled shares has no position, so _settle_expired_windows skips it and no "Settled ... PnL=$" line ever appears — this is correct behavior, not a bug
type: project
---

Markets that post ladders but have zero fills (gate-miss into directional-only that never takes) have `position_manager.positions.get(mid) is None`. `_settle_expired_windows` in `polybot/bot.py` (around line 1069-1071) explicitly `continue`s when `pos is None`. Consequence: no "Window expired for X — pending settlement" and no "Settled X: OUTCOME, PnL=$Y" log line is emitted.

**Why:** Zero position = zero PnL = nothing to settle. The settlement plumbing (redemption queue, PnL update, bankroll update, settlement_log.jsonl write) only runs inside the `if pos:` branch of `_settle_position`.

**How to apply:** If you are asked to investigate "settlement loop silently broken" because some market expired without a "Settled" line, first check whether that market ever had a fill (`grep "FILL.*<market_id>" polybot.log`). No fills → no position → no settlement log is **expected**. This is NOT a cycle 29 / gate-persistence regression.

**Evidence (2026-04-18 incident, falsely attributed to 8d200ba):**
- `btc-updown-15m-1776480300` — posted DN-only ladder (10 rungs @ $0.04-$0.13), DIRECTIONAL BUY @ $0.25 x 123 — zero fills, cancel at 05:59:51, expired 06:00:09, no Settled line. Polymarket resolved DOWN but we had no position.
- `btc-updown-15m-1776481200` — same pattern, 3 DN rungs posted, no fills, cancel at 06:14:52, expired 06:15:10.
- `btc-updown-15m-1776479400` — same strategy, but DIRECTIONAL BUY @ $0.54 x 51 DID fill → position of dn_qty=89.46 → Settled DOWN PnL=$58.

Bot STATUS at time of "bug" report: `positions=0 ladders=0 trades=11 pnl=$59.90 bankroll=$614.65` — healthy.

**If you do want a settlement event for zero-position expiries** (audit/visibility reason), the change would be in `_settle_expired_windows` to emit a "Window expired, no position" log and write a zero-PnL settlement_log.jsonl entry — but that is a feature request, not a fix.
