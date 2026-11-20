#!/usr/bin/env bash
# prose-timing-test.sh -- verify prose-check.sh completes in < 1000ms
# Usage: ./tests/fixtures/prose-timing-test.sh [path-to-prose-check.sh] [path-to-sample-contract.json]

set -euo pipefail

SCRIPT="${1:-lib/prose-check.sh}"
CONTRACT="${2:-tests/fixtures/prose-test-banned.json}"

if [ ! -f "$SCRIPT" ]; then
  echo "ERROR: script not found: $SCRIPT" >&2
  exit 1
fi
if [ ! -f "$CONTRACT" ]; then
  echo "ERROR: contract not found: $CONTRACT" >&2
  exit 1
fi

START=$(python3 -c "import time; print(int(time.time() * 1000))")
"$SCRIPT" "$CONTRACT" > /dev/null
END=$(python3 -c "import time; print(int(time.time() * 1000))")
ELAPSED=$((END - START))

echo "prose-check elapsed: ${ELAPSED}ms"
if [ "$ELAPSED" -lt 1000 ]; then
  echo "PASS: completed in ${ELAPSED}ms (< 1000ms)"
  exit 0
else
  echo "FAIL: too slow (${ELAPSED}ms >= 1000ms)" >&2
  exit 1
fi
