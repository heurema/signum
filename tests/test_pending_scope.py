"""Scoped inbox checks through the candidate's public command-line tools."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from uuid import uuid4


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"
# A process outside the project provides local, paged Desktop evidence to the public tools.
SERVER = '''import json, os, sys
state = json.load(open(os.environ["TEST_HISTORY"]))
for line in sys.stdin:
    req = json.loads(line)
    method, p = req.get("method"), req.get("params", {})
    if "id" not in req:
        continue
    if method == "initialize":
        result = {}
    elif method == "thread/list":
        result = {"data": [] if p.get("archived") else [state["thread"]]}
    elif method == "thread/read":
        result = {"thread": state["thread"]}
    elif method == "thread/turns/list":
        result = {"data": state["turns"]}
    elif method == "thread/items/list":
        turn = p["turnId"]
        result = {"data": [{"turnId": turn, "item": item} for item in state["items"][turn]]}
    else:
        raise RuntimeError(method)
    print(json.dumps({"id": req["id"], "result": result}), flush=True)
'''


class PendingScopeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        fake = self.root / "codex"
        fake.write_text(f"#!{sys.executable}\n" + SERVER)
        fake.chmod(0o700)
        self.history = {"thread": {"id": str(uuid4()), "cwd": str(self.project), "source": "cli",
                                   "updatedAt": time.time()}, "turns": [], "items": {}}
        self.env = {**os.environ, "PATH": str(self.root) + os.pathsep + os.environ["PATH"],
                    "CODEX_APP_RUNTIME_CLI_PATH": "", "TEST_HISTORY": str(self.root / "history.json"),
                    "XDG_STATE_HOME": str(self.root / "user-state")}
        self.call("signum", "init")
        self.source = self.turn("We use PostgreSQL")
        self.call("signum", "capture", payload={"thread": self.history["thread"]["id"], "turn": self.source,
                  "choices": [{"topic": "engine", "summary": "We use PostgreSQL", "quote": "We use PostgreSQL",
                               "expected": None, "state": "chosen"}]})
        self.engine = self.call("signum", "status", "--topic", "engine")["confirmed"][0]["id"]
        self.permission = self.permit("card")

    def call(self, script, *args, payload=None, error=None):
        Path(self.env["TEST_HISTORY"]).write_text(json.dumps(self.history))
        cmd = [sys.executable, "-B", str(SCRIPTS / (script + ".py"))]
        if "--hook" not in args:
            cmd += ["--project", str(self.project)]
        result = subprocess.run([*cmd, *args], env=self.env, capture_output=True, text=True, timeout=12,
                                input=json.dumps(payload) if payload is not None else None)
        if error:
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertRegex(result.stderr, error)
            self.assertNotIn("Traceback", result.stderr)
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def permit(self, topic, dependency=None):
        dependency = dependency or {"topic": "engine", "id": self.engine}
        proposal = self.call("signum", "draft", "--topic", topic, payload={
            "owner_turn": "codex:turn:" + str(uuid4()), "request": f"Build the local {topic} file.",
            "outcome": f"Write {topic}.", "excludes": "Other files", "rationale": "Scoped test",
            "verification": "Check the file.", "files": [topic + ".txt"], "report": topic + ".json",
            "depends_on": [dependency]})
        self.call("signum", "confirm", proposal["id"])
        return proposal["id"]

    def turn(self, text, status="completed"):
        turn = str(uuid4())
        stamp = time.time()
        self.history["thread"]["updatedAt"] = stamp
        self.history["turns"].insert(0, {"id": turn, "startedAt": stamp, "status": status})
        self.history["items"][turn] = [{"type": "userMessage", "id": str(uuid4()),
                                      "content": [{"type": "text", "text": text}]}]
        if status == "completed":
            self.history["items"][turn].append({"type": "agentMessage", "id": str(uuid4()),
                                                "phase": "final_answer", "text": "Understood."})
        self.call("capture_inbox", "--hook", payload={"hook_event_name": "UserPromptSubmit",
                  "cwd": str(self.project), "session_id": self.history["thread"]["id"], "turn_id": turn,
                  "prompt": text, "transcript_path": str(self.root / "transcript")})
        return turn

    def review(self, turn, *args, error=None):
        return self.call("pending_scope", "--permission", self.permission, "--turn",
                         self.history["thread"]["id"] + "/" + turn, *args, error=error)

    def exclude(self, turn):
        stamp = self.review(turn)["fingerprint"]
        self.review(turn, "--unrelated", stamp, "--reason", "Separate IPO news research")

    def start(self, permission=None, *args, error=None):
        return self.call("signum", "start", permission or self.permission, *args, error=error)

    def test_scoped_review_leaves_turn_pending_and_requires_authority_for_each_version(self):
        other = self.permit("summary")
        turn = self.turn("Research IPO news without database changes.", "interrupted")
        self.start(error=turn)
        self.exclude(turn)
        self.start(None, "--touch", "summary.txt", error="outside exact file scope")
        self.start(other, error=turn)
        self.assertEqual(self.start()["permission"], self.permission)
        pending = self.call("capture_inbox")
        self.assertEqual([item["turn"] for item in pending["pending"]], [turn])
        self.assertEqual(self.call("signum", "status", "--topic", "engine")["confirmed"][0]["id"], self.engine)

    def test_scope_review_tracks_complete_source_and_does_not_clear_other_pending_turns(self):
        turn = self.turn("IPO research", "completed")
        self.exclude(turn)
        wording = "Consider replacing the database."
        other = self.turn(wording)
        self.start(error=other)
        captured = self.call("signum", "capture", payload={"thread": self.history["thread"]["id"], "turn": other,
                  "choices": [{"topic": "engine", "summary": wording, "quote": wording,
                               "expected": self.engine, "state": "open"}]})
        self.assertEqual(captured["choices"]["engine"]["state"], "open")
        self.assertEqual(self.start()["permission"], self.permission)
        self.history["items"][turn][-1]["text"] = "The earlier answer has changed."
        self.start(error=turn)
        # The same stale review must block when only exact receipt lookup finds the turn.
        self.history["thread"]["updatedAt"] = 0
        self.start(error=turn)

    def test_dependency_source_cannot_be_ignored_or_disappear_after_capture(self):
        self.review(self.source, error="declared decision: engine")
        wording = "We use Markdown"
        format_turn = self.turn(wording)
        self.call("signum", "capture", payload={"thread": self.history["thread"]["id"], "turn": format_turn,
                  "choices": [{"topic": "format", "summary": wording, "quote": wording,
                               "expected": None, "state": "chosen"}]})
        format_id = self.call("signum", "status", "--topic", "format")["confirmed"][0]["id"]
        formatting = self.permit("summary", {"topic": "format", "id": format_id})
        self.history["turns"] = [turn for turn in self.history["turns"] if turn["id"] != self.source]
        self.start(error=f"{self.source} for decision engine is unavailable")
        self.assertEqual(self.start(formatting)["permission"], formatting)

    def test_unverifiable_pending_revision_is_scoped_to_the_named_dependency(self):
        text = "We could use SQLite later."
        turn = self.turn(text)
        self.call("signum", "capture", payload={"thread": self.history["thread"]["id"], "turn": turn,
                  "choices": [{"topic": "engine", "summary": text, "quote": text,
                               "expected": self.engine, "state": "open"}]})
        self.start()
        self.history["turns"] = [row for row in self.history["turns"] if row["id"] != turn]
        self.start(error=f"{turn} for decision engine is unavailable")

    def test_changed_source_or_nonterminal_turn_cannot_be_excluded(self):
        turn = self.turn("Separate IPO work", "completed")
        token = self.review(turn)["fingerprint"]
        self.history["items"][turn][-1]["text"] += " Something else."
        self.review(turn, "--unrelated", token, "--reason", "IPO case", error="differs from the displayed turn")
        running = self.turn("Current research", "inProgress")
        self.review(running, error="complete, terminal source")

    def test_new_dependency_version_blocks_after_scope_review(self):
        self.exclude(self.turn("Separate IPO work", "interrupted"))
        self.start()
        turn = self.turn("We use SQLite")
        self.call("signum", "capture", payload={"thread": self.history["thread"]["id"], "turn": turn,
                  "choices": [{"topic": "engine", "summary": "We use SQLite", "quote": "We use SQLite",
                               "expected": self.engine, "state": "chosen"}]})
        self.start(error="Decision engine changed from")


if __name__ == "__main__":
    unittest.main()
