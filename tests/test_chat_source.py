import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"
sys.path.insert(0, str(SCRIPTS))
import chat_source


class History:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.threads = {"main": {"id": "main", "cwd": str(root), "source": "vscode", "updatedAt": 12}}
        self.turns = {"main": [{"id": "reply", "startedAt": 12, "status": "inProgress"},
                               {"id": "offer", "startedAt": 10, "status": "completed"}]}
        self.items = {
            "reply": [
                {"type": "hookPrompt", "id": "context", "fragments": []},
                {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "Yes.\n"}]},
            ],
            "offer": [
                {"type": "agentMessage", "phase": "commentary", "id": "x", "text": "Interim"},
                {"type": "agentMessage", "phase": "final_answer", "id": "f", "text": "Use the first option?"},
                {"type": "agentMessage", "phase": "final_answer", "delivery": "async", "id": "a", "text": "Update"},
            ],
        }

    def call(self, method, params):
        self.calls.append((method, params))
        thread = params.get("threadId")
        if method == "thread/read":
            return {"thread": self.threads[thread]}
        if method == "thread/list":
            data = sorted(self.threads.values(), key=lambda x: x["updatedAt"], reverse=True)
            rows = [t for t in data if bool(t.get("archived")) == params["archived"]]
        elif method == "thread/turns/list":
            rows = self.turns[thread]
        elif method == "thread/items/list":
            turn = params["turnId"]
            rows = [{"turnId": turn, "item": i} for i in self.items[turn]]
        else:
            raise AssertionError(method)
        # Small pages exercise cross-page lookups for turns and individual items.
        offset = int(params.get("cursor", 0))
        end = offset + min(params["limit"], 2)
        return {"data": rows[offset:end], "nextCursor": str(end) if end < len(rows) else None}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ChatSourceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / ".signum").mkdir()
        (self.root / ".signum/state.json").write_text(json.dumps({"schema_version": 1, "events": []}))
        self.history = History(self.root)
        server = mock.patch.object(chat_source, "AppServer", return_value=self.history)
        server.start()
        self.addCleanup(server.stop)
        self.native = mock.Mock(return_value=False)
        inbox = mock.patch.dict(sys.modules, {"capture_inbox": types.SimpleNamespace(native_prompt_seen=self.native)})
        inbox.start()
        self.addCleanup(inbox.stop)

    def test_read_requires_matching_native_input_and_keeps_final_only(self):
        self.native.side_effect = lambda p, th, tr, text: (p, th, tr, text) == (self.root, "main", "reply", "Yes.\n")
        reply = chat_source.read_turn(self.root, "main", "reply")
        self.assertEqual((reply["user"], reply["user_id"], reply["previous_turn"]), ("Yes.\n", "u", "offer"))
        self.assertTrue(reply["allow_owner"])
        self.assertFalse(reply["completed"])
        self.assertEqual(reply["assistant"], "")
        shown = chat_source.read_turn(self.root, "main", "offer")
        self.assertEqual((shown["assistant"], shown["assistant_id"]), ("Use the first option?", "f"))
        self.assertTrue(shown["completed"])
        self.assertFalse(shown["allow_owner"])

    def test_relay_multipart_unknown_origin_and_fork_cannot_attest(self):
        self.assertFalse(chat_source.read_turn(self.root, "main", "reply")["allow_owner"])
        self.native.return_value = True
        self.history.items["reply"] = [{"type": "functionCallOutput", "id": "r", "output": "Yes"}]
        self.assertEqual(chat_source.read_turn(self.root, "main", "reply")["user"], "")
        self.history.items["reply"] = [{"type": "userMessage", "id": "u",
                                        "content": [{"type": "text", "text": "See this"}, {"type": "image"}]}]
        self.assertFalse(chat_source.read_turn(self.root, "main", "reply")["allow_owner"])
        for key, value in (("forkedFromId", "parent"), ("source", {"subAgent": "review"}),
                           ("cwd", str(self.root.parent)), ("parentThreadId", "parent")):
            original = self.history.threads["main"].copy()
            self.history.threads["main"][key] = value
            self.history.calls.clear()
            with self.assertRaises(chat_source.ChatSourceError):
                chat_source.read_turn(self.root, "main", "reply")
            self.assertEqual(len(self.history.calls), 1)
            self.history.threads["main"] = original

    def test_index_and_time_window_include_archived_and_report_truncation(self):
        archived = dict(self.history.threads["main"], id="archived", archived=True, updatedAt=11)
        self.history.threads["archived"] = archived
        self.history.turns["archived"] = [{"id": "offer", "startedAt": 10, "status": "completed"}]
        result = chat_source.recent_turns(self.root, since=11, thread_limit=2, turn_limit=1)
        self.assertTrue(result["complete"])
        self.assertEqual([turn["turn"] for turn in result["turns"]], ["reply"])
        scans = [p for method, p in self.history.calls if method == "thread/list"]
        self.assertEqual({p["archived"] for p in scans}, {False, True})
        self.assertTrue(all(p["useStateDbOnly"] and p["cwd"] == str(self.root) for p in scans))
        self.assertFalse(chat_source.recent_turns(self.root, since=0, thread_limit=1)["complete"])
        self.assertFalse(chat_source.recent_turns(self.root, thread_id="main", since=0, turn_limit=1)["complete"])

    def test_reviewed_window_drains_across_pages_and_chats(self):
        self.history.turns["main"][0]["status"] = "completed"
        for index in range(4):
            name = f"old-{index}"
            self.history.threads[name] = dict(self.history.threads["main"], id=name, updatedAt=5 - index)
            self.history.turns[name] = [{"id": "offer", "startedAt": 2, "status": "completed"}]
        self.history.turns["old-3"][0]["status"] = "inProgress"
        reviewed = {("main", "offer"), ("main", "reply")}
        reviewed.update((f"old-{index}", "offer") for index in range(4))
        result = chat_source.recent_turns(self.root, since=0, thread_limit=1, turn_limit=1, reviewed=reviewed)
        self.assertTrue(result["complete"])
        self.assertEqual([(t["thread"], t["turn"]) for t in result["turns"]], [("old-3", "offer")])
        item_threads = {params["threadId"] for method, params in self.history.calls if method == "thread/items/list"}
        self.assertEqual(item_threads, {"old-3"})

    def test_runtime_ignores_project_binaries_and_unrecognized_override(self):
        with tempfile.TemporaryDirectory() as outside:
            trusted = Path(outside).resolve() / "bin"
            trusted.mkdir()
            installed = trusted / "codex"
            installed.write_text("#!/bin/sh\n")
            installed.chmod(0o700)
            local = self.root / "codex"
            local.write_text("#!/bin/sh\n")
            local.chmod(0o700)
            link_dir = trusted.parent / "symlinks"
            link_dir.mkdir()
            (link_dir / "codex").symlink_to(local)
            environment = {"CODEX_APP_RUNTIME_CLI_PATH": str(local), "PATH": os.pathsep.join(
                (str(self.root), str(link_dir), str(trusted)))}
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(chat_source.codex_executable(self.root), str(installed))
            with mock.patch.dict(os.environ, {"PATH": str(link_dir), "CODEX_APP_RUNTIME_CLI_PATH": str(local)}):
                with self.assertRaises(chat_source.ChatSourceError):
                    chat_source.codex_executable(self.root)

    def test_full_project_required_before_connecting(self):
        (self.root / ".signum/state.json").unlink()
        with self.assertRaises((OSError, chat_source.ChatSourceError)):
            chat_source.read_turn(self.root, "main", "reply")
        self.assertEqual(self.history.calls, [])


if __name__ == "__main__":
    unittest.main()
