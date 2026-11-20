#!/usr/bin/env python3
"""Create an empty Cargo workspace, create a starter, or attach the Signum Rust task."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ASSETS = Path(__file__).resolve().parents[1] / "assets"
TASK = Path("mise-tasks/signum-rust")
KINDS = ("cli", "backend", "web")
EMPTY_WORKSPACE = b'[workspace]\nresolver = "3"\nmembers = []\n'


def verify_empty_workspace(root):
    metadata = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1", "--offline"],
        cwd=root, env={**os.environ, "RUSTUP_AUTO_INSTALL": "0"},
        text=True, capture_output=True,
    )
    if metadata.returncode:
        raise ValueError(metadata.stderr.strip() or "Cargo could not read the workspace")
    data = json.loads(metadata.stdout)
    if Path(data["workspace_root"]).resolve() != root or data["workspace_members"]:
        raise ValueError("expected an empty Cargo workspace at the project root")


def empty_workspace(root):
    manifest = root / "Cargo.toml"
    if manifest.exists() or manifest.is_symlink():
        if (manifest.is_symlink() or not manifest.is_file()
                or manifest.read_bytes() != EMPTY_WORKSPACE):
            raise ValueError("Cargo.toml already exists and differs; inspect the project-owned manifest")
        verify_empty_workspace(root)
        return "Verified existing"
    for parent in root.parents:
        enclosing = parent / manifest.name
        if enclosing.exists() or enclosing.is_symlink():
            raise ValueError(f"project is inside an existing Cargo project at {parent}")

    created_root = False
    try:
        if not root.exists():
            root.mkdir()
            created_root = True
        with tempfile.NamedTemporaryFile(dir=root, prefix=".signum-workspace-") as staged:
            staged.write(EMPTY_WORKSPACE)
            staged.flush()
            os.link(staged.name, manifest)
            try:
                verify_empty_workspace(root)
            except (OSError, ValueError):
                if not manifest.is_symlink() and manifest.exists() and manifest.samefile(staged.name):
                    manifest.unlink()
                raise
    except (OSError, ValueError):
        if created_root and not any(root.iterdir()):
            root.rmdir()
        raise
    return "Created"


def attach(root, kind, *, check_workspace=True):
    manifest = root / "Cargo.toml"
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("expected a regular Cargo.toml in the project root")
    if check_workspace:
        located = subprocess.run(
            ["cargo", "locate-project", "--workspace", "--message-format", "plain"],
            cwd=root, env={**os.environ, "RUSTUP_AUTO_INSTALL": "0"},
            text=True, capture_output=True,
        )
        if located.returncode:
            raise ValueError(located.stderr.strip() or "Cargo could not locate the workspace")
        if Path(located.stdout.strip()).resolve() != manifest:
            raise ValueError("attach at the Cargo workspace root")
    task = root / TASK
    if task.parent.is_symlink() or task.is_symlink():
        raise ValueError("refusing to follow a symlink for the project task")
    template = (ASSETS / TASK.name).read_text(encoding="utf-8")
    default = 'KIND = "cli"'
    if template.count(default) != 1:
        raise ValueError("invalid bundled Rust task")
    contents = template.replace(default, f'KIND = "{kind}"').encode("utf-8")
    task.parent.mkdir(exist_ok=True)
    # Link a finished executable without replacing an existing project task.
    with tempfile.NamedTemporaryFile(dir=task.parent) as staged:
        staged.write(contents)
        staged.flush()
        os.fchmod(staged.fileno(), 0o755)
        try:
            os.link(staged.name, task)
        except FileExistsError:
            if (task.is_symlink() or not task.is_file() or
                    task.read_bytes() != contents or not os.access(task, os.X_OK)):
                raise ValueError(f"{TASK} already exists and differs; inspect the project-owned task")
            return "Already attached"
    return "Attached"


def new_project(root, kind):
    # Reuse only an initialized, otherwise empty Signum root.
    existing = root.exists()
    if existing:
        state = root / ".signum/state.json"
        if (not root.is_dir() or state.parent.is_symlink() or state.is_symlink()
                or not state.is_file()
                or {p.name for p in root.iterdir()} - {".signum", "AGENTS.md", ".git"}):
            raise ValueError("expected a new path or a project containing only Signum setup files")
        journal = json.loads(state.read_text(encoding="utf-8"))
        if (not isinstance(journal, dict) or journal.get("schema_version") != 1
                or not isinstance(journal.get("events"), list)):
            raise ValueError("invalid Signum project journal")
    with tempfile.TemporaryDirectory(dir=root.parent, prefix=".signum-starter-") as scratch:
        ready = Path(scratch) / "starter"
        shutil.copytree(ASSETS / "starters" / kind, ready)
        attach(ready, kind, check_workspace=False)
        if not existing:
            root.mkdir()
        created = []
        try:
            for item in sorted(ready.rglob("*")):
                dest = root / item.relative_to(ready)
                if item.is_dir():
                    dest.mkdir()
                else:
                    os.link(item, dest)
                created.append((item, dest))
        except OSError:
            for item, dest in reversed(created):
                try:
                    if item.is_dir():
                        dest.rmdir()
                    elif item.samefile(dest):
                        dest.unlink()
                except OSError:
                    pass
            if not existing:
                try:
                    root.rmdir()
                except OSError:
                    pass
            raise
    return "Created"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("attach", "new", "workspace"))
    parser.add_argument("project", type=Path)
    parser.add_argument("--kind", choices=KINDS)
    args = parser.parse_args()
    if args.project.is_symlink():
        parser.error("expected a project path, not a symlink")
    if args.action == "workspace":
        if args.kind is not None:
            parser.error("workspace does not take --kind")
    elif args.kind is None:
        parser.error(f"{args.action} requires --kind")
    project = args.project.resolve()
    try:
        if args.action == "workspace":
            result = empty_workspace(project)
        else:
            result = (attach if args.action == "attach" else new_project)(project, args.kind)
    except (OSError, ValueError) as error:
        sys.exit(f"signum-rust: {error}")
    subject = "empty Cargo workspace" if args.action == "workspace" else f"{args.kind} workflow"
    print(f"{result} {subject} in {project}")


if __name__ == "__main__":
    main()
