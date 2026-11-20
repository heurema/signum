# Signum: Project Intent, Contract Hierarchy, and Clarification Architecture

Date: 2026-03-15
Author: Codex
Verification status: `partially_verified`
Research mode: `delve`

## Question

How should a Signum-like contract-first system handle large-project intent, task-local contract clarity, and cross-contract coherence without turning each contract into a bloated copy of the whole project?

## Executive Summary

The main conclusion is structural:

- `Intent Clarification` should remain inside `CONTRACT`, not become a separate source of truth.
- Large projects need a project-level intent layer above task contracts.
- Task contracts should stay narrow and executable, inheriting context by reference plus a small explicit snapshot.
- `repo-contract.json` is necessary, but it is not enough to preserve project meaning.
- Cross-contract coherence requires first-class lineage, glossary discipline, ADR retrieval, and stale-contract detection.
- Self-improvement is useful, but only with a frozen evaluator. The immediate win is a stricter `draft -> critique -> revise -> gate` loop around `contract.json`.

For Signum, the right target architecture is:

`project intent / constitution / ADRs -> initiative artifact -> task contract -> code -> audit`

Signum already implements the task-local end of this chain. What is missing is the project and initiative layer, plus inheritance and consistency mechanisms across many contracts.

## What Sources Say

### 1. Current Signum already treats clarification as part of contract drafting

This is explicit in the current system:

- the contractor transforms a vague user request into a precise `draft` contract;
- the schema already has `assumptions`, `openQuestions`, and `requiredInputsProvided`;
- the pipeline hard-stops when unresolved inputs or open questions remain;
- `parentContractId` and `relatedContractIds` already exist for minimal lineage;
- `repo-contract.json` already acts as a repo-wide invariant layer.

That means the current Signum model is not "clarify before contract". It is "clarify inside draft contract".

### 2. Prior art converges on layered artifacts, not one monolithic spec

Across the systems reviewed, the recurring pattern is not to stuff all context into one task artifact:

- `Kiro` separates `Requirements -> Design -> Tasks` and uses steering files.
- `GitHub Spec-Kit` uses constitution-style guidance plus per-spec artifacts.
- `Tessl` pushes toward feature-sized specs and warns against bloated full-app context.
- `Archgate` makes architectural decisions executable and retrievable before implementation.
- `Augment Intent`, `SpecStory`, and similar systems preserve durable intent/context artifacts instead of relying on chat history alone.

The shared lesson is simple: large-project context must be durable, layered, and retrievable.

### 3. Preventing semantic drift needs more than task-local validation

The local research and product review converge on four practical defenses:

- a shared glossary or ubiquitous language;
- durable upstream intent artifacts;
- traceability between intent, contracts, code, and decisions;
- explicit consistency checks across artifacts.

`Clover`-style consistency checking is especially relevant here: when direct ground truth is hard to obtain, consistency between upstream goal, local contract, and downstream evidence becomes a workable proxy.

### 4. Self-improvement loops help, but only with a frozen gate

`autoresearch` is relevant because it separates mutable proposals from a fixed evaluator. That pattern transfers well to Signum, but only in a constrained form:

- inside one task: refine the draft contract through explicit critique passes;
- across many tasks: improve contractor/reviewer prompts against a stable contract-quality evaluator.

What does not transfer cleanly is open-ended "let the model improve itself" without an external quality gate.

## Codex Synthesis

### Core position

The right design is not a new pre-contract canonical document. The right design is a hierarchy:

- project-wide intent and rules live above task contracts;
- task contracts inherit the relevant slice of that context;
- clarification remains part of `CONTRACT`, but the system becomes better at inheritance, lineage, and readiness gating.

So the answer to the earlier uncertainty is:

- `Intent Clarification` is a substage of `CONTRACT`;
- a user-facing clarification summary can exist, but only as a derived view over the draft contract;
- the missing piece is not "another contract-like document", but a project-level intent layer plus contract graph semantics.

## Recommended Artifact Hierarchy

### MVP hierarchy

If Signum adds the minimum viable structure for large projects, it should be:

- `project.intent.md`
  - what the project is
  - who it is for
  - core goals
  - non-goals
  - major capabilities
  - glossary
  - success criteria
- `repo-contract.json`
  - repo-wide executable invariants
- `docs/adr/`
  - architecture decisions and rationale
- `.signum/contracts/<id>/contract.json`
  - narrow executable task contract

### More complete target hierarchy

For sustained multi-contract projects, a fuller stack is justified:

- `project.intent.md`
- `project.constitution.md`
- `repo-contract.json`
- `docs/adr/`
- `docs/agdr/` or equivalent for agent/human decision records
- `docs/initiatives/<id>.md`
- `.signum/contracts/<id>/contract.json`

This separates:

- long-lived project meaning;
- stable engineering and governance rules;
- architectural decisions;
- initiative-level decomposition;
- task-local execution contracts.

## Recommended Contract Inheritance Model

Task contracts should not embed the full project context. They should inherit by reference plus a compact snapshot.

### Fields worth adding

- `projectRef`
- `initiativeRef`
- `epicRef`
- `repoContractRef`
- `adrRefs`
- `glossaryRefs` or `glossaryVersion`
- `globalConstraintsInherited`
- `nonGoalsInherited`
- `dependsOnContractIds`
- `blockedByContractIds`
- `supersedesContractIds`
- `interfacesTouched`
- `sourceIntentRefs`
- `contextSnapshotRefs`
- `contextSnapshotHash`
- `staleIfChanged`
- `stalenessStatus`

### Why this model is better

It avoids three failure modes:

1. `Prompt bloat`
Every local contract becomes unreadable and unstable if it embeds the whole project.

2. `Hidden drift`
If a contract only "implicitly" depends on project context, the system cannot tell when it became stale.

3. `Conflicting local truths`
Without explicit lineage and inherited constraints, isolated task contracts will diverge semantically even if each one is locally valid.

## Clarification Architecture

### Recommended position

`Intent Clarification` should stay inside `CONTRACT`.

The output of the clarification substage is still the draft contract, but with a clearer internal structure and better gates.

### Minimal clarification outputs inside the draft contract

- `goal`
- `inScope`
- `outOfScope`
- `assumptions`
- `openQuestions`
- `requiredInputsProvided`
- `acceptanceCriteria`
- `readinessForPlanning`

### Useful new fields

- `ambiguityCandidates`
- `contradictionsFound`
- `clarificationDecisions`
- `assumptionProvenance`
- `readinessForPlanning`

### User-facing artifact

If Signum wants a lighter UX layer, it should render a `clarification summary`.

But this summary should be a view over the draft contract, not a second canonical artifact. Otherwise the system creates the exact drift problem it is trying to solve.

## Cross-Contract Coherence

Large-project correctness is not only "does code satisfy this one contract?". It is also "does this contract still fit the project?"

### Mechanisms Signum should add

1. `Glossary check`
Block or warn when critical domain terms are undefined, conflicting, or replaced by drifting synonyms.

2. `Cross-contract overlap check`
Detect overlapping scope, duplicate effort, and conflicting assumptions.

3. `ADR relevance retrieval`
Before writing a contract, retrieve relevant ADRs based on interfaces, paths, or initiative tags.

4. `Intent diff check`
Compare the contract against project and initiative intent, not only against repo invariants.

5. `Upstream change invalidation`
If the referenced glossary, project intent, or ADR set changes, mark the contract as `warning` or `stale`.

6. `Dependency graph`
Treat contracts as a graph, not a flat list of JSON files.

### Practical blocking policy

- `BLOCK` on contradiction with repo invariants or ADR-backed rules
- `BLOCK` on unresolved required inputs
- `BLOCK` on missing explicit dependency when an interface or upstream slice is required
- `WARN` on terminology drift
- `WARN` on probable overlap with another contract
- `WARN` on possible semantic conflict with low confidence

## Self-Improvement Layer

### Useful now: within-task refinement

The immediate improvement loop should be:

1. generate `draft contract`
2. run `ambiguity review`
3. run `missing-input review`
4. run `contradiction review`
5. run `goal reconstruction / coverage review`
6. revise
7. stop and ask user if required inputs remain unresolved

This is a direct extension of the current Signum structure.

### Useful later: cross-run optimization

After enough contracts exist, Signum can improve:

- contractor prompts
- review prompts
- ask-vs-assume policy
- rubric weights

But only against a frozen evaluator, for example:

- contradiction/consistency checks
- unresolved-question policy
- goal-reconstruction score
- downstream underspecification rate found by `EXECUTE` or `AUDIT`
- quality of user approval, not just approval frequency

### Hard rule

Do not optimize the contractor and the evaluator together.

If both move at once, the system will optimize for passing its own rubric rather than preserving user intent.

## Recommended MVP for Signum

If the goal is a minimal, high-leverage next step, I would implement only this:

1. Add `project.intent.md`
2. Keep clarification inside `draft contract`
3. Extend contracts with:
   - `projectRef`
   - `initiativeRef`
   - `adrRefs`
   - `contextSnapshotHash`
   - `staleIfChanged`
   - `stalenessStatus`
4. Add a basic glossary section to `project.intent.md`
5. Add two new checks:
   - `intent_diff_check`
   - `upstream_change_invalidation_check`
6. Make the contract graph explicit through dependency fields

This is enough to make local contracts project-aware without overbuilding a heavy platform.

## Tradeoffs and Disagreements

### Open tradeoffs

- One `project.intent.md` may be enough for MVP, but large repos may quickly need initiative-level artifacts.
- Hard-blocking every stale contract may create too much churn; advisory invalidation may be better before final approval.
- Glossary enforcement can start lexically, but stronger semantic drift detection likely needs a hybrid deterministic + LLM review pipeline.
- A richer schema improves governance, but schema changes are heavier and riskier than prompt/rubric changes.

### Weakly supported areas

- Exact field naming is design synthesis, not established standard.
- There is no dominant off-the-shelf open-source solution for glossary extraction plus automatic drift detection in this exact workflow.
- The best thresholds for automated prompt optimization in contract systems are still unclear.

## Verification Status

Overall label: `partially_verified`

What is strongly verified:

- current Signum contract fields and hard-stop behavior
- existence of repo-wide invariant handling
- current lineage fields
- existence of critique/reconstruction patterns inside Signum

What is partially verified:

- convergence claims across Kiro, Spec-Kit, Tessl, Archgate, and related systems
- recommended artifact hierarchy above task contracts
- the transfer of `autoresearch`-style ratchet loops into contract-prompt evolution

## Open Questions

1. Should `project.intent.md` and `project.constitution.md` both exist from day one, or only the first one?
2. Where should initiative artifacts live: under `docs/`, under `.signum/`, or both?
3. What exactly should invalidate a contract: any upstream change, only semantic changes, or only referenced artifacts?
4. Should stale contracts hard-block execution or only hard-block approval?
5. Is glossary global for the whole repo or scoped by bounded context?
6. How much contradiction detection can be deterministic before LLM judgment is required?
7. When should Signum infer from inherited context and when should it stop and ask the user?

## Recommended Next Steps

1. Add `project.intent.md` to the Signum model and define its minimum schema.
2. Extend `contract.schema.json` with inheritance and staleness fields.
3. Add `intent_diff_check` and `upstream_change_invalidation_check`.
4. Add a basic glossary review pass during `CONTRACT`.
5. Keep self-improvement limited to prompt/rubric ratchets against a frozen evaluator.
6. Only after that, decide whether initiative-level artifacts need to become first-class.

## Source List

### Signum files

- [README.md](/Users/vi/personal/skill7/devtools/signum/README.md)
- [signum.md](/Users/vi/personal/skill7/devtools/signum/commands/signum.md)
- [contractor.md](/Users/vi/personal/skill7/devtools/signum/agents/contractor.md)
- [contract.schema.json](/Users/vi/personal/skill7/devtools/signum/lib/schemas/contract.schema.json)

### Supporting Codex research in this repo

- [2026-03-15-signum-context-inheritance-codex.md](/Users/vi/personal/skill7/devtools/signum/docs/research/2026-03-15-signum-context-inheritance-codex.md)
- [2026-03-15-signum-codex-semantic-drift-across-contracts.md](/Users/vi/personal/skill7/devtools/signum/docs/research/2026-03-15-signum-codex-semantic-drift-across-contracts.md)
- [2026-03-15-codex-contract-clarification-self-improvement-loops.md](/Users/vi/personal/skill7/devtools/signum/docs/research/2026-03-15-codex-contract-clarification-self-improvement-loops.md)

### Prior local research

- [2026-03-12-intent-preservation.md](/Users/vi/vicc/docs/research/2026-03-12-intent-preservation.md)
- [2026-03-12-spec-driven-development.md](/Users/vi/vicc/docs/research/2026-03-12-spec-driven-development.md)
- [2026-03-12-nl-consistency-checking.md](/Users/vi/vicc/docs/research/2026-03-12-nl-consistency-checking.md)
- [2026-03-14-autoresearch-delve-synthesis-2026.md](/Users/vi/vicc/docs/research/2026-03-14-autoresearch-delve-synthesis-2026.md)
- [2026-03-14-prompt-optimization-multi-agent-2026.md](/Users/vi/vicc/docs/research/2026-03-14-prompt-optimization-multi-agent-2026.md)

### External URLs

- [Kiro](https://kiro.dev/)
- [GitHub Spec-Kit blog post](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)
- [Tessl](https://tessl.io/)
- [Augment Intent](https://www.augmentcode.com/product/intent)
- [Archgate](https://archgate.dev/)
- [Martin Fowler on spec-driven development](https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html)
- [Clover paper](https://arxiv.org/abs/2310.17807)
- [autoresearch](https://github.com/karpathy/autoresearch)
- [Self-Refine](https://arxiv.org/abs/2303.17651)
- [Reflexion](https://arxiv.org/abs/2303.11366)
- [DSPy optimizers](https://dspy.ai/learn/optimization/optimizers/)
