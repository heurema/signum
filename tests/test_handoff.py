import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"
TOPIC = "revise-example"
DECISION = "example-format"
QUESTION = "Which wording should the example use?"
PREVIOUS = "Compare example formats"
WORKER = "Editor"
FILE = "example.txt"
REPORT = "check.json"
TEST = "Compare the example with the selected wording."
SOURCE = "Owner's example-format discussion"


class HandoffTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name) / "connected"
        self.project.mkdir()
        self.call("init")

    def cli(self, *args, data=None, project=None):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), "--project", str(project or self.project), *args],
            input=json.dumps(data) if data is not None else None, text=True, capture_output=True, timeout=10,
        )

    def call(self, *args, **kwargs):
        result = self.cli(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def decision(self, summary, topic=DECISION, replaces=None, **fields):
        return self.call("propose", data={
            "kind": "decision", "topic": topic, "summary": summary, "source": SOURCE,
            "reason": "Select one format.", "replaces": replaces, **fields,
        })["id"]

    def link_args(self, topic=TOPIC, decision=DECISION, worker=WORKER):
        return ("link-task", topic, "--after", PREVIOUS, "--assignee", worker,
                "--decision", decision, "--question", QUESTION)

    def draft(self, basis_id, basis_topic=DECISION, topic=TOPIC, *, file=FILE, report=REPORT):
        terms = {
            "owner_turn": "codex:turn:11111111-1111-4111-8111-111111111111",
            "request": "Update the example.", "outcome": "Use the selected wording.",
            "rationale": "Keep the example current.", "excludes": "Other files.", "files": [file],
            "report": report, "verification": TEST, "basis": {"topic": basis_topic, "id": basis_id},
        }
        return self.call("draft", "--topic", topic, data=terms)["id"]

    def tree(self, project):
        return {path.relative_to(project): (path.stat().st_mtime_ns, path.read_bytes())
                for path in project.rglob("*") if path.is_file()}

    def card(self, result, *topic, project=None):
        target = project or self.project
        before = self.tree(target)
        card = self.call("handoff", *topic, project=target)
        self.assertEqual(card["result"], result, card)
        self.assertEqual(self.tree(target), before)
        return card

    def test_owner_choice_to_checked_delivery_in_a_fresh_process_at_each_step(self):
        summary = "Use the selected wording."
        limit_one, limit_two = "Only the word 'one'.", "Only the word 'two'."
        first = self.decision(summary, authority_limit=limit_one)
        chosen = self.decision(summary, authority_limit=limit_two)
        self.call(*self.link_args())
        waiting = self.card("waiting_for_owner")
        self.assertEqual((waiting["assignee"], waiting["question"]), ("owner", QUESTION))
        self.assertEqual({entry["id"]: (entry["summary"], entry["authority_limit"]) for entry in waiting["choices"]},
                         {first: (summary, limit_one), chosen: (summary, limit_two)})
        self.assertEqual(waiting["previous_task"], PREVIOUS)
        self.assertFalse((self.project / FILE).exists())
        self.assertNotEqual(self.cli("start", chosen, "--touch", FILE).returncode, 0)

        self.call("confirm", chosen)
        self.assertEqual(self.card("needs_contract", TOPIC)["assignee"], "coordinator")
        permission = self.draft(chosen)
        self.call("confirm", permission)
        ready = self.card("start", TOPIC)
        self.assertEqual(ready["assignee"], WORKER)
        self.assertEqual(ready["permission"], permission)
        self.assertEqual(ready["previous_task"], PREVIOUS)
        self.assertEqual(ready["scope"], {"files": [FILE], "report": REPORT})
        self.assertEqual(ready["verification"], TEST)
        selected, = ready["decisions"]
        self.assertEqual((selected["id"], selected["summary"], selected["source"], selected["authority_limit"]),
                         (chosen, summary, SOURCE, limit_two))
        self.assertNotEqual(self.cli("start", permission, "--touch", "other.txt").returncode, 0)
        self.call("start", permission, "--touch", FILE)
        self.card("implement", TOPIC)
        (self.project / FILE).write_text("two", encoding="utf-8")
        self.card("verify", TOPIC)

        check = self.cli("verify", "--report", REPORT, "--name", TEST, "--", sys.executable, "-I", "-c",
                         "from pathlib import Path; assert Path('example.txt').read_text() == 'two'")
        self.assertEqual(check.returncode, 0, check.stderr)
        self.card("register_evidence", TOPIC)
        evidence = self.call("snapshot", FILE, "--artifact", REPORT)
        evidence.update(criteria=TEST, result="Example uses the selected wording.", status="pass")
        delivery = self.call("evidence", permission, data=evidence)["id"]
        review = self.card("awaiting_acceptance", TOPIC)
        self.assertEqual(review["delivery"], delivery)
        self.assertEqual(review["assignee"], "owner")
        self.assertEqual(review["permission"], permission)
        self.assertEqual(review["decisions"][0]["id"], chosen)
        self.assertEqual(review["verification"], TEST)
        report_path = self.project / REPORT
        report_bytes = report_path.read_bytes()
        self.assertEqual(review["evidence"]["artifact"], {
            "path": REPORT, "sha256": hashlib.sha256(report_bytes).hexdigest(),
        })
        self.assertEqual(review["evidence"]["result"], evidence["result"])
        self.assertEqual(review["evidence"]["files"], [{"path": FILE, "sha256": hashlib.sha256(b"two").hexdigest()}])
        held_report = self.project.parent / "held-report.json"
        report_path.rename(held_report)
        self.card("blocked", TOPIC)
        held_report.rename(report_path)
        self.card("awaiting_acceptance", TOPIC)
        self.call("accept", delivery, "--reason", "Reviewed the example.", "--source", "Owner review")
        self.assertEqual(self.card("complete", TOPIC)["delivery"], delivery)
        report_path.unlink()
        self.assertEqual(self.card("investigate", TOPIC)["delivery"], delivery)

    def test_restore_recovers_the_pending_question_and_versioned_choice(self):
        choice = self.decision("Write two.")
        link = self.call(*self.link_args())["id"]
        backup = Path(__file__).resolve().parents[1] / "tools/project_backup.py"
        archive = self.project.parent / "handoff.zip"
        target = self.project.parent / "restored"
        for args in (
            ["export", "--project", str(self.project), "--include", ".signum/state.json",
             "--optional", ".signum/handoffs.json"],
            ["restore", "--target", str(target)],
        ):
            subprocess.run([sys.executable, "-B", str(backup), *args, "--archive", str(archive)],
                           check=True, capture_output=True, timeout=10)
        restored = self.card("waiting_for_owner", project=target)
        self.assertEqual((restored["id"], restored["question"], restored["recipient"]), (link, QUESTION, WORKER))
        self.assertEqual([item["id"] for item in restored["choices"]], [choice])
        self.call("confirm", choice, project=target)
        self.card("needs_contract", project=target)
        self.card("waiting_for_owner", project=self.project)

    def test_exact_selection_project_isolation_and_link_replacement(self):
        self.card("none")
        plain_project = self.project.parent / "plain"
        plain_project.mkdir()
        self.card("disabled", project=plain_project)
        self.assertFalse((plain_project / ".signum").exists())
        self.assertNotEqual(self.cli(*self.link_args()).returncode, 0)
        self.card("none")
        self.decision("Write two.")
        original = self.call(*self.link_args())["id"]
        self.assertEqual(self.call(*self.link_args())["id"], original)
        update = self.link_args(worker="Reviewer")
        self.assertNotEqual(self.cli(*update).returncode, 0)
        replacement = self.call(*update, "--expected", original)["id"]
        self.assertNotEqual(replacement, original)
        self.assertNotEqual(self.cli(*self.link_args(), "--expected", original).returncode, 0)
        self.assertEqual(self.call(*update)["id"], replacement)
        self.call(*self.link_args(topic="revise-second-example"))
        self.card("ambiguous")
        self.card("none", "example")
        self.card("waiting_for_owner", TOPIC)
        self.card("disabled", project=plain_project)
        self.assertFalse((plain_project / ".signum").exists())

    def test_current_permission_must_depend_on_the_linked_decision_version(self):
        selected = self.decision("Write two.")
        self.call("confirm", selected)
        other = self.decision("Use a heading.", topic="headings")
        self.call("confirm", other)
        self.call(*self.link_args())
        cross_linked = self.draft(other, basis_topic="headings")
        self.call("confirm", cross_linked)
        self.card("blocked", TOPIC)
        self.assertNotEqual(self.cli(*self.link_args(topic="other", decision=TOPIC)).returncode, 0)

        valid_topic = "second-example"
        self.call(*self.link_args(topic=valid_topic))
        permission = self.draft(selected, topic=valid_topic)
        self.call("confirm", permission)
        self.card("start", valid_topic)
        newer = self.decision("Write three.", replaces=selected)
        self.call("confirm", newer)
        self.card("blocked", valid_topic)

    def test_new_proposal_blocks_the_linked_start_until_withdrawal(self):
        selected = self.decision("Write two.")
        self.call("confirm", selected)
        self.call(*self.link_args())
        permission = self.draft(selected)
        self.call("confirm", permission)
        unrelated = self.decision("Use a heading.", topic="headings")
        self.call("confirm", unrelated)
        neighbor = self.draft(unrelated, basis_topic="headings", topic="add-heading",
                              file="heading.txt", report="heading-check.json")
        self.call("confirm", neighbor)
        self.card("start", TOPIC)

        alternative = self.decision("Write three.", replaces=selected)
        pending = self.card("waiting_for_owner", TOPIC)
        self.assertEqual([choice["id"] for choice in pending["choices"]], [alternative])
        self.assertEqual(self.call("next", "--query", TOPIC)["result"], "blocked")
        before = self.tree(self.project)
        self.assertNotEqual(self.cli("start", permission, "--touch", FILE).returncode, 0)
        self.assertEqual(self.tree(self.project), before)
        self.assertEqual(self.call("next", "--query", "add-heading")["result"], "start")
        self.call("start", neighbor, "--touch", "heading.txt")
        self.assertEqual(self.call("next", "--query", "add-heading")["result"], "implement")

        self.call("withdraw", alternative, "--reason", "Keep the original wording.")
        self.assertEqual(self.card("start", TOPIC)["permission"], permission)
        self.assertEqual(self.call("next", "--query", TOPIC)["result"], "start")
        self.call("start", permission, "--touch", FILE)
        self.card("implement", TOPIC)

    def test_a_later_wrong_kind_on_either_linked_topic_blocks_the_card(self):
        other = self.decision("Use a heading.", topic="headings")
        self.call("confirm", other)
        self.decision("Write two.")
        self.call(*self.link_args())
        self.card("waiting_for_owner", TOPIC)

        wrong_task = self.decision("Pick an example.", topic=TOPIC)
        self.card("blocked", TOPIC)
        self.call("withdraw", wrong_task, "--reason", "Use a permission for this task.")
        self.card("waiting_for_owner", TOPIC)

        self.draft(other, basis_topic="headings", topic=DECISION)
        self.card("blocked", TOPIC)


if __name__ == "__main__":
    unittest.main()
