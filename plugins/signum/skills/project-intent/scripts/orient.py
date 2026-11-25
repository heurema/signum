"""Read a small project inventory before discussing where to start."""
import argparse
import json
from pathlib import Path
import sys

import signum
from delivery import Deliveries
from signum import Journal, SignumError, journal_directory


def source_files(project, folder, pattern):
    directory = project / folder
    if directory.is_symlink() or not directory.is_dir():
        return []
    names = sorted(p.relative_to(project).as_posix() for p in directory.glob(pattern)
                   if not p.is_symlink() and p.is_file())
    return names[:20]


def source_index(project, journal, deliveries):
    checks = []
    for version, entry in reversed(deliveries.versions.items()):
        if deliveries.latest_version[entry["permission"]] != version:
            continue
        proof = entry["evidence"]
        checks.append({
            "topic": journal.record(entry["permission"])["topic"] if entry["permission"] in journal.proposed else None,
            "permission": entry["permission"], "version": version,
            "report": proof["artifact"]["path"], "check_status": proof["status"],
            "checked_at": proof.get("checked_at"), "registered_at": entry["at"],
            "owner_review": deliveries.decisions.get(version, {}).get("event", "awaiting_review"),
        })
        if len(checks) == 12:
            break
    return {
        "instructions": "AGENTS.md" if (project / "AGENTS.md").is_file() else None,
        "documents": source_files(project, "docs", "*.md"),
        "reports": source_files(project, ".checks", "*.json"),
        "registered_checks": checks,
    }


def orientation(project, *, sources=False):
    project = project.resolve(strict=True)
    directory = journal_directory(project)
    journal = Journal.read(directory / "state.json", required=True)
    deliveries = Deliveries.read(directory / "delivery.json", signum)
    status = journal.status()
    # List names only. Do not open source files or configuration while surveying.
    entries = sorted(p.name for p in project.iterdir() if p.name not in {
        ".git", ".signum", "AGENTS.md", ".DS_Store",
    })
    work = []
    for entry in status["confirmed"]:
        if entry["kind"] != "permission":
            continue
        permission = entry["id"]
        latest = deliveries.latest_version.get(permission)
        start = deliveries.open_start(permission)
        if start and (latest is None or deliveries.versions[latest]["start"] != start["id"]):
            stage = "started"
        elif latest:
            stage = deliveries.decisions.get(latest, {}).get("event", "awaiting_review")
        else:
            stage = "not_started"
        if stage != "accepted":
            work.append({"topic": entry["topic"], "stage": stage, "basis_state": entry["state"]})
    inventory = {
        "project": str(project),
        "project_entries": entries[:20],
        "other_entry_count": max(0, len(entries) - 20),
        "cargo_manifest": (project / "Cargo.toml").is_file(),
        "rust_workflow": (project / "mise-tasks/signum-rust").is_file(),
        "confirmed_topics": [r["topic"] for r in status["confirmed"] if r["state"] == "current"],
        "stale_topics": [r["topic"] for r in status["confirmed"] if r["state"] == "stale"],
        "unfinished_work": work,
        "pending_topics": [r["topic"] for r in status["proposals"]],
        "withdrawn_count": len(journal.withdrawn),
    }
    if sources:
        inventory["source_index"] = source_index(project, journal, deliveries)
    return inventory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--sources", action="store_true", help="include pointers to project evidence")
    args = parser.parse_args()
    try:
        print(json.dumps(orientation(args.project, sources=args.sources), ensure_ascii=False))
    except (OSError, ValueError, SignumError) as error:
        sys.exit(f"signum: {error}")


if __name__ == "__main__":
    main()
