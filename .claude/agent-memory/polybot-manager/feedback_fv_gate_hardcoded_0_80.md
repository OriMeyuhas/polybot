---
name: FV gate threshold is hardcoded 0.80 in source, config value ignored
description: polybot/strategy/ladder_manager.py line 1045 hardcodes `cert >= 0.80` for FV gate. The config field fv_gate_certainty_threshold is inert. Backtester respects it, live does not.
type: feedback
---

When the bot runs live with `FV_GATE_ENABLED=true`, the directional-gate
threshold is not read from `FV_GATE_CERTAINTY_THRESHOLD` / `fv_gate_certainty_threshold`.
`polybot/strategy/ladder_manager.py` line 1045 uses a hardcoded literal:

```python
elif self.cfg.fv_gate_enabled and cert >= 0.80:
```

The `tools/backtester.py` (both local and Dome mode) does read
`cfg.fv_gate_certainty_threshold`, so holdout sweeps that vary this knob are
measuring a different decision boundary than what live actually applies.

Additionally, in Dome-mode backtester, the "FV" input at entry is
`up_mid / (up_mid+dn_mid)` — i.e., the book-mid, not Binance spot. That
makes Dome-mode fv_gate semantically equivalent to live's `book_mid_gate`,
not live's Binance-FV `fv_gate`.

**Why:** Cycle 17 planned to flip `FV_GATE_ENABLED=true` based on a holdout
showing +$9.50/mkt over gate-off. Investigation revealed the holdout was
actually measuring book-mid at 0.55 with no spread guard — a code path that
already exists live (`BOOK_MID_GATE_ENABLED=true`) but is silenced by a tight
spread guard. Flipping the FV flag would activate Binance-FV at 0.80 (a
different mechanism entirely) and almost certainly NOT reproduce the holdout
uplift.

**How to apply:**
1. Never use Dome-mode backtester `fv_gate_enabled` results as evidence for
   whether to flip `FV_GATE_ENABLED`. Map the Dome backtester's FV-at-entry
   to live's `book_mid_gate` path instead.
2. If you want to sweep the live FV gate threshold, you must either:
   (a) patch line 1045 to read `self.cfg.fv_gate_certainty_threshold` first,
   OR (b) build a Binance-aware backtester (price log replay) — Dome-mode
   cannot validate Binance-FV because it doesn't have intra-window spot
   history for every market.
3. Before recommending any Dome-sweep result as live-ready, diff the
   relevant code path in `ladder_manager.py` vs `backtester.py` — they have
   drifted and should not be treated as equivalent.
4. The real actionable finding from the cycle-17 holdout is to loosen
   `BOOK_MID_GATE_MAX_SPREAD` (currently 0.05 — blocks all live fires).
   Queue that as the next experiment, not the FV gate flip.
