---
name: project-intent
description: For opted-in Signum projects, capture ordinary chat decisions and recover missed agreements, start or revisit a kickoff, resolve intent questions internally and recommend the next step, or deliver a scoped change. For “продолжи разбор поставки” use scripts/resume_delivery.py --project ROOT --rehearse. For brief recall use scripts/recall.py. Skip routine independent edits.
---

# Project intent

Use the user's project root. Resolve scripts beside this installed file. Reply in the user's language.

Apply the project workflow when the root has its own `.signum/state.json` or its project instructions identify Signum. The core requires the local journal for writes. In other projects continue in ordinary chat. Only an explicit request to connect a project starts [project setup](../setup/SKILL.md). A parent project, another open chat or an installed language profile grants no opt-in.

## Decisions in ordinary chat

On a project choice or a continuation, follow [chat decisions](references/chat-decisions.md).
Use the native turn receipt when available. Split independent choices, retain open
questions, and check recent project turns before answering from memory or starting dependent work.
Simple unrelated edits need no journal call.

## Starting a project or deciding where to begin

When asked to start slowly, initiate a project or work out what to do next, run `python3 -B <this-skill>/scripts/orient.py --project ROOT` and read the project's instructions. This read-only inventory shows the actual files, toolchain markers and recorded work. Open the relevant project plan, brief, latest mockup or earlier owner discussion. If there is no local link, use available project chat search by project name and subject. Check the owner's actual words before relying on a summary. An empty folder can belong to a project with substantial design work elsewhere.

On a repeated kickoff, start from today's files, current decisions and the owner's corrections in the earlier project chat. A request to revisit the starting point in an opted-in project calls for a fresh look. Carry forward useful materials. Examples, mockups, assistant suggestions and withdrawn proposals supply context and do not authorize a new rule, contract, scaffold or implementation.

Resolve intent questions internally first. Read the relevant source or try an inexpensive reversible example. On consequential ambiguity, the lead (typically Sol) can delegate one independent check to a native Astra agent while developing a recommendation. Give it the minimum relevant facts and ask it to find an unsupported premise or a counterexample. Check its objection against evidence and synthesize once. If that reviewer is unavailable, do the check locally. Agreement between models cannot supply an owner's preference or approval.

Give the owner a brief concrete result and the next step. If the decision can wait, proceed within scope with zero questions. When only the owner can resolve a choice that changes the next step, ask one focused question and say what depends on it. If the owner has no formed preference yet, offer a worked example. Avoid turning an internal list of questions into an interview. Ordinary independent tasks need no second agent.

Use the same reasoning later when a substantive request appears to assume earlier work is finished or changes an agreed sequence. Look at the relevant decision, task list or handoff and actual files or delivery result. Tell the owner which earlier step is still open and how it affects this request. A real prerequisite or an explicit prior commitment warrants pausing the dependent part and recommending a way forward. A preferable order alone is advice. Independent work can proceed. A consciously changed priority is the owner's choice. Do not turn every open task into a blocker or require a planning round on routine edits.

For an adopted Rust direction, distinguish an empty Cargo workspace from selection of an application starter or the first domain crate. A concrete owner instruction to recover a schema or create only an empty workspace gives the scope for that action. Carry out that scope directly, without reopening settled choices. A regular change in a working codebase keeps its normal path.

## From a request to working code

During a pilot with a designated Signum feedback backlog, capture observed plugin bugs, friction or ideas there. Attach a project alias, workflow step, impact and a local turn or contract reference. Group repeats. If the backlog is outside the writable workspace, buffer the observation in `.signum/feedback-pending.md` and report it as pending until transferred. Continue the host project's task. Pause a dependent action if scope or acceptance cannot be trusted. Logging grants no authority to repair the plugin.

Use this path for a substantive change or when the owner asks to work under a contract. A routine edit can stay in ordinary chat. Read the relevant existing behavior. Put the user's request in their own words and the intended result in yours. If two plausible readings produce different outcomes, give the smallest example that separates them and ask one question. Let an undecided owner compare the outcomes. Research a material unknown only until it changes the choice.

If the project has a language workflow in `mise-tasks/signum-<language>`, inspect that project-owned task when drafting verification. Choose the smallest checks that exercise the promised behavior, name them in the contract and run them through the normal evidence step. A missing required check leaves the criterion open. Projects can keep their existing verification commands.

Use the project root of a small isolated working copy. Run the adjacent `scripts/signum.py --project ROOT draft --topic KEY` with JSON on stdin:

```json
{"request":"Full verbatim owner message","owner_turn":"codex:turn:<actual turn UUID>","outcome":"Observable behavior","rationale":"Why, including any clarifying answer","excludes":"Behavior and actions outside this change","files":["relative/file"],"report":"check.json","verification":"Concrete success example and a counterexample"}
```

List decision dependencies in `depends_on` as exact `{"topic":"…","id":"…"}` versions. Use an empty list for genuinely independent work. Use a new report path. Supply a real owner turn identifier from the conversation and the full original wording when it actually assigns this work. Alternatively use `basis` with the topic and exact ID of a current owner-confirmed decision that selects it. Read the source and check the scope yourself. Never make up an identifier or use an example as an assignment. For a revision pass `expected` with the confirmed permission ID. Show the outcome, limits, paths, check and report. Only confirm the displayed permission when its full scope is authorized.

Run `start ID --touch path` before editing; repeat `--touch` for additional intended files. Implement the smallest change. If the work needs a wider boundary, stop and propose a revision. Record the check and hand the exact version to the owner using [the delivery steps](references/delivery.md). A failed check or an extra workspace change blocks delivery. A local scope check covers persistent changes in this copy; it grants no external side effects.

## Delivery investigations

Use the current Desktop receipt when supplied. Otherwise run `scripts/resume_delivery.py --project ROOT --rehearse`. If several records match, ask which one. Use `--id` or an explicitly selected `--pair ID1 ID2` with the supplied IDs. For a trial result, state what verified in the disposable copy and what still requires the owner's decision. See [delivery](references/delivery.md) for recovery details.

## “What did we agree?”

Run `scripts/recall.py --project ROOT --query "distinctive words"` first. Answer from a current match. If unclear, use the topic index, then `--topic KEY`. Preserve version, authority and uncertainty. A read failure leaves the state unknown.

## A decision in conversation

Keep ordinary edits, small reversible choices and open brainstorming in the normal conversation. Continue independent work while an important choice is open. Use the local journal for a real decision. `core` below is `python3 -B <skill-directory>/scripts/signum.py --project <root>`.

For a known topic and complete wording, read this skill and run `core prepare --topic KEY` together. Send `summary, source, reason` on stdin. Include `expected` for a particular predecessor or `null` for a deliberately new topic. Display the exact pending terms. For relative changes first resolve the current wording. Surface any conflict.

For a native chat decision use the source-checked capture workflow first. In the manual journal workflow, for whole, unqualified assent to a displayed plain decision, run `core assent` once. Send JSON with `reply` (verbatim), `shown` (complete wording), and the proposal `id`. If the wording was shown before staging, send `record` instead of `id`, including the known predecessor. A returned `confirmed` or `already_current` completes the turn. Changed terms stay open. For a rich record, permission, competing version or a clear affirmative outside the short vocabulary follow [Recording a choice](references/decisions.md). Preserve the original authority limits and the project's agreed research method.

## Resuming or delivering work

For a contract use `python3 -B <skill-directory>/scripts/signum.py --project <root> contract [--topic <key>]`. Include intended result, scope that can proceed now, open decisions, acceptance observation and authority limits. Distinguish proposed checks from observed evidence. Use `history --topic <key>` for earlier wording. For a change from agreed contract through verification and owner acceptance follow [Delivering an agreed change](references/delivery.md).
