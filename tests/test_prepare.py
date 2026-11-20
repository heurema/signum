from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SIGNUM = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"
TOPIC = "review-trigger"
CURRENT = "Разбирать заявки вручную."
PROPOSED = "Разбирать заявки только по будням."


class PrepareTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.state = self.project / ".signum/state.json"
        self.call("init")

    def command(self, *args, data=None):
        return subprocess.run(
            [sys.executable, "-B", str(SIGNUM), "--project", str(self.project), *args],
            input=None if data is None else json.dumps(data, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )

    def call(self, *args, data=None):
        result = self.command(*args, data=data)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_denied(self, *args, data):
        before = self.state.read_bytes() if self.state.exists() else None
        result = self.command(*args, data=data)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.state.read_bytes() if self.state.exists() else None, before)

    def payload(self, summary=PROPOSED, **fields):
        return {
            "summary": summary,
            "source": "Owner discussion",
            "reason": "Record the rule under review",
            **fields,
        }

    def record(self, summary=CURRENT, **fields):
        return {
            **self.payload(summary),
            "topic": TOPIC,
            "kind": "decision",
            "replaces": None,
            **fields,
        }

    def confirm_record(self, record):
        if record["kind"] == "permission" and "basis" not in record:
            record = {**record, "owner_turn": "codex:turn:11111111-1111-4111-8111-111111111111"}
        proposal = self.call("propose", data=record)
        return self.call("confirm", proposal["id"])["id"]

    def prepare(self, **fields):
        return self.call("prepare", "--topic", TOPIC, data=self.payload(**fields))

    def events(self):
        return json.loads(self.state.read_bytes())["events"]

    def assent(self, receipt):
        proposal = receipt["proposal"]
        return {"id": proposal["id"], "shown": proposal["summary"], "reply": "Согласен"}

    def test_prepare_binds_exact_card_and_leaves_grant_active_until_assent(self):
        current = self.confirm_record(self.record())
        permission_topic = "read-queue"
        permission = self.confirm_record(self.record(
            "Read the queue.",
            topic=permission_topic,
            kind="permission",
            basis={"topic": TOPIC, "id": current},
        ))
        prior_events = self.events()
        prior_grant = self.call("status", "--topic", permission_topic)
        receipt = self.prepare()
        pending = receipt["proposal"]["id"]
        self.assertEqual(receipt["result"], "prepared")
        self.assertEqual(receipt["current"], {"id": current, "summary": CURRENT})
        self.assertEqual(receipt["proposal"], {"id": pending, "summary": PROPOSED})
        self.assertNotEqual(pending, current)
        self.assertEqual(self.events()[:-1], prior_events)
        proposed = self.events()[-1]
        self.assertEqual((proposed["event"], proposed["id"]), ("proposed", pending))
        self.assertEqual(proposed["record"], self.record(PROPOSED, replaces=current))
        self.assertEqual(self.call("status", "--topic", permission_topic), prior_grant)
        status = self.call("status", "--topic", TOPIC)
        self.assertEqual([(item["id"], item["state"]) for item in status["confirmed"]], [(current, "current")])
        self.assertEqual([(item["id"], item["state"]) for item in status["proposals"]], [(pending, "pending")])

        assent = self.assent(receipt)
        self.assert_denied("assent", data={**assent, "shown": PROPOSED + " Или вечером."})
        self.assert_denied("assent", data={**assent, "reply": "Согласен, если заявки срочные."})
        prior_events = self.events()
        result = self.call("assent", data=assent)
        self.assertEqual((result["result"], result["id"]), ("confirmed", pending))
        self.assertEqual(self.events()[:-1], prior_events)
        confirmed = self.events()[-1]
        self.assertEqual((confirmed["event"], confirmed["id"]), ("confirmed", pending))
        grant, = self.call("status", "--topic", permission_topic)["confirmed"]
        self.assertEqual((grant["id"], grant["state"]), (permission, "stale"))

    def test_new_topic_requires_explicit_null_and_current_duplicate_is_read_only(self):
        self.assert_denied("prepare", "--topic", TOPIC, data=self.payload())
        self.assert_denied("prepare", "--topic", TOPIC, data=self.payload(expected="1" * 24))
        receipt = self.prepare(expected=None)
        self.assertEqual(receipt["result"], "prepared")
        self.assertIsNone(receipt["current"])
        identifier = receipt["proposal"]["id"]
        self.assertEqual(receipt["proposal"], {"id": identifier, "summary": PROPOSED})
        proposed, = self.events()
        self.assertEqual(proposed["record"], self.record(PROPOSED))
        self.call("assent", data=self.assent(receipt))
        before = self.state.read_bytes()

        for fields in ({}, {"expected": identifier}, {"source": "Another owner note"}):
            with self.subTest(fields=fields):
                self.assertEqual(self.prepare(**fields), {
                    "result": "already_current",
                    "current": receipt["proposal"],
                    "proposal": None,
                })
                self.assertEqual(self.state.read_bytes(), before)
        self.assert_denied("prepare", "--topic", TOPIC, data=self.payload(expected=None))

    def test_mismatched_expected_and_stale_card_are_rejected_without_writing(self):
        old = self.confirm_record(self.record())
        receipt = self.prepare(expected=old)
        updated_summary = "Разбирать заявки утром."
        current = self.confirm_record(self.record(updated_summary, replaces=old))
        self.assertNotEqual(current, old)
        for summary in (PROPOSED, updated_summary):
            with self.subTest(summary=summary):
                self.assert_denied("prepare", "--topic", TOPIC, data=self.payload(summary, expected=old))
        for summary in (PROPOSED, PROPOSED.replace(" только", "\nтолько")):
            with self.subTest(stale_wording=summary):
                self.assert_denied("prepare", "--topic", TOPIC, data=self.payload(summary))
        self.assert_denied("assent", data=self.assent(receipt))

        fresh = self.prepare(expected=current)
        self.assertEqual(fresh["current"], {"id": current, "summary": updated_summary})
        self.assertNotEqual(fresh["proposal"]["id"], receipt["proposal"]["id"])
        self.assertEqual(self.events()[-1]["record"], self.record(PROPOSED, replaces=current))
        self.assert_denied("assent", data=self.assent(receipt))
        before = self.state.read_bytes()
        self.assertEqual(self.prepare(), fresh)
        self.assertEqual(self.state.read_bytes(), before)

    def test_rich_and_nondecision_current_rules_require_focused_review(self):
        for topic, fields in (
            ("scoped", {"scope": "Weekdays only"}),
            ("questions", {"questions": ["Which requests?"]}),
            ("verified", {"verification": "Read the queue at noon"}),
            ("limited", {"authority_limit": "No external access"}),
            ("permission", {"kind": "permission"}),
        ):
            with self.subTest(topic=topic):
                self.confirm_record(self.record(topic=topic, **fields))
                self.assert_denied("prepare", "--topic", topic, data=self.payload())

    def test_existing_competing_or_rich_proposals_require_focused_review(self):
        current = self.confirm_record(self.record())
        rich = self.call("propose", data=self.record(PROPOSED, replaces=current, scope="Weekdays only"))
        self.assert_denied("prepare", "--topic", TOPIC, data=self.payload())
        self.call("withdraw", rich["id"], "--reason", "Request a plain proposal")
        self.prepare()
        self.call("propose", data=self.record("Разбирать заявки утром.", replaces=current))
        self.assert_denied("prepare", "--topic", TOPIC, data=self.payload())

    def test_identical_concurrent_preparations_and_retry_reuse_one_proposal(self):
        current = self.confirm_record(self.record())
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.prepare, expected=current) for _ in range(2)]
            receipts = [future.result(timeout=12) for future in futures]
        self.assertEqual(receipts[0], receipts[1])
        identifier = receipts[0]["proposal"]["id"]
        self.assertEqual([(event["event"], event["id"]) for event in self.events()], [
            ("proposed", current), ("confirmed", current), ("proposed", identifier),
        ])
        before = self.state.read_bytes()
        self.assertEqual(self.prepare(
            expected=current, source="Another owner note", reason="Retry the displayed rule",
        ), receipts[0])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.events()[-1]["record"], self.record(PROPOSED, replaces=current))

    def test_different_concurrent_preparations_leave_one_pending_card(self):
        current = self.confirm_record(self.record())
        candidates = (PROPOSED, "Разбирать заявки утром.")
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(
                self.command, "prepare", "--topic", TOPIC,
                data=self.payload(summary, expected=current),
            ) for summary in candidates]
            results = [future.result(timeout=12) for future in futures]
        self.assertCountEqual([result.returncode for result in results], [0, 1])
        winner = next(index for index, result in enumerate(results) if result.returncode == 0)
        receipt = json.loads(results[winner].stdout)
        self.assertEqual(receipt["proposal"]["summary"], candidates[winner])
        self.assertEqual(results[1 - winner].stdout, "")
        self.assertEqual([(event["event"], event["id"]) for event in self.events()], [
            ("proposed", current), ("confirmed", current), ("proposed", receipt["proposal"]["id"]),
        ])


if __name__ == "__main__":
    unittest.main()
