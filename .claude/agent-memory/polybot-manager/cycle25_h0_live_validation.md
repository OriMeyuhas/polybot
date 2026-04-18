---
name: cycle25_h0_live_validation
description: First live validation pass of cycle 24 H0 (skip_on_gate_miss=true, commit 36f273e). Crossed-book misses dominate; post-ship sample too small for verdict.
type: project
---

Cycle 25 — first look at H0 (skip paired-ladder on gate-miss) after ship of commit 36f273e at 2026-04-18 04:04:46 local.

**Observed window:** ship through 04:15:50 (~11 min).

**Raw counts (post-ship log only):**
- Gate FIRES (`BOOK MID GATE: ... cert=...`): 1
  - Single fire at 04:10:26: `btc-updown-15m-1776474000` cert=91% UP, budget=$18 (cap=$18), book_mid_up=0.955
- Gate MISSES (`BOOK MID GATE SKIP`): 651 total across 2 markets
  - `crossed_book`: 572
  - `certainty_too_low`: 79
  - `spread_too_wide`: 0
  - `missing_bid_ask`: 0
- `PAIRED SKIP` (H0 skip action fired): 651 — matches misses 1:1, i.e. H0 is engaging correctly on every miss

**Unique market_ids post-ship:** 2 (`btc-updown-15m-1776474000`, `btc-updown-15m-1776474900`)

**Settlements post-ship:** 1 only
- `btc-updown-15m-1776474000`: outcome=UP, up_qty=10.2, dn_qty=0.0, pnl=+$0.82, pair_cost=None → this is the DIRECTIONAL-ONLY win from the single gate fire. Gate predicted UP at cert=91%, market resolved UP. Working as intended.

**Per-market gate fire breakdown:**
- Market 1776474000: 1 fire / 469 checks = 0.2% check-level fire rate; market-level FIRED=yes
- Market 1776474900: 0 fires / 183 checks = 0.0%; market-level FIRED=no (but not yet settled)

**Market-level fire rate (k=1):** 50% (1 of 2 markets had at least one fire). Above 15% rollback floor, but n=2 is statistically meaningless.

**Dominant miss reason: crossed_book (88% of misses).**
This is the cycle 20/21 finding resurfacing at load. At window-open the WS book snapshots are stale/crossed (bid > ask) for tens of seconds; the crossed-book guard correctly rejects them but also denies the gate a chance to fire early. When the book eventually un-crosses, most of the window has elapsed and certainty has to clear 0.80. Two consequences:

1. **Under H0, crossed_book → PAIRED SKIP immediately.** So a significant chunk of markets will now be SKIPPED outright for their first 30-90s. Fewer paired ladders posted → less paired win volume.
2. The 0.2% check-level fire rate is NOT a rollback trigger in itself — most checks happen during crossed/low-cert windows. What matters is MARKET-LEVEL fire rate (did any gate fire fire in market M's lifetime?). That number is 50% (1/2) here but we need ≥20 settlements to trust it.

**Rollback guard status: GREEN (no flip).** Market-level fire rate 50% >> 15% floor, and settlements count n=1 is far below the n=20 needed for a real measurement. Dome projection was 26% fire rate — we're above that with k=1 so nothing to flip.

**What could go wrong (risks to track in cycle 26+):**
- If crossed_book misses persist and markets settle before the book un-crosses, market-level fire rate could collapse below projection.
- The one-observed fire was directional (paid +$0.82 on $18 notional). Dome backtest said gate-fire subset is +$4.36/mkt — watch for the mean to converge there or collapse below.
- PAIRED SKIP suppresses paired-ladder posting. If gate never fires on a market, we post ZERO orders — so bankroll just sits. That's the intended H0 behavior, but if 74% of markets hit this path we'll have very low trade volume.

**Memory writes this cycle:** this file only. No other memory touched.

**Cycle 26 plan:**
- Earliest valid measurement: ship was 04:04:46. With 15m windows, a full new-window settlement accrues every ~15 min. Need ≥20 post-ship settlements for statistical signal.
- 20 × 15min = 5 hours. Earliest valid measurement: **2026-04-18 09:04 local** (~4h 50m from 04:15).
- At cycle 26 (rotation 26 manager wake-up), read `data/settlement_log.jsonl` entries with ts > 1776474286 and compute:
  - Mean PnL per settlement (H0 effect vs pre-H0 rolling 20 baseline of +$45.39 / 20 = +$2.27/mkt)
  - Market-level fire rate (should be ≥15% floor, target Dome's 26%)
  - Decompose by gate_fired={yes,no}: fires should be +$4.36/mkt-ish; misses-that-now-skip should be ~$0/mkt (no posting = no PnL)
  - If fire rate < 15% over n≥20 → flip SKIP_ON_GATE_MISS=false (rollback)
  - If fires-subset < $0/mkt over n≥10 fires → investigate (may be book-mid gate overfit)
- Do NOT ship anything in cycle 25/26 until n≥20 post-ship accrues.

**Orthogonal work (OK to pursue between now and cycle 26):**
- Potential instrumentation: log the post-uncross fire opportunity (time-to-first-non-crossed-book per market) so we know if crossed_book is a 10s or 10min problem.
- None shipped this cycle — cycle 25 is validate-only.

---

## Cycle 27 update (running tally, post-ship ts > 1776474286)

**n=3 post-H0 settlements:**
- 1776474000: UP outcome, pnl=+$0.82 (directional gate fire, cert=91% UP — correct)
- 1776474900: DOWN outcome, pnl=−$15.38 (posted up_cost=$17.60 / dn_cost=$6.68 — UP-heavy, market went DOWN)
- 1776475800: DOWN outcome, pnl=−$8.45 (posted up_cost=$10.00 / dn_cost=$9.55 — UP-tilted, market went DOWN)

**Running:** 1W / 2L, net −$23.01 on 3 markets = **−$7.67/mkt** (vs Dome fires projection +$4.36/mkt and pre-H0 rolling-20 baseline +$2.27/mkt). Sample is n=3 — noise dominates, do not act.

**Directional-bias hypothesis (flagged, investigate at n≥10):**
Both post-H0 losses have outcome=DOWN with up_cost > dn_cost (heavier UP exposure). The single win was also UP. Pattern so far: the strategy (and/or the book-mid gate) is systemically long BTC on short windows. If BTC is range-trading or mean-reverting at the current hour, the UP skew loses. Possible causes to check when n sufficient:
1. Book-mid gate cert=0.80 threshold may be too permissive on UP-skewed books when Binance FV disagrees — gate fires UP on stale/lagging CLOB while spot is rolling over.
2. Paired-ladder sizing skew may be UP-biased even in non-fire markets (LADDER_SIZE_SKEW config).
3. Recent BTC regime shift (2026-04-17 — today) may have flipped from up-trending to down-trending; if the gate's calibration data was from an up-trend period, it will misfire here.

**Do NOT act on n=3.** Confirming standing plan: wait for n≥20 post-ship settlements. Earliest actionable: **cycle ≈34** at ~09:04 local (still ~4h away from current 04:45-ish).

**Cycle 27 action taken:** observation only, this memory update only. Bot healthy (PIDs 31968, 40768 running), 0 errors, rollback guard still GREEN by projection.
