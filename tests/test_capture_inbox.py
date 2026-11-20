import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"
sys.path.insert(0, str(SCRIPTS))
import capture_inbox as inbox
import chat_source
import decision_capture
import delivery
import signum


class CaptureInboxTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.folder = self.root / ".signum"
        self.folder.mkdir()
        self.path = self.folder / "state.json"
        signum.write_journal(self.path, signum.Journal())
        self.event = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.root),
                      "transcript_path": str(self.root / "history.jsonl"),
                      "session_id": str(uuid4()), "turn_id": str(uuid4()), "prompt": "SQLx подходит, откат обсудим позже"}
        self.key = self.event["session_id"], self.event["turn_id"]

    def test_session_without_persistent_history_leaves_no_receipt(self):
        for fields in ({}, {"transcript_path": None}, {"transcript_path": ""}):
            with self.subTest(fields=fields):
                event = dict(self.event)
                del event["transcript_path"]
                self.assertEqual(inbox.receive(dict(event, **fields)), {})
                self.assertFalse(inbox.active(self.root))
        inbox.receive(self.event)
        self.assertTrue(inbox.native_prompt_seen(self.root, *self.key, self.event["prompt"]))

    def test_foreign_project_subagent_and_conflicting_retry(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertFalse(inbox.receive(dict(self.event, cwd=other)))
            self.assertFalse((Path(other) / ".signum").exists())
        self.assertFalse(inbox.receive(dict(self.event, agent_id="worker")))
        self.assertFalse(inbox.active(self.root))
        for _ in range(2):
            self.assertIn("hookSpecificOutput", inbox.receive(self.event))
        self.assertTrue(inbox.native_prompt_seen(self.root, *self.key, self.event["prompt"] + "\n"))
        self.assertFalse(inbox.native_prompt_seen(self.root, *self.key, "something else"))
        with self.assertRaises(ValueError):
            inbox.receive(dict(self.event, prompt="changed"))
        self.assertNotIn(self.event["prompt"].encode(), (self.folder / inbox.DB).read_bytes())

    def test_inbox_recovery_and_restart(self):
        inbox.receive(self.event)
        item = {"thread": self.key[0], "turn": self.key[1], "user_id": "u", "user": self.event["prompt"],
                "assistant_id": "a", "assistant": "Acknowledged", "started_at": 1, "allow_owner": True,
                "previous_turn": None, "completed": True}
        history = {"turns": [item], "complete": True, "warnings": []}
        def read(project, thread, turn):
            self.assertEqual((project, thread, turn), (self.root, *self.key))
            return dict(item, allow_owner=inbox.native_prompt_seen(project, thread, turn, item["user"]))
        with patch.object(chat_source, "recent_turns", return_value=history), patch.object(chat_source, "read_turn", side_effect=read):
            journal = signum.Journal.read(self.path)
            with self.assertRaisesRegex(ValueError, "pending"):
                inbox.check_start(self.root, journal, {"depends_on": [{"topic": "test", "id": "1" * 24}]})
            inbox.check_start(self.root, journal, {"depends_on": []})
            pending = inbox.inspect(self.root, journal, include_text=True)["pending"][0]
            self.assertEqual(pending["user"], self.event["prompt"])
            choices = [
                {"topic": "migration-tool", "summary": "SQLx подходит", "quote": "SQLx подходит", "state": "chosen", "expected": None},
                {"topic": "rollback", "summary": "Rollback remains open", "quote": "откат обсудим позже", "state": "open", "expected": None},
            ]
            payload = {"thread": self.key[0], "turn": self.key[1], "choices": choices}
            decision_capture.capture(signum, self.root, self.folder, payload, read)
            saved = self.path.read_bytes()
            decision_capture.capture(signum, self.root, self.folder, payload, read)
            self.assertEqual(self.path.read_bytes(), saved)
            recovered = signum.Journal.read(self.path)
            self.assertEqual(set(recovered.confirmed), {"migration-tool"})
            self.assertEqual(recovered.status("rollback")["proposals"][0]["state"], "pending")
            self.assertEqual(inbox.inspect(self.root, recovered)["pending"], [])
            with patch.object(chat_source, "recent_turns", return_value=dict(history, complete=False)):
                with self.assertRaisesRegex(ValueError, "incomplete.*original project"):
                    inbox.check_start(self.root, recovered, {"depends_on": [{"topic": "test", "id": "1" * 24}]})

    def test_stale_declared_dependency_with_separate_owner_permission(self):
        j = signum.Journal()
        def add(record):
            version = j.propose(record)["id"]
            j.confirm(version)
            return version
        rule = {"topic": "engine", "kind": "decision", "summary": "Use engine A", "source": "owner", "reason": "chosen", "replaces": None}
        version = add(rule)
        permission = {"kind": "permission", "summary": "Write schema", "verification": "Review the schema", "reason": "requested", "replaces": None,
                      "source": "codex:turn:" + self.key[1] + "\n\nWrite a schema."}
        linked = add(dict(permission, topic="schema", depends_on=[{"topic": "engine", "id": version}]))
        separate = add(dict(permission, topic="notes", depends_on=[]))
        self.assertIsNotNone(delivery.require_permission(j, linked))
        with self.assertRaises(signum.SignumError):
            add(dict(permission, topic="bad", depends_on=[{"topic": "unknown", "id": version}]))
        add(dict(rule, summary="Use engine B", replaces=version))
        with self.assertRaisesRegex(delivery.DeliveryError, "changed"):
            delivery.require_permission(j, linked)
        self.assertIsNotNone(delivery.require_permission(j, separate))
