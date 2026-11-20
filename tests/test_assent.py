from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import unicodedata


SIGNUM = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"


class AssentTest(unittest.TestCase):
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

    def decision(self, summary="Разбирать заявки вручную.", **fields):
        return {
            "topic": "review-trigger",
            "kind": "decision",
            "summary": summary,
            "source": "Owner discussion",
            "reason": "Record the agreed rule",
            "replaces": None,
            **fields,
        }

    def propose(self, record):
        if record["kind"] == "permission":
            record = {**record, "owner_turn": "codex:turn:11111111-1111-4111-8111-111111111111"}
        return self.call("propose", data=record)["id"]

    def late(self, record):
        return {"reply": "Согласен", "shown": record["summary"], "record": record}

    def staged(self, record):
        return {"reply": "Согласен", "shown": record["summary"], "id": self.propose(record)}

    def journal_bytes(self):
        return self.state.read_bytes() if self.state.exists() else None

    def assert_denied(self, assent):
        before = self.journal_bytes()
        result = self.command("assent", data=assent)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(self.journal_bytes(), before)

    def assert_confirmation(self, identifier, record):
        events = json.loads(self.journal_bytes())["events"]
        self.assertEqual([(entry["event"], entry["id"]) for entry in events], [
            ("proposed", identifier), ("confirmed", identifier),
        ])
        self.assertEqual(events[0]["record"], record)
        entry, = self.call("status")["confirmed"]
        self.assertEqual(entry["id"], identifier)
        self.assertEqual(entry["state"], "current")

    def test_staged_assent_confirms_exact_proposal_and_retries_without_a_write(self):
        record = self.decision()
        assent = self.staged(record)
        receipt = self.call("assent", data=assent)
        self.assertEqual(receipt["result"], "confirmed")
        self.assertEqual(receipt["id"], assent["id"])
        self.assert_confirmation(assent["id"], record)
        before = self.journal_bytes()
        retry = self.call("assent", data=assent)
        self.assertEqual(retry["result"], "already_current")
        self.assertEqual(retry["id"], assent["id"])
        self.assertEqual(self.journal_bytes(), before)

    def test_concurrent_late_assent_records_one_proposal_and_one_confirmation(self):
        record = self.decision()
        assent = self.late(record)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(self.command, "assent", data=assent) for _ in range(2)]
            for future in results:
                result = future.result(timeout=12)
                self.assertEqual(result.returncode, 0, result.stderr)
        entry, = self.call("status")["confirmed"]
        self.assert_confirmation(entry["id"], record)
        before = self.journal_bytes()
        self.call("assent", data=assent)
        self.assertEqual(self.journal_bytes(), before)

    def test_conditional_reply_or_changed_displayed_text_leaves_current_rule(self):
        current = self.propose(self.decision())
        self.call("confirm", current)
        record = self.decision("Разбирать заявки только по будням.", replaces=current)
        for route in (self.late, self.staged):
            assent = route(record)
            for changes in (
                {"reply": "Согласен, если ночью будет автоматическая сортировка."},
                {"reply": "Согласен. Добавь ночную сортировку."},
                {"reply": "Не согласен"},
                {"shown": "Разбирать заявки по будням."},
                {"shown": record["summary"] + " Ночная сортировка допускается."},
            ):
                with self.subTest(route=route.__name__, changes=changes):
                    self.assert_denied({**assent, **changes})
        entry, = self.call("status")["confirmed"]
        self.assertEqual(entry["id"], current)

    def test_display_wrapping_and_unicode_spelling_preserve_the_rule(self):
        record = self.decision("Вернуть предложение при следующей проверке.")
        assent = self.late(record)
        assent["shown"] = unicodedata.normalize("NFD", "  Вернуть предложение\nпри следующей проверке.  ")
        receipt = self.call("assent", data=assent)
        self.assert_confirmation(receipt["id"], record)

    def test_competing_pending_versions_require_a_separate_decision(self):
        current = self.propose(self.decision())
        self.call("confirm", current)
        first = self.decision("Разбирать заявки по будням.", replaces=current)
        second = self.decision("Разбирать заявки каждый вечер.", replaces=current)
        staged = self.staged(first)
        self.assert_denied(self.late(second))
        self.propose(second)
        self.assert_denied(staged)
        self.assert_denied(self.late(first))
        self.assertEqual(self.call("confirm", staged["id"])["id"], staged["id"])

    def test_stale_predecessor_cannot_add_a_confirmation_or_late_proposal(self):
        original = self.propose(self.decision())
        self.call("confirm", original)
        old = self.decision("Разбирать заявки по будням.", replaces=original)
        staged = self.staged(old)
        newer = self.propose(self.decision("Разбирать заявки утром.", replaces=original))
        self.call("confirm", newer)
        self.assert_denied(staged)
        self.assert_denied(self.late(old))
        self.assert_denied(self.late(self.decision("Разбирать заявки вечером.", replaces=original)))

    def test_replay_of_a_superseded_confirmation_is_denied(self):
        original = self.decision()
        staged = self.staged(original)
        self.call("assent", data=staged)
        newer = self.decision("Разбирать заявки утром.", replaces=staged["id"])
        current = self.call("assent", data=self.late(newer))["id"]
        self.assert_denied(staged)
        self.assert_denied(self.late(original))
        reoffer = {**original, "replaces": current, "reason": "Restore the original wording"}
        self.assert_denied(self.late(reoffer))
        replacement = self.staged(reoffer)
        self.assert_denied(staged)
        self.call("assent", data=replacement)
        self.assert_denied(staged)
        self.assert_denied(self.late(reoffer))

    def test_withdrawn_proposal_requires_a_new_explicitly_displayed_id(self):
        record = self.decision()
        old = self.staged(record)
        self.call("withdraw", old["id"], "--reason", "Displayed revision withdrawn")
        self.assert_denied(old)
        self.assert_denied(self.late(record))
        replacement = self.staged(record)
        self.assertNotEqual(old["id"], replacement["id"])
        self.assert_denied(old)
        self.assert_denied(self.late(record))
        self.call("assent", data=replacement)
        self.assert_denied(old)
        self.assert_denied(self.late(record))

    def test_fast_path_is_limited_to_compact_decisions(self):
        permission = self.decision("Run the local trial.", topic="trial", kind="permission")
        accepted_permission = self.propose(self.decision("Read the queue.", topic="queue", kind="permission"))
        self.call("confirm", accepted_permission)
        check = self.decision("Read the queue.", topic="queue-check", kind="check", basis={
            "topic": "queue", "id": accepted_permission,
        })
        for record in (
            permission,
            check,
            self.decision(topic="scoped", scope="Weekdays only"),
            self.decision(topic="questions", questions=["Which requests?"]),
            self.decision(topic="verified", verification="Check the queue at noon"),
            self.decision(topic="limited", authority_limit="No external access"),
        ):
            for route in (self.late, self.staged):
                with self.subTest(record=record, route=route.__name__):
                    self.assert_denied(route(record))


if __name__ == "__main__":
    unittest.main()
