from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MARKET = json.loads((ROOT / ".agents/plugins/marketplace.json").read_bytes())
PACKAGE = next(entry["source"]["path"] for entry in MARKET["plugins"] if entry["name"] == "signum")
SCRIPTS = ROOT / PACKAGE / "skills/project-intent/scripts"


class ProjectInitTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = self.project / ".signum"
        self.state = self.store / "state.json"
        self.pointer = self.project / "AGENTS.md"

    def cli(self, *args, script="signum.py"):
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPTS / script), "--project", str(self.project), *args],
            input="", text=True, capture_output=True, timeout=10,
        )

    def call(self, *args, script="signum.py"):
        result = self.cli(*args, script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def blocked(self, *args):
        result = self.cli(*args)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr

    def verification(self):
        return (
            "verify", "--report", "result.json", "--name", "known checker", "--",
            sys.executable, "-c", "from pathlib import Path; Path('ran').touch()",
        )

    def test_uninitialized_project_allows_reads_without_creating_files(self):
        self.assertIs(self.call("status")["enabled"], False)
        self.assertIs(self.call("status", "--topic", "example")["enabled"], False)
        self.call("history")
        self.call("contract")
        self.call("delivery")
        self.call("snapshot", "--deleted", "missing")
        self.call("--query", "example", script="recall.py")
        self.call("--rehearse", script="resume_delivery.py")
        self.assertNotEqual(self.cli(script="orient.py").returncode, 0)
        self.assertEqual(list(self.project.iterdir()), [])

    def test_orientation_reports_existing_materials_without_writing(self):
        self.call("init")
        before = self.state.read_bytes()
        (self.project / "design.md").touch()
        view = self.call(script="orient.py")
        self.assertEqual(view["project_entries"], ["design.md"])
        self.assertEqual(view["unfinished_work"], [])
        self.assertEqual(view["confirmed_topics"], [])
        self.assertFalse(view["cargo_manifest"])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(set(self.store.iterdir()), {self.state, self.store / "write.lock"})

    def test_uninitialized_project_refuses_all_mutations_before_reading_input_or_running_checker(self):
        for args in (
            ("draft", "--topic", "sample"), ("propose", "--input", "missing"),
            ("prepare", "--topic", "sample"), ("confirm", "sample"),
            ("assent",), ("withdraw", "sample", "--reason", "test"),
            ("start", "sample"), ("evidence", "sample", "--input", "missing"),
            ("accept", "sample", "--reason", "test", "--source", "fixture"),
            ("return", "sample", "--reason", "test", "--source", "fixture"),
            self.verification(),
        ):
            with self.subTest(command=args[0]):
                self.assertIn("disabled", self.blocked(*args))
                self.assertEqual(list(self.project.iterdir()), [])

    def test_concurrent_init_is_idempotent_and_enables_verification(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(lambda _: self.call("init"), range(4)))
        self.assertTrue(all(receipt["enabled"] for receipt in receipts))
        self.assertEqual(sum(receipt["initialized"] for receipt in receipts), 1)
        self.assertEqual(sum(receipt["agents_md"] == "created" for receipt in receipts), 1)
        self.assertEqual(json.loads(self.state.read_bytes()), {"schema_version": 1, "events": []})
        self.assertIn("project-intent", self.pointer.read_text())
        self.assertIs(self.call("status")["enabled"], True)
        verification = self.cli(*self.verification())
        self.assertEqual(verification.returncode, 0, verification.stderr)
        self.assertTrue((self.project / "ran").exists())
        self.assertTrue((self.project / "result.json").exists())

    def test_legacy_state_and_existing_instructions_are_retained(self):
        self.store.mkdir()
        baseline = b'{"schema_version": 1, "events": []}\n\n'
        self.state.write_bytes(baseline)
        self.pointer.symlink_to(self.root / "missing")
        self.assertIs(self.call("status")["enabled"], True)
        self.assertEqual(self.call("init"), {
            "project": str(self.project.resolve()), "enabled": True, "initialized": False, "agents_md": "retained",
        })
        self.assertEqual(self.state.read_bytes(), baseline)
        self.assertEqual(self.pointer.readlink(), self.root / "missing")
        self.assertFalse(self.pointer.exists())

    def test_path_collision_and_corrupt_journal_refuse_init_and_verification(self):
        self.store.write_text("owner file")
        self.blocked("init")
        self.blocked(*self.verification())
        self.assertEqual(self.store.read_text(), "owner file")
        self.store.unlink()
        other = self.root / "other"
        other.mkdir()
        self.store.symlink_to(other)
        self.blocked("init")
        self.blocked(*self.verification())
        self.assertEqual(list(other.iterdir()), [])
        self.store.unlink()
        self.store.mkdir()
        self.state.symlink_to(other / "missing")
        self.blocked("init")
        self.blocked(*self.verification())
        self.assertEqual(self.state.readlink(), other / "missing")
        self.state.unlink()
        os.mkfifo(self.state)
        self.blocked("init")
        self.blocked(*self.verification())
        self.assertTrue(self.state.is_fifo())
        self.state.unlink()
        self.state.write_bytes(b'{"schema_version": 1, "events": [], "events": []}')
        self.blocked("init")
        self.blocked(*self.verification())
        self.assertEqual(self.state.read_bytes(), b'{"schema_version": 1, "events": [], "events": []}')
        self.assertEqual(list(self.store.iterdir()), [self.state])
        self.assertFalse(self.pointer.exists())
        self.assertFalse((self.project / "result.json").exists())
        self.assertFalse((self.project / "ran").exists())

    def test_orphan_delivery_prevents_initialization(self):
        self.store.mkdir()
        delivery = self.store / "delivery.json"
        delivery.write_text('{"schema_version": 1, "events": []}')
        self.assertIs(self.call("status")["enabled"], False)
        self.blocked("init")
        self.assertEqual(list(self.store.iterdir()), [delivery])
        self.assertFalse(self.pointer.exists())


if __name__ == "__main__":
    unittest.main()
