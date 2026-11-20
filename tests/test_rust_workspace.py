import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "plugins/signum-rust/skills/rust-workflow/scripts/setup.py"


@unittest.skipUnless(shutil.which("cargo"), "needs Cargo")
class RustWorkspaceTest(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.env = dict(os.environ, CARGO_NET_OFFLINE="true", RUSTUP_AUTO_INSTALL="0",
                        CARGO_HOME=str(self.root / "registry"), SIGNUM_RUST_CACHE=str(self.root / "cache"))
        self.env.pop("MISE_PROJECT_ROOT", None)

    def call(self, directory, *args, ok=True):
        result = subprocess.run(args, cwd=directory, env=self.env, capture_output=True,
                                text=True, timeout=45)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return result.stdout if ok else result.stdout + result.stderr

    def test_empty_workspace_preserves_local_files_and_accepts_first_member(self):
        project = self.root / "future-project"
        originals = {"AGENTS.md": b"local rules\r\n", ".signum/state.json": b"{}\n",
                     "docs/design.md": b"Local design\n"}
        for path, content in originals.items():
            file = project / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)

        setup = (sys.executable, "-B", SETUP, "workspace", project)
        self.assertIn("Created", self.call(self.root, *setup))
        self.assertIn("Verified existing", self.call(self.root, *setup))
        self.assertEqual({str(path.relative_to(project)): path.read_bytes()
                          for path in project.rglob("*") if path.is_file()},
                         {**originals, "Cargo.toml": b'[workspace]\nresolver = "3"\nmembers = []\n'})
        metadata = ("cargo", "metadata", "--no-deps", "--format-version", "1")
        self.assertEqual(json.loads(self.call(project, *metadata))["workspace_members"], [])

        self.call(project, "cargo", "new", "--lib", "--vcs", "none", "crates/sample-library")
        self.assertEqual([pkg["name"] for pkg in json.loads(self.call(project, *metadata))["packages"]],
                         ["sample-library"])
        manifest = project / "Cargo.toml"
        previous = manifest.read_bytes()
        self.assertIn("already exists and differs", self.call(self.root, *setup, ok=False))
        self.assertEqual(manifest.read_bytes(), previous)
        self.call(self.root, sys.executable, "-B", SETUP, "attach", project, "--kind", "cli")
        self.assertTrue((project / "mise-tasks/signum-rust").is_file())

    def test_empty_workspace_rejects_nested_paths(self):
        project = self.root / "parent"
        setup = (sys.executable, "-B", SETUP, "workspace")
        self.call(self.root, *setup, project)
        child = project / "future-project"
        self.assertIn("inside an existing Cargo project", self.call(self.root, *setup, child, ok=False))
        self.assertFalse(child.exists())

    def test_all_starters_register_new_capabilities(self):
        for kind in ("cli", "backend", "web"):
            with self.subTest(kind=kind):
                project = self.root / kind
                self.call(self.root, sys.executable, "-B", SETUP, "new", project, "--kind", kind)
                self.call(project, "cargo", "new", "--lib", "--vcs", "none", "crates/sample-library")
                metadata = self.call(project, "cargo", "metadata", "--no-deps", "--format-version", "1")
                data = json.loads(metadata)
                self.assertEqual(len(data["workspace_members"]), 2)
                self.assertEqual({pkg["name"] for pkg in data["packages"]}, {"signum-starter", "sample-library"})

    def test_package_check_is_scoped_to_workspace_members(self):
        project = self.root / "project"
        self.call(self.root, "cargo", "new", "--bin", "--vcs", "none", project)
        manifest = project / "Cargo.toml"
        with manifest.open("a") as file:
            file.write('\n[workspace]\nresolver = "3"\n')
        self.call(project, "cargo", "new", "--lib", "--vcs", "none", "crates/sample-library")
        self.call(project, "cargo", "add", "--path", "crates/sample-library", "-p", "project")
        self.call(project, "cargo", "generate-lockfile")
        self.call(project, sys.executable, "-B", SETUP, "attach", project, "--kind", "cli")
        (project / "crates/sample-library/src/lib.rs").write_text(
            "pub fn revenue(period: u16) -> u16 { period }\n"
            "#[test] fn year() { assert_eq!(revenue(2025), 2025); }\n"
        )
        (project / "src/main.rs").write_text("fn main() { let _ = sample_library::revenue(); }\n")
        task = project / "mise-tasks/signum-rust"
        self.assertIn("1 passed", self.call(project, task, "test", "-p", "sample-library"))
        self.assertIn("E0061", self.call(project, task, "test", "-p", "project", ok=False))
        self.assertIn("not a member", self.call(project, task, "test", "-p", "unknown", ok=False))
        self.assertFalse((project / "target").exists())
