#!/bin/bash
# TeammateIdle quality gate for polybot-tester
# Prevents the tester from going idle while any test is failing.
# Exit code 2 = block idle + feed stderr back as feedback to the agent.

INPUT=$(cat)
TEAMMATE=$(echo "$INPUT" | jq -r '.teammate_name' 2>/dev/null)

# Only gate the polybot-tester — all other teammates pass through
if [ "$TEAMMATE" != "polybot-tester" ]; then
  exit 0
fi

CWD=$(echo "$INPUT" | jq -r '.cwd' 2>/dev/null)
cd "$CWD" || exit 0

# Run a quick pytest check (quiet mode, no traceback for speed)
RESULT=$(python -m pytest tests/ -q --tb=no 2>&1 | tail -3)

if echo "$RESULT" | grep -qE "failed|error"; then
  echo "Tests still failing. Fix all failures before stopping. Current status: $RESULT" >&2
  exit 2
fi

exit 0