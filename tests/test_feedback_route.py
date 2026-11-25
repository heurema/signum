from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/signum/skills/project-intent/scripts"
BACKLOG = Path("docs/operations/plugin-feedback.md")
OBSERVATION = {
    "project": "pilot-A", "source": "codex:turn:example", "symptom": "duplicate-prompt",
    "kind": "friction", "step": "review", "expected": "one prompt", "observed": "two prompts", "impact": "a second reply",
}
TABLE = "# Signum pilot\n\n## Открыто\n\n| ID | Вид | Этап | Симптом | Источник |\n|---|---|---|---|---|\n"


class FeedbackRouteTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.project = self.root / "A"
        self.project.mkdir()
        self.central = self.root / "central"
        self.backlog = self.central / BACKLOG
        self.backlog.parent.mkdir(parents=True)
        self.backlog.write_text(TABLE)
        self.command("signum.py", self.project, "init")
        self.queue = self.project / ".signum/feedback.json"

    def command(self, script, project, *args, data=None, success=True):
        run = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / script), "--project", str(project), *args],
            input=json.dumps(data), text=True, capture_output=True, timeout=15,
        )
        if success:
            self.assertEqual(run.returncode, 0, run.stderr)
            return json.loads(run.stdout)
        self.assertNotEqual(run.returncode, 0)
        self.assertNotIn("Traceback", run.stderr)
        return run.stderr

    def record(self, record=OBSERVATION, project=None, deliver=True):
        args = ("--central-root", str(self.central)) if deliver else ()
        return self.command("feedback.py", project or self.project, "record", *args, data=record)

    def flush(self):
        return self.command("feedback.py", self.project, "flush", "--central-root", str(self.central))

    def test_outage_retry_same_episode_and_ack_recovery(self):
        self.backlog.unlink()
        recorded = self.record()
        self.assertEqual(recorded["status"], "pending")
        self.assertIsNone(recorded["receipts"][0]["id"])
        self.assertFalse(self.backlog.exists())
        self.backlog.write_text(TABLE)
        delivered = self.flush()
        self.assertEqual(delivered["status"], "delivered")
        self.assertEqual(delivered["receipts"][0]["id"], "SGF-001")
        acknowledged = self.backlog.read_bytes()
        self.assertEqual(self.record(dict(OBSERVATION, impact="reworded")), delivered)
        # Reconstruct a crash between central commit and local acknowledgement.
        saved = json.loads(self.queue.read_text())
        saved["episodes"][recorded["receipts"][0]["episode"]]["id"] = None
        self.queue.write_text(json.dumps(saved))
        self.assertEqual(self.flush(), delivered)
        self.assertEqual(self.backlog.read_bytes(), acknowledged)

    def test_no_permission_does_not_reuse_saved_central_path(self):
        self.record()
        old = self.backlog.read_bytes()
        result = self.record(dict(OBSERVATION, source="second-turn"), deliver=False)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(self.command("feedback.py", self.project, "flush")["status"], "pending")
        self.assertEqual(old, self.backlog.read_bytes())
        wrong = self.command("feedback.py", self.project, "flush", "--central-root", str(self.root))
        self.assertEqual(wrong["status"], "pending")
        self.assertIn("different central", wrong["receipts"][-1]["reason"])
        self.assertEqual(self.flush()["status"], "delivered")

    def test_disabled_project_is_untouched_and_does_not_capture_parent(self):
        child = self.project / "B"
        child.mkdir()
        self.assertEqual(self.record(project=child), {"status": "disabled", "receipts": []})
        self.assertEqual(list(child.iterdir()), [])
        self.assertEqual(self.backlog.read_text(), TABLE)
        self.assertFalse(self.queue.exists())

    def test_same_symptom_groups_across_projects_and_concurrent_deliveries(self):
        projects = []
        for index in range(5):
            project = self.root / f"pilot-{index}"
            project.mkdir()
            self.command("signum.py", project, "init")
            projects.append((project, dict(OBSERVATION, project=f"pilot-{index}")))

        def upload(pair):
            project, record = pair
            return self.record(record, project)["receipts"][0]["id"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(upload, projects * 2))
        self.assertEqual(set(ids), {"SGF-001"})
        text = self.backlog.read_text()
        self.assertEqual(text.count("<!-- signum-feedback "), len(projects))
        self.assertEqual(text.count("| SGF-001 |"), 1)
        other = self.record(dict(OBSERVATION, symptom="different-issue"))
        self.assertEqual(other["receipts"][0]["id"], "SGF-002")

    def test_manual_open_or_historical_issues_receive_repeats(self):
        self.backlog.write_text(TABLE + "| SGF-004 | issue | review | original | reference |\n\n### SGF-006\n\nOld note.\n")
        for issue in ("SGF-004", "SGF-006"):
            result = self.record(dict(OBSERVATION, issue=issue, symptom="repeats-" + issue.lower()))
            self.assertEqual(result["receipts"][0]["id"], issue)
        self.assertEqual(self.record()["receipts"][0]["id"], "SGF-007")
        self.assertIn("original | reference<br>", self.backlog.read_text())
        self.assertIn("Old note.", self.backlog.read_text())

    def test_legacy_notes_are_preserved_and_input_cannot_create_rows_or_links(self):
        notes = self.project / ".signum/feedback-pending.md"
        notes.write_text("# Existing notes\n\n## SGF-012\n")
        before = notes.read_bytes()
        self.record(dict(OBSERVATION, observed="<a>|[open](file:///secret)"))
        self.assertNotIn("](file:", self.backlog.read_text())
        self.assertIn("&#124;", self.backlog.read_text())
        self.assertEqual(notes.read_bytes(), before)
        self.assertIn("single-line", self.command(
            "feedback.py", self.project, "record", data=dict(OBSERVATION, observed="a\nb"), success=False,
        ))

    def test_linked_targets_or_missing_table_leave_observation_pending(self):
        wrong_root = self.root / "linked-central"
        wrong_root.symlink_to(self.central, target_is_directory=True)
        linked = self.command("feedback.py", self.project, "record", "--central-root", str(wrong_root),
                              data=OBSERVATION)
        self.assertEqual(linked["status"], "pending")
        self.assertTrue(self.queue.exists())
        other = self.root / "other-documents"
        other.mkdir()
        shutil.move(self.central / "docs", other / "docs")
        (self.central / "docs").symlink_to(other / "docs", target_is_directory=True)
        self.assertEqual(self.record()["status"], "pending")
        untouched = (other / BACKLOG).read_text()
        (self.central / "docs").unlink()
        self.backlog.parent.mkdir(parents=True)
        self.backlog.symlink_to(other / BACKLOG)
        self.assertEqual(self.flush()["status"], "pending")
        self.assertEqual((other / BACKLOG).read_text(), untouched)
        self.backlog.unlink()
        self.backlog.write_text("# Something else\n")
        self.assertEqual(self.flush()["status"], "pending")
        self.assertEqual(self.backlog.read_text(), "# Something else\n")
        self.backlog.write_text(TABLE)
        self.assertEqual(self.flush()["status"], "delivered")


if __name__ == "__main__":
    unittest.main()
