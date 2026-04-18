"""Cycle 24 Phase A: bootstrap 95% CI for mean(pnl | fired=0) at threshold 0.55.

Reads results/sweep/certainty_resweep_per_market.csv and runs 1000 bootstrap iters
with seed=42 to estimate whether mean PnL on gate-miss markets is reliably negative.
"""
import csv
import json
import random
from pathlib import Path

CSV_PATH = Path("C:/Users/pc/Desktop/Bots/PolyBot/results/sweep/certainty_resweep_per_market.csv")
OUT_PATH = Path("C:/Users/pc/Desktop/Bots/PolyBot/results/cycle24/phaseA_bootstrap.json")

THRESHOLD = "0.55"
N_ITERS = 1000
SEED = 42

pnls_unfired = []
pnls_fired = []
with CSV_PATH.open() as f:
    reader = csv.DictReader(f)
    for row in reader:
        if row["threshold"] != THRESHOLD:
            continue
        pnl = float(row["pnl"])
        if row["fired"] == "0":
            pnls_unfired.append(pnl)
        else:
            pnls_fired.append(pnl)

n = len(pnls_unfired)
point_mean = sum(pnls_unfired) / n if n else 0.0
point_fired_mean = sum(pnls_fired) / len(pnls_fired) if pnls_fired else 0.0

rng = random.Random(SEED)
means = []
for _ in range(N_ITERS):
    sample = [pnls_unfired[rng.randrange(n)] for _ in range(n)]
    means.append(sum(sample) / n)
means.sort()

ci_low = means[int(0.025 * N_ITERS)]
ci_high = means[int(0.975 * N_ITERS)]

n_above_zero = sum(1 for m in means if m >= 0.0)
p_one_sided = n_above_zero / N_ITERS  # P(mean >= 0) under bootstrap dist

# Win rate on unfired subset
# Define a win as pnl > 0 on the paired-ladder fallback.
n_wins_unfired = sum(1 for p in pnls_unfired if p > 0.0)
wr_unfired = n_wins_unfired / n if n else 0.0

n_wins_fired = sum(1 for p in pnls_fired if p > 0.0)
wr_fired = n_wins_fired / len(pnls_fired) if pnls_fired else 0.0

result = {
    "threshold": float(THRESHOLD),
    "n_unfired": n,
    "n_fired": len(pnls_fired),
    "unfired": {
        "point_mean_per_market": round(point_mean, 4),
        "sum_pnl": round(sum(pnls_unfired), 2),
        "win_rate": round(wr_unfired, 4),
        "bootstrap_95ci_low": round(ci_low, 4),
        "bootstrap_95ci_high": round(ci_high, 4),
        "bootstrap_p_mean_geq_0": round(p_one_sided, 4),
        "ci_excludes_zero_on_negative_side": ci_high < 0.0,
    },
    "fired": {
        "point_mean_per_market": round(point_fired_mean, 4),
        "sum_pnl": round(sum(pnls_fired), 2),
        "win_rate": round(wr_fired, 4),
    },
    "conclusion": (
        "UNFIRED SUBSET PnL IS RELIABLY NEGATIVE (95% CI excludes 0)"
        if ci_high < 0.0
        else "UNFIRED SUBSET PnL NOT RELIABLY NEGATIVE"
    ),
    "bootstrap_iters": N_ITERS,
    "seed": SEED,
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
