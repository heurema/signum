# Pilot feedback

In a pilot with an explicitly designated Signum backlog, record each observed plugin incident during the task. Use the existing checkout address provided for this pilot. A project without its own enabled journal continues ordinary work.

Run `python3 -B <skill>/scripts/feedback.py --project PROJECT record` with a short JSON observation on stdin. When this task permits writing to the designated Signum checkout, append `--central-root ABSOLUTE_SIGNUM_ROOT`. The only central destination is its existing `docs/operations/plugin-feedback.md`. A sandboxed pilot can always keep the observation in its own queue. Supply the central root only within the current write authority, including on retry.

```json
{
  "project": "pilot-A",
  "symptom": "repeated-review-prompt",
  "source": "codex:turn:example",
  "kind": "friction",
  "step": "review",
  "expected": "one prompt",
  "observed": "two prompts",
  "impact": "one extra reply"
}
```

Use a stable project alias, a local turn or report reference, and a stable slug for the symptom. Keep the same three values on retry. Distinct symptoms in one turn can be recorded separately. Use `bug`, `friction`, or `idea`. Include `"issue": "SGF-012"` when the observation belongs to a known issue already in the backlog. Existing machine-recorded observations with the same symptom are grouped automatically. Only supply a sanitized description and a short local reference. Keep source text, private data and file contents in their originating project.

The command persists the observation in `.signum/feedback.json` before attempting the central write. A delivered receipt contains the SGF number. A pending receipt gives a reason. At handoff or a blocker, run `python3 -B <skill>/scripts/feedback.py --project PROJECT flush`, adding the authorized `--central-root` when available. Report the returned numbers and outstanding observations. Retain the local queue through handoff. Independent project work can continue.

Older human notes remain in `.signum/feedback-pending.md`. At handoff, inspect outstanding notes and submit each sanitized observation through the route. Preserve previous acknowledgements and skip notes already carrying a confirmed central ID. Leave existing notes in place. The route never copies notebook content into the backlog.
