# Self-Contained Proofpack v4.0

## Goal

Make `proofpack.json` a single self-contained artifact for both CI validation and long-term audit trail. Currently v3.0 is a manifest with file paths referencing ~10 files in `.signum/`.

## Decision Record

Panel consensus (Claude + Codex + Gemini): all three agreed on every design decision.

## Approach

**Inline embedding** with schema bump v3 -> v4. Each artifact field changes from `string` (path) to `object` (envelope with content + metadata). One file = one truth.

## Envelope Format

Every artifact is wrapped in a standard envelope:

```json
{
  "content": "<object|string|null>",
  "sha256": "hex-string",
  "sizeBytes": 1234,
  "status": "present|omitted|error",
  "omitReason": "optional string"
}
```

- `status: present` — content is embedded
- `status: omitted` — content is null, validate by sha256 (e.g., diff > threshold)
- `status: error` — artifact generation failed, content is null, omitReason explains why
- `sha256` — computed from worktree bytes (file on disk at generation time)
- `sizeBytes` — size of original file on disk in bytes

## Proofpack v4.0 Structure

```json
{
  "schemaVersion": "4.0",
  "signumVersion": "4.0.0",
  "createdAt": "2026-03-04T12:00:00Z",
  "runId": "signum-2026-03-04-abc123",
  "decision": "AUTO_OK",
  "summary": "All checks passed",
  "confidence": { "overall": 85 },
  "auditChain": {
    "contractSha256": "hex",
    "approvedAt": "2026-03-04T10:00:00Z",
    "baseCommit": "abc123def"
  },
  "contract": {
    "content": { "...redacted, holdouts stripped..." },
    "sha256": "hex-of-embedded-redacted-content",
    "fullSha256": "hex-of-original-with-holdouts",
    "sizeBytes": 2048,
    "status": "present"
  },
  "diff": {
    "content": null,
    "sha256": "hex",
    "sizeBytes": 250000,
    "status": "omitted",
    "omitReason": "exceeds 100KB threshold"
  },
  "baseline": { "content": {}, "sha256": "hex", "sizeBytes": 512, "status": "present" },
  "executeLog": { "content": {}, "sha256": "hex", "sizeBytes": 1024, "status": "present" },
  "checks": {
    "mechanic": { "content": {}, "sha256": "hex", "sizeBytes": 3072, "status": "present" },
    "holdout": { "content": {}, "sha256": "hex", "sizeBytes": 768, "status": "present" },
    "reviews": {
      "claude": { "content": {}, "sha256": "hex", "sizeBytes": 4096, "status": "present" },
      "codex": { "content": {}, "sha256": "hex", "sizeBytes": 2048, "status": "present" },
      "gemini": { "content": null, "sha256": null, "sizeBytes": 0, "status": "error", "omitReason": "provider unavailable" }
    },
    "auditSummary": { "content": {}, "sha256": "hex", "sizeBytes": 1536, "status": "present" }
  }
}
```

## Design Decisions

### 1. sha256 = worktree bytes

Hash computed from raw file bytes on disk at generation time. All `.signum/` artifacts are generated files (not git-tracked), so `core.autocrlf` is irrelevant.

### 2. No top-level checksums

Per-artifact sha256 in each envelope is sufficient. Top-level checksums removed — they duplicated per-artifact hashes without a strict algorithm.

### 3. Envelope status field

`present | omitted | error` with optional `omitReason`. Distinguishes "intentionally omitted" from "failed to generate" from "successfully embedded".

### 4. Contract: two hashes

- `sha256` — hash of embedded (redacted) content
- `fullSha256` — hash of original contract including holdouts

This allows CI to validate the embedded content while audit can verify the original was not tampered with (if original is available).

### 5. camelCase everywhere

Unified naming convention. Previous v3 mixed camelCase and snake_case (`approved_at`, `base_commit`). All fields now camelCase.

### 6. Hash format: plain hex

No `sha256:` prefix. Just lowercase hex string. Simpler to parse, no algorithm ambiguity (always SHA-256).

### 7. signumVersion + createdAt

Top-level fields for reproducibility and audit trail.

### 8. Reviews as dynamic map

Keys are provider names (not fixed to claude/codex/gemini). Supports adding/removing providers without schema changes.

## Embedding Rules

| Artifact | Content type | Omit threshold | Special handling |
|----------|-------------|----------------|------------------|
| contract | object | never | holdouts stripped, fullSha256 for original |
| diff | string | >100KB -> null | omitReason set |
| baseline | object | never | — |
| executeLog | object | never | — |
| mechanic | object | never | — |
| holdout | object | never | — |
| reviews/* | object | never | dynamic keys, error status if provider unavailable |
| auditSummary | object | never | — |

## Deferred (not in v4.0)

- Binary artifacts / mediaType / contentEncoding
- Cryptographic signatures / Sigstore
- finalDecision for human review outcome
- gzip+base64 for large diffs
- Redaction policy for non-contract artifacts
- Per-check confidence breakdown
- Encoding/charset specification

## Schema Changes

- `schemaVersion`: `"3.0"` -> `"4.0"`
- All artifact fields: `string` -> envelope object
- New top-level: `signumVersion`, `createdAt`
- `auditChain` fields: snake_case -> camelCase
- `checks.audit_summary` -> `checks.auditSummary`
- `checksums` field: removed
- `contract` envelope: adds `fullSha256`

## Files to Modify

1. `lib/schemas/proofpack.schema.json` — new v4.0 schema
2. `commands/signum.md` — Phase 4 (PACK) generation logic
3. `docs/reference.md` — artifact documentation
