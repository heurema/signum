import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_receipt_lifecycle import DesktopHistory, chat_source, delivery, inbox, scope_guard, signum


class NextStepTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.folder = self.project / ".signum"
        self.state = self.folder / "state.json"
        self.folder.mkdir()
        self.journal = signum.Journal()
        self.basis = self.propose("engine", "decision", summary="Choose a local file.")
        self.permission = self.permit("report", summary="Save one record.")
        self.history = DesktopHistory(self.project)
        server = patch.object(chat_source, "AppServer", return_value=self.history)
        server.start()
        self.addCleanup(server.stop)

    def propose(self, topic, kind, *, confirm=True, **fields):
        record = {"topic": topic, "kind": kind, "summary": "A decision", "reason": "Owner's choice",
                  "source": "Owner", "replaces": None, **fields}
        identifier = self.journal.propose(record)["id"]
        if confirm:
            self.journal.confirm(identifier)
        signum.write_journal(self.state, self.journal)
        return identifier

    def permit(self, topic, **fields):
        return self.propose(topic, "permission", basis={"topic": "engine", "id": self.basis},
                            scope=scope_guard.encode([f"{topic}.txt"], f"{topic}.json"),
                            authority_limit="Local files only", verification="Open and compare the file.", **fields)

    def run_core(self, command, **fields):
        args = SimpleNamespace(project=self.project, command=command, **fields)
        return signum.run(args)

    def tree(self):
        return {path.relative_to(self.project): (path.stat().st_mtime_ns, path.read_bytes())
                for path in self.project.rglob("*") if path.is_file()}

    def check(self, status, query="report"):
        before = self.tree()
        data = self.run_core("next", query=query)
        self.assertEqual(self.tree(), before)
        self.assertEqual(data["result"], status, data)
        return data

    def start(self):
        self.run_core("start", permission=self.permission, touch=["report.txt"], started_at=None, source=None)

    def add_receipt(self, text="What is next?", status="inProgress"):
        turn = self.history.add(text, status)
        inbox.receive({"cwd": str(self.project), "hook_event_name": "UserPromptSubmit",
                       "transcript_path": str(self.project / "transcript"), "session_id": self.history.thread,
                       "turn_id": turn, "prompt": text})
        return turn

    def report(self):
        args = SimpleNamespace(argv=["--", sys.executable, "-c", "pass"], name="Check", report="report.json")
        chain = delivery.Deliveries.read(self.folder / "delivery.json", signum)
        context = delivery.verification_context("report.json", self.journal, chain)
        self.assertEqual(delivery.run_check(self.project, args, context), 0)

    def register(self):
        chain = delivery.Deliveries.read(self.folder / "delivery.json", signum)
        evidence = {"files": [delivery.file_entry(self.project, "report.txt")],
                    "artifact": delivery.file_entry(self.project, "report.json"), "status": "pass",
                    "criteria": "Open and compare the file.", "result": "File matches."}
        result = chain.register(self.project, self.journal, self.permission, evidence)
        signum.write_journal(self.folder / "delivery.json", chain)
        return result["id"]

    def test_public_cli_and_current_permission_search(self):
        command = [sys.executable, "-B", signum.__file__, "--project", str(self.project), "next", "--query", "save record"]
        before = self.tree()
        proc = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["result"], "start")
        self.assertEqual(data["scope"], {"report": "report.json", "files": ["report.txt"]})
        self.assertEqual(data["decisions"][0]["id"], self.basis)
        self.assertEqual(self.tree(), before)
        self.permit("record-copy", summary="Save one record.")
        self.check("ambiguous", "save record")
        self.check("ambiguous", None)
        self.check("no_permission", "unrelated")
        self.permit("new-proposal", confirm=False)
        self.check("unconfirmed", "new-proposal")
        self.check("start", self.permission)
        self.propose("engine", "decision", replaces=self.basis, summary="Use a different file.")
        self.assertIn("changed from", self.check("blocked")["reason"])

    def test_current_prompt_is_conditional_and_unreviewed_changes_still_block(self):
        turn = self.add_receipt()
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.history.thread}):
            data = self.check("request_pending")
            self.assertEqual(data["if_scope_unchanged"]["result"], "start")
            self.assertEqual(data["request"]["turn"], turn)
            with self.assertRaisesRegex(ValueError, turn):
                self.start()
            self.history.items[turn].append(self.history.message("Change the agreed output format."))
            self.assertIn(turn, self.check("blocked")["reason"])
            self.history.items[turn].pop()
            self.history.turns[0]["status"] = "completed"
            self.assertIn(turn, self.check("blocked")["reason"])
            self.add_receipt("Show the next step.")
            self.assertIn(turn, self.check("blocked")["reason"])

    def test_forwarded_current_context_never_supplies_owner_evidence(self):
        with inbox.database(self.project, write=True):
            pass
        turn = self.history.add("Forwarded question about the next step", "inProgress")
        with patch.dict(os.environ, {"CODEX_THREAD_ID": self.history.thread}):
            self.check("request_pending")
            self.assertFalse(chat_source.read_turn(self.project, self.history.thread, turn)["allow_owner"])
        with patch.dict(os.environ, {"CODEX_THREAD_ID": ""}):
            self.assertIn(turn, self.check("blocked")["reason"])

    def test_reading_clean_inbox_preserves_tracking_cursor(self):
        with inbox.database(self.project, write=True) as db:
            db.execute("UPDATE tracking SET since=1")
        self.check("start")
        with inbox.database(self.project) as db:
            self.assertEqual(db.execute("SELECT since FROM tracking").fetchone(), (1,))

    def test_verified_delivery_and_owner_decision(self):
        self.start()
        self.check("implement")
        (self.project / "report.txt").write_text("one record")
        self.check("verify")
        self.report()
        self.check("register_evidence")
        identifier = self.register()
        self.assertEqual(self.check("awaiting_acceptance")["delivery"], identifier)
        (self.project / "report.txt").write_text("different data")
        self.assertIn("report.txt", self.check("blocked")["reason"])
        (self.project / "report.txt").write_text("one record")
        self.run_core("accept", id=identifier, reason="Reviewed the file.", source="Owner response.")
        self.check("complete")
        (self.project / "report.txt").unlink()
        data = self.check("investigate")
        self.assertIn("report.txt", data["reason"])
        self.assertEqual(data["delivery_state"], "accepted")
        self.assertEqual(data["delivery"], identifier)

    def test_owner_return_and_rework_baseline(self):
        self.start()
        (self.project / "report.txt").write_text("first record")
        self.report()
        identifier = self.register()
        self.run_core("return", id=identifier, reason="Correct the record.", source="Owner response.")
        data = self.check("blocked")
        self.assertEqual(data["delivery_state"], "returned")
        self.assertEqual(data["owner_reason"], "Correct the record.")
        self.assertIn("absent at start", data["reason"])
        (self.project / "report.json").unlink()
        self.assertEqual(self.check("start")["delivery_state"], "returned")
        self.start()
        self.check("implement")

    def test_distinguishes_existing_check_from_owner_acceptance(self):
        self.propose("observed", "check", summary="Earlier file opened.",
                     basis={"topic": "report", "id": self.permission})
        data = self.check("start")
        self.assertEqual(data["recorded_observations"][0]["observation"], "Earlier file opened.")
        self.start()
        (self.project / "unrelated").write_text("outside")
        self.assertIn("outside", self.check("blocked")["reason"])

    def test_reader_detects_journal_change_without_creating_a_lock(self):
        import next_step
        original = next_step.describe

        def change_during_read(*args):
            result = original(*args)
            self.permit("second", summary="Second change.")
            return result

        with patch.object(next_step, "describe", side_effect=change_during_read):
            result = self.run_core("next", query="report")
        self.assertEqual(result["result"], "blocked")
        self.assertIn("changed during this read", result["reason"])
        self.assertFalse((self.folder / "write.lock").exists())

    def test_existing_report_must_describe_the_same_start_and_snapshot(self):
        (self.project / "report.json").write_text("{}")
        self.assertIn("absent at start", self.check("blocked")["reason"])
        (self.project / "report.json").unlink()
        self.start()
        (self.project / "report.txt").write_text("first record")
        self.report()
        (self.project / "report.txt").write_text("changed after check")
        self.assertIn("changed file snapshot", self.check("blocked")["reason"])


if __name__ == "__main__":
    unittest.main()
