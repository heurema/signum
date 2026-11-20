from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"
CRITERIA = "Run the tests and compare the preserved result."


class DeliveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.state = self.project / ".signum" / "delivery.json"
        self.changed = self.project / "change.txt"
        self.changed.write_bytes(b"change v1\n")
        self.artifact = self.project / "check.json"
        self.report("Проверка: 3 passed")

    def report(self, details):
        document = {"checks": [{"name": "Unit tests", "exit_code": 0}], "details": details}
        self.artifact.write_bytes((json.dumps(document, ensure_ascii=False) + "\r\n").encode("utf-8"))

    def cli(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), "--project", str(self.project), *args],
            input=None if data is None else json.dumps(data, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )

    def call(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def blocked(self, message, *args, **kwargs):
        before = self.state.read_bytes() if self.state.exists() else None
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(message, result.stderr)
        self.assertEqual(before, self.state.read_bytes() if self.state.exists() else None)

    def propose(self, topic="change", kind="permission", **fields):
        if not self.state.parent.exists():
            self.call("init")
        if kind == "permission" and "basis" not in fields:
            fields["owner_turn"] = "codex:turn:11111111-1111-4111-8111-111111111111"
        return self.call("propose", data={
            "topic": topic,
            "kind": kind,
            "summary": "Update the change file",
            "source": "Owner note 1",
            "reason": "Agree on the work",
            "replaces": None,
            **fields,
        })["id"]

    def permission(self, **fields):
        identifier = self.propose(verification=CRITERIA, **fields)
        return self.call("confirm", identifier)["id"]

    def evidence(self, **fields):
        return {
            "files": [self.hash_entry(self.changed)],
            "artifact": self.hash_entry(self.artifact),
            "criteria": CRITERIA,
            "result": "The tests reported 3 passes.",
            "status": "pass",
            **fields,
        }

    def hash_entry(self, path):
        return {"path": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def decision(self, command, identifier, reason="The owner reviewed the displayed version."):
        return command, identifier, "--reason", reason, "--source", "Owner note 2"

    def test_delivers_version_with_exact_criteria_hashes_and_owner_decision(self):
        self.assertEqual(self.call("delivery"), {"starts": [], "deliveries": []})
        self.assertFalse(self.state.parent.exists())
        permission = self.permission()
        self.blocked("Start work", "evidence", permission, data=self.evidence())
        start = self.call("start", permission)
        self.assertEqual(self.call("start", permission), start)
        self.assertEqual(datetime.fromisoformat(start["started_at"]).tzinfo, timezone.utc)
        data = self.evidence(
            checked_at=datetime.now(timezone.utc).isoformat(),
            measurements={"source": "Execution timer", "elapsed_seconds": 0.5, "output_tokens": 10},
        )
        version = self.call("evidence", permission, data=data)
        self.assertEqual(self.call("evidence", permission, data=data), version)
        self.assertEqual(self.call("delivery", version["id"]), version)
        self.assertEqual(version["state"], "review")
        self.assertEqual(version["permission"]["verification"], CRITERIA)
        self.assertEqual(version["evidence"]["criteria"], CRITERIA)
        self.assertEqual(version["evidence"]["measurements"], data["measurements"])
        self.assertNotIn("input_tokens", version["evidence"]["measurements"])
        self.assertEqual(version["acceptance_blockers"], [])
        self.assertEqual(version["evidence"]["files"], [{
            "path": self.changed.name,
            "sha256": hashlib.sha256(self.changed.read_bytes()).hexdigest(),
        }])
        self.assertEqual(version["evidence"]["artifact"], {
            "path": self.artifact.name,
            "sha256": hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
            "text": self.artifact.read_bytes().decode("utf-8"),
        })
        self.blocked("full ID", *self.decision("accept", version["id"][:-1]))
        self.blocked("non-empty", *self.decision("accept", version["id"], " "))
        args = self.decision("accept", version["id"])
        decided = self.call(*args)
        self.assertEqual(self.call(*args), decided)
        self.assertEqual(datetime.fromisoformat(decided["at"]).tzinfo, timezone.utc)
        [accepted] = self.call("delivery")["deliveries"]
        self.assertEqual(accepted["evidence"], version["evidence"])
        self.assertEqual(accepted["state"], "accepted")
        self.assertEqual(accepted["owner_decision"], decided)
        self.blocked("already has an owner decision", *self.decision("return", version["id"]))

    def test_requires_a_current_permission_and_recorded_start_provenance(self):
        not_confirmed = self.propose(verification=CRITERIA)
        self.blocked("confirmed permission", "start", not_confirmed)
        missing = self.call("confirm", self.propose("missing"))["id"]
        self.blocked("no agreed verification criteria", "start", missing)
        permission = self.call("confirm", not_confirmed)["id"]
        instant = datetime.now(timezone.utc).isoformat()
        self.blocked("start source", "start", permission, "--started-at", instant)
        self.blocked(
            "between permission confirmation and now",
            "start", permission, "--started-at", "2000-01-01T00:00:00Z", "--source", "Timer log",
        )
        args = ("start", permission, "--started-at", instant, "--source", "Earlier timer log")
        start = self.call(*args)
        self.assertEqual(self.call(*args), start)
        self.assertEqual(start["started_at"], instant)
        self.assertEqual(start["source"], "Earlier timer log")
        self.assertGreaterEqual(start["at"], start["started_at"])
        no_criteria = self.evidence()
        del no_criteria["criteria"]
        self.blocked("Evidence requires", "evidence", permission, data=no_criteria)
        self.blocked("exact verification criteria", "evidence", permission, data=self.evidence(criteria="Looser"))
        self.blocked("Result must be", "evidence", permission, data=self.evidence(result=""))
        self.blocked("between the start and now", "evidence", permission, data=self.evidence(
            checked_at="2000-01-01T00:00:00Z",
        ))
        self.blocked("Measurement source", "evidence", permission, data=self.evidence(
            measurements={"input_tokens": 10},
        ))

    def test_failed_or_changed_result_is_returned_and_preserved_across_rework(self):
        permission = self.permission()
        start = self.call("start", permission)
        failed = self.call("evidence", permission, data=self.evidence(status="fail", result="One failure."))
        self.blocked("verification did not pass", *self.decision("accept", failed["id"]))
        decision = self.call(*self.decision("return", failed["id"], "Fix the failing test."))
        self.assertEqual(decision["event"], "returned")
        self.blocked("Start work", "evidence", permission, data=self.evidence())
        self.assertNotEqual(self.call("start", permission)["id"], start["id"])
        version = self.call("evidence", permission, data=self.evidence())
        self.artifact.write_text("A changed receipt", encoding="utf-8")
        self.blocked("Changed or unavailable check artifact", *self.decision("accept", version["id"]))
        self.changed.write_text("Changed work", encoding="utf-8")
        self.blocked("Changed or unavailable project file", *self.decision("accept", version["id"]))
        self.report("The second verification passed.")
        current = self.call("evidence", permission, data=self.evidence(result="Checked the revised work."))
        self.assertNotEqual(current["id"], version["id"])
        self.blocked("later delivery version", *self.decision("accept", version["id"]))
        accepted = self.call(*self.decision("accept", current["id"]))
        self.assertEqual(accepted["event"], "accepted")
        history = self.call("delivery")["deliveries"]
        self.assertEqual([item["state"] for item in history], ["returned", "superseded", "accepted"])
        self.assertEqual(history[0]["evidence"], failed["evidence"])
        self.assertEqual(history[1]["evidence"], version["evidence"])
        self.assertEqual(history[0]["owner_decision"], decision)

    def test_competing_permission_and_changed_basis_block_acceptance(self):
        basis = self.call("confirm", self.propose("basis", "decision"))["id"]
        permission = self.permission(basis={"topic": "basis", "id": basis})
        self.call("start", permission)
        version = self.call("evidence", permission, data=self.evidence())
        first = self.propose(replaces=permission, summary="Option 1")
        second = self.propose(replaces=permission, summary="Option 2")
        self.blocked("competing proposals", *self.decision("accept", version["id"]))
        for option in (first, second):
            self.call("withdraw", option, "--reason", "Owner dismissed the alternative.")
        revised = self.propose("basis", "decision", replaces=basis, summary="Changed basis")
        self.call("confirm", revised)
        self.blocked("Basis basis changed", *self.decision("accept", version["id"]))
        self.blocked("Basis basis changed", "evidence", permission, data=self.evidence())
        self.assertEqual(self.call(*self.decision("return", version["id"]))["event"], "returned")

    def test_records_absence_and_rejects_unsafe_paths_and_tampered_journal(self):
        permission = self.permission()
        self.call("start", permission)
        self.blocked("relative path", "evidence", permission, data=self.evidence(
            files=[{**self.hash_entry(self.changed), "path": "../outside"}],
        ))
        symlink = self.project / "link.txt"
        symlink.symlink_to(self.changed)
        self.blocked("link.txt", "evidence", permission, data=self.evidence(
            artifact={**self.hash_entry(self.changed), "path": symlink.name},
        ))
        manifest = self.call("snapshot", self.changed.name, "--deleted", "removed.txt")
        self.assertEqual(manifest, {"files": [
            self.hash_entry(self.changed),
            {"path": "removed.txt", "status": "deleted"},
        ]})
        version = self.call("evidence", permission, data=self.evidence(**manifest))
        (self.project / "removed.txt").write_text("Restored", encoding="utf-8")
        self.blocked("Changed or unavailable project file: removed.txt", *self.decision("accept", version["id"]))
        (self.project / "removed.txt").unlink()
        document = json.loads(self.state.read_text(encoding="utf-8"))
        document["events"][-1]["evidence"]["result"] = "Rewritten"
        self.state.write_text(json.dumps(document), encoding="utf-8")
        self.blocked("content does not match its ID", *self.decision("accept", version["id"]))

    def test_snapshot_pins_checked_file_and_receipt_before_registration(self):
        before_check = self.call("snapshot", self.changed.name)
        self.assertFalse(self.state.parent.exists())
        self.assertEqual(before_check, {"files": [self.hash_entry(self.changed)]})
        permission = self.permission()
        self.call("start", permission)
        receipt = self.call("snapshot", "--artifact", self.artifact.name)
        self.assertEqual(receipt, {"artifact": self.hash_entry(self.artifact)})
        data = self.evidence(**before_check, **receipt)
        old_bytes = self.changed.read_bytes()
        self.changed.write_bytes(b"Edited after the check.\n")
        self.blocked("Project file differs from the snapshot", "evidence", permission, data=data)
        self.changed.write_bytes(old_bytes)
        self.artifact.write_bytes(b"Edited after collecting the receipt.\n")
        self.blocked("Check artifact differs from the snapshot", "evidence", permission, data=data)
        self.blocked("snapshot entry", "evidence", permission, data=self.evidence(
            files=[self.changed.name],
        ))
        self.assertEqual(self.call("delivery")["deliveries"], [])

    def test_verify_records_actual_exit_7_and_refuses_to_register_it_as_pass(self):
        permission = self.permission()
        self.call("start", permission)
        self.artifact = self.project / "failed.json"
        result = self.cli(
            "verify", "--report", self.artifact.name, "--name", "Actual exit",
            "--", sys.executable, "-c", "print('output stays outside the report'); raise SystemExit(7)",
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        original = self.artifact.read_bytes()
        self.assertNotIn(b"output stays outside the report", original)
        report = json.loads(original)
        checked_at = datetime.fromisoformat(report["checked_at"])
        self.assertEqual(checked_at.tzinfo, timezone.utc)
        [check] = report["checks"]
        self.assertEqual(check["name"], "Actual exit")
        self.assertEqual(check["exit_code"], 7)
        self.assertGreaterEqual(check["elapsed_seconds"], 0)
        self.blocked("exit code 7", "evidence", permission, data=self.evidence())
        version = self.call("evidence", permission, data=self.evidence(status="fail", result="The check failed."))
        self.blocked("verification did not pass", *self.decision("accept", version["id"]))
        self.assertEqual(self.call(*self.decision("return", version["id"]))["event"], "returned")
        self.blocked(
            "report already exists", "verify", "--report", self.artifact.name, "--name", "Retry",
            "--", sys.executable, "-c", "from pathlib import Path; Path('ran').touch()",
        )
        self.assertEqual(self.artifact.read_bytes(), original)
        self.assertFalse((self.project / "ran").exists())

    def test_pass_requires_an_unambiguous_structured_report_with_only_zero_exits(self):
        permission = self.permission()
        self.call("start", permission)
        for text, error in (
            ("FAIL exit=7", "JSON check artifact"),
            ("{}", "non-empty checks"),
            ('{"checks":[]}', "non-empty checks"),
            ('{"checks":[{"name":" ","exit_code":0}]}', "non-empty name"),
            ('{"checks":[{"name":"test","exit_code":false}]}', "integer exit_code"),
            ('{"checks":[{"name":"test","exit_code":0.0}]}', "integer exit_code"),
            ('{"checks":[{"name":"test","exit_code":7,"exit_code":0}]}', "unambiguous fields"),
            ('{"checks":[{"name":"first","exit_code":0},{"name":"second","exit_code":3}]}', "exit code 3"),
        ):
            with self.subTest(text=text):
                self.artifact.write_text(text, encoding="utf-8")
                self.blocked(error, "evidence", permission, data=self.evidence())

        self.artifact = self.project / "passed.json"
        result = self.cli(
            "verify", "--report", self.artifact.name, "--name", "Passing verifiable check",
            "--", sys.executable, "-c", "raise SystemExit(0)",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        version = self.call("evidence", permission, data=self.evidence())
        self.assertEqual(self.call(*self.decision("accept", version["id"]))["event"], "accepted")

    def test_rechecks_structured_report_when_accessing_preexisting_evidence(self):
        permission = self.permission()
        self.call("start", permission)
        self.call("evidence", permission, data=self.evidence())
        self.artifact.write_text("FAIL exit=7", encoding="utf-8")
        document = json.loads(self.state.read_text(encoding="utf-8"))
        old_version = document["events"][-1]
        old_version["evidence"]["artifact"] = {**self.hash_entry(self.artifact), "text": "FAIL exit=7"}
        del old_version["id"]
        body = json.dumps(old_version, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        old_version["id"] = hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]
        self.state.write_text(json.dumps(document), encoding="utf-8")
        self.blocked("JSON check artifact", *self.decision("accept", old_version["id"]))
        self.assertEqual(self.call(*self.decision("return", old_version["id"]))["event"], "returned")

    def test_concurrent_retries_keep_one_start_one_version_and_one_owner_decision(self):
        permission = self.permission()
        with ThreadPoolExecutor(max_workers=2) as executor:
            starts = list(executor.map(lambda _: self.call("start", permission), range(2)))
            self.assertEqual(starts[0], starts[1])
            versions = list(executor.map(lambda _: self.call("evidence", permission, data=self.evidence()), range(2)))
            self.assertEqual(versions[0], versions[1])
            outcomes = list(executor.map(
                lambda command: self.cli(*self.decision(command, versions[0]["id"])),
                ("accept", "return"),
            ))
        self.assertEqual(sorted(result.returncode for result in outcomes), [0, 1])
        [version] = self.call("delivery")["deliveries"]
        self.assertIn(version["state"], ("accepted", "returned"))
        self.assertEqual(len(json.loads(self.state.read_text(encoding="utf-8"))["events"]), 3)


if __name__ == "__main__":
    unittest.main()
