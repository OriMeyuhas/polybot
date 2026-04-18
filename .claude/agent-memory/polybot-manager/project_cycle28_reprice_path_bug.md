---
name: Cycle 28 — reprice-path gate-persistence bug
description: Book-mid gate suppresses only the first ladder post; REPRICE events rebuild both-sided ladders, nullifying the gate's one-sided decision. Root cause of cycle 28 losses.
type: project
---

**Fact**: The book-mid gate's one-sided budget (`budget_up=cap, budget_dn=0` or vice versa) is
applied on the initial `LADDER POSTED` call. REPRICE events (every 10s for 15m = ~90/market)
call `post_orders` on both sides without consulting the gate. The loser-side budget is reset
to the paired allocation. By mid-window the posted orders on the loser side often exceed the
winner-side allocation 10–20x.

**Evidence**: `polybot.log` 2026-04-18 04:26:27 onwards on market btc-updown-15m-1776474900:
- 04:26:27 `LADDER POSTED: 0 UP rungs + 10 DN rungs | UP=$0 DN=$18` (gate-compliant)
- 04:26:37 REPRICE posts 10 UP rungs totaling ~140 shares (gate decision lost)
- Settlement: up_qty=198.3, dn_qty=8.9 — completely inverted from gate intent.

Direction of 4/4 recent gate-fires was CORRECT, but pnl was -$27.93 because the loser side
overfilled.

**Why**: The gate sets local `budget_up`/`budget_dn` variables within `post_ladder()`. These
are not persisted onto the ladder state / market state. The reprice path (`_reprice_ladder` or
similar in `ladder_manager.py`) recomputes budgets from scratch and treats the market as a
normal paired window.

**How to apply**:
- Cycle 29 MUST address this with a proper plan → coder → tester chain. Do not ship threshold
  changes or research-driven strategy tweaks until this is fixed — they'll measure against a
  broken baseline.
- Preferred fix: persist `(gate_fired: bool, gate_winner_side: Side | None, gate_budget_cap)`
  on the ladder state when first set. Reprice honors these: reposting only winner side, cap
  at `gate_budget_cap`.
- Alternative: rerun gate on every reprice. Riskier — late-window certainty may drop below
  threshold, causing the ladder to belatedly post the loser side when there's no time to fill.
- Tests needed: reprice path called N times after a gate-fire initial post should NEVER place
  orders on the loser side. Add to `tests/test_book_mid_gate.py`.

**Impact quantification**: at POSITION_SIZE_FRACTION=0.05, 4-market sample showed -$6.98/mkt.
Projected gate-clean behavior (loser side skipped): roughly +$4/mkt per Dome (gate-fire subset).
Fix could flip -$7/mkt → +$4/mkt = +$11/mkt uplift. On 10-20 markets/hr = +$110/hr paper upside.
