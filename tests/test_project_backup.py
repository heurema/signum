import fcntl
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
BACKUP = ROOT / "tools/project_backup.py"
SIGNUM = Path("plugins/signum/skills/project-intent/scripts/signum.py")
STATE = Path(".signum/state.json")
DELIVERY = Path(".signum/delivery.json")


class ProjectBackupTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.source = self.workspace / "source"
        self.source.mkdir()
        self.target = self.workspace / "target"
        self.archive = self.workspace / "backup.zip"

    def write(self, name, data):
        destination = self.source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    def backup_command(self, command, *args):
        return [sys.executable, "-B", str(BACKUP), command, "--archive", str(self.archive), *map(str, args)]

    def backup(self, command, *args, error=None):
        result = subprocess.run(self.backup_command(command, *args), capture_output=True, text=True, timeout=10)
        if error is not None:
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(error, result.stderr)
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def export(self, *args, **kwargs):
        return self.backup("export", "--project", self.source, *args, **kwargs)

    def restore(self, *args, **kwargs):
        return self.backup("restore", "--target", self.target, *args, **kwargs)

    def signum(self, project, *args, data=None):
        result = subprocess.run(
            [sys.executable, "-B", str(project / SIGNUM), "--project", str(project), *args],
            input=None if data is None else json.dumps(data), capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def propose(self, project, topic, kind="decision", **fields):
        if kind == "permission":
            fields["owner_turn"] = "codex:turn:11111111-1111-4111-8111-111111111111"
        return self.signum(project, "propose", data={
            "topic": topic, "kind": kind, "summary": "Change the readme",
            "source": "Owner discussion", "reason": "Record the agreement", "replaces": None,
            **fields,
        })["id"]

    def test_round_trip_reads_journals_and_refuses_to_erase_new_history(self):
        shutil.copytree(ROOT / "plugins", self.source / "plugins",
                        ignore=shutil.ignore_patterns("__pycache__"))
        marketplace = ".agents/plugins/marketplace.json"
        self.write(marketplace, (ROOT / marketplace).read_bytes())
        self.signum(self.source, "init")
        permission = self.propose(self.source, "readme", "permission", verification="Review README")
        self.signum(self.source, "confirm", permission)
        self.signum(self.source, "start", permission)
        self.write("README.md", "Версия 1\r\n".encode("utf-8"))
        patch = ".signum/review/sample/change.patch"
        self.write(patch, b"unchanged review bytes \x00\r\n")
        self.write("tools/check.sh", b"#!/bin/sh\n").chmod(0o700)
        (self.source / "tools/empty").mkdir()
        self.write("plugins/.git/config", b"excluded")
        self.write("plugins/__pycache__/entry.pyc", b"excluded")
        self.write("not-selected.txt", b"excluded")

        selected = ["README.md", "plugins", marketplace, STATE, DELIVERY, ".signum/review", "tools"]
        flags = [value for path in selected for value in ("--include", path)]
        response = self.export(*flags, "--optional", "docs")
        self.assertEqual(response["skipped"], {"docs": "missing"})
        manifest = self.backup("inspect")
        self.assertEqual(manifest["version"], 1)
        self.assertNotIn(str(self.source), json.dumps(manifest))
        self.assertEqual(self.restore()["status"], "restored")
        for name in ("README.md", marketplace, STATE, DELIVERY, patch, "tools/check.sh"):
            self.assertEqual((self.target / name).read_bytes(), (self.source / name).read_bytes())
        self.assertTrue((self.target / "tools/check.sh").stat().st_mode & 0o100)
        self.assertTrue((self.target / "tools/empty").is_dir())
        for name in (".signum/write.lock", "plugins/.git", "plugins/__pycache__", "not-selected.txt"):
            self.assertFalse((self.target / name).exists())
        for command in ("history", "delivery"):
            self.assertEqual(self.signum(self.target, command), self.signum(self.source, command))
        self.assertFalse((self.target / ".signum/write.lock").exists())
        (self.target / "plugins/__pycache__").mkdir()
        self.assertEqual(self.restore()["status"], "already-restored")
        self.assertEqual((self.target / STATE).read_bytes(), (self.source / STATE).read_bytes())
        self.propose(self.target, "new-owner-decision")
        updated = (self.target / STATE).read_bytes()
        self.restore(error="target differs")
        self.assertEqual((self.target / STATE).read_bytes(), updated)

    def test_missing_required_and_links_do_not_export_unapproved_content(self):
        external = self.workspace / "outside.txt"
        external.write_bytes(b"outside")
        (self.source / "README.md").symlink_to(external)
        self.export("--include", "README.md", error="symbolic link")
        self.export("--include", "docs", error="missing")
        self.export("--include", "../outside.txt", error="Invalid project path")
        self.export("--include", "private.md", error="allowed paths")
        self.assertFalse(self.archive.exists())
        self.write("docs/allowed.txt", b"allowed")
        (self.source / "docs/link.txt").symlink_to(external)
        self.export("--include", "docs", "--optional", "README.md")
        self.assertEqual(self.backup("inspect")["skipped"], {
            "README.md": "symbolic link", "docs/link.txt": "symbolic link",
        })
        self.restore()
        self.assertEqual(sorted(path.name for path in (self.target / "docs").iterdir()), ["allowed.txt"])
        self.assertFalse((self.target / "README.md").exists())

    def test_restore_rejects_source_and_descendants_and_preserves_other_directory(self):
        self.write("README.md", b"original")
        self.export("--include", "README.md")
        for forbidden in (self.source, self.source / "nested"):
            self.backup("restore", "--target", forbidden, error="inside the source project")
        alias = self.workspace / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        self.backup("restore", "--target", alias / "nested", error="inside the source project")
        self.backup("restore", "--target", alias, error="symbolic link")
        moved = self.workspace / "moved"
        self.source.rename(moved)
        self.backup("restore", "--target", moved / "nested", error="inside the source project")
        self.target.mkdir()
        (self.target / "README.md").write_bytes(b"different")
        self.restore(error="target differs")
        self.assertEqual((self.target / "README.md").read_bytes(), b"different")
        self.assertFalse((moved / "nested").exists())

    def test_invalid_payloads_and_members_never_publish_staging(self):
        self.write("README.md", b"first file")
        self.write("docs/last.txt", b"last file")
        self.export("--include", "README.md", "--include", "docs")
        with zipfile.ZipFile(self.archive) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        baseline = self.archive.read_bytes()
        for replacement, error in [
            ({"files/docs/last.txt": b"bad bytes"}, "Checksum mismatch"),
            ({"files/../escape": b"bad bytes"}, "members do not match"),
        ]:
            with zipfile.ZipFile(self.archive, "w") as archive:
                for name, content in (members | replacement).items():
                    archive.writestr(name, content)
            self.restore(error=error)
            self.assertFalse(self.target.exists())
            self.assertFalse(list(self.workspace.glob(".signum-restore-*")))
        self.archive.write_bytes(baseline[: len(baseline) // 2])
        self.target.mkdir()
        before = self.target.stat().st_ino
        self.restore(error="zip file")
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(self.target.stat().st_ino, before)

    def test_export_waits_for_an_in_flight_journal_writer(self):
        self.write(STATE, b"old state bytes")
        self.write(DELIVERY, b"old delivery bytes")
        lock = self.write(".signum/write.lock", b"")
        command = self.backup_command("export", "--project", self.source, "--include", STATE,
                                      "--include", DELIVERY)
        with lock.open("ab") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
                try:
                    deadline = time.monotonic() + 5
                    while not list(self.workspace.glob(".signum-export-*")):
                        self.assertIsNone(process.poll())
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(0.01)
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.15)
                    self.write(STATE, b"new state bytes")
                    self.write(DELIVERY, b"new delivery bytes")
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
        self.restore()
        self.assertEqual((self.target / STATE).read_bytes(), b"new state bytes")
        self.assertEqual((self.target / DELIVERY).read_bytes(), b"new delivery bytes")


if __name__ == "__main__":
    unittest.main()
