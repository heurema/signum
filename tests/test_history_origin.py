import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from test_receipt_lifecycle import DesktopHistory, chat_source, decision_capture, inbox, scope_guard, signum


REGISTER = Path(chat_source.__file__).with_name("history_origin.py")
PERMISSION = {"topic": "search", "depends_on": [{"topic": "storage", "id": "1" * 24}],
              "scope": scope_guard.encode(["search.sql"], "check.json")}


class ProjectHistories:
    def __init__(self, *histories):
        self.threads = {history.thread: history for history in histories}
        self.lists = []

    def call(self, method, params):
        if method == "thread/list":
            self.lists.append(params["cwd"])
            rows = [h.metadata for h in self.threads.values()
                    if h.metadata["cwd"] == params["cwd"] and h.visible and not params["archived"]]
            return {"data": rows, "nextCursor": None}
        history = self.threads.get(params["threadId"])
        if history is None or not history.visible:
            raise chat_source.MissingTurn("Desktop has not loaded this chat.")
        return history.call(method, params)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class HistoryOriginTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.source = self.root / "source"
        self.copy = self.root / "copy"
        self.initialize(self.source)
        self.source_chat = DesktopHistory(self.source)
        self.copy_chat = DesktopHistory(self.copy)
        self.server = ProjectHistories(self.source_chat, self.copy_chat)
        server = patch.object(chat_source, "AppServer", return_value=self.server)
        server.start()
        self.addCleanup(server.stop)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "private-state")})
        env.start()
        self.addCleanup(env.stop)
        self.wording = "Используем PostgreSQL"
        self.key = self.turn(self.source, self.source_chat, self.wording)
        shutil.copytree(self.source, self.copy)

    @staticmethod
    def initialize(root):
        folder = root / ".signum"
        folder.mkdir(parents=True)
        journal = signum.Journal()
        version = journal.propose({"topic": "storage", "kind": "decision", "summary": "Use SQL",
                                   "source": "owner", "reason": "project scope", "replaces": None})
        journal.confirm(version["id"])
        signum.write_journal(folder / "state.json", journal)

    @staticmethod
    def turn(root, history, text):
        turn = history.add(text, "completed")
        history.items[turn].append({"type": "agentMessage", "phase": "final_answer", "id": str(uuid4()), "text": "OK"})
        inbox.receive({"hook_event_name": "UserPromptSubmit", "cwd": str(root), "transcript_path": str(root / "trace"),
                       "session_id": history.thread, "turn_id": turn, "prompt": text})
        return history.thread, turn

    def register(self, source=None, copy=None, *, succeeds=True):
        result = subprocess.run([sys.executable, "-B", str(REGISTER), "--project", str(copy or self.copy),
                                 "--source", str(source or self.source)], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, succeeds, result.stdout + result.stderr)

    @staticmethod
    def contents(root):
        return {file.relative_to(root): file.read_bytes() for file in root.rglob("*") if file.is_file()}

    def journal(self):
        return signum.Journal.read(self.copy / ".signum/state.json")

    def test_normal_review_recovers_only_inherited_turn_and_writes_only_the_copy(self):
        later = self.turn(self.source, self.source_chat, self.wording)
        before = self.contents(self.source)
        self.register()
        result = inbox.inspect(self.copy, self.journal(), include_text=True)
        self.assertTrue(result["complete"], result)
        self.assertEqual([(row["thread"], row["turn"], row["user"]) for row in result["pending"]],
                         [(*self.key, self.wording)])
        recovered = chat_source.read_turn(self.copy, *self.key)
        self.assertTrue(recovered["allow_owner"])
        with self.assertRaises(ValueError):
            chat_source.read_turn(self.copy, *later)
        payload = {"thread": self.key[0], "turn": self.key[1], "choices": [
            {"topic": "database-engine", "summary": self.wording, "quote": self.wording,
             "state": "chosen", "expected": None}]}
        decision_capture.capture(signum, self.copy, self.copy / ".signum", payload, chat_source.read_turn)
        accepted = self.journal().confirmed["database-engine"]
        saved = (self.copy / ".signum/state.json").read_bytes()
        decision_capture.capture(signum, self.copy, self.copy / ".signum", payload, chat_source.read_turn)
        self.assertEqual(self.journal().confirmed["database-engine"], accepted)
        self.assertEqual((self.copy / ".signum/state.json").read_bytes(), saved)
        inbox.check_start(self.copy, self.journal(), PERMISSION)
        local = self.turn(self.copy, self.copy_chat, "Проверим схему")
        self.assertEqual(chat_source.read_turn(self.copy, *local)["user"], "Проверим схему")
        self.assertNotIn(str(self.source), self.server.lists)
        self.assertEqual(self.contents(self.source), before)

    def test_missing_origin_turn_stays_visible_after_readable_turn_is_reviewed(self):
        missing = DesktopHistory(self.source)
        self.server.threads[missing.thread] = missing
        missing_key = self.turn(self.source, missing, self.wording)
        shutil.copy2(self.source / ".signum" / inbox.DB, self.copy / ".signum" / inbox.DB)
        self.register()
        missing.visible = False
        before = self.contents(self.copy / ".signum")
        state = inbox.inspect(self.copy, self.journal())
        self.assertEqual([(r["thread"], r["turn"], r["source"]) for r in state["origin_recovered"]],
                         [(*self.key, str(self.source))])
        self.assertFalse(state["complete"])
        self.assertIn(missing_key[1], " ".join(state["warnings"]))
        self.assertEqual(state["start_blockers"], [])
        with self.assertRaisesRegex(ValueError, f"pending message.*{self.key[1]}"):
            inbox.check_start(self.copy, self.journal(), PERMISSION)
        inbox.check_start(self.copy, self.journal(), {"topic": "independent", "depends_on": []})
        self.assertEqual(self.contents(self.copy / ".signum"), before)
        payload = {"thread": self.key[0], "turn": self.key[1], "choices": [
            {"topic": "database-engine", "summary": self.wording, "quote": self.wording,
             "state": "chosen", "expected": None}]}
        decision_capture.capture(signum, self.copy, self.copy / ".signum", payload, chat_source.read_turn)
        inbox.check_start(self.copy, self.journal(), PERMISSION)
        self.assertIn(missing_key[1], " ".join(inbox.inspect(self.copy, self.journal())["warnings"]))
        missing.visible = True
        self.assertTrue(chat_source.read_turn(self.copy, *missing_key)["receipt_matches"])
        with self.assertRaisesRegex(ValueError, f"pending message.*{missing_key[1]}"):
            inbox.check_start(self.copy, self.journal(), PERMISSION)

    def test_rejects_unrelated_journal_and_a_registration_moved_to_another_copy(self):
        other = self.root / "unrelated"
        shutil.copytree(self.source, other)
        (other / ".signum/state.json").unlink()
        journal = signum.Journal()
        journal.propose({"topic": "other", "kind": "decision", "summary": "Other", "source": "another owner",
                         "reason": "different project", "replaces": None})
        signum.write_journal(other / ".signum/state.json", journal)
        self.register(source=other, succeeds=False)
        self.register()
        nested = self.root / "moved-copy"
        shutil.copytree(self.copy, nested)
        with self.assertRaises(ValueError):
            chat_source.read_turn(nested, *self.key)

    def test_receipt_and_desktop_provenance_are_rechecked_at_read_time(self):
        self.register()
        for project in [self.source, self.copy]:
            with self.subTest(changed_receipt=project.name):
                with inbox.database(project, write=True) as db:
                    db.execute("UPDATE prompts SET digest=?", (inbox.digest("other text"),))
                with self.assertRaises(ValueError):
                    chat_source.read_turn(self.copy, *self.key)
                with self.assertRaisesRegex(ValueError, "origin digest"):
                    inbox.check_start(self.copy, self.journal(), PERMISSION)
                with inbox.database(project, write=True) as db:
                    db.execute("DELETE FROM prompts")
                with self.assertRaises(ValueError):
                    chat_source.read_turn(self.copy, *self.key)
                with self.assertRaisesRegex(ValueError, "origin digest|inherited receipt is missing"):
                    inbox.check_start(self.copy, self.journal(), PERMISSION)
                with inbox.database(project, write=True) as db:
                    db.execute("INSERT INTO prompts (thread,turn,digest) VALUES (?,?,?)",
                               (*self.key, inbox.digest(self.wording)))
        self.source_chat.items[self.key[1]][0]["content"][0]["text"] = "another message"
        with self.assertRaises(ValueError):
            chat_source.read_turn(self.copy, *self.key)
        with self.assertRaisesRegex(ValueError, "does not match the inherited receipt"):
            inbox.check_start(self.copy, self.journal(), PERMISSION)
        self.source_chat.items[self.key[1]][0]["content"][0]["text"] = self.wording
        self.source_chat.metadata["cwd"] = str(self.root / "unrelated")
        with self.assertRaises(ValueError):
            chat_source.read_turn(self.copy, *self.key)
        with self.assertRaisesRegex(ValueError, "outside this project"):
            inbox.check_start(self.copy, self.journal(), PERMISSION)

    def test_inherited_compound_message_does_not_attest_the_neighbor_as_owner(self):
        self.source_chat.items[self.key[1]].append(DesktopHistory.message("Use a different engine"))
        self.register()
        item = chat_source.read_turn(self.copy, *self.key)
        self.assertTrue(item["receipt_matches"])
        self.assertFalse(item["allow_owner"])

    def test_duplicate_receipt_with_new_turn_id_cannot_expand_registration(self):
        self.register()
        key = self.turn(self.source, self.source_chat, self.wording)
        with inbox.database(self.copy, write=True) as db:
            db.execute("INSERT INTO prompts (thread,turn,digest) VALUES (?,?,?)", (*key, inbox.digest(self.wording)))
        with self.assertRaises(ValueError):
            chat_source.read_turn(self.copy, *key)
        self.assertTrue(chat_source.read_turn(self.copy, *self.key)["allow_owner"])

    def test_registration_requires_independent_journal_and_receipt_files(self):
        for name in ("state.json", inbox.DB):
            with self.subTest(name=name):
                local = self.copy / ".signum" / name
                original = self.source / ".signum" / name
                local.unlink()
                os.link(original, local)
                self.register(succeeds=False)
                local.unlink()
                shutil.copy2(original, local)

    def test_registered_copy_cannot_start_when_a_receipt_database_is_missing(self):
        self.register()
        payload = {"thread": self.key[0], "turn": self.key[1], "choices": [
            {"topic": "database-engine", "summary": self.wording, "quote": self.wording,
             "state": "chosen", "expected": None}]}
        decision_capture.capture(signum, self.copy, self.copy / ".signum", payload, chat_source.read_turn)
        self.assertEqual(inbox.inspect(self.copy, self.journal())["pending_count"], 0)
        for root in (self.source, self.copy):
            with self.subTest(missing=root.name):
                store = root / ".signum" / inbox.DB
                saved = store.with_suffix(".saved")
                store.rename(saved)
                with self.assertRaisesRegex(ValueError, "is waiting"):
                    inbox.check_start(self.copy, self.journal(), PERMISSION)
                saved.rename(store)


if __name__ == "__main__":
    unittest.main()
