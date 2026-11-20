import argparse
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"
sys.path.insert(0, str(SCRIPTS))
import decision_capture
import signum


class DecisionCaptureTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / ".signum"
        self.path = self.directory / "state.json"
        signum.run(argparse.Namespace(project=self.root, command="init"))
        self.thread = str(uuid4())
        self.sources = {}

    def turn(self, user, assistant="", **kwargs):
        turn = str(uuid4())
        self.sources[turn] = dict(thread=self.thread, turn=turn, user=user, assistant=assistant,
                                  user_id=str(uuid4()), assistant_id=str(uuid4()) if assistant else "",
                                  allow_owner=True, started_at=len(self.sources) + 100,
                                  completed=bool(assistant), previous_turn=None)
        self.sources[turn].update(kwargs)
        return turn

    def read(self, project, thread, turn):
        if project != self.root or thread != self.thread:
            raise ValueError("Different project or thread.")
        return self.sources[turn]

    def capture(self, turn, *choices):
        payload = {"thread": self.thread, "turn": turn, "choices": list(choices)}
        return decision_capture.capture(signum, self.root, self.directory, payload, self.read)

    def item(self, topic, quote, state="chosen", summary=None, expected=None):
        return {"topic": topic, "quote": quote, "summary": summary or quote, "state": state, "expected": expected}

    def journal(self):
        return signum.Journal.read(self.path, required=True)

    def test_split_choice_records_source_without_granting_permission(self):
        turn = self.turn("SQLx подходит, откат пока оставим открытым", "Обсудим отдельно.")
        accepted = self.item("migrations", "SQLx подходит")
        pending = self.item("rollback", "откат пока оставим открытым", "open", "Выбрать политику отката")
        result = self.capture(turn, accepted, pending)
        before = self.path.read_bytes()
        self.assertEqual(result, self.capture(turn, pending, accepted))
        self.assertEqual(before, self.path.read_bytes())
        journal = self.journal()
        self.assertEqual(len(journal.events), 3)
        self.assertEqual(journal.status()["proposals"][0]["topic"], "rollback")
        chosen = journal.status()["confirmed"][0]
        self.assertEqual(chosen["summary"], "SQLx подходит")
        self.assertIn(self.sources[turn]["user_id"], chosen["owner_evidence"])
        self.assertEqual(journal.contract()["draft"]["scope"], [])
        self.assertEqual(len(journal.reviewed), 1)

    def test_clear_choices_across_domains_and_qualified_counterexamples(self):
        cases = [
            ("SQLx подходит, откат обсудим позже", "SQLx подходит", "chosen"),
            ("Переходим на Protobuf.", "Переходим на Protobuf.", "chosen"),
            ("We use Monday for weekly reviews.", "We use Monday for weekly reviews.", "chosen"),
            ("SQLx подходит только для теста.", "SQLx подходит", "open"),
            ("We use Monday for weekly reviews if everyone agrees.", "We use Monday for weekly reviews", "open"),
        ]
        for i, (wording, quote, expected_state) in enumerate(cases):
            with self.subTest(wording=wording):
                topic = f"example-{i}"
                result = self.capture(self.turn(wording), self.item(topic, quote))
                self.assertEqual(result["choices"][topic]["state"], expected_state)

    def test_unsupported_answers_and_untrusted_quotes_stay_open(self):
        samples = [
            ("SQLx подходит?", "SQLx подходит", "SQLx подходит", True),
            ("Например, SQLx подходит", "SQLx подходит", "SQLx подходит", True),
            ("SQLx подходит, но не для миграций", "SQLx подходит", "SQLx подходит", True),
            ("Что дальше?", "SQLx подходит", "SQLx подходит", True),
            ("SQLx подходит", "SQLx подходит", "SQLx подходит и мы отменяем откаты", True),
            ("SQLx подходит", "SQLx подходит", "SQLx подходит", False),
            ("Она пишет «SQLx подходит».", "SQLx подходит", "SQLx подходит", True),
        ]
        for n, (user, quote, summary, owner) in enumerate(samples):
            with self.subTest(user=user):
                turn = self.turn(user, "SQLx подходит", allow_owner=owner)
                topic = f"topic-{n}"
                self.assertEqual(self.capture(turn, self.item(topic, quote, summary=summary))
                                 ["choices"][topic]["state"], "open")
        self.assertFalse(self.journal().confirmed)

    def test_atomic_invalid_batch_and_inconsistent_replay(self):
        turn = self.turn("Берём SQLx.")
        item = self.item("migration", "Берём SQLx.")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(signum.SignumError, "absent"):
            self.capture(turn, item, self.item("problem", "absent"))
        self.assertEqual(before, self.path.read_bytes())
        self.capture(turn, item)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(signum.SignumError, "different interpretation"):
            self.capture(turn, dict(item, summary="Surprise change"))
        self.assertEqual(before, self.path.read_bytes())

    def test_receipt_tracks_arriving_final_without_rewriting_chosen(self):
        turn = self.turn("Берём SQLx.")
        self.capture(turn, self.item("migration", "Берём SQLx."))
        self.assertFalse(self.journal().reviewed[(self.thread, turn)]["completed"])
        self.sources[turn].update(assistant="Политика отката обсуждается.", assistant_id="answer-id", completed=True)
        self.capture(turn, self.item("rollback", "Политика отката обсуждается.", "open"))
        receipt = self.journal().reviewed[(self.thread, turn)]
        self.assertTrue(receipt["completed"])
        self.assertEqual(set(receipt["choices"]), {"rollback", "migration"})
        self.sources[turn]["assistant"] = "Changed"
        with self.assertRaisesRegex(signum.SignumError, "content changed"):
            self.capture(turn)

    def test_short_assent_uses_single_previous_display_and_concrete_version(self):
        shown = "Use SQLx for migrations."
        offer = self.turn("Предложи инструмент.", "Предлагаю: **" + shown + "**\n\nСогласны?")
        self.capture(offer, self.item("migration", shown, "open"))
        turn = self.turn("Да", "", previous_turn=offer)
        self.assertEqual(self.capture(turn, self.item("migration", "Да", summary=shown))
                         ["choices"]["migration"]["state"], "chosen")
        self.assertIn(self.sources[turn]["user_id"], self.journal().status()["confirmed"][0]["owner_evidence"])

    def test_ambiguous_assent_after_prose_with_two_questions_stays_open(self):
        shown = "Use SQLx for migrations."
        offer = self.turn("С чего начать?", shown + " Also deploy now?")
        self.capture(offer, self.item("migration", shown, "open"))
        turn = self.turn("Да", previous_turn=offer)
        self.assertEqual(self.capture(turn, self.item("migration", "Да", summary=shown))
                         ["choices"]["migration"]["state"], "open")

    def test_chronologically_late_import_cannot_overwrite_newer_decision(self):
        old = self.turn("Берём Dbmate.")
        new = self.turn("Берём SQLx.")
        version = self.capture(new, self.item("migration", "Берём SQLx."))["choices"]["migration"]["id"]
        with self.assertRaisesRegex(signum.SignumError, "older"):
            self.capture(old, self.item("migration", "Берём Dbmate.", expected=version))
        third = self.turn("Берём Flyway.")
        self.assertEqual(self.capture(third, self.item("migration", "Берём Flyway.", expected=version))
                         ["choices"]["migration"]["state"], "chosen")

    def test_old_matching_observation_cannot_backdate_a_manual_confirmation(self):
        journal = self.journal()
        record = {"topic": "migration", "kind": "decision", "summary": "Берём SQLx.",
                  "replaces": None, "source": "Recorded separately", "reason": "Manual decision"}
        current = journal.propose(record)["id"]
        journal.confirm(current)
        signum.write_journal(self.path, journal)
        self.capture(self.turn("Берём SQLx."), self.item("migration", "Берём SQLx.", expected=current))
        with self.assertRaisesRegex(signum.SignumError, "older"):
            self.capture(self.turn("Берём Dbmate."), self.item("migration", "Берём Dbmate.", expected=current))
        self.assertEqual(self.journal().confirmed["migration"], current)

    def test_empty_review_keeps_only_origin_and_fingerprint(self):
        turn = self.turn("Private routine wording", "Private routine answer")
        self.capture(turn)
        self.assertNotIn(b"Private routine", self.path.read_bytes())
        self.assertEqual(self.journal().reviewed[(self.thread, turn)]["choices"], {})

    def test_parallel_retries_produce_one_version(self):
        turn = self.turn("SQLx подходит.")
        item = self.item("migration", "SQLx подходит.")
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: self.capture(turn, item), range(3)))
        self.assertEqual(results[0], results[-1])
        self.assertEqual(len(self.journal().events), 2)
        self.assertEqual(len(self.journal().reviewed), 1)

    def test_cli_uses_verified_project_before_requesting_history(self):
        from types import SimpleNamespace
        turn = self.turn("Что дальше?")
        data = json.dumps({"thread": self.thread, "turn": turn, "choices": []})
        with patch.dict(sys.modules, {"chat_source": SimpleNamespace(read_turn=self.read, ChatSourceError=RuntimeError)}):
            with patch("sys.stdin", StringIO(data)):
                self.assertEqual(signum.run(argparse.Namespace(project=self.root, command="capture"))["result"], "reviewed")
            elsewhere = self.root / "other"
            elsewhere.mkdir()
            with patch("sys.stdin", StringIO(data)), self.assertRaisesRegex(signum.SignumError, "disabled"):
                signum.run(argparse.Namespace(project=elsewhere, command="capture"))
            signum.run(argparse.Namespace(project=elsewhere, command="init"))
            with patch("sys.stdin", StringIO(data)), self.assertRaisesRegex(ValueError, "Different project"):
                signum.run(argparse.Namespace(project=elsewhere, command="capture"))
            self.assertFalse(signum.Journal.read(elsewhere / ".signum/state.json").events)
