# Research: Context Inheritance And Cross-Contract Coherence In Signum

Question: How should task-local contracts inherit project-wide context and stay coherent across a large project?

Verification status: `partially_verified`

## 1. Short answer

Task-local contracts should not embed the whole project context. They should inherit project context by reference plus a small frozen snapshot.

For Signum, that means:

- keep `contract.json` narrow and executable;
- keep `repo-contract.json` for repo-wide invariants;
- add a project/initiative layer above task contracts;
- add explicit lineage and dependency edges between contracts;
- record which upstream intent, ADRs, glossary terms, and global constraints were inherited;
- mark contracts stale when those upstream artifacts change.

Current Signum already has the beginnings of this model, but only at task scope: `assumptions`, `openQuestions`, `requiredInputsProvided`, `parentContractId`, `relatedContractIds`, and `repo-contract.json`. It does not yet model project-wide intent inheritance, initiative/epic linkage, or invalidation on upstream changes.

## 2. Concrete claims

1. Current Signum already supports clarification state and minimal lineage at the task-contract level.
Evidence:
- `assumptions`, `openQuestions`, `requiredInputsProvided`, `parentContractId`, and `relatedContractIds` exist in the schema.
- The contractor populates lineage by checking overlap with prior contracts in `.signum/contracts/index.json`.

2. Current Signum lineage is overlap-based and too weak for large-project coherence.
Evidence:
- `parentContractId` is set from the most recent overlapping contract.
- `relatedContractIds` is populated for overlapping or dependent contracts.
- There is no first-class concept of `project`, `initiative`, `epic`, `ADR`, `glossary`, or `stale contract`.

3. Repo-wide invariants should remain outside task-local contracts.
Evidence:
- Signum already treats `repo-contract.json` as a project-root invariant layer that blocks regardless of task-local acceptance criteria.
- This separation matches the architectural need to avoid repeating global constraints in every task slice.

4. Prior art converges on layered artifacts rather than a single monolithic spec.
Evidence:
- Kiro uses `Requirements -> Design -> Tasks` plus steering files.
- GitHub Spec-Kit is organized around a constitution plus per-spec branches/files.
- Tessl argues for one feature at a time and warns against overloading context with full-app specs.
- Archgate turns ADRs into executable governance and exposes architectural decisions to agents before coding.

5. Cross-contract semantic coherence needs a shared glossary or ubiquitous language layer.
Evidence:
- DDD-style ubiquitous language is repeatedly identified as the base for precise AI-readable specs.
- Local research found no mature off-the-shelf open-source stack that fully automates glossary extraction, alias resolution, drift detection, and contradiction detection; this remains a gap to assemble explicitly.

6. Large-project coherence requires invalidation when upstream intent changes.
Evidence:
- Current Signum captures proof and repo invariant baselines, but not whether a task contract was derived from an outdated project intent or superseded ADR.
- Prior art around constitutions, steering files, ADRs, and living specs implies upstream context must be versioned and re-applied, not just read once.

## 3. Sources with URLs and local file refs

### Signum baseline

- [contract.schema.json](/Users/vi/personal/skill7/devtools/signum/lib/schemas/contract.schema.json#L138)
  Fields for `assumptions`, `openQuestions`, `invariants`, `requiredInputsProvided`, `parentContractId`, `relatedContractIds`.
- [contractor.md](/Users/vi/personal/skill7/devtools/signum/agents/contractor.md#L32)
  Contractor writes `draft` contracts, blocks on unresolved ambiguity, and detects lineage from overlapping contract scope.
- [README.md](/Users/vi/personal/skill7/devtools/signum/README.md#L74)
  `repo-contract.json` is a separate repo-wide invariant layer.
- [signum.md](/Users/vi/personal/skill7/devtools/signum/commands/signum.md#L430)
  Hard stop on `requiredInputsProvided=false` and non-empty `openQuestions`.
- [signum.md](/Users/vi/personal/skill7/devtools/signum/commands/signum.md#L736)
  Clover reconstruction exists as a consistency check, but not as cross-contract graph validation.

### Prior art and research

- [intent-preservation.md](/Users/vi/vicc/docs/research/2026-03-12-intent-preservation.md#L108)
  Local synthesis of Kiro, Spec-Kit, Tessl, Augment Intent, Archgate, AgDR, Git AI, SpecStory.
- [spec-driven-development.md](/Users/vi/vicc/docs/research/2026-03-12-spec-driven-development.md#L146)
  Notes that large full-app specs overload context; recommends feature-sized specs and highlights DDD glossary value.
- [nl-consistency-checking.md](/Users/vi/vicc/docs/research/2026-03-12-nl-consistency-checking.md#L96)
  Finds glossary drift detection remains largely manual and requires an assembled pipeline.

### URLs cited by the local research

- [Kiro](https://kiro.dev/)
- [GitHub Blog: Spec-driven development with AI](https://github.blog/ai-and-ml/generative-ai/spec-driven-development-with-ai-get-started-with-a-new-open-source-toolkit/)
- [Tessl](https://tessl.io/)
- [Augment Intent](https://www.augmentcode.com/product/intent)
- [Archgate](https://archgate.dev/)
- [Agent Decision Record](https://github.com/me2resh/agent-decision-record)
- [Git AI](https://usegitai.com/blog/introducing-git-ai)
- [SpecStory](https://specstory.com/)
- [Martin Fowler: Understanding Spec-Driven Development](https://martinfowler.com/articles/exploring-gen-ai/sdd-3-tools.html)

## 4. Proposed inheritance/linkage fields

Below is the minimal field set I would add above and around Signum's current lineage model.

### Project and initiative linkage

- `projectRef: string`
  Stable ID or path to the project-intent artifact.
- `initiativeRef: string`
  Stable ID for the initiative or workstream this contract belongs to.
- `epicRef: string`
  Optional finer-grained grouping when initiative is too broad.
- `boundedContext: string`
  DDD-style ownership or domain boundary.

### Inherited context

- `repoContractRef: string`
  Ref to `repo-contract.json` or a versioned equivalent.
- `adrRefs: string[]`
  ADRs or executable governance rules that constrain the work.
- `glossaryRefs: string[]`
  Relevant glossary artifacts or term namespaces.
- `globalConstraintsInherited: string[]`
  Explicit IDs of inherited rules that apply to this contract.
- `nonGoalsInherited: string[]`
  Project or initiative non-goals propagated into the task slice.

### Lineage and dependency graph

- `parentContractId: string`
  Keep existing field.
- `relatedContractIds: string[]`
  Keep existing field, but narrow semantics.
- `dependsOnContractIds: string[]`
  Contracts that must land first.
- `blockedByContractIds: string[]`
  Explicit blockers for planning/execution.
- `supersedesContractIds: string[]`
  Older contracts replaced by this one.
- `supersededByContractId: string`
  Reverse pointer when a contract becomes obsolete.

### Traceability and staleness

- `contextSnapshotHash: string`
  Hash over the inherited upstream artifacts at contract creation time.
- `contextSnapshotRefs: string[]`
  Exact artifact versions used when drafting the contract.
- `sourceIntentRefs: string[]`
  Links to the user brief, initiative doc, or planning artifact from which the contract was derived.
- `interfacesTouched: string[]`
  Named interfaces, schemas, or service boundaries this contract changes.
- `staleIfChanged: string[]`
  Upstream artifact refs that invalidate this contract when changed.
- `stalenessStatus: "fresh" | "warning" | "stale"`
  Execution gate or advisory state.

### Optional governance helpers

- `decisionRefs: string[]`
  Agent or human decisions that materially shaped the contract.
- `ownerContext: string`
  Human/team owner for the bounded context or initiative.

## 5. Open questions

1. What is the minimal canonical project-level artifact stack for Signum MVP?
Is one `project.intent.md` enough, or does Signum need both `project.intent.md` and `project.constitution.md` plus ADRs?

2. Where should initiative/epic artifacts live?
Inside `.signum/`, in `docs/`, or as first-class repo objects with IDs and indexes?

3. What should invalidate a contract?
Any upstream change, only semantic changes, or only changes to referenced constraints/ADRs/interfaces?

4. Should invalidation hard-block execution?
For large projects, hard blocking every stale contract may be too noisy. An advisory mode plus required refresh before approval may be better.

5. How much inherited context should be copied into the contract?
Too little causes hidden drift; too much recreates context bloat. The right split is still open.

6. Should lineage stay overlap-based or become explicit?
Overlap detection is a good heuristic, but it is not a substitute for explicit initiative/epic/dependency edges.

7. Where should glossary enforcement live?
Inside the contractor, a separate validator, or a project-level consistency checker?

## 6. Confidence

Confidence: `0.87`

Reasoning:

- High confidence on the current Signum baseline because the schema, contractor instructions, and orchestration docs are explicit.
- Medium-high confidence on the architectural direction because multiple prior-art systems point to layered artifacts, steering/constitution files, ADR retrieval, and feature-sized specs.
- Lower confidence on exact field naming and invalidation semantics because this part is design synthesis, not an established standard.

## Codex synthesis

The right model for large projects is not `one contract per task in isolation`. It is:

`project intent / constitution / ADRs -> initiative or epic artifacts -> task-local contract -> code -> audit`

Signum already has the task-local end of this chain. The missing part is the project and initiative layer, plus explicit staleness and dependency semantics.

If Signum adds only one thing next, it should add project/initiative refs and `contextSnapshotHash` plus `staleIfChanged`. That gives immediate leverage:

- contracts can inherit context without bloat;
- downstream stages can tell whether the contract is still valid;
- multiple contracts in one project become graph-connected rather than isolated JSON files.
