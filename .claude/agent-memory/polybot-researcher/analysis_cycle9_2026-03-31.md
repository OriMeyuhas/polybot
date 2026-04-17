---
name: Cycle 9 Comprehensive Data Analysis
description: 235 settlements analyzed (181 real). 7 ranked findings: one-side-cap inverts winners, exposure_factor unwired, VWAP guard leaks 55%, settlement log polluted by tests, reprice churn 97x/ladder, LIGHT-SIDE TIGHTEN dead, config dead code.
type: project
---

## Cycle 9: Comprehensive Data-Driven Analysis (2026-03-31)

### Data Source
- settlement_log.jsonl: 235 records, 181 real (54 test pollution from test/btc-5m-100 market IDs)
- 6 paper sessions from 2026-03-29 22:50 to 2026-03-31 02:15
- polybot.log: 10MB, latest session at $523 bankroll
- All core strategy code read and cross-referenced

### Overall Real Performance (181 settlements)
- 69W/112L (38.1% WR), Total PnL: $149.69
- Avg PnL/settlement: $0.83
- Two-sided: 99/181 (55%), PnL=$121.94, avg pair_cost=0.890
- One-sided: 82/181 (45%), PnL=$27.75
- Best session (S5): 70 settlements, 87% two-sided, 49% WR, PnL=$67.76

### Performance by Timeframe
| TF | Count | WR | PnL | Avg PC | 2-sided% |
|----|-------|----|-----|--------|----------|
| 5m | 35 | 20% | -$24.34 | 0.928 | 34% |
| 15m | 110 | 42% | +$60.47 | 0.898 | 66% |
| 1h | 36 | 44% | +$113.56 | 0.811 | 39% |

### Finding 1 (CRITICAL): One-Side Cap Inverts Winners Into Losers
**Observation:** DN-only positions have 5W/40L (89% loss rate). UP-only have 11W/26L. DN-only outcomes are 86% UP. UP-only outcomes are 78% DOWN. The one-side-cap locks the WINNING side, leaving only the LOSING side exposed.

**Evidence:**
- 37 DN-only real settlements: 32 resolved UP (86%), 5 resolved DOWN (14%)
- 23 UP-only real settlements: 5 resolved UP (22%), 18 resolved DOWN (78%)
- DN-only PnL: -$59.06 (avg -$1.31/trade)
- UP-only PnL: -$35.07 for DOWN outcomes, +$29.09 for UP outcomes

**Mechanism:** When market trends UP -> UP tokens fill first (cheaper) -> ONE-SIDE CAP locks UP (heavy side) -> only DN fills remain -> DN-only position -> market resolves UP -> LOSS. The cap locks the side that was filling BECAUSE the market moved that way.

**Proposed Change:** Replace one-side-cap cancellation with proportional sizing reduction:
1. When ratio > 3:1, REDUCE heavy side rung sizes to 20% (not cancel entirely)
2. Keep light side at 100% or boost
3. This preserves both-side exposure while limiting imbalance growth
4. Alternative: remove _check_one_side_cap entirely, rely on check_imbalance only

**Expected Impact:** Recovering $59 from DN-only structural losers. Converting half of 82 one-sided to two-sided at 0.89 avg pair cost adds ~$4.50/settlement expected value.

**Risk:** Without any guard, positions could become extremely imbalanced. Keep severe imbalance handler as backstop.

### Finding 2 (CRITICAL): exposure_factor Is Cosmetic -- Never Applied to Sizing
**Observation:** risk_manager.exposure_factor() returns 0.5 after 3+ consecutive losses, but ladder_manager never calls it. 36 settlements at exposure_factor=0.5 had 0W/26L, PnL=-$81.34.

**Evidence:**
- grep exposure_factor polybot/strategy/ladder_manager.py -> 0 matches
- risk_manager.py:35-42 defines the method
- bot.py:1035,1546 logs it; app.js:699 displays it
- ladder_manager.post_ladder() line 155: budget = min(bankroll * lp.position_size_fraction, available) -- no exposure_factor multiplication

**Proposed Change:** In post_ladder(), multiply budget by self.risk.exposure_factor():
budget *= self.risk.exposure_factor()

**Expected Impact:** 26 losses at 0.5x would have lost ~$40 instead of $81. Net savings: ~$40 per similar drawdown.

**Risk:** Could reduce profit during false loss streaks. 0.5x is mild.

### Finding 3 (HIGH): VWAP Pair Cost Guard Leaks 55% of Two-Sided Positions
**Observation:** 54/99 (55%) of two-sided positions have realized pair_cost > 0.90, despite guard set at 0.90.

**Evidence:**
- Pair cost 0.90-0.95: 30 settlements, 14W/16L, PnL=$3.37
- Pair cost 0.95-1.00: 18 settlements, 9W/9L, PnL=-$12.10
- Pair cost >= 1.00: 7 settlements, 2W/5L
- Pair cost <= 0.90: 31/85, 19W/12L (61% WR), PnL=$66.95, avg=$2.16/trade

Guard checks FULL LADDER VWAP but only top rungs fill. Identified in Cycle 4 and Cycle 8, still not fixed.

**Proposed Change:** Replace VWAP guard with TOP-3-RUNG pair cost check. Check the 3 most expensive rungs per side instead of full-ladder average.

**Expected Impact:** Markets passing the tighter guard have 61% WR and $2.16/trade avg vs overall 54% WR and $1.23.

**Risk:** Fewer trades. Monitor fill-rate-per-hour.

### Finding 4 (HIGH): Settlement Log Polluted by Test Data
**Observation:** 54/235 (23%) records are from tests. Market IDs test and btc-5m-100 inject fake $10,000 bankroll settlements.

**Evidence:** Records 0-2 have timestamps 0.03s apart, market_id=test/btc-5m-100, bankroll=$1000/$10009/$10057.

**Proposed Change:** Write test settlements to separate file or skip logging when market_id matches test patterns.

**Expected Impact:** Clean analysis data.

### Finding 5 (MEDIUM): Reprice Churn -- 97 Reprices Per Ladder
**Observation:** 1,552 REPRICE events for 16 ladders = 97/ladder. 579 REPRICE SKIPs (37%). MIN_REPRICE_INTERVAL=5s with reprice_threshold=0.05 means any 5-cent move triggers full cancel-repost.

**Evidence:** 1h windows repriced every 5s for 30+ minutes straight on DN side. Each reprice cancels all resting orders (killing fill opportunities) and replaces them.

**Proposed Change:** Scale reprice cooldown by timeframe: 5s for 5m, 15s for 15m, 30s for 1h. Increase threshold from 0.05 to 0.08 for 15m and 0.10 for 1h.

**Expected Impact:** 50% fewer reprices, more fill time.

### Finding 6 (MEDIUM): LIGHT-SIDE TIGHTEN Never Fires
**Observation:** 0 LIGHT-SIDE TIGHTEN events in entire log. Band is only 0.30-0.35 (5pp). Also, it only CANCELS -- does not REPOST tighter.

**Evidence:**
- max_imbalance_ratio loaded as 0.35 (env), not 0.60 (class default)
- Tighten path line 549 needs 0.30 < imbalance <= 0.35
- Even when triggered, cancelled orders are not replaced

**Proposed Change:** Widen band to 0.20-0.50. After cancelling far orders, REPOST at best_ask - 0.01. Raise max_imbalance_ratio from 0.35 to 0.50.

**Expected Impact:** Active rebalancing converts one-sided to two-sided.

### Finding 7 (LOW): Dead Config Fields
**Observation:** position_size_fraction_5m, position_size_fraction_1h, and position_size_fraction are defined but never read by get_ladder_params(). That method uses get_trading_rules().position_fraction with hardcoded multipliers.

**Evidence:** grep for these fields in get_ladder_params -> 0 hits. Only config.py definition and load_bot_config reference them.

**Proposed Change:** Wire them as overrides or remove to avoid confusion.

### Status of Cycle 8 Findings
1. 100% imbalance timeout -- PARTIALLY: timeout_sec 30->120, max_imbalance 0.60->0.35, but one-side-cap still creates structural losers
2. VWAP guard understatement -- NOT FIXED: 55% leak rate (Finding 3)
3. 1h budget multiplier -- FIXED: now 1.0x (was 0.50x)
4. BTC >> ETH -- CONFIRMED: BTC 60% WR vs ETH 40% WR across all real data
5. NoneType bug -- FIXED: None guard added at line 170

### Priority Ranking
1. Finding 1 (one-side-cap inverts): Highest PnL impact, $59+ direct losses
2. Finding 2 (exposure_factor unwired): Safety-critical for live trading
3. Finding 3 (VWAP guard leaks 55%): Profitability, well-understood fix
4. Finding 6 (LIGHT-SIDE TIGHTEN dead): Synergy with Finding 1
5. Finding 5 (reprice churn 97x): Efficiency and fill opportunity preservation
6. Finding 4 (settlement log pollution): Data integrity
7. Finding 7 (dead config fields): Housekeeping
