import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"


class RecallTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.state = self.project / ".signum/state.json"

    def command(self, script, *args, data=None):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / script), "--project", str(self.project), *args],
            input=None if data is None else json.dumps(data, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )

    def call(self, script, *args, data=None):
        result = self.command(script, *args, data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def recall(self, *args):
        return self.call("recall.py", *args)

    def propose(self, topic, summary, *, kind="decision", replaces=None, **fields):
        if not self.state.exists():
            self.call("signum.py", "init")
        return self.call("signum.py", "propose", data={
            "topic": topic, "kind": kind, "summary": summary, "replaces": replaces,
            "source": "Owner discussion", "reason": "Record the wording", **fields,
        })["id"]

    def confirm(self, identifier):
        self.call("signum.py", "confirm", identifier)
        return identifier

    def test_empty_unknown_and_corrupt_have_distinct_results(self):
        self.assertEqual(self.recall(), {"result": "empty"})
        self.assertFalse(self.state.parent.exists())
        self.propose("review-queue", "Вернуть отложенное предложение в общую очередь.")
        self.assertEqual(self.recall("--query", "удалили архив"), {"result": "unknown"})
        self.assertEqual(self.recall("--topic", "wrong"), {"result": "unknown", "topic": "wrong"})
        self.state.write_text('{"schema_version":99,"events":[]}', encoding="utf-8")
        failed = self.command("recall.py")
        self.assertEqual(failed.returncode, 1)
        self.assertEqual(failed.stdout, "")
        self.assertIn("invalid journal format", failed.stderr)

    def test_query_recalls_full_pending_record_with_russian_inflections(self):
        pending = self.propose(
            "review-queue", "Вернуть отложенное предложение в общую очередь.",
            scope="Current review only", questions=["Как назначить отдельный срок?"],
        )
        self.confirm(self.propose("backups", "Keep encrypted snapshots."))
        state_bytes = self.state.read_bytes()
        result = self.recall("--query", "отложили предложения очереди")
        self.assertEqual(result["result"], "matched")
        topic, = result["topics"]
        self.assertEqual(topic["topic"], "review-queue")
        self.assertEqual(topic["matched_terms"], ["отложили", "предложения", "очереди"])
        item, = topic["entries"]
        self.assertEqual(item["id"], pending)
        self.assertEqual(item["state"], "pending")
        self.assertEqual(item["scope"], "Current review only")
        self.assertEqual(item["source"], "Owner discussion")
        self.assertEqual(item["questions"], ["Как назначить отдельный срок?"])
        self.assertEqual(self.state.read_bytes(), state_bytes)

    def test_ambiguous_candidates_are_bounded_and_have_preview_states(self):
        for number in range(5):
            self.propose(f"snapshot-{number}", "Retain encrypted snapshots")
        result = self.recall("--query", "encrypted snapshots")
        self.assertEqual(result["result"], "ambiguous")
        self.assertEqual(result["candidate_count"], 5)
        self.assertEqual(len(result["topics"]), 3)
        self.assertEqual(result["topics"][0]["entries"][0]["state"], "pending")
        self.assertEqual(self.recall("--query", "unknown encrypted")["result"], "ambiguous")
        self.assertEqual(len(self.recall()["topics"]), 5)

    def test_replacements_and_legacy_history_keep_current_and_prior_wording_distinct(self):
        original = {
            "topic": "retention", "kind": "decision", "summary": "Keep alphabetical archives.",
            "replaces": None, "source": "Owner discussion", "reason": "Original scope",
        }
        encoded = json.dumps(original, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        legacy = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
        self.state.parent.mkdir()
        self.state.write_text(json.dumps({"schema_version": 1, "events": [
            {"event": "proposed", "id": legacy, "at": "2026-01-01", "record": original},
            {"event": "confirmed", "id": legacy, "at": "2026-01-01"},
        ]}), encoding="utf-8")
        current = self.confirm(self.propose("retention", "Keep monthly exports.", replaces=legacy))
        result = self.recall("--query", "alphabetical archives")
        self.assertEqual(result["result"], "historical")
        topic, = result["topics"]
        self.assertEqual(topic["historical_matches"], [legacy])
        self.assertEqual(topic["history_versions"], 1)
        item, = topic["entries"]
        self.assertEqual(item["id"], current)
        self.assertEqual(item["replaces"], legacy)
        self.assertEqual(item["state"], "current")
        self.assertEqual(item["summary"], "Keep monthly exports.")

    def test_permission_remains_blocked_through_competition_and_staleness(self):
        before = self.confirm(self.propose("acceptance", "Verify local output."))
        permission = self.confirm(self.propose(
            "trial", "Run local trial.", kind="permission", scope="One local run",
            authority_limit="No publishing", verification="Read the receipt",
            basis={"topic": "acceptance", "id": before},
        ))
        observed = self.confirm(self.propose(
            "trial-check", "Observed receipt locally.", kind="check",
            basis={"topic": "trial", "id": permission},
        ))
        first = self.propose("acceptance", "Verify remotely.", replaces=before)
        second = self.propose("acceptance", "Verify on test fixture.", replaces=before)
        acceptance = self.recall("--topic", "acceptance")["topics"][0]
        self.assertEqual([item["state"] for item in acceptance["entries"]], ["current", "competing", "competing"])
        trial = self.recall("--topic", "trial")["topics"][0]
        self.assertEqual(trial["blocked"][0]["alternatives"], {"acceptance": [first, second]})
        self.assertEqual(trial["related_checks"][0]["relation"], "current")
        self.confirm(first)
        trial = self.recall("--topic", "trial")["topics"][0]
        trial_entry, = trial["entries"]
        self.assertEqual(trial_entry["state"], "stale")
        self.assertEqual(trial_entry["scope"], "One local run")
        self.assertEqual(trial_entry["authority_limit"], "No publishing")
        self.assertTrue(trial_entry["issues"])
        self.assertEqual(trial["related_checks"][0]["relation"], "historical")
        self.assertEqual(trial["related_checks"][0]["id"], observed)
        acceptance = self.recall("--topic", "acceptance")["topics"][0]
        self.assertEqual([item["state"] for item in acceptance["entries"]], ["current", "conflict"])
        self.assertEqual(acceptance["entries"][1]["id"], second)


if __name__ == "__main__":
    unittest.main()
