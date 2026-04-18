---
name: Cycle 28 — size revert to 0.01 and regime-shift note
description: Rollback guard fired 2026-04-18. POSITION_SIZE_FRACTION reverted 0.05 → 0.01. SKIP_ON_GATE_MISS kept true. Real root cause is reprice-path bug, not regime shift.
type: project
---

**Fact**: 2026-04-18 rolling-10 hit -$48.45, tripping the cycle-14 rollback guard (-$30).
Reverted `POSITION_SIZE_FRACTION 0.05 → 0.01` in `.env`. Kept `SKIP_ON_GATE_MISS=true` because
gate-direction was 4/4 correct in the failing sample — the damage came from loser-side fills
via the reprice path (see `project_cycle28_reprice_path_bug.md`), not from the H0 skip logic.

**Why**: Cycle-14 guard explicitly called for revert at rolling-10 < -$30. Standing orders
honor shipped guards. Additionally, diagnosis of the 4-market post-H0 sample showed the
losses scale linearly with position size (the loser side overfills because reprice posts
paired ladder; at 0.05 size each rung is 5x larger). Cutting size cuts the bleed until the
real fix ships.

**How to apply**:
- Current state: BANKROLL=$551.13, POSITION_SIZE_FRACTION=0.01, SKIP_ON_GATE_MISS=true,
  FV_GATE_ENABLED=false, book-mid gate on at 0.55. Bot running PIDs 35808/38420.
- Next 10 settlements: measure rolling-10 for bug-confirmation. If it lifts toward 0 or
  positive, the reprice-path hypothesis is confirmed.
- Next 10 settlements: do NOT ship any other strategy changes. The reprice-path fix (cycle 29)
  must land first — other changes would be measured against a broken baseline.
- Original "regime shift against UP bias" hypothesis from cycle 27 is likely WRONG.
  Cycle-27 saw UP-heavy fills and attributed it to market preference; cycle-28 evidence
  shows the UP-heaviness is the reprice-path bug reposting both sides. Do NOT pursue
  regime-based directional bias work until this bug is fixed.

**Rollback guards now active**:
1. Revert SKIP_ON_GATE_MISS to false if next 20 settlements sum PnL < -$10
2. STOP bot + escalate if bankroll < $500
3. Cycle-14 guard still armed at rolling-10 < -$30 (but should not fire at 0.01 size
   under normal variance)
