# Thin CLI Extraction Plan: signum lib/ -> Rust Crate

Migration path: Claude Code plugin -> standalone CLI (Rust) -> hosted service.
Aligns with specpunk workspace (punk-core already has verification primitives).

## Script Classification

### Tier A: Pure Deterministic (extract first)

These scripts have zero LLM dependency, use only jq/bash/python stdlib:

| Script | LOC | What it does | Rust equivalent |
|--------|-----|-------------|-----------------|
| `boundary-verifier.sh` | 550 | Phase-boundary integrity, hashes, scope verification | `signum-core::boundary::verify()` |
| `dsl-runner.sh` | 413 | Typed verify DSL executor (http/exec/expect, 6-binary whitelist) | `signum-core::dsl::run()` |
| `init-scanner.sh` | 349 | Project signal extraction (naming, imports, errors) | `signum-core::scan::init()` |
| `policy-scanner.sh` | 211 | Regex scan on diff for 13 security patterns | `signum-core::policy::scan()` |
| `mechanic-parser.sh` | 399 | Parse mechanic blocks, extract AC/verify DSL | `signum-core::mechanic::parse()` |
| `transition-verifier.sh` | 209 | State machine validation, TaskState transitions | `signum-core::state::verify()` |
| `contract-injection-scan.sh` | 70 | Unicode injection defense (MINJA) | `signum-core::security::scan_contract()` |
| `session-manager.sh` | 80 | Cross-run session with TTL | `signum-core::session::*` |
| `proofpack-index.sh` | 120 | Hash-linked proofpack chain | `signum-core::chain::*` |
| `metric-ratchet.sh` | 150 | Weekly performance comparison | `signum-core::metrics::ratchet()` |
| `policy-resolver.sh` | 90 | TOML policy routing | `signum-core::policy::resolve()` |
| `snapshot-tree.sh` | 128 | Workspace tree capture | `signum-core::snapshot::tree()` |
| `staleness-check.sh` | 149 | SHA-256 staleness detection | `signum-core::staleness::check()` |
| `prose-check.sh` | 162 | Goal/AC prose quality heuristics | `signum-core::prose::check()` |
| `terminology-check.sh` | 124 | Forbidden synonym detection | `signum-core::glossary::check()` |

**Total: ~3,200 LOC bash -> ~2,500 LOC Rust** (15 scripts)

### punk-core Reuse Opportunities

punk-core already provides modules that overlap with signum lib/:
- `receipt` -> boundary-verifier receipt chain
- `policy` -> policy-scanner patterns
- `dsl` -> dsl-runner typed executor
- `session` -> session-manager frozen snapshots
- `scan` -> init-scanner conventions

signum-core should depend on punk-core, not reimplement.

### Tier B: Deterministic with Project Context

These read project files but don't call LLMs:

| Script | What it does | Dependency |
|--------|-------------|-----------|
| `init-scanner.sh` | Scan project for context files | Filesystem |
| `glossary-check.sh` | Check terminology consistency | glossary.json |
| `assumption-check.sh` | Cross-contract assumption validation | contracts/ |
| `overlap-check.sh` | Scope overlap detection | contracts/ |
| `adr-check.sh` | ADR reference validation | docs/adr/ |
| `prose-check.sh` | Ban vague phrases | contract.json |
| `terminology-check.sh` | Enforce canonical terms | glossary.json |

### Tier C: LLM-Dependent (keep as plugin/thin wrapper)

These require LLM orchestration and stay as markdown:

| Component | Why it stays |
|-----------|-------------|
| contractor.md | LLM generates contract |
| engineer.md | LLM implements code |
| reviewer-claude.md | LLM reviews diff |
| synthesizer.md | LLM synthesizes verdict |
| init-synthesizer.md | LLM synthesizes intent |
| signum.md (orchestrator) | Multi-agent coordination |

## Proposed Crate Structure

```
specpunk/punk/
  punk-core/          # FROZEN verification (existing)
  punk-orch/          # Orchestration (existing)
  punk-run/           # CLI binary (existing)
  signum-core/        # NEW: signum deterministic primitives
    src/
      lib.rs
      policy.rs       # policy-scanner + policy-resolver
      receipt.rs      # boundary-verifier + receipt chain
      dsl.rs          # dsl-runner
      security.rs     # contract-injection-scan
      session.rs      # session-manager
      chain.rs        # proofpack-index
      metrics.rs      # metric-ratchet
      snapshot.rs     # snapshot-tree + staleness-check
```

## punk-core Reuse

punk-core already provides:
- `punk init` (brownfield scan)
- `punk plan` (contract generation)
- `punk check` (scope gate)
- `punk receipt` (completion proof)

signum-core would use punk-core for:
- Contract schema validation (shared JSON schema)
- Scope gate check (reuse punk check logic)
- Receipt format (shared receipt struct)

## Migration Order

1. **v4.18**: Thin SKILL.md wrapper -> `signum` CLI forwards to bash lib/
2. **v4.20**: Extract Tier A scripts to signum-core Rust crate
3. **v5.0**: Standalone `signum` binary (Rust CLI wrapping signum-core)
4. **v5.0+**: `signum` invocable from punk-run as adapter
