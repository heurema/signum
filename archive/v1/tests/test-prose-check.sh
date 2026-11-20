#!/usr/bin/env bash
# test-prose-check.sh — tests for lib/prose-check.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CHECKER="$SCRIPT_DIR/../lib/prose-check.sh"

passed=0
failed=0

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

assert_pass() {
  local name="$1" contract="$2"
  local output
  output=$("$CHECKER" "$contract" 2>&1)
  local total
  total=$(echo "$output" | jq -r '.pass')
  if [[ "$total" == "true" ]]; then
    printf '  PASS: %s\n' "$name"
    passed=$((passed + 1))
  else
    printf '  FAIL: %s — expected pass=true, got: %s\n' "$name" "$output"
    failed=$((failed + 1))
  fi
}

assert_findings() {
  local name="$1" contract="$2" category="$3" min_count="$4"
  local output
  output=$("$CHECKER" "$contract" 2>&1)
  local count
  count=$(echo "$output" | jq -r ".categories.${category}.count")
  if [[ "$count" -ge "$min_count" ]]; then
    printf '  PASS: %s (%s=%s >= %s)\n' "$name" "$category" "$count" "$min_count"
    passed=$((passed + 1))
  else
    printf '  FAIL: %s — expected %s count >= %s, got %s\n' "$name" "$category" "$min_count" "$count"
    failed=$((failed + 1))
  fi
}

assert_total() {
  local name="$1" contract="$2" expected="$3"
  local output
  output=$("$CHECKER" "$contract" 2>&1)
  local total
  total=$(echo "$output" | jq -r '.total_findings')
  if [[ "$total" -eq "$expected" ]]; then
    printf '  PASS: %s (total=%s)\n' "$name" "$total"
    passed=$((passed + 1))
  else
    printf '  FAIL: %s — expected total=%s, got %s\n' "$name" "$expected" "$total"
    failed=$((failed + 1))
  fi
}

# --- Fixtures ---

# Clean contract (no issues)
cat > "$WORK/clean.json" <<'EOF'
{
  "goal": "Add a health check endpoint that returns HTTP 200 with JSON body",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "GET /health returns 200 status code"},
    {"id": "AC2", "description": "Response body contains {\"status\": \"ok\"}"}
  ]
}
EOF

# Banned phrases
cat > "$WORK/banned.json" <<'EOF'
{
  "goal": "Add logging as needed for the service",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "Log errors when appropriate"},
    {"id": "AC2", "description": "Add various log levels etc"}
  ]
}
EOF

# Imprecise quantifiers
cat > "$WORK/quantifiers.json" <<'EOF'
{
  "goal": "Handle several edge cases in the parser",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "Fix many known parsing bugs"},
    {"id": "AC2", "description": "Add a few more test cases"}
  ]
}
EOF

# Passive voice
cat > "$WORK/passive.json" <<'EOF'
{
  "goal": "Errors are logged by the middleware",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "Requests are validated before processing"},
    {"id": "AC2", "description": "Failed requests are rejected with 400"}
  ]
}
EOF

# Implementation leakage
cat > "$WORK/impl.json" <<'EOF'
{
  "goal": "Build the user service using Docker with PostgreSQL",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "Service starts on port 8080"}
  ]
}
EOF

# Word boundary test: "something" should NOT match "some"
cat > "$WORK/boundary.json" <<'EOF'
{
  "goal": "Do something with the configuration",
  "acceptanceCriteria": [
    {"id": "AC1", "description": "Update the configuration file"}
  ]
}
EOF

echo "=== Clean contract ==="
assert_pass "no findings on clean contract" "$WORK/clean.json"
assert_total "zero findings" "$WORK/clean.json" 0

echo ""
echo "=== Banned phrases ==="
assert_findings "detects 'as needed'" "$WORK/banned.json" "banned_phrases" 1
assert_findings "detects banned phrases in ACs" "$WORK/banned.json" "banned_phrases" 3

echo ""
echo "=== Imprecise quantifiers ==="
assert_findings "detects 'several'" "$WORK/quantifiers.json" "imprecise_quantifiers" 1
assert_findings "detects quantifiers in ACs" "$WORK/quantifiers.json" "imprecise_quantifiers" 3

echo ""
echo "=== Passive voice ==="
assert_findings "detects passive in goal" "$WORK/passive.json" "passive_voice" 1

echo ""
echo "=== Implementation leakage ==="
assert_findings "detects Docker/PostgreSQL" "$WORK/impl.json" "implementation_leakage" 1

echo ""
echo "=== Word boundaries ==="
assert_pass "'something' should NOT match 'some'" "$WORK/boundary.json"

echo ""
echo "=== Error handling ==="
# Missing argument
if "$CHECKER" 2>/dev/null; then
  printf '  FAIL: should fail without args\n'
  failed=$((failed + 1))
else
  printf '  PASS: fails without args\n'
  passed=$((passed + 1))
fi

# Nonexistent file
if "$CHECKER" "/tmp/nonexistent-contract-xyz.json" 2>/dev/null; then
  printf '  FAIL: should fail on missing file\n'
  failed=$((failed + 1))
else
  printf '  PASS: fails on missing file\n'
  passed=$((passed + 1))
fi

echo ""
echo "=== Results ==="
echo "Passed: $passed"
echo "Failed: $failed"
echo ""

if [ "$failed" -gt 0 ]; then
  echo "FAILED"
  exit 1
else
  echo "ALL PASSED"
  exit 0
fi
