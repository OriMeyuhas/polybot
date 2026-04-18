---
name: Holdout vs Live FV Gate config mismatch
description: The cycle15 live_baseline holdout was run with fv_gate_enabled=True but live .env has it False. All "+$5.76/mkt expected" projections citing that baseline are inflated and apply to a config the bot is not running.
type: feedback
---

The file `results/cycle15/live_baseline_holdout.json` has `config.fv_gate_enabled=True` with
`fv_gate_certainty_threshold=0.55`. The currently running `.env` has `FV_GATE_ENABLED=false`.
`polybot.log` confirms 48 instances of `"FV GATE DISABLED: would have fired cert=0.81 ...
posting bilateral"` in ~32 markets.

**Why:** Discovered during cycle-16 variance-vs-alpha audit. Rolling-20 live hit +$20.34 but
holdout baseline said it should be +$115.20 over 20 markets ($5.76 * 20). Gap z=1.93. Dug
into config and found fv_gate_enabled mismatch. Also: BOOK MID GATE fired 0 times in-session
(spread > max_spread=0.05 on live markets), SPOT SKIP fired 0 times. Only the FV cancel
(twice) and pair_cost_guard (4 times) fire. The bot is running as a pure market-maker without
the FV directional edge — which is the edge the holdout was measuring.

**How to apply:**
1. Never cite `cycle15_live_baseline` holdout metrics as the live-expected PnL. That config
   is NOT what's running. Before quoting any holdout $/mkt, open the JSON and diff the
   `config` block against `.env`.
2. If the user ever wants to evaluate "should I enable FV_GATE_ENABLED=true?", the right
   experiment is to run the backtester with fv_gate_enabled=False (matching live) as the
   actual baseline, then sweep fv_gate_certainty_threshold with gate enabled.
3. The decision to disable FV gate came from real evidence (cycle pre-14: "fv_gate@0.60
   produced 76-84% loss rate on 452 zero-fill one-sided windows" per ladder_manager.py
   comment). Don't just re-enable it — understand why it was disabled first.
4. Calibration.md's "post-cycle-14: fv_gate@0.55, fv_cancel@0.75, ..." line (bottom of file)
   is wrong and needs correcting to "fv_gate@disabled, fv_cancel@0.75, ...".
