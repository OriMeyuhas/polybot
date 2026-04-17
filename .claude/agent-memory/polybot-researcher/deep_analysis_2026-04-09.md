# Deep Analysis — 2026-04-09

Priority dispatch from manager. Three questions answered with hard data.

---

## 1. FILL REALISM ANALYSIS

### Methodology

Cross-referenced 152 fills from the last 20 settlements (order_log) against 2.17M book
snapshots (book_log) from the same time windows. For each fill, found the nearest
price_change event (within 30s) across all active asset_ids (best_bid in [0.05, 0.95]
range) and measured the gap between fill price and real best_bid.

### Results

| Category | Fills | % of Fills | Cost ($) | % of Cost |
|----------|-------|------------|----------|-----------|
| Realistic (within 5c of best_bid) | 87 | 57.2% | $399.61 | 52.0% |
| Phantom (>5c from best_bid) | 6 | 3.9% | $36.84 | 4.8% |
| No book data available | 59 | 38.8% | $331.38 | 43.2% |
| **Total** | **152** | | **$767.83** | |

Gap distribution (verified fills only):
- Median gap: $0.010
- P90 gap: $0.040
- Max gap: $0.150

### Interpretation

**The good news**: Only 3.9% of fills are verifiably phantom (>5c from any real best_bid).
When book data IS available, the paper client's `_get_real_book_depth()` check works — 93.5%
of book-verified fills are within 5c.

**The bad news**: 38.8% of fills have ZERO book data to validate against. These fills went
through the paper client's fallback path (`return True, float("inf")`) — meaning they were
filled based on pure probabilistic simulation with no real book check.

**Root cause**: When a new 15m market opens, the book WS subscription takes time to receive
data for the new token_ids. During this lag window (often the first 30-60 seconds), the paper
client simulates fills without any real book validation. This is exactly when 58% of all fills
happen (pre-open period, -30s to 0s).

### Profit Impact

- Last 20 settlements PnL: **$133.58**
- Unverified fill cost: $368.22 (48.0% of all fills)
- If we conservatively assume 50% of unverified fills are phantom (would not have filled on
  real books), the "real" PnL could be ~40-60% lower than reported
- **Estimated realistic PnL: $55-80** vs reported $133.58

### Recommendation

**CRITICAL FIX**: In `PaperClobClient._get_real_book_depth()`, change the fallback from
`return True, float("inf")` to `return False, 0.0` when no book data exists. This will
block fills that have no real book validation, making paper PnL a reliable signal.

Additionally, add HTTP book seeding (`book_manager.seed_book_http()`) for new tokens before
the pre-open fill window begins.

---

## 2. 1H vs 15M ANALYSIS

### Full Historical Data (583 settlements)

| Metric | 15m (442 stl) | 1h (141 stl) |
|--------|---------------|--------------|
| Total PnL | $3,195.17 | $3,207.88 |
| Avg PnL/settlement | $7.23 | $22.75 |
| Win rate | 52.7% | 47.5% |
| Paired rate | 52.0% | 44.0% |
| One-sided rate | 48.0% | 56.0% |
| Avg pair cost | $0.831 | $0.751 |

### Recent Performance (Last 30 of each)

| Metric | 15m (30 stl) | 1h (30 stl) |
|--------|--------------|-------------|
| Total PnL | $90.52 | $316.37 |
| Avg PnL/stl | $3.02 | $10.55 |
| Win rate | 63% | 67% |
| Paired rate | 57% | 47% |
| Paired WR | 71% | 79% |
| One-sided WR | 50% | 56% |
| Avg cost deployed | $30.03 | $66.72 |

### PnL/Hour Efficiency

- **15m**: $3.02/stl x 4 stl/hr = **$12.10/hr**
- **1h**: $10.55/stl x 1 stl/hr = **$10.55/hr**
- Historical: 15m=$28.92/hr, 1h=$22.75/hr

### One-Sided Fill Analysis (1h)

1h settlements are 56% one-sided (vs 48% for 15m). But one-sided 1h settlements have
a 56% win rate with avg $8.92 PnL — the FV brain is correctly predicting direction
more often than not in the longer timeframe.

1h paired settlements average $12.40 PnL with 79% win rate — excellent.

### Capital Competition

1h and 15m run in PARALLEL — they do not compete for capital. The position_manager
tracks them independently. So 1h markets are purely additive to PnL.

The 1h fill rate is low (2.7% vs 15.8% for 15m), meaning 97.3% of posted orders are
cancelled. This costs almost nothing (maker fee = 0%) but does consume order capacity.

### Verdict: KEEP 1H MARKETS

**Do NOT disable 1h markets.** The math is clear:

1. 1h contributes $10.55/hr of marginal PnL
2. 1h has higher per-settlement PnL ($10.55 vs $3.02)
3. 1h does not block 15m capital
4. 1h has improving metrics: recent 67% WR vs historical 47.5%
5. 1h paired WR of 79% is the highest in the system

The only concern is the 2.7% fill rate (wasteful posting), but since maker fees are $0,
the cost is negligible. If anything, **increase 1h position size** since it has strong
recent performance.

---

## 3. STRATEGIC ANALYSIS — Money Left on the Table

### A. Fill Rate: 6.8% Overall — Can We Do Better?

| Window | Posts | Fills | Fill Rate |
|--------|-------|-------|-----------|
| 15m | 5,592 | 884 | 15.8% |
| 1h | 12,071 | 322 | 2.7% |
| **Total** | **17,663** | **1,206** | **6.8%** |

The ladder posts at prices from $0.00 to $0.55, but fills cluster at $0.40-$0.55. Orders
below $0.35 almost never fill. The 1h markets especially waste orders — 51+ rungs per side
across many repricing cycles, but only 322 fills total.

**Improvement**: Dynamic ladder floor. Don't post below `midpoint - 0.10`. This concentrates
orders where fills actually happen. Won't increase fill count but reduces wasted API calls
(matters for live mode).

### B. Timing: 58% of Fills Happen Pre-Open

| Window Phase | Fills | % | Avg Price |
|-------------|-------|---|-----------|
| Pre-open (<0s) | 512 | 57.9% | UP=0.467, DN=0.453 |
| Early (0-3min) | 267 | 30.2% | UP=0.457, DN=0.431 |
| Mid (3-10min) | 83 | 9.4% | UP=0.459, DN=0.444 |
| Late (10-15min) | 22 | 2.5% | UP=0.450, DN=0.287 |

Most fills happen in the 30-second pre-open window. After 3 minutes, fill rate drops
dramatically. This makes sense: early in the window, both UP and DN are near 50/50,
so both sides are attractive. As time passes, one side becomes clear and only that side
fills (one-sided).

**Improvement**: Enter even earlier. If the pre-open window could be extended from 30s
to 60s, we'd capture more two-sided fills. Check if Polymarket creates markets earlier
than 30s before open.

### C. Position Size: Severely Under-Allocated

- Current POSITION_SIZE_FRACTION: **5%** ($25/side on $500 bankroll)
- Avg cost deployed per settlement: $32.87 (6.1% of bankroll)
- MAX_DAILY_DRAWDOWN_PCT: 15%

The position sizing analysis shows:

| Multiplier | PnL (50 stl) | Max Drawdown | DD % of Bankroll |
|------------|-------------|--------------|------------------|
| 0.5x | $15.73 | $75.18 | 15.0% |
| 1.0x (current) | $31.46 | $150.36 | 30.1% |
| 1.5x | $47.19 | $225.53 | 45.1% |
| 2.0x | $62.91 | $300.71 | 60.1% |

The 30.1% drawdown at 1.0x is spread across multiple days (50 settlements), not a single
day. With the 15% daily drawdown limit, the actual daily exposure is capped.

**However**: Until fill realism is fixed (Issue #1), we cannot trust the drawdown numbers.
Don't increase position size until paper fills are validated against real books.

### D. Adverse Selection: Better Than Expected

One-sided 15m settlements (last 50): 50% of settlements are one-sided.
- Win rate on one-sided: **61%** (historically 30%, recently much better)
- UP-only fills: 11, DN-only fills: 6 (bias toward UP fills)
- FV brain is correctly canceling losing side more often than not

The FV cancel at 60% certainty is well-calibrated. The 61% win rate on one-sided fills
means the information edge is real.

### E. Pair Cost: Room to Tighten

Recent paired pair cost: $0.918 (avg). Config MAX_PAIR_COST: $0.95.

The spread per pair is $1.00 - $0.918 = $0.082 (8.2% margin). This is healthy. The
pair cost range goes as low as $0.793 — some trades have 20%+ margins.

### F. Structural Edge: Fee Optimization

- All our orders are passive BUY orders = **maker = $0 fee**
- If we were taker, we'd pay $218/day in fees
- Fee structure strongly favors our passive approach
- No improvement needed here

### G. Fill Clustering

88% of fills happen in bursts of 2+ within 10 seconds (avg burst: 6.2 fills).
This is consistent with the paper client's tick-based simulation where multiple
orders can fill on the same tick cycle. In live mode, fills would be more spaced out.

---

## SINGLE HIGHEST-IMPACT IMPROVEMENT

### Fix Paper Fill Realism (Estimated Impact: Accurate PnL measurement)

**The problem**: 39% of paper fills have no book data validation. The paper client
fills them purely on probabilistic simulation. This makes ALL performance metrics
unreliable — we can't tell if our strategy is truly profitable or if we're seeing
phantom fills that would never happen on real books.

**The fix**: Two changes in `polybot/oms/clob_client.py`:

1. **Block fills without book data**: Change `_get_real_book_depth()` to return
   `(False, 0.0)` when `book._last_update == 0` instead of `(True, inf)`.

2. **Pre-seed books**: Call `book_manager.seed_book_http()` for each new market's
   tokens as soon as discovered by gamma, not waiting for WS data.

**Why this is #1**: Every other improvement (position sizing, timing, ladder width)
depends on accurate paper performance measurement. If 39% of fills are phantom, we
can't tell which changes actually improve real-world performance. Fix the measurement
first, then optimize.

**Expected outcome**: Paper PnL will likely DROP by 30-50% (unverified fills will
be blocked). But the REMAINING PnL will be trustworthy, enabling confident parameter
optimization and eventual live deployment.

---

## Summary

| Question | Answer |
|----------|--------|
| Fill realism | 57% verified, 4% phantom, **39% unvalidated** — critical measurement gap |
| 1h markets | **KEEP** — $10.55/hr marginal, 67% WR, additive to 15m |
| Highest-impact change | **Fix book validation** — blocks 39% of unverified fills, makes PnL trustworthy |

**Bottom line**: The strategy logic is sound (FV brain works, pair cost is healthy, adverse
selection is controlled). But the paper simulation has a critical flaw: 39% of fills bypass
book validation. Fix this first, then we can trust the numbers enough to optimize position
sizing and timing.
