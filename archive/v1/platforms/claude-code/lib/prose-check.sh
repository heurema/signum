#!/usr/bin/env bash
# prose-check.sh -- deterministic prose quality gate for contract.json
# Usage: prose-check.sh <path/to/contract.json>
# Outputs: JSON report to stdout
# Exit: 0 always (non-blocking informational check)
# Note: glossary synonym checking is handled by lib/glossary-check.sh

set -euo pipefail

CONTRACT="${1:-}"
if [ -z "$CONTRACT" ]; then
  echo '{"error":"Usage: prose-check.sh <contract.json>"}' >&2
  exit 1
fi
if [ ! -f "$CONTRACT" ]; then
  echo "{\"error\":\"File not found: $CONTRACT\"}" >&2
  exit 1
fi

GOAL=$(jq -r '.goal // ""' "$CONTRACT")
AC_DESCS=$(jq -r '.acceptanceCriteria[] | .id + "|||" + .description' "$CONTRACT" 2>/dev/null || true)

# -----------------------------------------------------------------------
# Temp file directory for accumulating findings as JSONL
# -----------------------------------------------------------------------
TMPDIR_LOCAL=$(mktemp -d)
trap 'rm -rf "$TMPDIR_LOCAL"' EXIT

BANNED_FILE="$TMPDIR_LOCAL/banned.jsonl"
QUANTIFIER_FILE="$TMPDIR_LOCAL/quantifier.jsonl"
PASSIVE_FILE="$TMPDIR_LOCAL/passive.jsonl"
IMPL_FILE="$TMPDIR_LOCAL/impl.jsonl"
touch "$BANNED_FILE" "$QUANTIFIER_FILE" "$PASSIVE_FILE" "$IMPL_FILE"

# -----------------------------------------------------------------------
# Pattern lists
# -----------------------------------------------------------------------
BANNED_PHRASES='as needed|if necessary|when appropriate|as applicable|\betc\b|and so on|among others|\bvarious\b|\bsuitable\b'
QUANTIFIERS='\bseveral\b|\bmany\b|\bfew\b|\bsome\b|a lot|a number of|\bnumerous\b|\bplenty\b'
PASSIVE_PATTERN="(is|are|was|were|be|been|being)[[:space:]]+[a-z]+ed"
IMPL_LEAKAGE="use React|with PostgreSQL|via REST API|using Docker|in Python|with Redis|MySQL query"

# -----------------------------------------------------------------------
# Helper: append a finding JSON line to a file
# Usage: append_finding <file> <matched_text> <location> <suggestion>
# -----------------------------------------------------------------------
append_finding() {
  local file="$1" matched="$2" location="$3" suggestion="$4"
  jq -n --arg text "$matched" --arg location "$location" --arg suggestion "$suggestion" \
    '{"text":$text,"location":$location,"suggestion":$suggestion}' >> "$file"
}

# -----------------------------------------------------------------------
# Helper: scan text for pipe-separated patterns, append findings
# Usage: scan_patterns <text> <location> <pattern_pipe_list> <suggestion_prefix> <output_file>
# -----------------------------------------------------------------------
scan_patterns() {
  local text="$1" location="$2" pattern_list="$3" suggestion="$4" outfile="$5"
  while IFS= read -r phrase; do
    phrase=$(echo "$phrase" | tr -d '\r')
    [ -z "$phrase" ] && continue
    matched=$(echo "$text" | grep -oiE "$phrase" 2>/dev/null | head -1 || true)
    if [ -n "$matched" ]; then
      append_finding "$outfile" "$matched" "$location" "${suggestion}: replace '$matched' with specific language"
    fi
  done < <(echo "$pattern_list" | tr '|' '\n')
}

# -----------------------------------------------------------------------
# Helper: check passive voice in text, append to passive file
# -----------------------------------------------------------------------
check_passive() {
  local text="$1" location="$2"
  while IFS= read -r matched; do
    [ -z "$matched" ] && continue
    append_finding "$PASSIVE_FILE" "$matched" "$location" "Rewrite in active voice"
  done < <(echo "$text" | grep -oiE "$PASSIVE_PATTERN" 2>/dev/null || true)
}

# -----------------------------------------------------------------------
# Check GOAL
# -----------------------------------------------------------------------
scan_patterns "$GOAL" "goal" "$BANNED_PHRASES" "Remove vague hedge phrase" "$BANNED_FILE"
scan_patterns "$GOAL" "goal" "$QUANTIFIERS" "Use exact count instead of imprecise quantifier" "$QUANTIFIER_FILE"
check_passive "$GOAL" "goal"

# Implementation leakage in goal only (per assumption A3)
while IFS= read -r phrase; do
  phrase=$(echo "$phrase" | tr -d '\r')
  [ -z "$phrase" ] && continue
  matched=$(echo "$GOAL" | grep -oiE "$phrase" 2>/dev/null | head -1 || true)
  if [ -n "$matched" ]; then
    append_finding "$IMPL_FILE" "$matched" "goal" "Remove implementation detail from goal; specify behaviour, not technology"
  fi
done < <(echo "$IMPL_LEAKAGE" | tr '|' '\n')

# -----------------------------------------------------------------------
# Check each AC description
# -----------------------------------------------------------------------
while IFS= read -r ac_line; do
  [ -z "$ac_line" ] && continue
  ac_id="${ac_line%%|||*}"
  ac_desc="${ac_line#*|||}"
  scan_patterns "$ac_desc" "$ac_id" "$BANNED_PHRASES" "Remove vague hedge phrase" "$BANNED_FILE"
  scan_patterns "$ac_desc" "$ac_id" "$QUANTIFIERS" "Use exact count instead of imprecise quantifier" "$QUANTIFIER_FILE"
  check_passive "$ac_desc" "$ac_id"
  # Note: impl leakage is goal-only per assumption A3
done <<< "$AC_DESCS"

# -----------------------------------------------------------------------
# Convert JSONL files to JSON arrays
# -----------------------------------------------------------------------
jsonl_to_array() {
  local file="$1"
  if [ -s "$file" ]; then
    jq -s '.' "$file"
  else
    echo "[]"
  fi
}

BANNED_ARR=$(jsonl_to_array "$BANNED_FILE")
QUANTIFIER_ARR=$(jsonl_to_array "$QUANTIFIER_FILE")
PASSIVE_ARR=$(jsonl_to_array "$PASSIVE_FILE")
IMPL_ARR=$(jsonl_to_array "$IMPL_FILE")

BANNED_COUNT=$(echo "$BANNED_ARR" | jq 'length')
QUANTIFIER_COUNT=$(echo "$QUANTIFIER_ARR" | jq 'length')
PASSIVE_COUNT=$(echo "$PASSIVE_ARR" | jq 'length')
IMPL_COUNT=$(echo "$IMPL_ARR" | jq 'length')
TOTAL=$((BANNED_COUNT + QUANTIFIER_COUNT + PASSIVE_COUNT + IMPL_COUNT))

if [ "$TOTAL" -le 3 ]; then
  PASS="true"
else
  PASS="false"
fi

# -----------------------------------------------------------------------
# Emit JSON report
# -----------------------------------------------------------------------
jq -n \
  --argjson total "$TOTAL" \
  --argjson pass "$PASS" \
  --argjson banned_count "$BANNED_COUNT" \
  --argjson banned "$BANNED_ARR" \
  --argjson quantifier_count "$QUANTIFIER_COUNT" \
  --argjson quantifier "$QUANTIFIER_ARR" \
  --argjson passive_count "$PASSIVE_COUNT" \
  --argjson passive "$PASSIVE_ARR" \
  --argjson impl_count "$IMPL_COUNT" \
  --argjson impl "$IMPL_ARR" \
  '{
    total_findings: $total,
    pass: $pass,
    categories: {
      banned_phrases: { count: $banned_count, findings: $banned },
      imprecise_quantifiers: { count: $quantifier_count, findings: $quantifier },
      passive_voice: { count: $passive_count, findings: $passive },
      implementation_leakage: { count: $impl_count, findings: $impl }
    }
  }'
