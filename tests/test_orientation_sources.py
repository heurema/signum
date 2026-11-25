import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/signum/skills/project-intent/scripts"


class OrientationSourcesTest(unittest.TestCase):
    def cli(self, project, script, *args):
        return json.loads(subprocess.check_output(
            [sys.executable, "-B", str(SCRIPTS / script), "--project", str(project), *args],
        ))

    def test_optional_index_updates_after_removal_and_ignores_external_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            project = base / "A"
            project.mkdir()
            self.cli(project, "signum.py", "init")
            checks = project / ".checks"
            checks.mkdir()
            hidden = base / "outside"
            hidden.mkdir()
            (hidden / "foreign.md").write_text("Private document")
            (project / "docs").symlink_to(hidden, target_is_directory=True)
            (checks / "linked.json").symlink_to(hidden / "foreign.md")
            scratch = checks / "temporary.json"
            scratch.write_text('{"result":"removed"}')
            routine = self.cli(project, "orient.py")
            self.assertNotIn("source_index", routine)
            index = self.cli(project, "orient.py", "--sources")["source_index"]
            self.assertEqual(index, {"instructions": "AGENTS.md", "documents": [],
                                     "reports": [".checks/temporary.json"], "registered_checks": []})
            scratch.unlink()
            (checks / "working.json").touch()
            self.assertEqual(self.cli(project, "orient.py", "--sources")["source_index"]["reports"],
                             [".checks/working.json"])


if __name__ == "__main__":
    unittest.main()
