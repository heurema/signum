import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
MARKET = json.loads((ROOT / ".agents/plugins/marketplace.json").read_bytes())
PACKAGE = next(plugin["source"]["path"] for plugin in MARKET["plugins"] if plugin["name"] == "signum")
sys.path.insert(0, str(ROOT / PACKAGE / "skills/project-intent/scripts"))
import capture_inbox as inbox
import chat_source
import decision_capture
import delivery
import scope_guard
import signum


class DesktopHistory:
    def __init__(self, project):
        self.thread = str(uuid4())
        self.metadata = {"id": self.thread, "cwd": str(project), "source": "vscode", "updatedAt": time.time()}
        self.turns = []
        self.items = {}
        self.visible = True

    def add(self, text, status):
        turn = str(uuid4())
        self.turns.insert(0, {"id": turn, "status": status, "startedAt": time.time()})
        self.items[turn] = [self.message(text)]
        return turn

    @staticmethod
    def message(text):
        return {"type": "userMessage", "id": str(uuid4()), "content": [{"type": "text", "text": text}]}

    def call(self, method, params):
        if method == "thread/read":
            if not self.visible:
                raise chat_source.MissingTurn("This chat is not indexed.")
            return {"thread": self.metadata}
        if method == "thread/list":
            rows = [self.metadata] if self.visible and not params["archived"] else []
        elif method == "thread/turns/list":
            rows = self.turns
        elif method == "thread/items/list":
            turn = params["turnId"]
            rows = [{"turnId": turn, "item": item} for item in self.items[turn]]
        else:
            raise AssertionError(method)
        offset = int(params.get("cursor", 0))
        end = offset + params["limit"]
        return {"data": rows[offset:end], "nextCursor": str(end) if end < len(rows) else None}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class ReceiptLifecycleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name).resolve()
        self.folder = self.project / ".signum"
        self.folder.mkdir()
        self.state = self.folder / "state.json"
        signum.write_journal(self.state, signum.Journal())
        with inbox.database(self.project, write=True):
            pass
        self.history = DesktopHistory(self.project)
        server = patch.object(chat_source, "AppServer", return_value=self.history)
        self.server = server.start()
        self.addCleanup(server.stop)
        self.permission = {"topic": "search", "depends_on": [{"topic": "engine", "id": "1" * 24}],
                           "scope": scope_guard.encode(["search.sql"], "check.json")}

    def receive(self, turn, text):
        inbox.receive({"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                       "transcript_path": str(self.project / "transcript"), "session_id": self.history.thread,
                       "turn_id": turn, "prompt": text})

    def start(self):
        inbox.check_start(self.project, signum.Journal.read(self.state), self.permission)

    def capture(self, turn, choices):
        payload = {"thread": self.history.thread, "turn": turn, "choices": choices}
        return decision_capture.capture(signum, self.project, self.folder, payload, chat_source.read_turn)

    def test_late_decision_and_redelivery(self):
        text = "Используем PostgreSQL"
        turn = self.history.add(text, "completed")
        choice = {"topic": "engine", "summary": text, "quote": text, "expected": None, "state": "chosen"}
        self.history.visible = False
        self.receive(turn, text)
        self.start()
        self.history.visible = True
        with self.assertRaisesRegex(ValueError, "pending"):
            self.start()
        self.capture(turn, [choice])
        saved = self.state.read_bytes()
        self.receive(turn, text)
        self.capture(turn, [choice])
        self.start()
        self.assertEqual(self.state.read_bytes(), saved)
        journal = signum.Journal.read(self.state)
        self.assertEqual([e["event"] for e in journal.events], ["proposed", "confirmed"])

    def test_receipt_after_initial_review_reopens_same_decision(self):
        text = "Используем PostgreSQL"
        turn = self.history.add(text, "completed")
        self.history.items[turn].append({"type": "agentMessage", "phase": "final_answer", "id": "a", "text": "OK"})
        choice = {"topic": "engine", "summary": text, "quote": text, "expected": None, "state": "chosen"}
        first = self.capture(turn, [choice])["choices"]["engine"]
        self.assertEqual(first["state"], "open")
        self.receive(turn, text)
        with self.assertRaisesRegex(ValueError, "pending"):
            self.start()
        with self.assertRaisesRegex(signum.SignumError, "engine"):
            self.capture(turn, [])
        second = self.capture(turn, [choice])["choices"]["engine"]
        self.assertEqual(second, {"id": first["id"], "state": "chosen"})
        self.start()

    def test_terminal_probes_do_not_consume_pending_window(self):
        for _ in range(15):
            self.history.add("проверяем связь\n", "interrupted")
        self.start()
        self.assertFalse(signum.Journal.read(self.state).events)

    def test_probe_with_condition_or_still_running_requires_review(self):
        turn = self.history.add("проверяем связь, меняем базу", "interrupted")
        with self.assertRaisesRegex(ValueError, turn):
            self.start()
        self.capture(turn, [])
        self.start()
        running = self.history.add("проверяем связь", "inProgress")
        with self.assertRaisesRegex(ValueError, running):
            self.start()

    def test_missing_content_cannot_be_cleared_as_a_probe(self):
        turn = self.history.add("проверяем связь", "interrupted")
        self.history.items[turn][0]["content"].append({"type": "image", "url": "example"})
        with self.assertRaisesRegex(ValueError, f"search.*{turn}.*unknown"):
            self.start()
        self.history.items[turn][0]["content"].pop()
        self.receive(turn, "Используем PostgreSQL")
        with self.assertRaisesRegex(ValueError, f"{turn}.*does not match"):
            self.start()

    def test_multi_message_turn_can_be_reviewed_but_cannot_confirm_a_combined_quote(self):
        turn = self.history.add("проверяем связь", "inProgress")
        self.receive(turn, "проверяем связь")
        self.capture(turn, [])
        self.history.items[turn].append(self.history.message("Используем PostgreSQL"))
        self.history.turns[0]["status"] = "completed"
        with self.assertRaisesRegex(ValueError, turn):
            self.start()
        source = chat_source.read_turn(self.project, self.history.thread, turn)
        self.assertTrue(source["receipt_matches"])
        self.assertFalse(source["allow_owner"])
        added = "Используем PostgreSQL"
        with self.assertRaisesRegex(signum.SignumError, "source identity"):
            self.capture(turn, [{"topic": "engine", "summary": added, "quote": added,
                                 "expected": None, "state": "chosen"}])
        self.capture(turn, [])
        self.start()
        self.assertFalse(signum.Journal.read(self.state).confirmed)

    def test_missing_receipts_share_one_connection_and_do_not_block_exact_work(self):
        self.history.visible = False
        turns = [str(uuid4()) for _ in range(8)]
        for turn in turns:
            self.receive(turn, "unknown")
        result = inbox.inspect(self.project, signum.Journal.read(self.state))
        self.assertFalse(result["complete"])
        self.assertEqual(result["start_blockers"], [])
        self.assertTrue(all(any(turn in msg for msg in result["warnings"]) for turn in turns))
        self.assertEqual(self.server.call_count, 2)  # Project index, then one exact lookup batch.
        self.start()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            inbox.check_start(self.project, signum.Journal.read(self.state), {**self.permission, "scope": "Explore"})
        inbox.check_start(self.project, signum.Journal.read(self.state), {"topic": "notes", "depends_on": []})

    def test_connection_failures_at_index_and_exact_lookup_still_block(self):
        self.receive(str(uuid4()), "unknown")
        for connections in ([chat_source.ChatSourceError("offline")], [self.history, OSError("offline")]):
            with self.subTest(connections=len(connections)):
                self.server.side_effect = connections
                with self.assertRaisesRegex(ValueError, "offline"):
                    self.start()

    def test_start_with_gaps_preserves_authority_files_and_receipts(self):
        journal = signum.Journal.read(self.state)
        decision = journal.propose({"topic": "engine", "kind": "decision", "summary": "Use SQL",
                                    "source": "owner", "reason": "project scope", "replaces": None})["id"]
        journal.confirm(decision)
        version = journal.propose({**self.permission, "kind": "permission", "summary": "Implement search",
                                   "source": "owner", "reason": "project scope", "replaces": None,
                                   "basis": {"topic": "engine", "id": decision},
                                   "depends_on": [{"topic": "engine", "id": decision}],
                                   "verification": "Run the search check."})["id"]
        signum.write_journal(self.state, journal)
        args = SimpleNamespace(command="start", permission=version, touch=["search.sql"], source=None, started_at=None)

        def start():
            return delivery.run(args, self.project, self.folder, signum)

        turn = str(uuid4())
        self.receive(turn, "A saved message")
        empty = self.history.add("", "interrupted")
        self.history.items[empty] = []
        with self.assertRaisesRegex(delivery.DeliveryError, "confirmed permission"):
            start()
        journal.confirm(version)
        signum.write_journal(self.state, journal)
        args.touch = ["other.sql"]
        with self.assertRaisesRegex(scope_guard.ScopeError, "outside exact file scope"):
            start()
        args.touch = ["search.sql"]
        self.assertEqual(start()["id"], start()["id"])
        self.assertFalse((self.project / "search.sql").exists())
        state = inbox.inspect(self.project, journal)
        self.assertFalse(state["complete"])
        self.assertTrue(all(any(key in message for message in state["warnings"]) for key in (turn, empty)))
        with inbox.database(self.project) as db:
            self.assertEqual(db.execute("SELECT thread, turn, digest FROM prompts").fetchall(),
                             [(self.history.thread, turn, inbox.digest("A saved message"))])
            self.assertEqual(db.execute("SELECT count(*) FROM archived_prompts").fetchone(), (0,))

        correction = "We use SQLite"
        option = {"topic": "engine", "kind": "decision", "summary": correction, "source": "proposal",
                  "reason": "alternative", "replaces": decision}
        proposed = journal.propose(option)["id"]
        self.assertEqual(journal.propose(option)["id"], proposed)
        signum.write_journal(self.state, journal)
        self.assertEqual(start()["permission"], version)
        self.assertEqual(signum.Journal.read(self.state).confirmed["engine"], decision)
        changed = self.history.add(correction, "completed")
        self.receive(changed, correction)
        with self.assertRaisesRegex(ValueError, changed):
            start()
        self.capture(changed, [{"topic": "engine", "summary": correction, "quote": correction,
                                "expected": decision, "state": "chosen"}])
        self.assertEqual(signum.Journal.read(self.state).confirmed["engine"], proposed)
        with self.assertRaisesRegex(delivery.DeliveryError, "changed from"):
            start()

    def test_explicit_archive_requires_unreadable_named_receipts_and_preserves_attestation(self):
        turn = self.history.add("Используем PostgreSQL", "completed")
        pair = f"{self.history.thread}/{turn}"
        self.receive(turn, "Используем PostgreSQL")
        with self.assertRaisesRegex(ValueError, "readable"):
            inbox.archive_receipts(self.project, [pair], "Owner reviewed the missing submission")
        self.history.visible = False
        with self.assertRaisesRegex(ValueError, "No receipt"):
            inbox.archive_receipts(self.project, [pair, f"{self.history.thread}/{uuid4()}"], "Owner review")
        self.start()
        for _ in range(2):
            inbox.archive_receipts(self.project, [pair], "Owner review")
            self.receive(turn, "Используем PostgreSQL")
        self.start()
        with inbox.database(self.project) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM prompts").fetchone(), (0,))
            reason, stamp = db.execute("SELECT reason, archived_at FROM archived_prompts").fetchone()
            self.assertEqual(reason, "Owner review")
            self.assertGreater(stamp, 0)
        self.history.visible = True
        self.assertTrue(chat_source.read_turn(self.project, self.history.thread, turn)["allow_owner"])


if __name__ == "__main__":
    unittest.main()
