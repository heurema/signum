---
title: "Signum Init: Auto-generating project.intent.md from existing codebases"
date: 2026-03-16
status: complete
depth: medium (scan-only due to context limits)
verification: unverified
---

# Signum Init: Project Context Bootstrapping

## Problem

When a user runs `/signum` for the first time in an existing project, the contractor agent works without project context. Medium/high-risk tasks trigger a blocking question about missing `project.intent.md`. Users need a way to bootstrap project context from their existing codebase.

## Landscape: How Others Solve This

### Claude Code `/init`
- Reads: `package*.json`, `*.md`, `.cursor/rules/**`, `.github/copilot-instructions.md`, build configs, test frameworks
- Generates: `CLAUDE.md` with build commands, test instructions, key directories, coding conventions
- Approach: LLM agent scans project files, synthesizes understanding
- Limitation: captures obvious patterns but misses workflow nuances — meant as starting point

### Kiro Steering Docs
- Auto-generates 3 files: `product.md`, `structure.md`, `tech.md`
- `product.md`: purpose, target users, key features, business objectives
- `structure.md`: file organization, naming conventions, architectural decisions
- `tech.md`: technology stack, frameworks, dependencies
- Trigger: "Generate Steering Docs" button or command palette
- These files are included in every interaction as baseline context

### BMAD-METHOD
- `generate-project-context` workflow: analyzes directory structure, frameworks, libraries, architectural patterns
- `document-project` workflow: produces project-context.md rules
- Key insight: making codebase analysis the FIRST step significantly improves quality
- The model first performs analysis phase BEFORE generating any documents

### GitHub Spec Kit
- `/specify`, `/plan`, `/tasks` commands
- Not auto-generation — structured prompts for human-driven specification
- Spec is the single source of truth, AI executes against it

### cc-bootstrap (ClaudeCodeBootstrap)
- Python CLI that samples files from existing project
- Uses LLM (Anthropic API / Bedrock) to generate context-aware configs
- Generates: CLAUDE.md, custom commands, MCP configs, settings

### Repomix
- Packs entire repo into single AI-friendly file
- `--compress` uses Tree-sitter to extract key code elements
- Not a context generator — a context serializer

## What Signals to Extract

| Source | Signal | Maps to |
|--------|--------|---------|
| `README.md` | Project description, features list | Goal, Core Capabilities |
| `package.json` / `pyproject.toml` / `Cargo.toml` | name, description, scripts, dependencies | Goal, tech stack |
| `CLAUDE.md` / `AGENTS.md` | Existing conventions, non-goals | Non-Goals, Conventions |
| `.github/` | CI config, issue templates | Success Criteria |
| `docs/` | Architecture docs, ADRs | Non-Goals, Glossary |
| `git log --oneline -20` | Recent activity, feature areas | Core Capabilities |
| Directory structure | Module boundaries, test presence | Core Capabilities, Personas |
| Existing `project.intent.md` | Merge/update, don't overwrite | All sections |
| Code comments / docstrings | Domain terms, abbreviations | Glossary |

## Design for `/signum init`

### Architecture Decision: Command, not script

Should be a **signum command** (`commands/init.md`) rather than standalone script because:
1. Needs LLM to synthesize — can't do this deterministically in bash
2. Follows Kiro pattern: IDE command that generates steering docs
3. Can use Claude's codebase understanding (Read, Glob, Grep tools)
4. Can present draft for interactive human editing

### Proposed Flow

```
/signum init [--force]

1. SCAN (deterministic, ~5s)
   - Read README.md, package.json/pyproject.toml/Cargo.toml
   - Read CLAUDE.md, docs/*.md (first 200 lines each)
   - git log --oneline -20
   - ls -R (depth 3) for structure
   - Check if project.intent.md already exists (--force to overwrite)

2. SYNTHESIZE (LLM, ~10s)
   - Generate project.intent.md with sections:
     - Goal (1-2 sentences from README description)
     - Core Capabilities (from features, scripts, module structure)
     - Non-Goals (inferred from what's NOT in the codebase + CLAUDE.md)
     - Success Criteria (from tests, CI, README badges)
     - Personas (from README usage patterns)
   - Generate project.glossary.json:
     - canonicalTerms from README headers, module names, API routes
     - aliases from common abbreviations found in code

3. PRESENT (interactive)
   - Show generated draft to user
   - Let them edit before saving
   - Write files to project root

4. VERIFY
   - Run glossary-check.sh against a hypothetical contract
   - Show: "Glossary has N terms, M aliases"
   - Show: "Intent covers: goal, N capabilities, N non-goals"
```

### Template for project.intent.md

```markdown
# <Project Name> — Project Intent

## Goal
<extracted from README first paragraph or package.json description>

## Core Capabilities
- <from README features section>
- <from package.json scripts>
- <from module/directory structure>

## Non-Goals
- <inferred: things the project explicitly does NOT do>
- <from CLAUDE.md if present>

## Success Criteria
- <from test suite presence: "All tests pass">
- <from CI config: "CI pipeline green">
- <from README: domain-specific criteria>

## Personas
- <from README usage examples>
```

### Key Design Principles (from research)

1. **Analysis first, generation second** (BMAD insight) — scan the full codebase before generating anything
2. **Starting point, not finished product** (Claude /init insight) — always present draft for human editing
3. **Foundation files = always loaded** (Kiro insight) — intent/glossary should be read by every contractor invocation
4. **Existing context preserved** (all tools) — if files exist, merge/update, don't overwrite

## Gaps Found + Solutions (Codex review + deep research)

1. **Source precedence** — `docs/how-it-works.md` more authoritative than README for Goal. Need ranking, not averaging
2. **Deep docs scan** — `docs/research/`, `docs/plans/`, `docs/reference.md` missed by "first 200 lines of docs/*.md"
3. **Build/CI signals** — `.github/workflows`, `Makefile`, `justfile`, `tox.ini`, `docker-compose` → better Success Criteria than README badges
4. **Public entrypoints** — `bin/`, `commands/`, `console_scripts`, API routes, OpenAPI → better Personas/Capabilities than directory structure
5. **Ignore set** — `.git`, `.signum/`, `dist/`, `node_modules/`, `tests/fixtures/` must be excluded from glossary extraction
6. **Git log horizon** — 20 commits too short, shows current initiative not project essence. Aggregate by path/scope over longer history
7. **Non-Goals from absence = hallucination** — only from explicit negative signals: "won't support", rejected ADRs, roadmap exclusions
8. **Glossary merge/update** — `project.glossary.json` may already exist as SSoT; need conflict resolution per term
9. **Low confidence mode** — when sources empty/contradictory, emit `TODO/Needs confirmation` not fiction. Per-section evidence + confidence
10. **Glossary verification** — check coverage of real nouns in authoritative docs, not hypothetical contract

## Solutions per Gap

### Gap 1: Non-Goals hallucination
**Problem:** Inferring non-goals from what's absent in code = fabrication.
**Solution:** Only extract from explicit negative signals:
- `docs/adr/` with status "Rejected" or "Deprecated" (log4brains format)
- README sections: "Not supported", "Out of scope", "Limitations"
- CLAUDE.md/AGENTS.md: explicit exclusions
- `.github/ISSUE_TEMPLATE/` with "won't fix" labels
- If zero signals found → emit `Non-Goals: TODO — no explicit non-goals found. Add manually.`

### Gap 2: Low confidence mode
**Problem:** LLM confidently generates fiction for sparse sections.
**Solution:** Per-section evidence tracking:
```markdown
## Core Capabilities
<!-- evidence: README.md:L12-L25, package.json scripts, 3 sources -->
<!-- confidence: high -->
- capability 1
- capability 2

## Non-Goals
<!-- evidence: none found -->
<!-- confidence: low -->
- TODO: No explicit non-goals detected. Review and add manually.
```
Use sampling-based consistency: generate 3 variants, keep only claims present in 2+.

### Gap 3: Source precedence
**Problem:** README description often generic marketing, docs/ has real technical truth.
**Solution:** Ranked source hierarchy:
1. `docs/how-it-works.md`, `docs/architecture.md` → Goal, Capabilities (authoritative)
2. `CLAUDE.md`, `AGENTS.md` → Conventions, Non-Goals (explicit instructions)
3. `README.md` first paragraph → Goal fallback
4. `package.json`/`pyproject.toml` description → Goal last resort
5. `git log --dirstat` → Capabilities validation

### Gap 4: Build/CI signals
**Problem:** README badges weak for Success Criteria.
**Solution:** Read task runner configs as first-class:
- `Makefile` / `justfile` / `Taskfile.yml` → extract target names as capabilities
- `.github/workflows/*.yml` → extract job names + success conditions
- `pytest.ini` / `ruff.toml` / `tsconfig.json` → extract quality gates
- `docker-compose.yml` → extract service names as capabilities
- Map to Success Criteria: "CI green", "lint pass", "tests pass", specific job names

### Gap 5: Public entrypoints
**Problem:** Directory structure alone misses user-facing surface area.
**Solution:** Scan for entrypoints:
- `bin/`, `commands/`, `skills/` → CLI commands as capabilities
- `package.json` `bin` / `scripts` fields → named commands
- `pyproject.toml` `[project.scripts]` → console_scripts
- `src/main.rs`, `cmd/` → binary entrypoints
- OpenAPI/swagger files → API surface
- Extract Personas from usage patterns in entrypoints

### Gap 6: Ignore set
**Solution:** Hardcoded exclusion list:
```
.git, .signum/, .signum, node_modules/, dist/, build/, .venv/,
__pycache__/, coverage/, .next/, .nuxt/, target/,
tests/fixtures/, tests/snapshots/, *.min.js, *.bundle.*
```
Plus respect `.gitignore` patterns.

### Gap 7: Git log horizon
**Problem:** `git log -20` shows current sprint, not project essence.
**Solution:** Use `git log --dirstat=files --since="6 months ago"` aggregated by top-level directory. Shows sustained activity patterns, not recent churn.
```bash
git log --dirstat=files,10 --since="6 months ago" | grep -E '^\s+[0-9]' | sort -rn | head -20
```

### Gap 8: Glossary merge/update
**Solution:** If `project.glossary.json` exists:
1. Load existing terms as SSoT
2. Scan code for NEW terms not in glossary (suggest additions)
3. Never remove existing entries
4. Present diff: "Found 5 new terms, 0 conflicts. Add? [y/n]"

### Gap 9: Glossary verification
**Solution:** After generation, verify coverage:
```bash
# Extract nouns from authoritative docs
grep -oiE '\b[A-Z][a-z]+([A-Z][a-z]+)+\b' README.md docs/*.md | sort -u
# Compare against canonicalTerms
# Report: "Glossary covers 15/23 domain terms (65%)"
```

### Gap 10: Deep docs scan
**Solution:** Read not just top-level `docs/*.md` but:
- `docs/research/*.md` (frontmatter only for topics)
- `docs/plans/*.md` (for capabilities/roadmap)
- `docs/adr/*.md` (for non-goals from rejected decisions)
- `docs/reference.md` (for glossary terms)
- Limit: first 100 lines per file, max 10 files

## Sources

- [Kiro Steering Docs](https://kiro.dev/docs/steering/)
- [Claude Code /init internals](https://kirshatrov.com/posts/claude-code-internals)
- [BMAD-METHOD codebase analysis](https://github.com/bmad-code-org/BMAD-METHOD/issues/1538)
- [GitHub Spec Kit](https://github.com/github/spec-kit)
- [cc-bootstrap](https://github.com/vinodismyname/ClaudeCodeBootstrap)
- [Codified Context (arXiv)](https://arxiv.org/html/2602.20478v1)
- [Mastering Project Context Files](https://eclipsesource.com/blogs/2025/11/20/mastering-project-context-files-for-ai-coding-agents/)
- [Build your own /init](https://kau.sh/blog/build-ai-init-command/)
- [Stop AI Hallucinations: 4 Essential Techniques](https://dev.to/aws/stop-ai-agent-hallucinations-4-essential-techniques-2i94)
- [LLM Uncertainty Quantification (OpenReview)](https://openreview.net/forum?id=gjeQKFxFpZ)
- [Automatic Glossary Construction from Source Code (FSE'19)](https://mingwei-liu.github.io/assets/pdf/fse19-wang-glossary.pdf)
- [Domain Term Extraction via Contextual Embeddings](https://arxiv.org/html/2502.17278v1)
- [just-mcp: Expose justfile to AI agents](https://github.com/toolprint/just-mcp)
- [git-quick-stats](https://github.com/git-quick-stats/git-quick-stats)
- [AGENTS.md best practices](https://gist.github.com/0xfauzi/7c8f65572930a21efa62623557d83f6e)
