"""Observe evidence for one Signum delivery and optionally queue a repair proposal."""

import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import signal
import stat
import sys


MARKETPLACE = ".agents/plugins/marketplace.json"
CORE_FILES = ("signum.py", "delivery.py", "scope_guard.py")
ROOT = Path(__file__).resolve().parents[1]
TIME_LIMIT_SECONDS = 10
EXIT_CODES = {"healthy": 0, "degraded": 1, "unknown": 2}
SOURCE_PREFIX = "Signum evidence observation "


class ObservationError(Exception):
    pass


def load_core(project):
    marketplace = json.loads((ROOT / MARKETPLACE).read_bytes())
    sources = [entry["source"] for entry in marketplace["plugins"] if entry["name"] == "signum"]
    if len(sources) != 1 or sources[0]["source"] != "local":
        raise ObservationError("The marketplace must select one local Signum package.")
    package = Path(sources[0]["path"])
    core = package / "skills/project-intent/scripts"
    if package.is_absolute() or not package.parts or ".." in package.parts:
        raise ObservationError("The selected Signum core must be inside the observer checkout.")
    script_dir = (ROOT / core).resolve(strict=True)
    if not script_dir.is_relative_to(ROOT):
        raise ObservationError("The selected Signum core must be inside the observer checkout.")
    for name in CORE_FILES:
        if not stat.S_ISREG((script_dir / name).lstat().st_mode):
            raise ObservationError(f"The selected Signum core has no regular {name}.")
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(script_dir))
    import signum
    import delivery

    target_package = project / package
    if target_package.exists() or target_package.is_symlink():
        for name in CORE_FILES:
            path = (core / name).as_posix()
            if delivery.file_entry(project, path) != delivery.file_entry(ROOT, path):
                raise ObservationError(f"The target core differs from the observer's core: {path}.")
    return signum, delivery


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def inspect_file(delivery, project, path, expected):
    try:
        actual = delivery.file_entry(project, path)["sha256"]
    except FileNotFoundError:
        return "missing", None
    except (OSError, delivery.DeliveryError):
        return "unreadable", None
    return ("verified" if actual == expected else "hash_mismatch"), actual


def load_supporting(delivery, project, evidence, result):
    artifact = evidence["artifact"]
    path = artifact["path"]
    delivery.path_parts(path)
    expected = artifact["sha256"]
    report = result["report"] = {
        "path": path, "expected_sha256": expected, "status": "invalid", "manifest": "unknown"
    }
    content = artifact["text"]
    if not delivery.expected_hash(expected) or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
        raise ObservationError("The embedded report bytes do not match the registered digest.")
    if evidence["status"] == "pass":
        issue = delivery.report_issue(content)
        if issue:
            raise ObservationError(issue)
    document = json.loads(content, object_pairs_hook=delivery.unique_fields)
    if not isinstance(document, dict):
        raise ObservationError("The original check report must be a JSON object.")
    report["status"], report["actual_sha256"] = inspect_file(delivery, project, path, expected)
    if report["status"] != "verified":
        raise ObservationError(f"The original check report is {report['status']}: {path}.")
    if "supporting_files" not in document:
        report["manifest"] = "not_declared"
        return {}
    supporting = document["supporting_files"]
    if not isinstance(supporting, dict):
        report["manifest"] = "invalid"
        raise ObservationError("The original report's supporting_files field must be an object.")
    try:
        for name, digest in supporting.items():
            delivery.path_parts(name)
            if not delivery.expected_hash(digest):
                raise ObservationError(f"Invalid supporting SHA-256: {name}.")
    except (ObservationError, delivery.DeliveryError):
        report["manifest"] = "invalid"
        raise
    report["manifest"] = "declared" if supporting else "empty"
    return supporting


def findings(delivery, project, identifier, supporting):
    result = []
    for path, expected in sorted(supporting.items()):
        reason, actual = inspect_file(delivery, project, path, expected)
        if reason != "verified":
            cause = {"reason": reason, "path": path, "expected_sha256": expected}
            key = {"delivery_id": identifier, **cause}
            fingerprint = hashlib.sha256(compact(key).encode("ascii")).hexdigest()
            result.append({**cause, "actual_sha256": actual, "fingerprint": fingerprint})
    return result


def proposal_record(identifier, report_path, finding, topic, basis):
    path = finding["path"]
    expected = finding["expected_sha256"]
    source = {"delivery_id": identifier, "report_path": report_path, **finding}
    return {
        "topic": topic,
        "kind": "permission",
        "summary": f"Restore the original delivery evidence at {path}.",
        "source": SOURCE_PREFIX + compact(source),
        "reason": f"The supporting file for delivery {identifier} is {finding['reason']}.",
        "replaces": None,
        "basis": basis,
        "scope": (
            f"Restore only {path} from a trusted local copy with SHA-256 {expected}. "
            "Verify the source before writing and preserve any differing current bytes for review. "
            "Stop if no verified source is available or if the observation no longer holds."
        ),
        "verification": (
            f"Recheck the finding before repair, verify SHA-256 {expected} after repair, and rerun "
            f"the evidence observer for delivery {identifier}. Register a new check report "
            "and present its exact delivery version for the owner's acceptance."
        ),
        "authority_limit": (
            "Pending owner review. Obtain explicit permission before restoration. "
            "The original delivery, pinned report and acceptance history must stay intact."
        ),
    }


def queue_proposals(core, journal, state_path, result, basis_id):
    if basis_id not in journal.accepted:
        raise ObservationError("The repair proposal needs a confirmed decision as its basis.")
    decision = journal.record(basis_id)
    if decision["kind"] != "decision" or journal.confirmed.get(decision["topic"]) != basis_id:
        raise ObservationError("The repair proposal needs a current confirmed decision as its basis.")
    basis = {"topic": decision["topic"], "id": basis_id}
    before = len(journal.events)
    proposals = []
    for finding in result["findings"]:
        fingerprint = finding["fingerprint"]
        topic = "evidence-repair-" + fingerprint[:48]
        existing = [
            identifier for identifier, item in journal.proposed.items() if item["record"]["topic"] == topic
        ]
        if existing:
            status = journal.status(topic)
            current = status["confirmed"] or status["proposals"]
            if current:
                identifier, state = current[0]["id"], current[0]["state"]
                if identifier in journal.accepted:
                    state = "confirmed"
            else:
                identifier, state = existing[-1], "withdrawn"
        else:
            record = proposal_record(result["delivery_id"], result["report"]["path"], finding, topic, basis)
            entry = journal.propose(record)
            identifier, state = entry["id"], entry["state"]
        saved = journal.record(identifier)
        proposals.append({
            "fingerprint": fingerprint, "topic": topic, "id": identifier, "state": state,
            "created": not existing, "summary": saved["summary"], "scope": saved.get("scope"),
            "verification": saved.get("verification"), "authority_limit": saved.get("authority_limit"),
        })
    if len(journal.events) != before:
        core.write_journal(state_path, journal)
    return proposals


def cleared_proposals(journal, identifier, findings):
    active = {(finding["path"], finding["expected_sha256"]) for finding in findings}
    cleared = []
    for entry in journal.status()["proposals"]:
        source = entry["source"]
        if entry["topic"].startswith("evidence-repair-") and source.startswith(SOURCE_PREFIX):
            try:
                origin = json.loads(source.removeprefix(SOURCE_PREFIX))
                fingerprint = origin["fingerprint"]
                target = (origin["path"], origin["expected_sha256"])
                if origin["delivery_id"] == identifier and target not in active:
                    cleared.append({
                        "id": entry["id"], "state": entry["state"], "fingerprint": fingerprint,
                    })
            except (ValueError, KeyError, TypeError):
                continue
    return cleared


def observe(args, result):
    project = args.project.resolve(strict=True)
    core, delivery = load_core(project)
    identifier = args.delivery
    if not core.matches(core.ID_PATTERN, identifier):
        raise ObservationError("Use the full ID of a recorded delivery version.")
    directory = project / ".signum"
    if not stat.S_ISDIR(directory.lstat().st_mode):
        raise ObservationError("Expected a .signum directory inside the project.")
    state_path = directory / "state.json"
    delivery_path = directory / "delivery.json"
    lock = core.locked_store(directory) if args.propose else nullcontext()
    with lock:
        paths = (state_path, delivery_path)
        revisions = []
        for path in paths:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ObservationError(f"Expected a regular journal: {path.name}.")
            revisions.append(delivery.file_revision(info))
        deliveries = delivery.Deliveries.read(delivery_path, core)
        journal = core.Journal.read(state_path)
        card = deliveries.card(project, journal, identifier)
        blockers = card["acceptance_blockers"]
        decision = card["owner_decision"]
        result["service"] = "available"
        result["card"] = {
            "state": card["state"], "green": not blockers,
            "owner_accepted": decision is not None and decision["event"] == "accepted",
            "acceptance_blockers": blockers,
        }
        if not result["card"]["owner_accepted"]:
            raise ObservationError("The journal does not record owner acceptance for this delivery.")
        supporting = load_supporting(delivery, project, card["evidence"], result)
        result["findings"] = findings(delivery, project, identifier, supporting)
        if any(finding["reason"] == "unreadable" for finding in result["findings"]):
            raise ObservationError("One or more supporting files could not be read safely.")
        result["cleared_pending_proposals"] = cleared_proposals(journal, identifier, result["findings"])
        if not args.propose and revisions != [delivery.file_revision(path.lstat()) for path in paths]:
            raise ObservationError("The project journals changed while being observed.")
        if result["findings"] or blockers:
            if args.propose and not blockers:
                result["proposals"] = queue_proposals(core, journal, state_path, result, args.basis)
            result["status"] = "degraded"
        else:
            result["status"] = "healthy"


def timeout(_signum, _frame):
    raise ObservationError(f"The observation exceeded {TIME_LIMIT_SECONDS} seconds.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--delivery", required=True, help="full delivery version ID")
    parser.add_argument("--propose", action="store_true", help="queue a pending native repair proposal")
    parser.add_argument("--basis", help="full ID of the current owner decision authorizing repair proposals")
    args = parser.parse_args()
    if args.propose != bool(args.basis):
        parser.error("--propose and --basis must be used together")
    result = {
        "status": "unknown", "delivery_id": args.delivery, "service": "unavailable",
        "card": None, "report": None, "findings": [], "proposals": [], "error": None,
        "cleared_pending_proposals": [],
    }
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(TIME_LIMIT_SECONDS)
    try:
        observe(args, result)
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {error}"
    finally:
        signal.alarm(0)
    print(compact(result))
    return EXIT_CODES[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
