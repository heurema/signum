# Shared Receipt Schema: Signum <-> punk-run Mapping

When punk-run daemon dispatches a task and calls `punk check` as post-gate,
signum proofpack and punk-run receipt should be interoperable.

## Field Mapping

| Specpunk Receipt Field | Type | Signum Proofpack Field | Status |
|------------------------|------|------------------------|--------|
| `schema_version` | int | `schemaVersion` ("4.8") | EXISTS (format differs: int vs string) |
| `task_id` | string | `runId` | EXISTS |
| `status` | enum | `decision` (AUTO_OK/AUTO_BLOCK/HUMAN_REVIEW) | EXISTS (values differ) |
| `agent` | string | - | MISSING (always "signum") |
| `model` | string | - | MISSING (multi-model, not single) |
| `project` | string | - | MISSING (derivable from CWD) |
| `category` | string | `riskLevel` | PARTIAL (risk != category) |
| `tokens_used` | int | - | MISSING |
| `cost_usd` | float | - | MISSING |
| `duration_ms` | int | `timing.durationMs` | EXISTS (added in v4.8) |
| `exit_code` | int | - | MISSING (multi-agent, no single exit) |
| `artifacts` | string[] | - | MISSING (derivable from diff) |
| `errors` | string[] | - | MISSING |
| `call_style` | enum | - | N/A (always "agent") |
| `parent_task_id` | string? | `contractId` | EXISTS |
| `punk_check_exit` | int? | `decision` mapped: AUTO_OK=0, else=1 | DERIVABLE |
| `summary` | string | `summary` | EXISTS |
| `created_at` | datetime | `createdAt` | EXISTS |

## Compatibility Adapter

For punk-run status to display signum results, a thin adapter maps proofpack -> receipt:

```bash
# Convert proofpack.json to receipt-compatible format
signum_to_receipt() {
  jq '{
    schema_version: 1,
    task_id: .runId,
    status: (if .decision == "AUTO_OK" then "success" elif .decision == "AUTO_BLOCK" then "failure" else "review" end),
    agent: "signum",
    model: "multi-model",
    category: .riskLevel,
    duration_ms: (.timing.durationMs // 0),
    punk_check_exit: (if .decision == "AUTO_OK" then 0 else 1 end),
    summary: .summary,
    created_at: .createdAt
  }' "$1"
}
```

## Critical Gaps (7 fields missing from proofpack)

1. `agent` - which agent ran (always "signum" for now)
2. `model` - which model (multi-model, need primary)
3. `project` - project slug (derivable from CWD)
4. `category` - task type (feature/bugfix/refactor/security/cleanup)
5. `cost_usd` - cost estimate (essential for punk-run budget)
6. `tokens_used` - token count (essential for cost estimation)
7. `errors[]` - flat error array (currently scattered in nested checks)

## Next Steps

1. v4.9: Add `agent`, `category` fields to proofpack (trivial, from contract.json)
2. v4.10: Add `cost_usd`, `tokens_used` estimates (from session billing data if available)
3. punk-run Phase 0: adapter in punk-orch that maps proofpack -> receipt
4. punk-run Phase 1: `punk-run status` queries both native receipts and adapted proofpacks
5. Shared JSON schema in specpunk/punk/schemas/ for validation
