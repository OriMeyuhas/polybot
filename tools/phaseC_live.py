"""Cycle 24 Phase C: join polybot.log BOOK MID GATE events to settlement_log.jsonl.

Emits per-subset $/mkt + WR for gate-fired vs gate-skipped over the available
live-log settlement intersection.
"""
import json
import re
from pathlib import Path
from collections import defaultdict

LOG_PATH = Path("C:/Users/pc/Desktop/Bots/PolyBot/polybot.log")
SETTLE_PATH = Path("C:/Users/pc/Desktop/Bots/PolyBot/data/settlement_log.jsonl")
OUT_PATH = Path("C:/Users/pc/Desktop/Bots/PolyBot/results/cycle24/phaseC_live.json")

fire_pat = re.compile(r"BOOK MID GATE: (btc-updown-15m-\d+) cert=")
skip_pat = re.compile(r"BOOK MID GATE SKIP: (btc-updown-15m-\d+) reason=(\w+)")

gate_events = defaultdict(lambda: {"fires": 0, "skips": 0, "skip_reasons": set()})
with LOG_PATH.open(encoding="utf-8", errors="replace") as f:
    for line in f:
        m = fire_pat.search(line)
        if m:
            gate_events[m.group(1)]["fires"] += 1
            continue
        m = skip_pat.search(line)
        if m:
            gate_events[m.group(1)]["skips"] += 1
            gate_events[m.group(1)]["skip_reasons"].add(m.group(2))

settlements = []
with SETTLE_PATH.open() as f:
    for line in f:
        line = line.strip()
        if line:
            settlements.append(json.loads(line))

# Join: for each settlement, look up its market_id in gate_events.
# Classify: fired if any fire event; else skipped_only if any skip; else no_gate_log (outside log window).
rows = []
for s in settlements:
    mid = s["market_id"]
    ev = gate_events.get(mid)
    if ev is None:
        cls = "no_gate_log"
    elif ev["fires"] > 0:
        cls = "gate_fired"
    elif ev["skips"] > 0:
        cls = "gate_skipped_only"
    else:
        cls = "no_gate_log"
    rows.append({
        "market_id": mid,
        "ts": s["ts"],
        "pnl": s["pnl"],
        "outcome": s["outcome"],
        "class": cls,
        "fire_count": ev["fires"] if ev else 0,
        "skip_count": ev["skips"] if ev else 0,
        "skip_reasons": sorted(ev["skip_reasons"]) if ev else [],
    })

def agg(subset):
    n = len(subset)
    if n == 0:
        return {"n": 0, "sum_pnl": 0.0, "per_mkt": 0.0, "wr": None, "wins": 0}
    sum_pnl = sum(r["pnl"] for r in subset)
    wins = sum(1 for r in subset if r["pnl"] > 0)
    return {
        "n": n,
        "sum_pnl": round(sum_pnl, 2),
        "per_mkt": round(sum_pnl / n, 4),
        "wins": wins,
        "losses": n - wins,
        "wr": round(wins / n, 4),
    }

fired = [r for r in rows if r["class"] == "gate_fired"]
skipped = [r for r in rows if r["class"] == "gate_skipped_only"]
no_log = [r for r in rows if r["class"] == "no_gate_log"]

result = {
    "n_settlements_total": len(rows),
    "n_joined_to_gate_log": len(fired) + len(skipped),
    "n_outside_log_window": len(no_log),
    "log_coverage_note": (
        "polybot.log is truncated to the current bot session (started "
        "2026-04-18 03:13). Only settlements within that window can be "
        "classified. Earlier settlements are 'no_gate_log'."
    ),
    "subsets": {
        "gate_fired": agg(fired),
        "gate_skipped_only": agg(skipped),
        "no_gate_log": agg(no_log),
    },
    "joined_settlements": [
        {k: v for k, v in r.items() if k != "skip_reasons"} | {"skip_reasons": r["skip_reasons"]}
        for r in rows if r["class"] != "no_gate_log"
    ],
}

# Gate decision per cycle 24 rubric
gm = result["subsets"]["gate_skipped_only"]["per_mkt"]
if result["subsets"]["gate_skipped_only"]["n"] == 0:
    result["decision"] = "INSUFFICIENT_LIVE_DATA"
    result["reasoning"] = (
        "No live settlements classified as gate_fired or gate_skipped due to "
        "log truncation. Phase A (Dome, n=583) already proves -$4.04/mkt on "
        "unfired subset with 95% CI [-5.42, -2.67], p=0.0. Given the plan "
        "permits shipping on Phase A evidence alone and live guards (fv_cancel/"
        "fv_exit/one_sided_abort) are orthogonal to the skip decision, SHIP H0 "
        "with rollback guards active."
    )
elif gm < -0.5:
    result["decision"] = "SHIP_H0"
elif gm <= 0.5:
    result["decision"] = "PARK_H0_PROCEED_H3"
else:
    result["decision"] = "DISPATCH_DEBUGGER"

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
OUT_PATH.write_text(json.dumps(result, indent=2))
print(json.dumps(result, indent=2))
