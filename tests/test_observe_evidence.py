from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CORE = Path("plugins/signum/skills/project-intent/scripts")
OBSERVER = ROOT / "tools/observe_evidence.py"
CRITERIA = "Run the checks and preserve the report."


class ObserveEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.call("init")
        proposed_basis = self.call("propose", data={
            "kind": "decision", "topic": "evidence-recovery",
            "summary": "Review a scoped repair proposal when an evidence attachment is damaged.",
            "source": "Fixture owner decision", "reason": "Preserve accepted evidence", "replaces": None,
        })
        decision = self.call("confirm", proposed_basis["id"])
        self.basis = {"topic": decision["topic"], "id": decision["id"]}
        self.proposal_flags = ("--propose", "--basis", decision["id"])
        self.changed = self.project / "work.txt"
        self.changed.write_bytes(b"completed work\n")
        self.support = self.project / "support.txt"
        self.support_bytes = b"original evidence\n"
        self.support.write_bytes(self.support_bytes)
        self.report = self.project / "check.json"
        self.journal = self.project / ".signum/state.json"
        self.delivery_journal = self.project / ".signum/delivery.json"
        self.manifest = {self.support.name: self.digest(self.support_bytes)}
        self.delivery = self.register("original", self.manifest)

    @staticmethod
    def digest(value):
        return hashlib.sha256(value).hexdigest()

    def call(self, *args, data=None):
        response = subprocess.run(
            [sys.executable, "-B", str(ROOT / CORE / "signum.py"), "--project", str(self.project), *args],
            input=json.dumps(data) if data is not None else None,
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(response.returncode, 0, response.stderr)
        return json.loads(response.stdout)

    def register(self, topic, manifest, accept=True, exit_code=0):
        report = {"checks": [{"name": "Regression checks", "exit_code": exit_code}]}
        if manifest is not None:
            report["supporting_files"] = manifest
        self.report.write_text(json.dumps(report), encoding="utf-8")
        proposal = self.call("propose", data={
            "topic": topic, "kind": "permission", "summary": "Change the sample file",
            "source": "Owner request", "reason": "Test the delivery", "replaces": None,
            "verification": CRITERIA,
            "owner_turn": "codex:turn:11111111-1111-4111-8111-111111111111",
        })
        permission = self.call("confirm", proposal["id"])
        self.call("start", permission["id"])
        manifest = self.call("snapshot", self.changed.name, "--artifact", self.report.name)
        version = self.call("evidence", permission["id"], data={
            **manifest, "criteria": CRITERIA, "result": "Regression check result",
            "status": "fail" if exit_code else "pass",
        })
        if accept:
            self.call("accept", version["id"], "--reason", "Reviewed", "--source", "Owner decision")
        return version["id"]

    def observe(self, *args, code=0, identifier=None, script=OBSERVER):
        response = subprocess.run(
            [sys.executable, "-B", str(script), "--project", str(self.project),
             "--delivery", self.delivery if identifier is None else identifier, *args],
            cwd=self.project, capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(response.returncode, code, response.stdout + response.stderr)
        return json.loads(response.stdout)

    def test_healthy_and_read_only_degraded_preserve_journals_and_card(self):
        before = self.journal.read_bytes(), self.delivery_journal.read_bytes()
        healthy = self.observe(script=ROOT / "tools/check_signum.py")
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(healthy["report"]["manifest"], "declared")
        self.support.unlink()
        degraded = self.observe(code=1)
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(degraded["service"], "available")
        self.assertEqual(degraded["card"], {
            "state": "accepted", "green": True, "owner_accepted": True, "acceptance_blockers": [],
        })
        [finding] = degraded["findings"]
        self.assertEqual(finding["reason"], "missing")
        self.assertEqual(finding["path"], self.support.name)
        self.assertEqual(finding["expected_sha256"], self.manifest[self.support.name])
        self.assertIsNone(finding["actual_sha256"])
        self.assertEqual(degraded["proposals"], [])
        self.assertEqual(before, (self.journal.read_bytes(), self.delivery_journal.read_bytes()))

    def test_proposal_requires_current_confirmed_decision_and_preserves_journals_on_refusal(self):
        self.support.unlink()
        replacement = self.call("propose", data={
            "kind": "decision", "topic": self.basis["topic"], "summary": "Pause repair planning.",
            "source": "Fixture owner decision", "reason": "Review the recovery process",
            "replaces": self.basis["id"],
        })
        self.call("confirm", replacement["id"])
        before = self.journal.read_bytes(), self.delivery_journal.read_bytes()
        [permission] = self.call("status", "--topic", "original")["confirmed"]
        for basis in ("0" * 24, self.basis["id"], permission["id"]):
            rejected = self.observe("--propose", "--basis", basis, code=2)
            self.assertEqual(rejected["proposals"], [])
            self.assertIn("confirmed decision", rejected["error"])
        for script in (OBSERVER, ROOT / "tools/check_signum.py"):
            result = subprocess.run(
                [sys.executable, "-B", str(script), "--project", str(self.project),
                 "--delivery", self.delivery, "--propose"],
                capture_output=True, encoding="utf-8", timeout=10,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("must be used together", result.stderr)
        self.assertEqual(before, (self.journal.read_bytes(), self.delivery_journal.read_bytes()))

    def test_observer_uses_the_marketplace_package_outside_its_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory)
            observer = bundle / "tools/observe_evidence.py"
            observer.parent.mkdir()
            shutil.copy2(OBSERVER, observer)
            package = "plugins/selected-signum"
            shutil.copytree(ROOT / CORE, bundle / package / "skills/project-intent/scripts")
            marketplace = bundle / ".agents/plugins/marketplace.json"
            marketplace.parent.mkdir(parents=True)
            marketplace.write_text(json.dumps({"plugins": [
                {"name": "signum", "source": {"source": "local", "path": "./" + package}},
            ]}), encoding="utf-8")
            result = self.observe(script=observer)
            self.assertEqual(result["status"], "healthy")

    def test_wrong_bytes_queue_once_across_concurrent_calls_and_preserve_owner_withdrawal(self):
        self.support.write_bytes(b"wrong bytes")
        before = self.delivery_journal.read_bytes()
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests = [executor.submit(self.observe, *self.proposal_flags, code=1)
                        for _ in range(2)]
            results = [request.result() for request in requests]
        findings = [item["findings"][0] for item in results]
        proposals = [item["proposals"][0] for item in results]
        self.assertEqual(findings[0], findings[1])
        self.assertEqual(findings[0]["reason"], "hash_mismatch")
        self.assertEqual(findings[0]["actual_sha256"], self.digest(b"wrong bytes"))
        self.assertEqual(sum(proposal["created"] for proposal in proposals), 1)
        self.assertEqual(proposals[0]["id"], proposals[1]["id"])
        created = proposals[0]
        [pending] = self.call("status", "--topic", created["topic"])["proposals"]
        self.assertEqual(pending["state"], "pending")
        self.assertEqual(created["scope"], pending["scope"])
        self.assertEqual(pending["basis"], self.basis)
        self.assertEqual(created["verification"], pending["verification"])
        self.assertIn(findings[0]["fingerprint"], pending["source"])
        self.assertIn(self.manifest[self.support.name], pending["verification"])
        self.assertEqual(self.support.read_bytes(), b"wrong bytes")
        self.support.write_bytes(b"different wrong bytes")
        [repeated] = self.observe(*self.proposal_flags, code=1)["proposals"]
        self.assertEqual(repeated["id"], created["id"])
        self.assertFalse(repeated["created"])
        self.call("withdraw", created["id"], "--reason", "Owner has a different plan")
        after_withdrawal = self.journal.read_bytes()
        [withdrawn] = self.observe(*self.proposal_flags, code=1)["proposals"]
        self.assertEqual(withdrawn["state"], "withdrawn")
        self.assertEqual(withdrawn["id"], created["id"])
        self.assertEqual(self.journal.read_bytes(), after_withdrawal)
        self.assertEqual(self.delivery_journal.read_bytes(), before)

    def test_recovered_file_reports_cleared_pending_proposal_without_deciding(self):
        self.support.unlink()
        [proposal] = self.observe(*self.proposal_flags, code=1)["proposals"]
        before = self.journal.read_bytes(), self.delivery_journal.read_bytes()
        self.support.write_bytes(b"wrong bytes")
        still_degraded = self.observe(code=1)
        self.assertEqual(still_degraded["findings"][0]["reason"], "hash_mismatch")
        self.assertEqual(still_degraded["cleared_pending_proposals"], [])
        self.support.write_bytes(self.support_bytes)
        recovered = self.observe(*self.proposal_flags)
        self.assertEqual(recovered["findings"], [])
        self.assertEqual(recovered["proposals"], [])
        self.assertEqual(recovered["cleared_pending_proposals"], [{
            "id": proposal["id"], "state": "pending", "fingerprint": proposal["fingerprint"],
        }])
        self.assertEqual(before, (self.journal.read_bytes(), self.delivery_journal.read_bytes()))

    def test_report_missing_unreadable_or_replaced_prevents_proposal(self):
        before = self.journal.read_bytes()
        self.support.unlink()
        self.report.unlink()
        missing = self.observe(*self.proposal_flags, code=2)
        self.assertEqual(missing["report"]["status"], "missing")
        self.assertEqual(missing["service"], "available")
        self.assertTrue(missing["card"]["owner_accepted"])
        self.assertFalse(missing["card"]["green"])
        self.assertEqual(missing["proposals"], [])
        self.report.symlink_to(self.changed)
        unreadable = self.observe(*self.proposal_flags, code=2)
        self.assertEqual(unreadable["report"]["status"], "unreadable")
        self.assertEqual(unreadable["proposals"], [])
        self.report.unlink()
        self.report.write_text('{"checks":[{"name":"Changed check","exit_code":0}]}', encoding="utf-8")
        replaced = self.observe(*self.proposal_flags, code=2)
        self.assertEqual(replaced["report"]["status"], "hash_mismatch")
        self.assertEqual(replaced["proposals"], [])
        self.assertEqual(before, self.journal.read_bytes())

    def test_invalid_identifier_unavailable_core_and_corrupt_journal_are_visible(self):
        invalid = self.observe(*self.proposal_flags, code=2, identifier=self.delivery[:-1])
        self.assertIn("full ID", invalid["error"])
        self.assertEqual(invalid["service"], "unavailable")
        shutil.copytree(ROOT / CORE, self.project / CORE)
        native = self.project / CORE / "signum.py"
        original = native.read_bytes()
        native.write_bytes(original + b"\n")
        incompatible = self.observe(*self.proposal_flags, code=2)
        self.assertIn("target core differs", incompatible["error"])
        self.assertEqual(incompatible["service"], "unavailable")
        native.unlink()
        absent = self.observe(*self.proposal_flags, code=2)
        self.assertIn("signum.py", absent["error"])
        self.assertEqual(absent["service"], "unavailable")
        native.write_bytes(original)
        guard = self.project / CORE / "scope_guard.py"
        original_guard = guard.read_bytes()
        guard.write_bytes(original_guard + b"\n")
        incompatible_guard = self.observe(*self.proposal_flags, code=2)
        self.assertIn("target core differs", incompatible_guard["error"])
        self.assertIn("scope_guard.py", incompatible_guard["error"])
        guard.write_bytes(original_guard)
        self.delivery_journal.write_text("invalid", encoding="utf-8")
        broken = self.observe(*self.proposal_flags, code=2)
        self.assertEqual(broken["status"], "unknown")
        self.assertEqual(broken["service"], "unavailable")
        self.assertIsNone(broken["card"])
        self.assertEqual(broken["proposals"], [])

    def test_absent_empty_and_malformed_manifests_are_distinct(self):
        for topic, manifest, status, code in (
            ("undeclared", None, "not_declared", 0),
            ("empty", {}, "empty", 0),
            ("invalid", {self.support.name: "not a SHA-256"}, "invalid", 2),
        ):
            with self.subTest(topic=topic):
                identifier = self.register(topic, manifest)
                result = self.observe(*self.proposal_flags, identifier=identifier, code=code)
                self.assertEqual(result["report"]["manifest"], status)
                self.assertEqual(result["proposals"], [])

    def test_native_blockers_degrade_and_prevent_proposal(self):
        self.changed.write_bytes(b"work changed after acceptance")
        self.support.unlink()
        result = self.observe(*self.proposal_flags, code=1)
        self.assertTrue(result["card"]["owner_accepted"])
        self.assertFalse(result["card"]["green"])
        self.assertIn("Changed or unavailable project file: work.txt.", result["card"]["acceptance_blockers"])
        self.assertEqual(result["proposals"], [])

    def test_valid_journal_prefix_without_owner_acceptance_is_unknown(self):
        document = json.loads(self.delivery_journal.read_bytes())
        self.assertEqual(document["events"].pop()["event"], "accepted")
        self.delivery_journal.write_text(json.dumps(document), encoding="utf-8")
        self.support.unlink()
        before = self.journal.read_bytes()
        result = self.observe(*self.proposal_flags, code=2)
        self.assertEqual(result["card"], {
            "state": "review", "green": True, "owner_accepted": False, "acceptance_blockers": [],
        })
        self.assertIn("does not record owner acceptance", result["error"])
        self.assertEqual(result["proposals"], [])
        self.assertEqual(self.journal.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
