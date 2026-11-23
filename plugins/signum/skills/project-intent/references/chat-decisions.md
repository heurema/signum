# Decisions from an ordinary conversation

These commands are for the agent in a project with its own Signum journal. Resolve scripts beside this skill. Use this file's installed version when an older conversation still mentions a previous plugin version. The owner can speak normally.

### Before recommending an approach

Identify the requested outcome and the current stage before suggesting how to build it. When the boundary is missing from the conversation or could have changed, read it from the project journal with `scripts/recall.py --project ROOT --query "distinctive words"`. If the match is unclear, omit `--query` to find the relevant topic, then read it with `--topic KEY`. Use the current entry and its scope. A withdrawn draft, a pending option or an implementation choice for another task cannot extend it.

If the outcome fits the current stage, answer within it. An ambiguous verb such as “open” or “run” does not itself request a new interface. When comparing architectures, first recover the earlier direction from the relevant project document or discussion. Follow its source link with the host's chat reader when available. Distinguish the owner's words from the agent's suggestions. If the source is unavailable, say which part is unknown. Ask only if it affects the requested answer.

Carry the same boundary into a draft and recheck its decision versions at start. If the outcome is settled and only its implementation is open, state the operation once and ask one question about how to perform it. Each option must deliver the same result for the same inputs and preserve the agreed evidence. Use the project's concrete alternatives. An existing test can demonstrate the result; running it is an implementation option only when it actually performs the requested operation. A decision governing a different component does not settle this choice. If the owner or a current decision already specifies the method for this operation, proceed within that scope. Comparing alternatives leaves adoption open.

### Recording and resuming

For a read-only question about the next step, use `signum.py --project ROOT next --query "topic words"` and answer from its result. If it flags the current message, compare the request with the existing scope. A proposed change stays open. Use the capture and start workflow below when the owner actually requests a decision or action.

1. When a discussion changes project scope, technology, behavior or a contract check, compare the owner's words with the visible proposal. Give separate topics to independently decidable points. Capture a choice from the user's current turn before claiming it is saved. State an assistant recommendation as an open proposal. If its final message is not yet available, use the existing `prepare` workflow to store the exact proposal before showing it. On recovery, attach the visible source. Never turn a choice into permission to implement.
2. With the thread and turn from the native hook or the history reader, run `python3 -B scripts/signum.py --project ROOT capture`. Pass JSON on stdin. Use the current version of each topic for `expected`, or null for a new topic. For a direct declaration, use the exact self-contained owner clause as both summary and quote. For a short assent, use the current reply as quote and the exact previously displayed proposal as summary. Follow the recorded result. An open result is an open choice. Avoid a second question until it affects the requested work.
3. For a continuation, find the confirmed permission and its decision versions. If the request fits, run the normal bounded `start`. Its check also reads the inbox. When it reports a pending readable message, use `python3 -B scripts/capture_inbox.py --project ROOT` and review its meaning. Read clipped text with `scripts/chat_source.py --project ROOT --thread THREAD --turn TURN`. Capture `choices: []` only after checking that the actual message contains no decision. Then retry. For a change that affects the next action, ask one question naming the existing choice and the proposed change. Wait for the answer before adopting the revision. When the owner has already confirmed that exact revision, use the recorded version.
4. For old conversations before first activation, or when a native receipt is missing, use the project-scoped history reader to find the relevant exchange. The reader alone cannot authenticate the speaker. Keep unsupported choices open. Recover existing accepted decisions and permissions from `status`. Consult the owner only when the remaining open choice changes what can be done.
5. When drafting work, list every actual decision dependency as `depends_on: [{"topic": "…", "id": "…"}]`. An owner request identifies the assignment; it does not establish missing decisions. Use `depends_on: []` for independent work. A pending topic cannot supply a confirmed version. Re-read the named decision after a correction. The core checks the contract, its dependencies and known source issues at start. A missing historical message alone leaves an otherwise valid exact-scope permission usable. The inbox retains that warning for recovery. A newly proposed option never silently supersedes an agreed version. Source records for one project never authorize work in another.

For “SQLx подходит, откат пока оставим открытым”, submit two choices with distinct topics. The first is chosen, with “SQLx подходит” as its summary and quote. The second is open, with the owner's deferral clause as its quote. Use the same structure for other project decisions. Never discard a qualifier or conditional phrase to manufacture a positive answer.

Example payload shape:

```json
{"thread":"<source thread UUID>","turn":"<source turn UUID>","choices":[{"topic":"migration-tool","summary":"SQLx подходит","quote":"SQLx подходит","state":"chosen","expected":null},{"topic":"migration-rollback","summary":"Rollback policy remains open","quote":"откат пока оставим открытым","state":"open","expected":null}]}
```

The submission hook records a digest and source IDs locally, independent of the model. It does not save the entire conversation or accept a proposal. Hooks require Codex trust. The history reader requires a local Codex App Server with an indexed project history.

When an owner authorizes a working copy to read its original project's chat evidence, run
`history_origin.py --project COPY --source ORIGINAL` once. The registration is local to the
user and exact copy directory. Inbox checks, exact reads and dependent starts can then look up
the inherited receipts in the original project's interactive chats. Only the registered turn IDs
are eligible. A matching journal prefix, both receipt digests and the actual source text are
checked before a recovered message is exposed. The copy continues to read its own new turns.
The inbox lists verified origin IDs in `origin_recovered`. A readable turn still needs ordinary
review when pending. Report missing receipts and preserve their status.

An interrupted turn can still contain a decision. Review its entire available message through the usual capture workflow. A standalone connection probe in a confirmed interrupted turn with no final answer can clear automatically. Keep inaccessible receipts available for later recovery. A source mismatch, a failed history check or a readable unresolved turn needs attention before dependent work. Report the waiting contract, the source turn and the reason shown by `start`. On the next check the inbox looks up missing receipts by their exact turn, including receipts older than its index window. If a receipt arrives after the message was reviewed, replay the original choices for the listed topics. Repeated delivery of the same decision keeps its version.

If the owner explicitly sets aside a particular unavailable receipt, use `capture_inbox.py --project ROOT --archive-receipt THREAD/TURN --reason TEXT`. Supply each selected pair separately. Archive only the named receipts under that instruction. The archive preserves their digest, reason and time. Replaying the hook alone does not requeue them. A source found in history is still available for review.

## After updating the plugin

Codex can load a new installation and its trust setting on disk while an open chat still uses its earlier handlers. Verify a fresh receipt against the exact chat, turn and prompt. If the current Desktop process has not loaded the change, restart Desktop between tasks and reopen the same saved chat. A new ordinary message can then test the updated handler. An earlier unobserved message remains unverified. Temporary Codex sessions without persistent history do not create receipts.
