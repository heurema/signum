---
title: "Deterministic Codebase Scanning for AI Agents: LSP vs Alternatives"
date: 2026-03-16
status: complete
depth: deep
verification: partially-verified
run_id: 20260316T065642Z-8261
agents: 6
sources: 14+
---

# Deterministic Codebase Scanning for AI Agents: LSP vs Alternatives

## Executive Summary

**Вердикт: для signum init LSP не нужен. Claude Code native tools (Glob + Grep + Read + Bash) — оптимальный выбор.**

LSP решает другую задачу (per-symbol semantic navigation during editing), а не architectural comprehension для bootstrapping. Serena оптимальна для coding sessions — но для scan phase `/signum init` это overkill с побочными эффектами (startup friction, per-language binaries, indexing time).

## Landscape: 9 подходов к codebase scanning

### Tier 1: Production-proven, высокая зрелость

| Tool | Подход | Лучший для | Не подходит для |
|------|--------|-----------|-----------------|
| **Serena** | LSP → MCP (30+ языков) | Coding sessions: find-references, go-to-definition, semantic edits | Bootstrapping, batch analysis, zero-setup |
| **Claude Code native LSP** | LSP plugins (.lsp.json) | IDE-like code intelligence в рабочей сессии | Batch scan, audit pipeline |
| **Probe** | ripgrep + tree-sitter + BM25 | Semantic code search в больших codebase, zero-setup | Project understanding (search ≠ comprehension) |
| **Aider RepoMap** | tree-sitter → PageRank | Token-efficient file selection для editing context (4.3-6.5% utilization) | Intent extraction (нет access к goals/personas/non-goals) |

### Tier 2: Специализированные

| Tool | Подход | Лучший для | Не подходит для |
|------|--------|-----------|-----------------|
| **ast-grep** | CST pattern matching (31 язык) | Structural refactoring, syntax-aware search | Semantic discovery, project understanding |
| **CodeGraph-Rust** | Knowledge graph (tree-sitter + LSP + FAISS + SurrealDB) | Impact analysis, architectural reasoning | Plugin ecosystem (Docker + DB = infrastructure) |
| **Code Pathfinder** | Multi-pass static analysis, call graphs | SAST/security taint analysis | Polyglot projects (Python-only) |
| **lsp-mcp** | Raw LSP-to-MCP bridge | Experimentation, raw LSP access | Production use (POC state) |

### Tier 3: Контекстные / комплементарные

| Tool | Подход | Роль |
|------|--------|------|
| **Kiro steering docs** | product.md + structure.md + tech.md | Persistent project context (дополняет, не заменяет scan) |
| **Claude Code Glob/Grep/Read/Bash** | Built-in tools | Универсальный scan для known-location файлов |

## Глубокий анализ: Serena

### Архитектура
- **Solid-LSP**: Python wrapper над multilspy, synchronous single-threaded
- **~40 curated MCP tools**: find_symbol, find_referencing_symbols, insert_after_symbol, и т.д.
- **Двухуровневый кэш**: in-memory + persistent index (5-10 MB per 100k LOC)
- **JetBrains Plugin**: альтернативный backend через IDE analysis engine

### Performance (заявленный, без независимой валидации)
- Symbol lookup: ~100ms vs ~45s для grep (450x ускорение)
- Index build: 2-5 минут для 100k LOC
- Index size: 5-10 MB per 100k LOC

### Реальные ограничения
1. **Startup friction**: initialization hangs, env var inheritance issues (особенно Windows/WSL), language server download failures
2. **Single-threaded**: все tool calls сериализуются — no parallel LSP queries
3. **One project per instance**: context window bloat при multi-repo
4. **Cross-language weak**: Python+TypeScript monorepo — не first-class
5. **Dynamic code invisible**: metaprogramming, DI, reflection не видны LSP static analysis
6. **Memory**: <8GB RAM → OOM errors при крупных проектах

### Когда Serena — правильный выбор
- Крупные существующие codebase (>50k LOC)
- Длительные coding sessions с навигацией по символам
- Semantic edits (rename, refactor)
- Когда пользователь уже установил LSP серверы для своих языков

### Когда Serena — неправильный выбор
- Batch scanning / bootstrapping (одноразовый анализ)
- Zero-setup constraint
- Greenfield projects
- <5 файлов в проекте

## Гибридные подходы: что работает

### Паттерн 1: Tree-sitter + Graph (доминирует в production)
- **Aider** (PageRank), **GitNexus**, **LocAgent** (94.16% SWE-Bench), **codebase-memory-mcp** (99.2% token reduction claim)
- Tree-sitter для parsing + graph traversal для relationships
- 60+ языков, tolerates broken code
- **Трейдофф**: indexing time (seconds to hours) vs dramatic query-time token savings

### Паттерн 2: LSP + Tree-sitter (LSPRAG)
- LSP semantic tokens + tree-sitter AST → hybrid context
- 39% fewer tokens чем CodeQA с 135% improvement в coverage
- **Трейдофф**: requires running LSP server, best for IDE integration

### Паттерн 3: Graph + Vector (CodeGraph-Rust)
- 70% vector similarity + 30% lexical + graph traversal
- Multi-hop accuracy: GraphRAG 80-90% vs pure vector RAG 50-67%
- **Трейдофф**: SurrealDB + FAISS = infrastructure-grade complexity

### Ключевой вывод
> Hybrid retrieval: 8% improvement в factual correctness over vector-only (University of Leeds).
> Multi-hop relational queries: GraphRAG 80-90% vs pure vector RAG 50-67%.
> Но для bootstrapping (one-shot scan → intent extraction) этот gap не оправдывает overhead.

## Production LSP реализации: уроки

### 5 операций покрывают ~90% ценности
1. **Diagnostics** (ошибки, warnings) — #1 по impact
2. **References** (find all call sites)
3. **Definition** (go-to-definition)
4. **Hover** (type info, docs)
5. **Symbols** (workspace symbol search)

### Паттерн "push, не poll"
OpenCode: post-edit → 150ms debounce → push diagnostics в context.
Kiro: 29% сокращение команд через reactive diagnostics.
**Вывод**: если signum добавит LSP, diagnostics должны push'иться автоматически, а не запрашиваться.

## Рекомендация для /signum init

### Решение: Claude Code native tools, без внешних зависимостей

```
SCAN: Glob + Grep + Read + Bash
  ↓ structured signals (known-location files)
SYNTHESIZE: LLM (Claude)
  ↓ project.intent.md + project.glossary.json
PRESENT: interactive edit/confirm
  ↓ user approval
VERIFY: deterministic coverage check
```

### Почему НЕ LSP/Serena/Probe/tree-sitter:

| Constraint | LSP/Serena | Probe | tree-sitter | **Native tools** |
|-----------|-----------|-------|-------------|-----------------|
| Zero-setup | ❌ (binary per language) | ❌ (npx/npm) | ❌ (parsers per language) | ✅ |
| <30s | ❌ (2-5 min indexing) | ✅ | ✅ | ✅ |
| Deterministic | ✅ | ✅ | ✅ | ✅ |
| Language-agnostic | ✅ (with servers) | ✅ | ✅ (with grammars) | ✅ |
| No persistent server | ❌ | ✅ | ✅ | ✅ |
| Extracts goals/personas | ❌ | ❌ | ❌ | ✅ (via LLM synthesis) |

### Ключевой инсайт
> Scan targets signum init **структурно предсказуемы**: well-known files (README, package.json, CLAUDE.md), well-known patterns (entrypoints в bin/, commands/), well-known conventions (ADRs в docs/adr/). Semantic code search не нужен — targeted file reading достаточен.

### Когда добавить LSP в signum (будущее)
LSP имеет смысл для **фазы EXECUTE** (engineer agent) и **фазы AUDIT** (diagnostics push):
- Post-edit diagnostic loop (write → LSP diagnose → fix → iterate)
- Call hierarchy для impact analysis в scope gate
- Type-aware refactoring verification

Но это отдельная feature, не часть `/signum init`.

## Ranked Signal Hierarchy для /signum init

| Priority | Source | Extractable Signals | Tool |
|----------|--------|-------------------|------|
| 1 | `docs/how-it-works.md`, `docs/architecture.md` | Goal, Capabilities (authoritative) | Read/Glob |
| 2 | `CLAUDE.md`, `AGENTS.md` | Conventions, Non-Goals | Read |
| 3 | `README.md` (first 100 lines) | Goal fallback | Read |
| 4 | `package.json` / `pyproject.toml` / `Cargo.toml` | Tech stack, scripts, description | Read |
| 5 | `.github/workflows/*.yml`, `Makefile`, `justfile` | Success Criteria, task targets | Read/Glob |
| 6 | `bin/`, `commands/`, `skills/`, `console_scripts` | Capabilities, Personas | Glob |
| 7 | `git log --dirstat --since="6 months ago"` | Activity-weighted Capabilities | Bash |
| 8 | `docs/adr/*.md` (rejected/deprecated) | Non-Goals (explicit only) | Glob/Grep |
| 9 | Module/package directory names | Glossary candidates | Glob |

## Plugin Ecosystem Compatibility (Decision Matrix)

| Approach | Setup Steps | Plugin Fit | Maintenance Risk |
|----------|------------|------------|------------------|
| **Custom Bash/Python** | 0 | Perfect | Zero (owned) |
| **ast-grep (optional)** | 1 (npx) | Excellent | Low (3-week cadence) |
| **Probe** | 1 (npx) | Good | Medium |
| **Claude Code native LSP** | 1+ per language | Native | Low/Medium |
| **Serena** | 2+ per language | Poor | High |
| **CodeGraph-Rust** | 5+ (Docker+DB) | Very Poor | High |

## Sources

### Primary (fetched and analyzed)
- [Serena GitHub](https://github.com/oraios/serena) — architecture, tools, issues
- [Probe GitHub](https://github.com/probelabs/probe) — ripgrep + tree-sitter architecture
- [lsp-mcp GitHub](https://github.com/jonrad/lsp-mcp) — raw LSP-to-MCP bridge
- [CodeGraph-Rust GitHub](https://github.com/Jakedismo/codegraph-rust) — knowledge graph approach
- [Code Pathfinder](https://codepathfinder.dev/mcp) — static analysis MCP
- [AI Coding Assistants for Large Codebases (kilo.ai)](https://blog.kilo.ai/p/ai-coding-assistants-for-large-codebases) — approach comparison
- [Tree-sitter vs LSP (Lambda Land)](https://lambdaland.org/posts/2026-01-21_tree-sitter_vs_lsp/) — fundamental comparison
- [Aider RepoMap](https://aider.chat/2023/10/22/repomap.html) — tree-sitter + PageRank
- [Claude Code Plugins Reference](https://code.claude.com/docs/en/plugins-reference) — LSP plugin architecture

### Secondary (search results analyzed)
- [Kiro Code Intelligence](https://kiro.dev/docs/cli/code-intelligence/) — steering docs pattern
- [OpenCode LSP](https://opencode.ai/docs/lsp/) — diagnostics-only exposure
- [oh-my-pi](https://github.com/can1357/oh-my-pi) — 40+ LSP language configs
- [ast-grep MCP](https://github.com/ast-grep/ast-grep-mcp) — structural AST pattern matching
- [LSPRAG paper](https://arxiv.org/html/2510.22210v1) — hybrid LSP+tree-sitter RAG
- [LocAgent paper](https://arxiv.org/html/2503.09089v1) — graph-guided LLM for code localization
- [RAG vs GraphRAG evaluation](https://arxiv.org/html/2502.11371v1) — systematic comparison
- [Evaluating LSP-based code intelligence (Nuanced.dev)](https://www.nuanced.dev/blog/evaluating-lsp) — performance impact analysis
