import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"
SOURCE = "wording.txt"
REPORT = "check.json"
CRITERIA = "Compare the wording with the expected revision."
OWNER_TURN = "codex:turn:11111111-1111-4111-8111-111111111111"


class VerificationBindingTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.source = self.project / SOURCE
        self.report = self.project / REPORT
        self.source.write_text("original", encoding="utf-8")
        self.call("init")

    def cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), "--project", str(self.project), *args],
            input=json.dumps(data) if data is not None else None,
            capture_output=True, text=True, timeout=15,
        )

    def call(self, *args, data=None):
        outcome = self.cli(*args, data=data)
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        return json.loads(outcome.stdout)

    def permission(self, *, legacy=False):
        if legacy:
            proposed = self.call("propose", data={
                "kind": "permission", "topic": "wording", "replaces": None,
                "owner_turn": OWNER_TURN, "source": "Revise the wording.",
                "summary": "The wording matches the new requirement.", "reason": "Update the example.",
                "scope": "Exact file scope v1\n" + json.dumps({"files": [SOURCE], "report": REPORT}),
                "verification": CRITERIA,
            })
        else:
            proposed = self.call("draft", "--topic", "wording", data={
                "owner_turn": OWNER_TURN, "request": "Revise the wording.",
                "outcome": "The wording matches the new requirement.", "rationale": "Update the example.",
                "excludes": "Other files.", "files": [SOURCE], "report": REPORT, "verification": CRITERIA,
            })
        return self.call("confirm", proposed["id"])["id"]

    def verify(self, program, *args, report=REPORT):
        return self.cli("verify", "--report", report, "--name", "Compare wording",
                        "--", sys.executable, "-I", "-c", program, *args)

    def check_wording(self, expected):
        return self.verify(
            "from pathlib import Path; import sys; assert Path(sys.argv[1]).read_text() == sys.argv[2]",
            SOURCE, expected,
        )

    def evidence(self):
        return {
            **self.call("snapshot", SOURCE, "--artifact", REPORT),
            "criteria": CRITERIA, "result": "The comparison passed.", "status": "pass",
        }

    def test_pass_rejects_a_report_for_an_earlier_revision(self):
        permission = self.permission()
        start = self.call("start", permission, "--touch", SOURCE)
        self.source.write_text("first revision", encoding="utf-8")
        outcome = self.check_wording("first revision")
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        self.source.write_text("second revision", encoding="utf-8")

        stale = self.cli("evidence", permission, data=self.evidence())
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("does not match this start and the changed file snapshot", stale.stderr)
        self.assertEqual(self.call("delivery")["deliveries"], [])

        self.report.unlink()
        outcome = self.check_wording("second revision")
        self.assertEqual(outcome.returncode, 0, outcome.stderr)
        evidence = self.evidence()
        report = json.loads(self.report.read_text(encoding="utf-8"))
        self.assertEqual(report["verification"], {"start": start["id"], "files": evidence["files"]})
        card = self.call("evidence", permission, data=evidence)
        self.assertEqual(card["acceptance_blockers"], [])
        self.assertEqual(card["evidence"]["checked_at"], report["checked_at"])
        self.call("accept", card["id"], "--reason", "Wording confirmed.", "--source", "Owner review")
        self.assertEqual(self.call("delivery", card["id"])["state"], "accepted")

    def test_verify_requires_a_start_and_stable_files(self):
        permission = self.permission()
        outcome = self.check_wording("original")
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("Start work", outcome.stderr)
        self.assertFalse(self.report.exists())

        self.call("start", permission, "--touch", SOURCE)
        self.source.write_text("first revision", encoding="utf-8")
        outcome = self.verify("from pathlib import Path; import sys; Path(sys.argv[1]).write_text('formatted')", SOURCE)
        self.assertNotEqual(outcome.returncode, 0)
        self.assertIn("workspace changed during verification", outcome.stderr)
        self.assertFalse(self.report.exists())

    def test_legacy_exact_scope_retains_its_report_format(self):
        permission = self.permission(legacy=True)
        self.call("start", permission, "--touch", SOURCE)
        self.source.write_text("legacy revision", encoding="utf-8")
        self.report.write_text(json.dumps({"checks": [{"name": "Manual check", "exit_code": 0}]}), encoding="utf-8")
        card = self.call("evidence", permission, data=self.evidence())
        self.call("accept", card["id"], "--reason", "Wording confirmed.", "--source", "Owner review")
        self.assertEqual(self.call("delivery", card["id"])["state"], "accepted")

    def test_standalone_verify_rejects_root_administrative_reports_before_running(self):
        (self.project / ".git").mkdir()
        for directory in (".git", ".GIT", ".signum", ".Signum"):
            with self.subTest(directory=directory):
                path = f"{directory}/check.json"
                outcome = self.verify("print('check ran')", report=path)
                self.assertNotEqual(outcome.returncode, 0)
                self.assertIn("outside the project's .git and .signum", outcome.stderr)
                self.assertEqual(outcome.stdout, "")
                self.assertFalse((self.project / path).exists())

        old_report = ".git/old-check.json"
        (self.project / old_report).write_text('{"checks": [{"name": "Old check", "exit_code": 0}]}', encoding="utf-8")
        artifact = self.call("snapshot", "--artifact", old_report)["artifact"]
        self.assertEqual(artifact["path"], old_report)


if __name__ == "__main__":
    unittest.main()
