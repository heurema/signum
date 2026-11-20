import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ProjectCheckTest(unittest.TestCase):
    def test_observer_failures_never_green_the_project(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            tools = project / "tools"
            tools.mkdir()
            checker = tools / "check_signum.py"
            shutil.copy2(ROOT / "tools/check_signum.py", checker)
            delivery = "1" * 24
            incomplete = {"delivery_id": delivery, "status": "healthy"}
            for case, code, error in (
                ("missing executable", None, "no valid JSON"),
                ("incomplete verdict", f"print({json.dumps(incomplete)!r})", "did not establish"),
                ("stalled observer", "import time\ntime.sleep(3)", "deadline"),
            ):
                with self.subTest(case=case):
                    if code is not None:
                        (tools / "observe_evidence.py").write_text(code)
                    result = subprocess.run([sys.executable, "-B", str(checker), "--project", str(project),
                                             "--delivery", delivery, "--deadline-seconds", "1"],
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 2)
                    outcome = json.loads(result.stdout)
                    self.assertEqual(outcome["status"], "unknown")
                    self.assertEqual(outcome["component"], "observer")
                    self.assertIn(error, outcome["error"])
                    self.assertEqual(outcome["proposal_outcome"], "unverified")
