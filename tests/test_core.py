import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts/signum.py"


def record(topic, kind, summary, **fields):
    source = "Owner discussion, note 1"
    if kind == "permission":
        source = f"codex:turn:11111111-1111-4111-8111-111111111111\n\n{source}"
    return {
        "topic": topic,
        "kind": kind,
        "summary": summary,
        "source": source,
        "reason": "Record the original scope",
        "replaces": None,
        **fields,
    }


class CoreTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.state = self.project / ".signum/state.json"
        self.call("init")

    def command(self, *args, data=None, project=None):
        return subprocess.run(
            [sys.executable, "-B", str(CLI), "--project", str(project or self.project), *args],
            input=None if data is None else json.dumps(data, ensure_ascii=False),
            capture_output=True,
            encoding="utf-8",
            timeout=10,
        )

    def call(self, *args, **kwargs):
        result = self.command(*args, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def propose(self, data):
        return self.call("propose", data=data)

    def commit(self, data):
        proposal = self.propose(data)
        return self.call("confirm", proposal["id"])["id"]

    def test_proposal_is_visible_before_exact_confirmation_and_correction_retains_history(self):
        instructions = self.project / "AGENTS.md"
        instructions.write_text("Existing project instructions\n", encoding="utf-8")
        self.assertEqual(self.call("status"), {"enabled": True, "confirmed": [], "proposals": []})
        self.assertEqual(self.call("contract")["draft"]["scope"], [])
        self.assertEqual(json.loads(self.state.read_bytes())["events"], [])

        initial = record(
            "purpose",
            "decision",
            "Цель — черновик спецификации.",
            questions=["Где хранить записи?"],
        )
        payload = self.project / "payload.json"
        payload.write_text(json.dumps(initial, ensure_ascii=False), encoding="utf-8")
        proposed = self.call("propose", "--input", str(payload))
        identifier = proposed["id"]
        self.assertEqual(proposed["state"], "pending")
        self.assertEqual(self.call("status")["proposals"], [proposed])
        self.assertEqual(self.call("status")["confirmed"], [])
        self.assertIn(initial["summary"], self.state.read_text(encoding="utf-8"))
        self.assertEqual(self.propose(initial)["id"], identifier)
        self.assertEqual(len(self.call("history")["events"]), 1)
        self.assertEqual(self.command("confirm", identifier[:-1]).returncode, 1)
        self.assertEqual(self.call("status")["confirmed"], [])

        confirmed = self.call("confirm", identifier)
        self.assertEqual(confirmed["state"], "current")
        self.assertEqual(self.call("confirm", identifier), confirmed)
        self.assertEqual(len(self.call("history")["events"]), 2)
        correction = {
            **initial,
            "summary": "Цель — черновик одного контракта.",
            "source": "Поправка владельца, заметка 2",
            "reason": "Исправлено слишком широкое описание результата",
            "replaces": identifier,
        }
        revised = self.commit(correction)
        self.assertEqual(self.command("confirm", identifier).returncode, 1)
        active = self.call("status")["confirmed"]
        self.assertEqual([entry["id"] for entry in active], [revised])
        self.assertEqual(active[0]["questions"], initial["questions"])
        self.assertEqual(active[0]["reason"], correction["reason"])
        journal = json.loads(self.state.read_text(encoding="utf-8"))
        expected = [
            ("proposed", identifier),
            ("confirmed", identifier),
            ("proposed", revised),
            ("confirmed", revised),
        ]
        self.assertEqual([(item["event"], item["id"]) for item in journal["events"]], expected)
        self.assertEqual(journal["events"][0]["record"], initial)
        self.assertEqual(journal["events"][2]["record"], correction)
        self.assertEqual(instructions.read_text(encoding="utf-8"), "Existing project instructions\n")

    def test_changed_pending_content_does_not_match_the_confirmed_id(self):
        identifier = self.propose(record("scope", "decision", "Prepare a draft"))["id"]
        journal = json.loads(self.state.read_text(encoding="utf-8"))
        journal["events"][0]["record"]["summary"] = "Publish the draft"
        modified = json.dumps(journal)
        self.state.write_text(modified, encoding="utf-8")

        result = self.command("confirm", identifier)
        self.assertEqual(result.returncode, 1)
        self.assertIn("content does not match its ID", result.stderr)
        self.assertEqual(self.state.read_text(encoding="utf-8"), modified)

    def test_withdrawal_keeps_old_id_inactive_when_identical_text_is_proposed_again(self):
        data = record("correction", "decision", "Review the draft")
        original = self.propose(data)["id"]
        self.assertEqual(self.propose(data)["id"], original)
        self.assertEqual(self.command("withdraw", original, "--reason", " ").returncode, 1)
        reason = "Withdraw the earlier wording review"
        withdrawn = self.call("withdraw", original, "--reason", reason)
        self.assertEqual(self.call("withdraw", original, "--reason", reason), withdrawn)
        self.assertEqual(self.call("status")["proposals"], [])

        fresh = self.propose(data)["id"]
        self.assertNotEqual(fresh, original)
        self.assertEqual(self.propose(data)["id"], fresh)
        delayed_confirmation = self.command("confirm", original)
        self.assertEqual(delayed_confirmation.returncode, 1)
        self.assertIn("has been withdrawn", delayed_confirmation.stderr)
        self.assertEqual(self.call("status")["confirmed"], [])
        self.call("confirm", fresh)
        self.assertEqual(self.command("withdraw", fresh, "--reason", reason).returncode, 1)
        history = self.call("history", "--topic", "correction")["events"]
        self.assertEqual([(item["event"], item["id"]) for item in history], [
            ("proposed", original),
            ("withdrawn", original),
            ("proposed", fresh),
            ("confirmed", fresh),
        ])
        self.assertEqual(history[1]["reason"], reason)

    def test_older_proposal_ids_and_confirmations_remain_readable(self):
        identifier = "166ba7d3ba6855dd68aaab7d"
        data = record("legacy", "decision", "Old draft")
        self.state.write_text(json.dumps({
            "schema_version": 1,
            "events": [{
                "event": "proposed",
                "id": identifier,
                "at": "2000-01-01T00:00:00+00:00",
                "record": data,
            }],
        }), encoding="utf-8")
        self.assertEqual(self.propose(data)["id"], identifier)
        self.call("confirm", identifier)
        self.assertEqual(self.call("status")["confirmed"][0]["id"], identifier)
        self.assertEqual(len(self.call("history")["events"]), 2)

    def test_old_path_permission_and_check_stay_bound_to_their_original_basis(self):
        old = self.commit(record("location", "decision", "Use the old checkout"))
        p1 = self.commit(record(
            "P", "permission", "Prepare the section in old/docs/v2",
            basis={"topic": "location", "id": old},
            scope="old/docs/v2",
            verification="Check each relative link from old/docs/v2",
        ))
        q = self.commit(record(
            "Q", "permission", "Describe the handoff to another chat",
            authority_limit="Text only",
        ))
        checked = self.commit(record(
            "links", "check", "Links worked in the old draft",
            basis={"topic": "P", "id": p1},
            scope="old/docs/v2",
        ))
        stale_candidate = self.propose(record(
            "P", "permission", "Update the old section",
            replaces=p1,
            basis={"topic": "location", "id": old},
        ))["id"]
        self.propose(record("storage", "decision", "Use SQLite and MCP"))
        new = self.commit(record(
            "location", "decision", "Use the new checkout",
            replaces=old,
            source="Later owner correction",
            reason="Use the new checkout",
            questions=["Which local storage should we use?"],
        ))

        contract = self.call("contract")
        expected_current = {
            "location": (new, "current"),
            "P": (p1, "stale"),
            "Q": (q, "current"),
            "links": (checked, "stale"),
        }
        self.assertEqual(
            {item["topic"]: (item["id"], item["state"]) for item in contract["confirmed"]},
            expected_current,
        )
        self.assertEqual(
            {item["topic"]: item["state"] for item in contract["proposals"]},
            {"P": "conflict", "storage": "pending"},
        )
        self.assertEqual(
            [(item["topic"], item["summary"]) for item in contract["draft"]["scope"]],
            [("Q", "Describe the handoff to another chat")],
        )
        self.assertEqual(contract["draft"]["scope"][0]["authority_limit"], "Text only")
        self.assertIn("New work and releases need their own authorization", contract["draft"]["authority_limit"])
        self.assertEqual({item["topic"] for item in contract["draft"]["blocked"]}, {"P", "links"})
        self.assertIn(
            ["Which local storage should we use?"],
            [item.get("questions") for item in contract["confirmed"]],
        )
        p_history = self.call("history", "--topic", "P")["events"]
        self.assertEqual(p_history[0]["record"]["scope"], "old/docs/v2")
        self.assertEqual(self.command("confirm", stale_candidate).returncode, 1)
        self.assertEqual(self.command("confirm", p1).returncode, 1)

        p2 = self.commit(record(
            "P", "permission", "Prepare the section in new/docs",
            replaces=p1,
            basis={"topic": "location", "id": new},
            scope="new/docs",
            verification="Check each relative link from new/docs",
        ))
        contract = self.call("contract")
        self.assertEqual({item["id"] for item in contract["draft"]["scope"]}, {p2, q})
        self.assertEqual([item["id"] for item in contract["draft"]["blocked"]], [checked])
        self.assertEqual(contract["draft"]["missing_verification"], [{"topic": "Q", "id": q}])
        self.assertTrue(contract["draft"]["verification_note"])
        scope_by_topic = {item["topic"]: item for item in contract["draft"]["scope"]}
        self.assertEqual(scope_by_topic["Q"], {
            "id": q,
            "topic": "Q",
            "summary": "Describe the handoff to another chat",
            "scope": None,
            "verification": None,
            "authority_limit": "Text only",
        })
        self.assertEqual(scope_by_topic["P"], {
            "id": p2,
            "topic": "P",
            "summary": "Prepare the section in new/docs",
            "scope": "new/docs",
            "verification": "Check each relative link from new/docs",
            "authority_limit": None,
        })
        self.assertEqual(self.call("contract", "--topic", "P")["draft"]["missing_verification"], [])
        fresh_check = self.commit(record(
            "links", "check", "One relative link is still broken in the revised draft",
            replaces=checked,
            basis={"topic": "P", "id": p2},
            scope="new/docs",
        ))
        fresh_status = self.call("status", "--topic", "links")["confirmed"]
        self.assertEqual([(item["id"], item["state"]) for item in fresh_status], [(fresh_check, "current")])
        self.assertEqual(fresh_status[0]["summary"], "One relative link is still broken in the revised draft")
        self.assertEqual(self.call("contract", "--topic", "links")["draft"]["scope"], [])

    def test_concurrent_writes_preserve_conflict_and_independent_change(self):
        original = self.commit(record("topic", "decision", "Original version"))
        inputs = [
            record("topic", "decision", "First replacement", replaces=original),
            record("topic", "decision", "Second replacement", replaces=original),
            record("independent", "permission", "Describe a separate task"),
        ]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            proposed = list(executor.map(self.propose, inputs))
            ids = [item["id"] for item in proposed]
            candidates = self.call("status", "--topic", "topic")["proposals"]
            self.assertEqual({item["id"]: item["state"] for item in candidates}, {
                ids[0]: "competing",
                ids[1]: "competing",
            })
            pending = [executor.submit(self.command, "confirm", identifier) for identifier in ids]
            before_or_after = executor.submit(self.call, "status")
            first, second, independent = (future.result() for future in pending)
            observed = {item["topic"]: item["id"] for item in before_or_after.result()["confirmed"]}
            self.assertIn(observed.pop("topic"), [original, *ids[:2]])
            self.assertIn(observed, [{}, {"independent": ids[2]}])

        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1])
        self.assertEqual(independent.returncode, 0, independent.stderr)
        winner, loser = ids[:2] if first.returncode == 0 else ids[1::-1]
        final = self.call("status")
        self.assertEqual({item["id"] for item in final["confirmed"]}, {winner, ids[2]})
        self.assertEqual([(item["id"], item["state"]) for item in final["proposals"]], [(loser, "conflict")])
        self.assertEqual(len(self.call("history")["events"]), 7)

        delayed = self.propose(record("topic", "decision", "Delayed replacement", replaces=original))
        self.assertEqual(delayed["state"], "conflict")
        before = self.state.read_bytes()
        self.assertEqual(self.command("confirm", delayed["id"]).returncode, 1)
        self.assertEqual(self.state.read_bytes(), before)

    def test_focused_contract_retains_only_checks_of_the_requested_permission(self):
        original = self.commit(record("P", "permission", "Prepare a draft for the old location"))
        other = self.commit(record("Q", "permission", "Describe the independent handoff"))
        previous = self.commit(record("links", "check", "Broken link at the old location",
                                      basis={"topic": "P", "id": original}))
        self.commit(record("handoff", "check", "Independent note exists",
                           basis={"topic": "Q", "id": other}))
        changed = self.commit(record("P", "permission", "Prepare a draft at the new location",
                                     replaces=original))
        self.propose(record("unconfirmed", "check", "All links pass",
                            basis={"topic": "P", "id": changed}))
        result = self.call("contract", "--topic", "P")
        checks = result["related_checks"]
        self.assertEqual([(c["id"], c["basis_id"], c["relation"]) for c in checks],
                         [(previous, original, "historical")])
        latest = self.commit(record("links", "check", "A link is still broken",
                                    replaces=previous, basis={"topic": "P", "id": changed}))
        checks = self.call("contract", "--topic", "P")["related_checks"]
        self.assertEqual([(c["id"], c["relation"], c["observation"]) for c in checks], [
            (previous, "historical", "Broken link at the old location"),
            (latest, "current", "A link is still broken"),
        ])

    def test_contract_pauses_only_the_contested_scope(self):
        directory = self.commit(record("directory", "decision", "Original address"))
        p = self.commit(record(
            "P", "permission", "Prepare the section at the original address",
            basis={"topic": "directory", "id": directory},
        ))
        q = self.commit(record("Q", "permission", "Describe the handoff"))
        first = self.propose(record("directory", "decision", "Address A", replaces=directory))["id"]
        self.assertEqual([item["id"] for item in self.call("contract", "--topic", "P")["draft"]["scope"]], [p])
        second = self.propose(record("directory", "decision", "Address B", replaces=directory))["id"]
        draft = self.call("contract")["draft"]
        self.assertEqual([item["id"] for item in draft["scope"]], [q])
        focused = self.call("contract", "--topic", "P")
        self.assertEqual(focused["confirmed"][0]["state"], "current")
        self.assertEqual(focused["draft"]["scope"], [])
        self.assertEqual(focused["draft"]["blocked"][0]["alternatives"], {"directory": [first, second]})
        self.call("withdraw", second, "--reason", "The owner dropped address B")
        self.assertEqual([item["id"] for item in self.call("contract", "--topic", "P")["draft"]["scope"]], [p])
        self.propose(record("Q", "permission", "Describe the handoff in one file", replaces=q))
        self.propose(record("Q", "permission", "Describe the handoff in two files", replaces=q))
        draft = self.call("contract")["draft"]
        self.assertEqual([item["id"] for item in draft["scope"]], [p])
        self.assertEqual([item["id"] for item in draft["blocked"]], [q])

    def test_plain_journal_can_move_to_another_project(self):
        identifier = self.commit(record("Q", "permission", "Describe the handoff"))
        with tempfile.TemporaryDirectory() as destination:
            project = Path(destination)
            journal_dir = project / ".signum"
            journal_dir.mkdir()
            shutil.copyfile(self.state, journal_dir / "state.json")
            self.assertEqual(
                self.call("contract", project=project)["draft"]["scope"][0]["id"],
                identifier,
            )
            self.assertFalse((journal_dir / "write.lock").exists())
            revised = self.call(
                "propose", project=project,
                data=record("Q", "permission", "Describe the draft handoff", replaces=identifier),
            )
            self.call("confirm", revised["id"], project=project)
            self.assertEqual(self.call("status")["confirmed"][0]["id"], identifier)
            self.assertEqual(self.call("status", project=project)["confirmed"][0]["id"], revised["id"])


if __name__ == "__main__":
    unittest.main()
