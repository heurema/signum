from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.observe_evidence import proposal_record


ROOT = Path(__file__).resolve().parents[1]
OBSERVER_CORE = Path("plugins/signum/skills/project-intent/scripts")
OBSERVER = ROOT / "tools/observe_evidence.py"
RESUME = ROOT / "plugins/signum/skills/project-intent/scripts/resume_delivery.py"
CRITERIA = "Run the checks and preserve the report."


class ResumeDeliveryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.native = ROOT / OBSERVER_CORE / "signum.py"
        self.run_cli(self.native, "init")
        proposed_basis = self.run_cli(self.native, "propose", data={
            "kind": "decision", "topic": "evidence-recovery",
            "summary": "Review a scoped repair proposal when an evidence attachment is damaged.",
            "source": "Fixture owner decision", "reason": "Preserve accepted evidence", "replaces": None,
        })
        decision = self.run_cli(self.native, "confirm", proposed_basis["id"])
        self.basis = {"topic": decision["topic"], "id": decision["id"]}
        self.report = self.project / "check.json"

    def run_cli(self, script, *args, data=None, code=0):
        result = subprocess.run(
            [sys.executable, "-B", str(script), "--project", str(self.project), *args],
            input=json.dumps(data) if data is not None else None,
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def register_delivery(self, *names, include_supporting_in_snapshot=False):
        work = self.project / "work.txt"
        work.write_bytes(b"completed work\n")
        for name in names:
            (self.project / name).write_bytes(name.encode())
        supporting = {name: sha256((self.project / name).read_bytes()).hexdigest() for name in names}
        self.report.write_text(json.dumps({
            "checks": [{"name": "Regression checks", "exit_code": 0}],
            "supporting_files": supporting,
        }), encoding="utf-8")
        permission = self.run_cli(self.native, "propose", data={
            "topic": "delivery-test", "kind": "permission", "summary": "Change the sample file",
            "source": "Owner request", "reason": "Test a real accepted delivery", "replaces": None,
            "verification": CRITERIA,
            "basis": self.basis,
        })["id"]
        self.run_cli(self.native, "confirm", permission)
        self.run_cli(self.native, "start", permission)
        snapshot_paths = [work.name, *names] if include_supporting_in_snapshot else [work.name]
        manifest = self.run_cli(self.native, "snapshot", *snapshot_paths, "--artifact", self.report.name)
        version = self.run_cli(self.native, "evidence", permission, data={
            **manifest, "criteria": CRITERIA, "result": "All checks passed", "status": "pass",
        })["id"]
        self.run_cli(self.native, "accept", version, "--reason", "Reviewed", "--source", "Owner decision")
        return version

    def remove_and_queue(self, *names):
        version = self.register_delivery(*names)
        for name in names:
            (self.project / name).unlink()
        observation = self.run_cli(OBSERVER, "--delivery", version, "--propose", "--basis", self.basis["id"], code=1)
        proposals = {proposal["fingerprint"]: proposal for proposal in observation["proposals"]}
        return {
            finding["path"]: (proposals[finding["fingerprint"]]["id"], finding)
            for finding in observation["findings"]
        }

    def project_files(self):
        return {
            str(path.relative_to(self.project)): (
                (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else None
            )
            for path in self.project.rglob("*")
        }

    def resume(self, *args, status):
        before = self.project_files()
        result = self.run_cli(RESUME, *args, code=2 if status == "unknown" else 0)
        self.assertEqual(result["status"], status, result)
        self.assertEqual(self.project_files(), before)
        return result

    def repair_source(self, content):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        source = Path(temporary.name).resolve() / "candidate.txt"
        source.write_bytes(content)
        return source

    def pair_with_recorded_sources(self):
        names = ("first.txt", "second.txt")
        version = self.register_delivery(*names, include_supporting_in_snapshot=True)
        for name in names:
            (self.project / name).unlink()
        observation = self.run_cli(OBSERVER, "--delivery", version, code=1)
        sources = {}
        files = []
        for finding in observation["findings"]:
            name = finding["path"]
            source = self.repair_source(name.encode())
            sources[name] = source
            topic = "evidence-repair-" + finding["fingerprint"][:48]
            record = proposal_record(version, self.report.name, finding, topic, self.basis)
            record["scope"] += f" Verified source: {source}. Reverify its digest before use."
            identifier = self.run_cli(self.native, "propose", data=record)["id"]
            files.append({"id": identifier, "path": name, "sha256": finding["expected_sha256"]})
        self.assertEqual({file["path"] for file in files}, set(names))
        return version, files, sources

    def test_selects_only_the_pending_proposal_with_a_live_finding(self):
        queued = self.remove_and_queue("active.txt", "cleared.txt", "withdrawn.txt")
        (self.project / "cleared.txt").write_bytes(b"cleared.txt")
        self.run_cli(self.native, "withdraw", queued["withdrawn.txt"][0], "--reason", "Owner declined")

        active_id, finding = queued["active.txt"]
        proposal = self.resume(status="ready")["proposal"]
        self.assertEqual(proposal["id"], active_id)
        self.assertEqual(proposal["finding"], finding)
        self.resume("--id", active_id[:-1], status="unknown")
        for identifier in (queued["cleared.txt"][0], queued["withdrawn.txt"][0], "0" * 24):
            with self.subTest(identifier=identifier):
                self.resume("--id", identifier, status="none")

        (self.project / "active.txt").write_bytes(b"active.txt")
        self.resume(status="none")

    def test_two_findings_require_choice_and_full_id_selects_exact_proposal(self):
        queued = self.remove_and_queue("first.txt", "second.txt")
        choices = self.resume(status="ambiguous")["choices"]
        self.assertEqual(len(choices), len(queued))
        self.assertEqual({choice["id"]: choice["finding"] for choice in choices}, dict(queued.values()))
        second_id, second_finding = queued["second.txt"]
        proposal = self.resume("--id", second_id, status="ready")["proposal"]
        self.assertEqual((proposal["id"], proposal["finding"]), (second_id, second_finding))

    def test_unavailable_report_cannot_resume_queued_proposal(self):
        queued = self.remove_and_queue("support.txt")
        self.report.unlink()
        self.resume(status="unknown")
        self.resume("--id", queued["support.txt"][0], status="unknown")
        result = self.resume("--rehearse", "--id", queued["support.txt"][0], status="unknown")
        self.assertIsNone(result.get("rehearsal"))

    def test_rehearsal_checks_replacement_without_modifying_project(self):
        queued = self.remove_and_queue("support.txt")
        source = self.repair_source(b"support.txt")
        before = source.stat().st_mtime_ns
        result = self.resume("--rehearse", "--source", str(source), status="ready")
        self.assertEqual(result["proposal"]["id"], queued["support.txt"][0])
        self.assertEqual(result["rehearsal"]["status"], "healthy", result)
        self.assertEqual(result["rehearsal"]["remaining"], [])
        self.assertEqual(source.read_bytes(), b"support.txt")
        self.assertEqual(source.stat().st_mtime_ns, before)

    def test_rehearsal_requires_a_choice_and_reports_unrepaired_support(self):
        queued = self.remove_and_queue("first.txt", "second.txt")
        source = self.repair_source(b"first.txt")
        args = ("--rehearse", "--source", str(source))
        ambiguous = self.resume(*args, status="ambiguous")
        self.assertIsNone(ambiguous.get("rehearsal"))

        result = self.resume(*args, "--id", queued["first.txt"][0], status="ready")
        self.assertEqual(result["rehearsal"]["status"], "degraded", result)
        self.assertEqual(result["rehearsal"]["remaining"], ["second.txt"])

    def test_rehearsal_reports_changed_delivery_snapshot(self):
        self.remove_and_queue("support.txt")
        (self.project / "work.txt").write_bytes(b"changed since acceptance")
        source = self.repair_source(b"support.txt")
        rehearsal = self.resume("--rehearse", "--source", str(source), status="ready")["rehearsal"]
        self.assertEqual(rehearsal["status"], "degraded", rehearsal)
        self.assertEqual(rehearsal["remaining"], [])
        self.assertIn("work.txt", " ".join(rehearsal["acceptance_blockers"]))

    def test_rehearses_both_recorded_sources_against_one_delivery(self):
        version, files, sources = self.pair_with_recorded_sources()
        source_revisions = {path: source.stat().st_mtime_ns for path, source in sources.items()}

        result = self.resume("--pair", *(file["id"] for file in files), "--rehearse", status="ready")

        self.assertIsNone(result["proposal"])
        rehearsal = result["rehearsal"]
        self.assertEqual(rehearsal["status"], "healthy", result)
        self.assertEqual(rehearsal["delivery_id"], version)
        self.assertCountEqual(rehearsal["files"], files)
        self.assertEqual(rehearsal["remaining"], [])
        self.assertEqual(rehearsal["acceptance_blockers"], [])
        for path, source in sources.items():
            self.assertFalse((self.project / path).exists())
            self.assertEqual(source.read_bytes(), path.encode())
            self.assertEqual(source.stat().st_mtime_ns, source_revisions[path])

    def test_pair_rejects_unknown_duplicate_or_stale_ids(self):
        _, files, _ = self.pair_with_recorded_sources()
        first, second = (file["id"] for file in files)
        for identifier in (first, "0" * 24, second[:-1]):
            with self.subTest(identifier=identifier):
                result = self.resume("--pair", first, identifier, "--rehearse", status="unknown")
                self.assertIsNone(result["proposal"])
                self.assertIsNone(result.get("rehearsal"))

        (self.project / files[1]["path"]).write_bytes(b"new content since observation")
        result = self.resume("--pair", first, second, "--rehearse", status="unknown")
        self.assertIsNone(result["proposal"])
        self.assertIsNone(result.get("rehearsal"))

    def test_pair_blocks_when_one_recorded_source_fails_verification(self):
        _, files, sources = self.pair_with_recorded_sources()
        sources[files[1]["path"]].write_bytes(b"wrong source contents")
        result = self.resume("--pair", *(file["id"] for file in files), "--rehearse", status="ready")
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["rehearsal"]["status"], "blocked", result)
        self.assertEqual(result["rehearsal"].get("files", []), [])
        self.assertTrue(result["rehearsal"]["reason"])

    def test_rehearsal_blocks_without_readable_digest_matched_source(self):
        self.remove_and_queue("support.txt")
        source = self.repair_source(b"incorrect contents")
        for source_args in ((), ("--source", str(source)), ("--source", str(source.with_name("missing.txt")))):
            with self.subTest(source_args=source_args):
                result = self.resume("--rehearse", *source_args, status="ready")
                self.assertEqual(result["rehearsal"]["status"], "blocked", result)

    def test_invalid_delivery_journal_cannot_resume_queued_proposal(self):
        self.remove_and_queue("support.txt")
        (self.project / ".signum/delivery.json").write_text("invalid", encoding="utf-8")
        self.resume(status="unknown")


if __name__ == "__main__":
    unittest.main()
