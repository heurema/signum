"""Exercise scoped search and native decision review in a selected candidate."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import sys
import unittest
from uuid import uuid4


def check_choices(scripts):
    sys.path.insert(0, str(scripts))
    import decision_capture
    import signum

    wording = "A small delay is acceptable if the evidence stays complete."
    alternative = "Someone suggested removing every check."
    cases = []
    for answer, attested, final, omitted, expected in [
        ("Yes", True, wording + " Confirm?", "", True),
        ("Yes, but skip the checks", True, wording + " Confirm?", "", False),
        ("Yes", False, wording + " Confirm?", "", False),
        ("Yes", True, wording + " " + alternative + " Confirm?", "", False),
        ("Yes", True, wording + " Confirm?", " Only for the first trial.", False),
    ]:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / ".signum"
            directory.mkdir()
            path = directory / "state.json"
            signum.write_journal(path, signum.Journal())
            thread, first, second = [str(uuid4()) for _ in range(3)]
            owner = wording + omitted + "\n" + alternative
            previous = dict(thread=thread, turn=first, user_id="original", user=owner, assistant_id="",
                            assistant="", completed=False, allow_owner=True, started_at=1, previous_turn=None)
            current = dict(thread=thread, turn=second, user_id="reply", user=answer, assistant_id="",
                           assistant="", completed=False, allow_owner=attested, started_at=2, previous_turn=first)
            def read(project, thread_id, turn):
                assert project == root and thread_id == thread
                return dict({first: previous, second: current}[turn])
            def choice(topic, summary, state, quote):
                return dict(topic=topic, summary=summary, state=state, quote=quote, expected=None)
            def capture(turn, choices):
                return decision_capture.capture(signum, root, directory, dict(thread=thread, turn=turn, choices=choices), read)
            receipt = capture(first, [choice("latency", wording, "chosen", wording),
                                      choice("alternative", alternative, "open", alternative)])
            assert not signum.Journal.read(path).confirmed
            assert receipt["choices"]["latency"].get("wording") == (None if omitted else wording)
            previous.update(assistant=final, assistant_id="offer", completed=True)
            selected = [choice("latency", wording, "chosen" if attested else "open", answer)]
            capture(second, selected)
            saved = path.read_bytes()
            capture(second, selected)
            assert saved == path.read_bytes()
            journal = signum.Journal.read(path)
            assert ("latency" in journal.confirmed) == expected and "alternative" not in journal.confirmed
            if expected:
                assert journal.record(journal.confirmed["latency"])["summary"] == wording
            cases.append({"answer": answer, "native": attested, "single_rule": alternative not in final,
                          "confirmed": expected, "alternative_open": True, "repeat_stable": True})
    return cases


class PilotGuidanceTest(unittest.TestCase):
    scripts = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"

    def test_next_native_reply_can_select_one_displayed_conditional_choice(self):
        # Other suites load the currently installed core in this interpreter.
        subprocess.run([sys.executable, "-B", __file__, str(self.scripts)], capture_output=True, check=True)

    def test_search_does_not_select_work_based_on_incidental_mentions(self):
        with tempfile.TemporaryDirectory() as name:
            def call(*args, data=None):
                run = subprocess.run([sys.executable, "-B", str(self.scripts / "signum.py"), "--project", name, *args],
                                     input=json.dumps(data) if data is not None else None, capture_output=True,
                                     text=True, check=True)
                return json.loads(run.stdout)
            call("init")
            record = {"topic": "import-audit", "kind": "permission", "summary": "Audit the Orchard importer",
                      "source": "Run the audit", "owner_turn": "codex:turn:" + str(uuid4()), "replaces": None,
                      "verification": "Check Orchard import output in the pilot.", "reason": "Owner request",
                      "depends_on": []}
            key = call("propose", data=record)["id"]
            call("confirm", key)
            self.assertEqual(call("next", "--query", "Orchard pilot latency token budget recovery")["result"], "no_permission")
            for query in ("import-audit", "continue importer"):
                self.assertEqual(call("next", "--query", query)["permission"], key)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scripts", type=Path)
    args = parser.parse_args()
    print(json.dumps(check_choices(args.scripts.resolve()), indent=2))
