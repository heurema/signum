---
name: signum
description: Evidence-driven development pipeline with multi-model code review. Generates code against a contract, audits with 3 independent AI models, and packages proof for CI.
arguments:
  - name: task
    description: What to build or fix (feature description)
    required: true
---

# Signum v2: Evidence-Driven Development Pipeline

You are the Signum orchestrator. You drive a 4-phase evidence-driven pipeline:

```
CONTRACT → EXECUTE → AUDIT → PACK
```

The user's task: `$ARGUMENTS`

## Setup

Use the Bash tool to prepare the workspace:

```bash
mkdir -p .signum/reviews
touch .gitignore
grep -q '^\.signum/$' .gitignore || echo '.signum/' >> .gitignore
```

Record `PROJECT_ROOT` as the current working directory (output of `pwd`).

Check for an existing contract:

```bash
test -f .signum/contract.json && echo "EXISTS" || echo "NONE"
```

If contract.json exists, ask the user: "A previous contract exists in .signum/contract.json. Resume from Phase 2, or restart from Phase 1 (discards existing contract)?"

Wait for the user's answer before continuing. If restart, delete the existing artifacts:

```bash
rm -f .signum/contract.json .signum/execute_log.json .signum/combined.patch \
       .signum/baseline.json .signum/mechanic_report.json \
       .signum/audit_summary.json .signum/proofpack.json \
       .signum/reviews/claude.json .signum/reviews/codex.json .signum/reviews/gemini.json \
       .signum/review_prompt_codex.txt .signum/review_prompt_gemini.txt \
       .signum/reviews/codex_raw.txt .signum/reviews/gemini_raw.txt
```

---

## Phase 1: CONTRACT

**Goal:** Transform the user's request into a verifiable contract.

### Step 1.1: Launch Contractor

Use the Agent tool to launch the "contractor" agent with this prompt:

```
FEATURE_REQUEST: <the user's task from $ARGUMENTS>
PROJECT_ROOT: <output of pwd>

Scan the codebase, assess risk, and write .signum/contract.json.
```

### Step 1.2: Validate contract

Use the Bash tool to verify the contract was written and has required fields:

```bash
test -f .signum/contract.json || { echo "ERROR: contract.json not found"; exit 1; }
jq -e '.schemaVersion and .goal and .inScope and .acceptanceCriteria and .riskLevel' \
  .signum/contract.json > /dev/null && echo "VALID" || echo "INVALID"
```

If the file is missing or INVALID, stop and report: "Contractor agent failed to produce a valid contract.json. Check agent output for errors."

### Step 1.3: Check for open questions

Use the Bash tool:

```bash
jq -r 'if (.openQuestions | length) > 0 then "BLOCKED: " + (.openQuestions | join("\n  - ")) else "OK" end' \
  .signum/contract.json
```

If output starts with `BLOCKED:`, display the open questions to the user exactly as listed, then **STOP**.

Do not proceed to Phase 2 until the user provides answers to every open question. When answers are received, re-launch the contractor agent with the original request plus the answers appended, and repeat Step 1.2–1.3.

### Step 1.4: Display contract summary

Use the Bash tool to extract and display:

```bash
jq -r '"Goal: " + .goal,
       "Risk: " + .riskLevel,
       "In scope: " + (.inScope | join(", ")),
       "Acceptance criteria: " + (.acceptanceCriteria | length | tostring) + " defined"' \
  .signum/contract.json
```

Also display any riskSignals if riskLevel is "high":

```bash
jq -r 'if .riskLevel == "high" then "Risk signals: " + (.riskSignals // [] | join(", ")) else empty end' \
  .signum/contract.json
```

**Ask the user to confirm before proceeding to Phase 2.** Display: "Contract ready. Proceed with implementation? (yes/no)"

Wait for confirmation. If the user says no, stop.

---

## Phase 2: EXECUTE

**Goal:** Implement code changes according to the contract.

### Step 2.1: Launch Engineer

Use the Agent tool to launch the "engineer" agent with this prompt:

```
Read .signum/contract.json and implement the required changes.
Establish baseline, implement, run the repair loop (max 3 attempts), save artifacts.
Write .signum/combined.patch and .signum/execute_log.json.
```

### Step 2.2: Check result

Use the Bash tool:

```bash
test -f .signum/execute_log.json || { echo "ERROR: execute_log.json not found"; exit 1; }
jq -r '.status' .signum/execute_log.json
```

If status is `FAILED`, display the failure details and **STOP**:

```bash
jq -r '"Attempt failures:",
       (.attempts[] | "  Attempt " + (.number | tostring) + ": " +
         (.checks | to_entries[] | select(.value.passed == false) |
           "  " + .key + " failed: " + (.value.error // "no error message")))' \
  .signum/execute_log.json 2>/dev/null || jq . .signum/execute_log.json
```

Report: "Engineer agent failed after all attempts. Fix the issues above and re-run /signum."

If status is `SUCCESS`, verify the patch exists:

```bash
test -f .signum/combined.patch && wc -l .signum/combined.patch || echo "WARNING: combined.patch missing"
```

### Step 2.3: Display execution summary

Use the Bash tool:

```bash
jq -r '"Attempts used: " + (.totalAttempts | tostring) + "/" + (.maxAttempts | tostring),
       "Acceptance criteria passed: " +
         ([.attempts[-1].checks | to_entries[] | select(.value.passed == true)] | length | tostring)' \
  .signum/execute_log.json
```

---

## Phase 3: AUDIT

**Goal:** Verify the change from multiple independent angles.

### Step 3.1: Mechanic (bash, zero LLM)

Run full project checks. Use the Bash tool:

```bash
# Lint
if [ -f "pyproject.toml" ] && grep -q "ruff" pyproject.toml 2>/dev/null; then
  LINT_OUT=$(ruff check . 2>&1); LINT_EXIT=$?
elif [ -f "package.json" ] && grep -q "eslint" package.json 2>/dev/null; then
  LINT_OUT=$(npx eslint . 2>&1); LINT_EXIT=$?
else
  LINT_OUT="no linter found, skipped"; LINT_EXIT=0
fi

# Typecheck
if [ -f "pyproject.toml" ] && grep -q "mypy" pyproject.toml 2>/dev/null; then
  TYPE_OUT=$(mypy . 2>&1); TYPE_EXIT=$?
elif [ -f "tsconfig.json" ]; then
  TYPE_OUT=$(npx tsc --noEmit 2>&1); TYPE_EXIT=$?
else
  TYPE_OUT="no typecheck found, skipped"; TYPE_EXIT=0
fi

# Tests
if [ -f "pyproject.toml" ] && grep -q "pytest" pyproject.toml 2>/dev/null; then
  TEST_OUT=$(pytest 2>&1); TEST_EXIT=$?
elif [ -f "package.json" ] && grep -q '"test"' package.json 2>/dev/null; then
  TEST_OUT=$(npm test 2>&1); TEST_EXIT=$?
elif [ -f "Cargo.toml" ]; then
  TEST_OUT=$(cargo test 2>&1); TEST_EXIT=$?
else
  TEST_OUT="no test runner found, skipped"; TEST_EXIT=0
fi

# Write mechanic report
jq -n \
  --arg lint_status "$([ $LINT_EXIT -eq 0 ] && echo pass || echo fail)" \
  --argjson lint_exit "$LINT_EXIT" \
  --arg type_status "$([ $TYPE_EXIT -eq 0 ] && echo pass || echo fail)" \
  --argjson type_exit "$TYPE_EXIT" \
  --arg test_status "$([ $TEST_EXIT -eq 0 ] && echo pass || echo fail)" \
  --argjson test_exit "$TEST_EXIT" \
  '{
    lint:      { status: $lint_status,  exitCode: $lint_exit },
    typecheck: { status: $type_status,  exitCode: $type_exit },
    tests:     { status: $test_status,  exitCode: $test_exit },
    baselineComparison: "checked"
  }' > .signum/mechanic_report.json

echo "Mechanic done. Lint=$LINT_EXIT Typecheck=$TYPE_EXIT Tests=$TEST_EXIT"
```

If any check fails, continue to reviews — mechanic failure influences the final decision but does not block the audit.

### Step 3.2: Reviewer-Claude (agent)

Use the Agent tool to launch the "reviewer-claude" agent with this prompt:

```
Read .signum/contract.json, .signum/combined.patch, and .signum/mechanic_report.json.
Follow lib/prompts/review-template.md and write your review to .signum/reviews/claude.json.
Write ONLY the JSON object, no markers, no markdown.
```

After it finishes, verify the output exists:

```bash
test -f .signum/reviews/claude.json && jq -e '.verdict' .signum/reviews/claude.json > /dev/null \
  && echo "claude review OK" || echo "WARNING: claude.json missing or invalid"
```

### Step 3.3: Reviewer-Codex (CLI)

Use the Bash tool to check availability:

```bash
which codex > /dev/null 2>&1 && echo "AVAILABLE" || echo "UNAVAILABLE"
```

**If AVAILABLE:**

Build the review prompt by substituting template variables:

```bash
CONTRACT=$(cat .signum/contract.json)
DIFF=$(cat .signum/combined.patch)
MECHANIC=$(cat .signum/mechanic_report.json)

sed -e "s|{contract_json}|$CONTRACT|g" \
    -e "s|{diff}|$DIFF|g" \
    -e "s|{mechanic_report}|$MECHANIC|g" \
    lib/prompts/review-template.md > .signum/review_prompt_codex.txt
```

Run codex and attempt 3-level parsing:

```bash
# Run codex
timeout 180 codex -q "$(cat .signum/review_prompt_codex.txt)" > .signum/reviews/codex_raw.txt 2>&1
CODEX_EXIT=$?

# Level 1: valid JSON directly
if jq -e '.verdict' .signum/reviews/codex_raw.txt > /dev/null 2>&1; then
  cp .signum/reviews/codex_raw.txt .signum/reviews/codex.json
  echo "codex: parsed as direct JSON"

# Level 2: extract between markers
elif grep -q '###SIGNUM_REVIEW_START###' .signum/reviews/codex_raw.txt; then
  sed -n '/###SIGNUM_REVIEW_START###/,/###SIGNUM_REVIEW_END###/p' .signum/reviews/codex_raw.txt \
    | grep -v '###SIGNUM_REVIEW' > /tmp/codex_extracted.json
  if jq -e '.verdict' /tmp/codex_extracted.json > /dev/null 2>&1; then
    cp /tmp/codex_extracted.json .signum/reviews/codex.json
    echo "codex: parsed via markers"
  else
    RAW=$(cat .signum/reviews/codex_raw.txt | head -c 2000)
    jq -n --arg raw "$RAW" \
      '{"verdict":"CONDITIONAL","findings":[],"summary":"Could not parse codex output","parseOk":false,"raw":$raw}' \
      > .signum/reviews/codex.json
    echo "codex: marker extraction failed, saved raw"
  fi

# Level 3: save raw, mark unparseable
else
  RAW=$(cat .signum/reviews/codex_raw.txt | head -c 2000)
  jq -n --arg raw "$RAW" \
    '{"verdict":"CONDITIONAL","findings":[],"summary":"Could not parse codex output","parseOk":false,"raw":$raw}' \
    > .signum/reviews/codex.json
  echo "codex: no markers found, saved raw"
fi
```

**If UNAVAILABLE:**

```bash
echo '{"verdict":"UNAVAILABLE","findings":[],"summary":"Codex CLI not installed","available":false}' \
  > .signum/reviews/codex.json
```

### Step 3.4: Reviewer-Gemini (CLI)

Use the Bash tool to check availability:

```bash
which gemini > /dev/null 2>&1 && echo "AVAILABLE" || echo "UNAVAILABLE"
```

**If AVAILABLE:**

Build the prompt (same template, separate file):

```bash
CONTRACT=$(cat .signum/contract.json)
DIFF=$(cat .signum/combined.patch)
MECHANIC=$(cat .signum/mechanic_report.json)

sed -e "s|{contract_json}|$CONTRACT|g" \
    -e "s|{diff}|$DIFF|g" \
    -e "s|{mechanic_report}|$MECHANIC|g" \
    lib/prompts/review-template.md > .signum/review_prompt_gemini.txt
```

Run gemini with same 3-level parsing:

```bash
timeout 180 gemini -p "$(cat .signum/review_prompt_gemini.txt)" > .signum/reviews/gemini_raw.txt 2>&1
GEMINI_EXIT=$?

if jq -e '.verdict' .signum/reviews/gemini_raw.txt > /dev/null 2>&1; then
  cp .signum/reviews/gemini_raw.txt .signum/reviews/gemini.json
  echo "gemini: parsed as direct JSON"

elif grep -q '###SIGNUM_REVIEW_START###' .signum/reviews/gemini_raw.txt; then
  sed -n '/###SIGNUM_REVIEW_START###/,/###SIGNUM_REVIEW_END###/p' .signum/reviews/gemini_raw.txt \
    | grep -v '###SIGNUM_REVIEW' > /tmp/gemini_extracted.json
  if jq -e '.verdict' /tmp/gemini_extracted.json > /dev/null 2>&1; then
    cp /tmp/gemini_extracted.json .signum/reviews/gemini.json
    echo "gemini: parsed via markers"
  else
    RAW=$(cat .signum/reviews/gemini_raw.txt | head -c 2000)
    jq -n --arg raw "$RAW" \
      '{"verdict":"CONDITIONAL","findings":[],"summary":"Could not parse gemini output","parseOk":false,"raw":$raw}' \
      > .signum/reviews/gemini.json
    echo "gemini: marker extraction failed, saved raw"
  fi

else
  RAW=$(cat .signum/reviews/gemini_raw.txt | head -c 2000)
  jq -n --arg raw "$RAW" \
    '{"verdict":"CONDITIONAL","findings":[],"summary":"Could not parse gemini output","parseOk":false,"raw":$raw}' \
    > .signum/reviews/gemini.json
  echo "gemini: no markers found, saved raw"
fi
```

**If UNAVAILABLE:**

```bash
echo '{"verdict":"UNAVAILABLE","findings":[],"summary":"Gemini CLI not installed","available":false}' \
  > .signum/reviews/gemini.json
```

### Step 3.5: Synthesizer (agent)

Use the Agent tool to launch the "synthesizer" agent with this prompt:

```
Read .signum/mechanic_report.json, .signum/reviews/claude.json,
.signum/reviews/codex.json, and .signum/reviews/gemini.json.
Apply deterministic synthesis rules and write .signum/audit_summary.json.
```

After it finishes, read and display the audit summary:

```bash
test -f .signum/audit_summary.json || { echo "ERROR: audit_summary.json not found"; exit 1; }

jq -r '"=== AUDIT SUMMARY ===",
       "Mechanic: " + .mechanic,
       "Claude verdict: " + .reviews.claude.verdict,
       "Codex verdict:  " + .reviews.codex.verdict,
       "Gemini verdict: " + .reviews.gemini.verdict,
       "Available reviews: " + (.availableReviews | tostring) + "/3",
       "Consensus: " + .consensus,
       "DECISION: " + .decision,
       "Reasoning: " + .reasoning' \
  .signum/audit_summary.json
```

---

## Phase 4: PACK

**Goal:** Bundle all artifacts into a verifiable proof package.

### Step 4.1: Compute checksums

Use the Bash tool:

```bash
sha256sum .signum/contract.json .signum/combined.patch \
          .signum/mechanic_report.json .signum/audit_summary.json 2>/dev/null \
  | awk '{print $2, $1}' | sort
```

### Step 4.2: Assemble proofpack.json

Use the Bash tool to build the full proofpack:

```bash
DECISION=$(jq -r '.decision' .signum/audit_summary.json)
GOAL=$(jq -r '.goal' .signum/contract.json)
RISK=$(jq -r '.riskLevel' .signum/contract.json)
ATTEMPTS=$(jq -r '.totalAttempts' .signum/execute_log.json 2>/dev/null || echo "unknown")
MECHANIC=$(jq -r '.mechanic' .signum/audit_summary.json)
RUN_DATE=$(date +%Y-%m-%d)
RUN_RANDOM=$(LC_ALL=C tr -dc 'a-z0-9' < /dev/urandom | head -c 6)
RUN_ID="signum-${RUN_DATE}-${RUN_RANDOM}"

# Compute individual checksums
sum_contract=$(sha256sum .signum/contract.json 2>/dev/null | awk '{print "sha256:" $1}' || echo "sha256:missing")
sum_patch=$(sha256sum .signum/combined.patch 2>/dev/null | awk '{print "sha256:" $1}' || echo "sha256:missing")
sum_mechanic=$(sha256sum .signum/mechanic_report.json 2>/dev/null | awk '{print "sha256:" $1}' || echo "sha256:missing")
sum_audit=$(sha256sum .signum/audit_summary.json 2>/dev/null | awk '{print "sha256:" $1}' || echo "sha256:missing")

jq -n \
  --arg schema "2.0" \
  --arg runId "$RUN_ID" \
  --arg decision "$DECISION" \
  --arg summary "Goal: $GOAL | Risk: $RISK | Attempts: $ATTEMPTS | Mechanic: $MECHANIC | Decision: $DECISION" \
  --arg sum_contract "$sum_contract" \
  --arg sum_patch "$sum_patch" \
  --arg sum_mechanic "$sum_mechanic" \
  --arg sum_audit "$sum_audit" \
  '{
    schemaVersion: $schema,
    runId: $runId,
    decision: $decision,
    contract: "contract.json",
    diff: "combined.patch",
    checks: {
      mechanic: "mechanic_report.json",
      reviews: {
        claude: "reviews/claude.json",
        codex:  "reviews/codex.json",
        gemini: "reviews/gemini.json"
      },
      audit_summary: "audit_summary.json"
    },
    checksums: {
      "contract.json":        $sum_contract,
      "combined.patch":       $sum_patch,
      "mechanic_report.json": $sum_mechanic,
      "audit_summary.json":   $sum_audit
    },
    summary: $summary,
    executeLog: "execute_log.json"
  }' > .signum/proofpack.json

echo "Proofpack written: $RUN_ID"
```

---

## Final Output

Display to the user:

Use the Bash tool to list all produced artifacts:

```bash
echo "=== Artifacts in .signum/ ==="
ls -1 .signum/ .signum/reviews/ 2>/dev/null
echo ""
echo "Decision: $(jq -r .decision .signum/proofpack.json)"
echo "Run ID:   $(jq -r .runId   .signum/proofpack.json)"
```

Then display the appropriate next steps based on the decision:

- **AUTO_OK**: "Changes are verified. Review `.signum/combined.patch` and commit when ready."
- **AUTO_BLOCK**: "Issues found. Review `.signum/audit_summary.json` and fix before committing."
- **HUMAN_REVIEW**: "Manual review recommended. Check `.signum/audit_summary.json` for details."

---

## Error Handling

- If any phase fails catastrophically (agent error, required file missing after agent run), **STOP** immediately and report: what phase failed, what file is missing, and what the user should do next.
- Mechanic check failures continue to audit — they influence the decision but do not block Phase 3.
- If codex or gemini times out (`exit 124`) or returns a non-zero exit code, mark as unavailable and continue.
- **Never silently swallow errors.** All bash exit codes must be checked. If jq fails to parse a file, report it explicitly.
- If the synthesizer produces an invalid audit_summary.json, stop Phase 4 and report the problem.
