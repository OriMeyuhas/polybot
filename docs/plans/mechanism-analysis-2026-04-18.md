# Mechanism Analysis — 2026-04-18 Live Session

**Window:** settlements with ts > 1776474286 (post reprice-gate-persist, commit 8d200ba at 05:27 UTC)
**Session end (this analysis):** last settled market ts = 1776492928 (06:15 UTC)
**Elapsed:** ~5 hours, 16 settled markets, all 15m BTC.
**Config in force:** `SKIP_ON_GATE_MISS=true`, `POSITION_SIZE_FRACTION=0.05` (after 06:47 for last handful), `BOOK_MID_GATE` threshold 0.55, `FV_GATE_ENABLED=false`, `DIRECTIONAL_BUDGET_CAP=$18`.

> Note on `POSITION_SIZE_FRACTION`: the 0.01→0.05 bump shipped at 06:47. All 5 settled gate fires occurred between 04:45 and 06:15 UTC, **before** the bump. So the $-figures below reflect the 0.01 regime — not the 0.05 regime currently live.

---

## 1. Bucket breakdown (16 settled markets)

| Bucket | Criterion | Count | Sum PnL | Mean $/mkt |
|---|---|---:|---:|---:|
| Gate-fired | `BOOK MID GATE:` info line fired; loser side skipped | 5 | +$22.41 | **+$4.48** |
| Gate-skipped with fills | No gate fire; paired-MM ran and took fills | 11 | +$29.90 | **+$2.72** |
| No-position-expire | Bot posted but zero fills | 0 | $0.00 | — |
| **Total** | — | 16 | **+$52.31** | **+$3.27** |

3 additional markets had gate fires (1776487500, 1776491100, 1776492900) but have not settled yet in this log window. They are excluded.

Headline reconciliation: prompt cites "+$8.78/fire signal from live". My computed live mean is **+$4.48/mkt**, not +$8.78. The +$8.78 figure matches the **Dome marginal edge between threshold bands 0.55 and 0.70** (Section 4), not observed live performance. Different metric, different source.

---

## 2. Gate-fired subset deep-dive (n=5)

| Market (15m start UTC) | Cert | Gate dir | Outcome | Book-mid UP at fire | PnL | Result |
|---|---:|:---:|:---:|---:|---:|:---:|
| 2026-04-18 04:30 | 57% | DN | DOWN | 0.215 | +$33.21 | WIN |
| 2026-04-18 05:00 | 61% | UP | UP    | 0.805 | +$0.78  | win (small) |
| 2026-04-18 05:15 | 55% | DN | DOWN | 0.225 | +$1.61  | win (small) |
| 2026-04-18 05:30 | 57% | DN | UP    | 0.215 | **-$13.19** | **LOSS** |
| 2026-04-18 06:00 | 55% | UP | UP    | 0.775 | -$0.00  | breakeven / no fills |

**Stats:**
- Hit rate: 4/5 = 80% (1 breakeven — no fills on winning side either)
- Mean win: +$11.87 ($0.78, $1.61, $33.21) — dominated by a single outlier
- Mean loss: -$13.19 (1 loss)
- Win/loss dollar ratio: 2.70
- Std dev: $17.17 (against a mean of $4.48 — noise-dominated)

**Bootstrap 95% CI on mean $/mkt (1000 resamples, seed=42):**
- CI = **[-$7.44, +$20.25]** — straddles zero
- P(boot mean > 0) = 0.735
- P(boot mean ≤ 0) = 0.265
- **Not statistically distinguishable from zero.**

**Outlier dependence (the most important finding):**
- Without the +$33.21 winner: mean = **-$2.70/mkt** (sum -$10.80 over 4 markets)
- Without the +$33.21 and the -$13.19: mean = +$0.80/mkt (trivial)
- The positive signal is a **single trade**. Remove it and the bucket is negative.

**What produced the +$33.21 winner?** Fills table (order_log_2026-04-18.jsonl):
- 9.8 DN @ $0.76, 8.3 DN @ $0.42, 6.7 DN @ $0.40, 9.0 DN @ $0.22, 8.6 DN @ $0.10, 7.9 DN @ $0.08
- Total 50.3 DN shares at avg **$0.34**, revenue $50.30 at settle, PnL +$33.21
- This is a **ladder accumulating DN into a crashing UP price** — fills ranged from $0.76 down to $0.08. Most of the profit came from rungs deep below book mid at fire (0.215) — i.e. fills obtained *after* the gate fire while price continued to trend.
- Characterization: this is a trend-continuation profit on a *very large* tick-to-settle move. Replicability depends on whether 04:30-04:45 UTC (16.5% BTC crypto move in 15m) is a normal sample. It is a tail event.

**Cert-vs-outcome directional hit rate interpretation:**
- Binomial test, n=5, observed 4 wins, 0 losses in the 4 non-breakeven trials
- P(≥4 wins in 5 | null = 0.57 average cert) = 0.287 — **consistent with null**
- P(≥4 wins in 5 | null = 0.50 coin-flip) = 0.188 — still not significant at α=0.05
- Hit rate is not the bottleneck — dollar-weighting is. The gate picks direction reasonably, but the lone loss (-$13.19) almost cancels all of the 3 non-outlier wins combined ($36.03 - $13.19 = $22.84; remove the tail outlier and it's $0.80/mkt).

---

## 3. Regime analysis

Gate-fired markets clustered in a narrow 90-minute window (04:30-06:00 UTC = ~21:30-23:00 PT). No TOD dispersion to analyze with n=5.

**Book-mid UP at fire (the signal itself):**
- DN fires: book_mid_up ∈ {0.215, 0.225, 0.215} → winners; {0.215} → loser. No discriminating value here; the loser sits at the same book-mid as winners.
- UP fires: book_mid_up ∈ {0.805} → winner; {0.775} → breakeven (no fills). Too few to compare.
- **Book-mid at fire alone does not separate gate winners from the one loser.** Whatever separates the -$13.19 market from the +$1.61 market (same cert, same direction, same book-mid) is not in this feature.

**Spread at fire:** all gate fires passed the crossed-book guard, so spreads were ≥ 0. Not captured cleanly in logs for the 5 fires — upstream state would need extra instrumentation.

**Fill depth pattern:** the only mkt that produced a meaningful dollar win had 6 fills across price tiers 0.76→0.08 (a deep ladder walk). The two small-$ winners had single fills at a single price tier. The loser had 3 fills at avg 0.47. **The gate's $ expectancy is governed by how far price runs after the fire, not by direction accuracy.** This means the signal is leveraged on trend magnitude — a realized-vol exposure the config does not gate on.

---

## 4. Dome holdout cross-reference

The backtester (`tools/backtester.py`) does **not** implement `skip_on_gate_miss` — that knob only exists in the live bot. I cannot reproduce the exact current-live mechanism against Dome.

What is available: threshold sweeps already on disk (`results/threshold_sweep_55.json`, `_60.json`, `_70.json`), which use `fv_gate_enabled=true` + directional budget cap + paired-MM otherwise. This approximates the live mechanism *except* it uses Binance-spot FV (not book-mid) at window-open and does not implement the "skip paired when gate missed" rule. With those caveats:

| Threshold | Markets | Fired% | Total PnL | Mean/mkt (full sample) | Win rate |
|---|---:|---:|---:|---:|---:|
| 0.55 | 790 | 84.8% | +$4,824.35 | +$6.11 | 88.6% |
| 0.60 | 790 | 70.8% | +$4,283.98 | +$5.42 | 82.9% |
| 0.70 | 790 | 41.4% | +$1,808.31 | +$2.29 | 61.1% |

**Marginal edge decomposition (delta between thresholds gives incremental edge of the marginal fires):**
- Fires at ≥0.70: 327 mkts → already baked into `_70` result
- Marginal 0.55-0.70 band: 343 additional fires, incremental PnL = $4824 - $1808 = **+$3,016**
- **Incremental $/mkt of the gate fires in the 0.55-0.70 band = +$8.79/mkt**

This is almost certainly the source of the "+$8.78/fire" figure in the dispatch prompt. It is a **Dome-backtest incremental** number, not a live observation.

**Consistency of Dome signal:**
- All three threshold sweeps show positive mean across the full sample.
- fv_blocked_rate=84.8% at 0.55 is implausibly high vs live (the backtester uses entry-moment Binance-spot FV, which exceeds 0.55 on almost every 15m window due to vol; the live book-mid gate only fires when the CLOB book materializes with conviction, which is typically late in the window and on only 30-40% of markets).
- This Dome-vs-live semantic gap is a known limitation; the two numbers are **not comparable apples-to-apples**.

**Caveat:** a clean Dome holdout mirroring the exact current-live config would require adding `skip_paired_on_gate_miss` to the backtester and using book-mid (not Binance FV) for the gate decision. That is not a 30-minute job.

---

## 5. Verdict

**The observed +$4.48/fire live signal (my computed value; the +$8.78 in the prompt appears to be Dome-derived) is noise-dominated at n=5 and is not distinguishable from zero.** Specifically:

- Bootstrap 95% CI **[-$7.44, +$20.25]** straddles zero.
- The positive sign is **entirely** produced by one tail-event fill sequence (+$33.21). Remove it and the bucket mean becomes **-$2.70/mkt**.
- The one loss (-$13.19) occurred in a market with identical gate features (cert=57, DN-dir, book_mid=0.215) to two of the winners — no in-sample feature separates it.
- Directional hit rate (4/5) is consistent with the coin-flip null (p=0.19).

**Dome-backtest evidence** (independent, n≈343 marginal fires) **does** suggest a meaningful +$8-9/mkt incremental edge in the 0.55-0.70 certainty band. But:
- Backtester uses Binance-spot FV, not book-mid (the live gate's actual input).
- Backtester's fire rate is ~85% vs live's ~30% — the semantic gap is large.
- Backtester does not implement `skip_on_gate_miss`; its paired-MM baseline is different.

**Best estimate of truth:**
1. The mechanism is **plausibly profitable** based on Dome data (different but related signal).
2. The **live** n=5 sample is **too small to confirm or refute** this. It is consistent with either (a) real +$4-9/mkt edge + noise, or (b) zero edge + random draw that happened to land a +$33 tail.
3. The strong recommendation for the next cycle is to let the bot accumulate ≥30 gate fires before claiming anything about the live signal. At $POSITION_SIZE_FRACTION=0.05 the std dev will be ~5× larger and the CI even wider per-trade, so expect ~n=50+ fires before CI tightens below a meaningful threshold.

**What to not conclude:** this session does **not** validate the Cycle 24 `SKIP_ON_GATE_MISS=true` change. It also does not invalidate it. The sample is too small. The +$22 total on gate fires is driven by one market; if the next dispatch uses +$8.78/fire as a live-validated prior, that is mixing Dome and live evidence incorrectly.

---

## Appendix — Data provenance

- Live settlements: `data/settlement_log.jsonl`, ts > 1776474286 → 16 rows (filtered from 57 total)
- Gate fire events: `polybot.log` grep `BOOK MID GATE:` (not SKIP) → 8 total, 5 settled within the window
- Fills: `data/order_log_2026-04-18.jsonl` (12,679 rows)
- Dome backtest: `results/threshold_sweep_{55,60,70}.json` (790 markets, pre-computed)
- Bootstrap: numpy random.seed(42), 1000 resamples with replacement
