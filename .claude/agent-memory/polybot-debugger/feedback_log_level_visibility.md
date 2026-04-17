---
name: LOG_LEVEL=ERROR hides all guard diagnostics
description: When LOG_LEVEL=ERROR, all INFO-level guards in ladder_manager.py are silent — the bot appears to work but nothing is logged
type: feedback
---

The .env default has LOG_LEVEL=ERROR. All guards in _post_ladder_core() log at INFO level:
- "TIGHT MARKET: ..."
- "NO ASKS: ..."
- "MIN CAPITAL: ..."
- "CAPITAL AT RISK: ..."
- "WINDOW TIMING: ..."

When investigating silent failures (0 ladder posts), the first step should be to temporarily set LOG_LEVEL=INFO in .env to see which guard is firing.

**Why:** At ERROR level the log file is empty/near-empty even when guards are firing on every market.

**How to apply:** Always recommend LOG_LEVEL=INFO when diagnosing "no ladders posted" or silent bot behavior.
