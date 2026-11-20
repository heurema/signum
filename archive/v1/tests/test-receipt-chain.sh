#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(CDPATH= cd "$(dirname "$0")/.." && pwd)"
LIB_SRC="$ROOT_DIR/lib"
export SIGNUM_TRUST_LOCAL=1  # allow workspace-local dsl-runner in test environments
passed=0
failed=0

assert_ok() {
  local name="$1"
  shift
  if "$@" >/tmp/test_out.$$ 2>&1; then
    printf ' PASS: %s\n' "$name"
    passed=$((passed + 1))
  else
    printf ' FAIL: %s\n' "$name"
    sed 's/^/    /' /tmp/test_out.$$
    failed=$((failed + 1))
  fi
  rm -f /tmp/test_out.$$
}

assert_fail() {
  local name="$1"
  shift
  if "$@" >/tmp/test_out.$$ 2>&1; then
    printf ' FAIL: %s (expected failure)\n' "$name"
    sed 's/^/    /' /tmp/test_out.$$
    failed=$((failed + 1))
  else
    printf ' PASS: %s\n' "$name"
    passed=$((passed + 1))
  fi
  rm -f /tmp/test_out.$$
}

make_repo() {
  local dir
  dir=$(mktemp -d)
  mkdir -p "$dir/lib" "$dir/src" "$dir/.signum"
  cp "$LIB_SRC"/*.sh "$dir/lib/"
  chmod +x "$dir/lib/"*.sh
  cat > "$dir/.gitignore" <<'GITEOF'
.signum/
GITEOF
  git -C "$dir" init >/dev/null 2>&1
  printf 'baseline\n' > "$dir/README.md"
  git -C "$dir" add README.md .gitignore >/dev/null 2>&1
  printf '%s\n' "$dir"
}

write_contract_pair() {
  local dir="$1"
  local risk="$2"
  local extra_inscope="$3"
  cat > "$dir/.signum/contract.json" <<EOFJSON
{
  "schemaVersion": "3.8",
  "contractId": "sig-20260319-test",
  "goal": "Create a greeting artifact with deterministic receipt verification.",
  "inScope": ["src/greeting.txt"${extra_inscope}],
  "acceptanceCriteria": [
    {
      "id": "AC01",
      "description": "Greeting file exists",
      "visibility": "visible",
      "verify": {
        "steps": [
          {"exec": {"argv": ["test", "-f", "src/greeting.txt"]}}
        ],
        "timeout_ms": 5000
      }
    },
    {
      "id": "AC02",
      "description": "Greeting file contains hello",
      "visibility": "visible",
      "verify": {
        "steps": [
          {"exec": {"argv": ["cat", "src/greeting.txt"]}, "capture": "greeting"},
          {"expect": {"stdout_contains": "hello", "source": "greeting"}}
        ],
        "timeout_ms": 5000
      }
    }
  ],
  "riskLevel": "$risk"
}
EOFJSON
  cp "$dir/.signum/contract.json" "$dir/.signum/contract-engineer.json"
  cat > "$dir/.signum/execution_context.json" <<'EOFJSON'
{"base_commit":"no-git","started_at":"2026-03-19T00:00:00Z","run_id":"sig-20260319-test"}
EOFJSON
}

write_vacuous_contract() {
  local dir="$1"
  cat > "$dir/.signum/contract.json" <<'EOFJSON'
{
  "schemaVersion": "3.8",
  "contractId": "sig-20260319-vacuous",
  "goal": "Create a greeting artifact but with a bad verify block.",
  "inScope": ["src/greeting.txt"],
  "acceptanceCriteria": [
    {
      "id": "AC01",
      "description": "Greeting exists",
      "visibility": "visible",
      "verify": {
        "steps": [
          {"exec": {"argv": ["ls", "src"]}}
        ],
        "timeout_ms": 5000
      }
    }
  ],
  "riskLevel": "medium"
}
EOFJSON
  cp "$dir/.signum/contract.json" "$dir/.signum/contract-engineer.json"
  cat > "$dir/.signum/execution_context.json" <<'EOFJSON'
{"base_commit":"no-git","started_at":"2026-03-19T00:00:00Z","run_id":"sig-20260319-vacuous"}
EOFJSON
}

simulate_engineer_success() {
  local dir="$1"
  local content="$2"
  mkdir -p "$dir/src"
  printf '%s\n' "$content" > "$dir/src/greeting.txt"
  printf 'diff --git a/src/greeting.txt b/src/greeting.txt\n' > "$dir/.signum/combined.patch"
  cat > "$dir/.signum/execute_log.json" <<'EOFJSON'
{"status":"SUCCESS","totalAttempts":1,"maxAttempts":3}
EOFJSON
}

scenario_happy_path() {
  local dir
  dir=$(make_repo)
  write_contract_pair "$dir" "medium" ""
  (cd "$dir" && lib/snapshot-tree.sh pre-execute >/dev/null)
  simulate_engineer_success "$dir" "hello world"
  (cd "$dir" && lib/boundary-verifier.sh execute >/dev/null)
  (cd "$dir" && lib/transition-verifier.sh execute audit >/dev/null)
  jq -e '.status == "PASS" and .summary.passed_acs == 2' "$dir/.signum/receipts/execute.json" >/dev/null
}

scenario_bypass_detected() {
  local dir
  dir=$(make_repo)
  write_contract_pair "$dir" "medium" ""
  (cd "$dir" && lib/snapshot-tree.sh pre-execute >/dev/null)
  simulate_engineer_success "$dir" "hello world"
  (cd "$dir" && lib/transition-verifier.sh execute audit >/dev/null)
}

scenario_missing_scope() {
  local dir
  dir=$(make_repo)
  write_contract_pair "$dir" "medium" ', "schemas/output.json"'
  (cd "$dir" && lib/snapshot-tree.sh pre-execute >/dev/null)
  simulate_engineer_success "$dir" "hello world"
  (cd "$dir" && lib/boundary-verifier.sh execute >/dev/null)
}

scenario_vacuous_verify() {
  local dir
  dir=$(make_repo)
  write_vacuous_contract "$dir"
  (cd "$dir" && lib/snapshot-tree.sh pre-execute >/dev/null)
  simulate_engineer_success "$dir" "hello world"
  (cd "$dir" && lib/boundary-verifier.sh execute >/dev/null)
}

scenario_chain_growth() {
  local dir
  dir=$(make_repo)
  write_contract_pair "$dir" "medium" ""
  (cd "$dir" && lib/snapshot-tree.sh attempt-01 >/dev/null)
  simulate_engineer_success "$dir" "hello v1"
  (cd "$dir" && lib/boundary-verifier.sh execute --snapshot "$dir/.signum/snapshots/attempt-01.json" >/dev/null)
  if command -v sha256sum >/dev/null 2>&1; then
    first_hash="sha256:$(sha256sum "$dir/.signum/runs/sig-20260319-test/execute-01.json" | awk '{print $1}')"
  else
    first_hash="sha256:$(shasum -a 256 "$dir/.signum/runs/sig-20260319-test/execute-01.json" | awk '{print $1}')"
  fi

  (cd "$dir" && lib/snapshot-tree.sh attempt-02 >/dev/null)
  simulate_engineer_success "$dir" "hello v2"
  (cd "$dir" && lib/boundary-verifier.sh execute --snapshot "$dir/.signum/snapshots/attempt-02.json" >/dev/null)
  (cd "$dir" && lib/transition-verifier.sh execute audit --snapshot "$dir/.signum/snapshots/attempt-02.json" >/dev/null)
  count=$(find "$dir/.signum/runs/sig-20260319-test" -maxdepth 1 -type f -name 'execute-*.json' | wc -l | tr -d '[:space:]')
  [[ "$count" == "2" ]]
  parent=$(jq -r '.parent_receipt_hash' "$dir/.signum/runs/sig-20260319-test/execute-02.json")
  [[ "$parent" == "$first_hash" ]]
}

scenario_out_of_scope_addition() {
  local dir
  dir=$(make_repo)
  write_contract_pair "$dir" "medium" ""
  (cd "$dir" && lib/snapshot-tree.sh pre-execute >/dev/null)
  simulate_engineer_success "$dir" "hello world"
  # Add an out-of-scope file that the engineer should not have created
  mkdir -p "$dir/docs"
  printf 'rogue file\n' > "$dir/docs/secret.md"
  (cd "$dir" && lib/boundary-verifier.sh execute >/dev/null)
}

echo "=== Receipt chain tests ==="
assert_ok "happy path generates PASS receipt and transition gate passes" scenario_happy_path
assert_fail "transition gate blocks when execute receipt is missing" scenario_bypass_detected
assert_fail "boundary verifier blocks when an inScope path is still missing" scenario_missing_scope
assert_fail "boundary verifier blocks vacuous verify commands on medium risk" scenario_vacuous_verify
assert_ok "iterative attempts append receipt chain with parent hash linkage" scenario_chain_growth
assert_fail "boundary verifier blocks out-of-scope file additions" scenario_out_of_scope_addition

echo ""
echo "Passed: $passed"
echo "Failed: $failed"
if [[ "$failed" -gt 0 ]]; then
  exit 1
fi
