import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MARKET = json.loads((ROOT / ".agents/plugins/marketplace.json").read_bytes())
PACKAGE = next(entry["source"]["path"] for entry in MARKET["plugins"] if entry["name"] == "signum")
CANDIDATE = ROOT / PACKAGE / "skills/project-intent/scripts/signum.py"
SOURCE = "labels.py"
REPORT = "labels-check.json"
CHECK_NAME = "Label behavior and boundaries"
CRITERIA = "Run check_labels.py: remove repeats, preserve first occurrence and case, and handle empty input."
OWNER_TURN = "codex:turn:11111111-1111-4111-8111-111111111111"
DRAFT = {
    "request": "The review list repeats labels. Keep each label once, in the order I entered it.",
    "outcome": "Print the first occurrence of each label in the input order.",
    "rationale": "Reviewers read labels in the order they were collected.",
    "excludes": "Keep letter case significant and do not sort the labels.",
    "files": [SOURCE],
    "report": REPORT,
    "verification": CRITERIA,
}
IMPLEMENTATION = "import sys\n\nfor label in dict.fromkeys(sys.argv[1:]):\n    print(label)\n"
CHECK = '''import subprocess
import sys

cases = [
    (["z", "a", "z", "Z", "a"], "z\\na\\nZ\\n"),
    (["only"], "only\\n"),
    ([], ""),
]
for labels, expected in cases:
    actual = subprocess.check_output([sys.executable, "-B", "labels.py", *labels], text=True)
    if actual != expected:
        raise SystemExit(f"{labels!r}: expected {expected!r}, got {actual!r}")
'''


class IntentCycleTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.delivery_journal = self.project / ".signum/delivery.json"
        self.call("init")
        self.check = [sys.executable, "-B", "check_labels.py"]
        self.write(SOURCE, "import sys\n\nfor label in sys.argv[1:]:\n    print(label)\n")
        self.write("check_labels.py", CHECK)
        self.write("notes.txt", "Print labels for the review list.\n")
        self.git("init", "--quiet", "--template=")
        self.git("add", SOURCE, "check_labels.py", "notes.txt")
        self.git("-c", "user.name=Test Fixture", "-c", "user.email=fixture@example.invalid",
                 "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Record fixture baseline")

    def write(self, path, contents):
        (self.project / path).write_text(contents, encoding="utf-8")

    def execute(self, argv, data=None):
        return subprocess.run(
            argv,
            cwd=self.project,
            env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
            input=None if data is None else json.dumps(data),
            capture_output=True,
            encoding="utf-8",
            timeout=15,
        )

    def git(self, *args):
        result = self.execute(["git", *args])
        self.assertEqual(result.returncode, 0, result.stderr)

    def cli(self, *args, data=None, script=CANDIDATE):
        return self.execute([sys.executable, "-B", str(script), "--project", str(self.project), *args], data)

    def call(self, *args, data=None):
        result = self.cli(*args, data=data)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def unfinished_stages(self):
        result = self.cli(script=CANDIDATE.with_name("orient.py"))
        self.assertEqual(result.returncode, 0, result.stderr)
        return [item["stage"] for item in json.loads(result.stdout)["unfinished_work"]]

    def blocked(self, *args, message=None, data=None):
        journal = self.project / ".signum/state.json"
        state_before = journal.read_bytes()
        before = self.delivery_journal.read_bytes() if self.delivery_journal.exists() else None
        result = self.cli(*args, data=data)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        if message is not None:
            self.assertIn(message, result.stderr)
        after = self.delivery_journal.read_bytes() if self.delivery_journal.exists() else None
        self.assertEqual(after, before)
        self.assertEqual(journal.read_bytes(), state_before)

    def test_permission_requires_specific_owner_turn_or_current_basis(self):
        self.blocked("draft", "--topic", "labels", data=DRAFT, message="current confirmed basis")
        self.blocked(
            "draft", "--topic", "labels",
            data={**DRAFT, "owner_turn": "codex://threads/11111111-1111-4111-8111-111111111111"},
            message="codex:turn:",
        )
        raw = {
            "topic": "labels", "kind": "permission", "summary": DRAFT["outcome"],
            "source": DRAFT["request"], "reason": DRAFT["rationale"], "replaces": None,
            "scope": "Exact file scope v1\n" + json.dumps({"files": [SOURCE], "report": REPORT}),
            "verification": CRITERIA,
        }
        self.blocked("propose", data=raw, message="current confirmed basis")
        self.blocked("propose", data={**raw, "scope": "Change labels only."}, message="current confirmed basis")
        self.blocked("propose", data={**raw, "owner_turn": OWNER_TURN, "source": " "}, message="source must")
        proposed = self.call("propose", data={**raw, "owner_turn": OWNER_TURN})
        self.assertEqual(proposed["source"], f"{OWNER_TURN}\n\n{raw['source']}")

    def test_revoked_and_superseded_bases_cannot_authorize_exact_work(self):
        decision = {
            "topic": "label-order", "kind": "decision", "summary": DRAFT["outcome"],
            "source": DRAFT["request"], "reason": DRAFT["rationale"], "replaces": None,
        }
        revoked = self.call("propose", data=decision)["id"]
        self.call("withdraw", revoked, "--reason", "Still considering this option")
        terms = {**DRAFT, "basis": {"topic": "label-order", "id": revoked}}
        self.blocked("draft", "--topic", "labels", data=terms, message="confirmed decision")

        basis = self.call("propose", data=decision)["id"]
        self.call("confirm", basis)
        terms["basis"]["id"] = basis
        permission = self.call("draft", "--topic", "labels", data=terms)["id"]
        self.call("confirm", permission)
        self.call("start", permission, "--touch", SOURCE)
        pending = self.call("draft", "--topic", "other-labels", data=terms)["id"]

        replacement = self.call(
            "propose", data={**decision, "summary": "Sort labels instead.", "replaces": basis},
        )["id"]
        self.call("confirm", replacement)
        self.blocked("draft", "--topic", "new-labels", data=terms, message="Basis label-order changed")
        self.blocked("confirm", pending, message="Basis label-order changed")
        self.blocked("start", permission, "--touch", SOURCE, message="Basis label-order changed")

    def test_legacy_confirmed_exact_permission_remains_usable(self):
        record = {
            "topic": "legacy", "kind": "permission", "summary": DRAFT["outcome"],
            "source": DRAFT["request"], "reason": DRAFT["rationale"], "replaces": None,
            "scope": "Exact file scope v1\n" + json.dumps({"files": [SOURCE], "report": REPORT}),
            "verification": CRITERIA,
        }
        encoded = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        permission = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        state = self.project / ".signum/state.json"
        at = "2000-01-01T00:00:00+00:00"
        events = [{"event": "proposed", "id": permission, "at": at, "record": record}]
        state.write_text(json.dumps({"schema_version": 1, "events": events}), encoding="utf-8")
        self.blocked("confirm", permission, message="current confirmed basis")
        # Replay a journal that was already confirmed under the earlier rules.
        events.append({"event": "confirmed", "id": permission, "at": at})
        state.write_text(json.dumps({"schema_version": 1, "events": events}), encoding="utf-8")
        self.assertEqual(self.call("confirm", permission)["id"], permission)
        self.call("start", permission, "--touch", SOURCE)

    def test_natural_request_reaches_review_and_requires_unchanged_scope_for_acceptance(self):
        draft = self.call("draft", "--topic", "unique-labels", data={**DRAFT, "owner_turn": OWNER_TURN})
        permission = draft["id"]
        [pending] = self.call("status", "--topic", "unique-labels")["proposals"]
        self.assertEqual(pending["id"], permission)
        self.assertEqual(pending["state"], "pending")
        self.assertNotIn("owner_turn", pending)
        self.assertEqual(pending["source"], f"{OWNER_TURN}\n\n{DRAFT['request']}")
        self.assertEqual(pending["verification"], CRITERIA)
        scope_version, scope_json = pending["scope"].split("\n", 1)
        self.assertEqual(scope_version, "Exact file scope v2")
        self.assertEqual(json.loads(scope_json), {"files": [SOURCE], "report": REPORT})
        self.blocked("start", permission, "--touch", SOURCE)

        # SIMULATED owner confirms the displayed contract in this fixture.
        self.assertEqual(self.call("confirm", permission)["id"], permission)
        self.assertEqual(self.unfinished_stages(), ["not_started"])
        self.blocked("start", permission, "--touch", "notes.txt", message="notes.txt")
        start = self.call("start", permission, "--touch", SOURCE)
        self.assertEqual(self.unfinished_stages(), ["started"])

        original = self.execute(self.check)
        self.assertEqual(original.returncode, 1, original.stdout + original.stderr)
        self.assertIn("expected", original.stderr)
        self.write(SOURCE, IMPLEMENTATION)
        files = self.call("snapshot", SOURCE)
        result = self.cli("verify", "--report", REPORT, "--name", CHECK_NAME, "--", *self.check)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        artifact = self.call("snapshot", "--artifact", REPORT)
        report = json.loads((self.project / REPORT).read_bytes())
        [check] = report["checks"]
        self.assertEqual((check["name"], check["exit_code"]), (CHECK_NAME, result.returncode))
        self.assertGreaterEqual(check["elapsed_seconds"], 0)
        self.assertEqual(report["verification"], {"start": start["id"], **files})
        evidence = {
            **files,
            **artifact,
            "criteria": CRITERIA,
            "checked_at": report["checked_at"],
            "result": f"{CHECK_NAME} exited {result.returncode}.",
            "status": "pass",
        }

        # The executor supplies the same clean manifest while concealing an unrelated edit.
        notes = self.project / "notes.txt"
        original_notes = notes.read_bytes()
        notes.write_bytes(original_notes + b"Unapproved edit.\n")
        self.blocked("evidence", permission, data=evidence, message="outside the exact file scope")
        self.assertEqual(self.call("delivery")["deliveries"], [])
        notes.write_bytes(original_notes)

        version = self.call("evidence", permission, data=evidence)
        self.assertEqual(self.unfinished_stages(), ["awaiting_review"])
        self.assertEqual(version["state"], "review")
        self.assertIsNone(version["owner_decision"])
        self.assertEqual(version["acceptance_blockers"], [])
        self.assertEqual(version["evidence"]["files"], [{
            "path": SOURCE,
            "sha256": hashlib.sha256((self.project / SOURCE).read_bytes()).hexdigest(),
        }])
        self.assertEqual(version["evidence"]["artifact"]["text"].encode("utf-8"),
                         (self.project / REPORT).read_bytes())
        self.assertEqual(self.call("delivery", version["id"]), version)

        # SIMULATED owner accepts the displayed evidence version in this fixture.
        accept = ("accept", version["id"], "--reason", "The displayed example meets the contract.",
                  "--source", "SIMULATED owner acceptance in the end-to-end fixture")
        late_file = self.project / "unreviewed.txt"
        late_file.write_bytes(b"Added after review.\n")
        self.blocked(*accept, message="outside the exact file scope")
        late_file.unlink()
        self.write(SOURCE, IMPLEMENTATION.replace("dict.fromkeys(sys.argv[1:])", "sorted(set(sys.argv[1:]))"))
        self.assertEqual(self.execute(self.check).returncode, 1)
        self.blocked(*accept, message=SOURCE)
        self.assertEqual(self.call("delivery", version["id"])["state"], "review")
        self.write(SOURCE, IMPLEMENTATION)

        self.assertEqual(self.call(*accept)["event"], "accepted")
        self.assertEqual(self.unfinished_stages(), [])
        accepted = self.call("delivery", version["id"])
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["evidence"], version["evidence"])


if __name__ == "__main__":
    unittest.main()
