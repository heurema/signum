#!/usr/bin/env bash
# session-manager.sh -- cross-run session context for signum
# Maintains .signum/session.json with last N audit results per project.
# Frozen snapshot pattern: read once at contractor start, write after PACK.
#
# Based on Specpunk/Hermes frozen snapshot design:
# - Typed entries: success/failure/scope_violation/model_disagreement
# - TTL-based eviction (entries expire after N runs)
# - Capped at 10 entries, oldest evicted first
# - Atomic writes (temp + mv)
#
# Usage:
#   source lib/session-manager.sh
#   session_read           # prints session.json contents (or empty template)
#   session_append <type> <fact> [ttl_runs]  # append entry after PACK

set -euo pipefail

SESSION_FILE="${SIGNUM_SESSION_FILE:-.signum/session.json}"
SESSION_MAX_ENTRIES="${SIGNUM_SESSION_MAX:-10}"
SESSION_DEFAULT_TTL="${SIGNUM_SESSION_TTL:-5}"

# Initialize empty session if file doesn't exist
session_init() {
  if [ ! -f "$SESSION_FILE" ]; then
    echo '{"schema_version":1,"entries":[]}' > "$SESSION_FILE"
  fi
}

# Read session (returns JSON, creates if missing)
session_read() {
  session_init
  cat "$SESSION_FILE"
}

# Append entry to session after PACK phase
# Args: type fact [ttl_runs]
# type: success | failure | scope_violation | model_disagreement | cost_overrun
session_append() {
  local entry_type="${1:?type required}"
  local fact="${2:?fact required}"
  local ttl="${3:-$SESSION_DEFAULT_TTL}"
  local ts
  ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  session_init

  # Atomic write: temp file + mv
  local tmp
  tmp=$(mktemp "${SESSION_FILE}.XXXXXX")

  jq --arg type "$entry_type" \
     --arg fact "$fact" \
     --argjson ttl "$ttl" \
     --arg ts "$ts" \
     --argjson max "$SESSION_MAX_ENTRIES" '
    # Decrement TTL on existing entries, remove expired
    .entries = [.entries[] | .ttl_runs -= 1 | select(.ttl_runs > 0)] |
    # Append new entry
    .entries += [{"type": $type, "fact": $fact, "ttl_runs": $ttl, "created_at": $ts}] |
    # Cap at max entries (remove oldest first)
    if (.entries | length) > $max then .entries = .entries[-$max:] else . end
  ' "$SESSION_FILE" > "$tmp"

  mv "$tmp" "$SESSION_FILE"
}

# Evict expired entries (TTL countdown without adding)
session_evict() {
  session_init
  local tmp
  tmp=$(mktemp "${SESSION_FILE}.XXXXXX")

  jq --argjson max "$SESSION_MAX_ENTRIES" '
    .entries = [.entries[] | .ttl_runs -= 1 | select(.ttl_runs > 0)] |
    if (.entries | length) > $max then .entries = .entries[-$max:] else . end
  ' "$SESSION_FILE" > "$tmp"

  mv "$tmp" "$SESSION_FILE"
}
