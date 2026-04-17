# PolyBot Researcher Memory Index

- [Codebase Analysis 2026-03-28](analysis_2026-03-28.md) -- First deep-dive: 5 critical findings for live readiness
- [Cycle 2 Analysis 2026-03-28](analysis_cycle2_2026-03-28.md) -- Reprice churn, unwired risk guards, cancel race conditions, 6 ranked findings
- [Cycle 3 Analysis 2026-03-28](analysis_cycle3_2026-03-28.md) -- Pre-live: event loop blocking, missing daily reset, redeemer false success
- [Cycle 4 Profitability 2026-03-28](analysis_cycle4_2026-03-28.md) -- Root cause: top-rung pair cost > 1.0, VWAP guard is misleading at small bankrolls
- [Cycle 5 Alpha Gen 2026-03-28](analysis_cycle5_2026-03-28.md) -- Imbalance is #1 PnL predictor; active rebalancing and tightening are highest-impact
- [Cycle 6 Rebalancing 2026-03-28](analysis_cycle6_2026-03-28.md) -- Two-tier active rebalancing design: tighten light side at 0.20, cluster at 0.35
- [Cycle 7 Paper Fill + Alpha 2026-03-28](analysis_cycle7_2026-03-28.md) -- Paper fill engine overstates profit by ~10c pair cost; rebalance has 3 crash bugs; price_to_beat unused
- [Cycle 8 Analysis 2026-03-30](analysis_cycle8_2026-03-30.md) -- 2hr paper run: 46% WR, -$3.17 PnL, 100% imbalance timeout, VWAP underestimates by $0.147
- [Cycle 9 Analysis 2026-03-31](analysis_cycle9_2026-03-31.md) -- 181 real settlements: one-side-cap inverts winners (89% DN-only loss rate), exposure_factor unwired, VWAP guard 55% leak
- [Cycle 10 Whale Imbalance 2026-03-31](analysis_cycle10_whale_imbalance_2026-03-31.md) -- 111K whale trades: 96.6% two-sided via patience not rebalancing, one-side-cap destroys 49% of winners, exits are loss-cutting
- [Cycle 11 Session Health 2026-04-08](project_cycle11_session_health.md) -- 77 settlements $472->$972, $29/hr. Paired excess drag 70% adverse. 15m margin tight. Healthy.
- [Cycle 12 Auto-Lock Excess 2026-04-08](analysis_cycle12_autolock_excess_2026-04-08.md) -- 25-set: paired +$53 destroyed by -$80 excess. AUTO-LOCK creates asymmetric accumulation. FV cancel 50% accuracy.
- [Cycle 15 Pair Cost Guard 2026-04-09](analysis_cycle15_2026-04-09.md) -- 621 stl: MAX_PAIR_COST 0.98→0.95 destroyed paired rate (64%→29%). Fix: raise back to 0.98.
- [Cycle 16 1h Bankroll Crisis 2026-04-09](analysis_cycle16_1h_bankroll_2026-04-09.md) -- 1h losses are bankroll-driven. 2x sizing at <$550 is the root cause. Disable 1h below $450, 1x sizing $450-600.
