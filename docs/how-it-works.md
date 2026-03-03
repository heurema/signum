# How Signum Works

## Philosophy

Contract-first, multi-model-verified, proof-packaged.

Signum treats every development task as a formal contract. Before any code is written, a structured `contract.json` captures the exact acceptance criteria, affected files, holdout scenarios, and test strategy. Implementation is measured against the contract. A panel of independent AI models audits the result from specialized angles. Everything is packaged into a `proofpack.json` that serves as a CI-gate artifact.

The core insight: a single-model review is a self-audit. Signum removes that by routing the finished diff to models with different training provenance — Claude Opus for semantic review, OpenAI Codex for security audit, and Google Gemini for performance audit — each reviewing blind, without seeing the others' findings.

## Pipeline

```
CONTRACT → EXECUTE → AUDIT → PACK
                       ↓
                  ┌────┼────┐
              Claude  Codex  Gemini
            (semantic)(security)(perf)
                  └────┼────┘
                       ↓
                  proofpack.json
```

### Phase 1: CONTRACT

**Agent:** Contractor (haiku or sonnet, zero implementation risk)

The contractor ingests the task description and produces `contract.json`:

```json
{
  "goal": "...",
  "inScope": [...],
  "acceptanceCriteria": [...],
  "holdoutScenarios": [...],
  "riskLevel": "low|medium|high"
}
```

Risk is assessed structurally (file count, keywords, surface area). Holdout scenarios are edge cases or negative tests that the Engineer will never see — they serve as blind validation after implementation. The contract is shown to the user before execution begins — this is the only approval gate.

### Phase 2: EXECUTE

**Orchestrator:** captures baseline, launches Engineer, enforces scope gate.

1. **Baseline capture**: orchestrator runs lint/typecheck/tests BEFORE any changes and saves exit codes to `.signum/baseline.json`. This is the trust anchor for regression detection.
2. **Engineer** (sonnet) implements against the contract with a 3-attempt repair loop.
3. **Scope gate**: deterministic check that all modified files are within `inScope` or `allowNewFilesUnder`. Stops pipeline on violation.

The baseline is captured by the orchestrator, not the Engineer — this prevents self-reporting bias.

### Phase 3: AUDIT

**Agents:** Mechanic (bash), Reviewer-Claude (opus), Reviewer-Codex (CLI), Reviewer-Gemini (CLI), Synthesizer (sonnet)

The multi-model audit panel is the primary differentiator.

**Mechanic** runs lint, typecheck, and tests, then compares with baseline to flag regressions. Pre-existing failures that existed before the Engineer's changes are not counted as regressions.

**Holdout validation** runs hidden acceptance criteria that the Engineer never saw. These test edge cases and boundary conditions as blind validation of implementation quality.

Each reviewer receives specialized prompts:
- **Claude** (opus): full semantic review — contract + diff + mechanic results
- **Codex** (CLI): security-focused — goal + diff only (adversarial isolation)
- **Gemini** (CLI): performance-focused — goal + diff only (adversarial isolation)

Codex and Gemini receive only the goal and diff — no contract details, no mechanic results. This is intentional adversarial isolation: they cannot be influenced by context that would bias their review.

**Synthesizer** applies deterministic rules:
- Any regression (NEW failure vs baseline) or critical finding → **AUTO_BLOCK**
- All reviews approve + no regressions + holdouts pass → **AUTO_OK**
- Everything else → **HUMAN_REVIEW**

The synthesizer also computes a confidence score (0-100) based on execution health, baseline stability, and review alignment.

### Phase 4: PACK

Assembles `proofpack.json` with SHA-256 checksums and confidence score:

```json
{
  "schemaVersion": "3.0",
  "runId": "signum-2026-03-03-abc123",
  "decision": "AUTO_OK",
  "confidence": { "overall": 92 },
  "checksums": { ... },
  "summary": "..."
}
```

AUTO_OK and HUMAN_REVIEW proofpacks are CI-gate artifacts. AUTO_BLOCK proofpacks halt the workflow.

## Agents

| Agent | Model | Phase | Responsibility |
|-------|-------|-------|----------------|
| Contractor | haiku (low) / sonnet (med/high) | CONTRACT | Parse task, produce contract.json with holdout scenarios |
| Engineer | sonnet | EXECUTE | Implement with repair loop (reads baseline, does not capture it) |
| Reviewer-Claude | opus | AUDIT | Semantic review of diff (full context) |
| Reviewer-Codex | codex CLI | AUDIT | Security-focused review (goal + diff only) |
| Reviewer-Gemini | gemini CLI | AUDIT | Performance-focused review (goal + diff only) |
| Synthesizer | sonnet | AUDIT + PACK | Verdict synthesis with confidence scoring, proofpack assembly |

## CLI Adapter

External model reviews run through a thin CLI adapter that:
1. Uses python3 to substitute template variables (goal + diff) into the specialized review template
2. Invokes the CLI with appropriate flags (`--no-interactive`, output to stdout)
3. Parses structured JSON from stdout using 3-level parser (direct JSON → marker extraction → raw fallback)
4. Validates the schema before passing findings to synthesis

If the CLI returns malformed output or a non-zero exit code, the provider is marked `unavailable` in the proofpack — the audit continues with remaining providers.

## Artifacts

All artifacts are written to `.signum/` (auto-added to `.gitignore`):

| File | Phase | Description |
|------|-------|-------------|
| `contract.json` | CONTRACT | Structured task contract with holdout scenarios |
| `baseline.json` | EXECUTE | Pre-change check exit codes (captured by orchestrator) |
| `combined.patch` | EXECUTE | Full git diff |
| `execute_log.json` | EXECUTE | Implementation log, repair attempts |
| `mechanic_report.json` | AUDIT | Post-change checks with baseline comparison |
| `holdout_report.json` | AUDIT | Hidden scenario pass/fail counts |
| `reviews/*.json` | AUDIT | Per-provider findings (specialized templates) |
| `audit_summary.json` | AUDIT | Verdict with confidence scores |
| `proofpack.json` | PACK | Final CI-gate artifact |

## Cost Estimates

Approximate per-run costs at standard API rates:

| Configuration | Estimate |
|---------------|----------|
| Claude only (haiku + sonnet) | ~$0.44 |
| Claude only (opus audit) | ~$0.85 |
| Claude + Codex | ~$1.20 |
| Claude + Codex + Gemini | ~$1.80 |
| High-risk, all providers | ~$2.35 |

Costs vary with diff size and contract complexity.

## Trust Boundaries

**Stays local:** contract generation, baseline capture, scope gate, holdout validation, orchestration, proofpack assembly.

**Sent to Anthropic:** feature description, diff, contract (standard Claude Code behavior).

**Sent to external providers (with consent):** goal + diff only — never the full codebase, contract details, or mechanic results (adversarial isolation).

No telemetry. No analytics. No phone-home.

## Limitations

- **Sequential execution**: CLI adapters for Codex and Gemini run sequentially, not in parallel. A 3-provider audit adds wall-clock time proportional to diff size.
- **CLI fragility**: External reviews depend on Codex/Gemini CLI auth state and version compatibility. Signum degrades gracefully but cannot guarantee external availability.
- **200K context limit**: Very large diffs (>10K lines) may exceed model context windows. The contract + diff must fit within 200K tokens.
- **Heuristic risk**: Risk level is computed from file count and keyword patterns, not semantic analysis. It can under-estimate novel refactors.
- **Interactive only**: Runs inside Claude Code sessions. Not suitable for unattended CI pipelines.
- **Finding validation**: Catches hallucinated file paths and line ranges, but cannot verify logical correctness of a finding's claim.
- **Single execution path**: v3 runs one implementation strategy. Multi-path execution (parallel strategies with winner selection) is planned for v3.1.
