"""Check current native repair proposals using only local delivery evidence."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

sys.dont_write_bytecode = True

import delivery
import signum


SOURCE_PREFIX = "Signum evidence observation "
TOPIC_PREFIX = "evidence-repair-"
ORIGIN_FIELDS = {"delivery_id", "report_path", "path", "reason", "expected_sha256", "actual_sha256", "fingerprint"}
TERMS = ("scope", "verification", "authority_limit")
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class ResumeError(ValueError):
    pass


def unique_fields(pairs):
    return signum.unique_json_object(pairs, operation="Evidence investigation")


def file_revision(directory, name):
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return delivery.file_revision(info)


def read_journal(directory, name, journal):
    try:
        descriptor = os.open(name, FILE_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return
    with os.fdopen(descriptor, encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ResumeError(f"Expected a regular journal: {name}.")
        document = json.load(stream, object_pairs_hook=unique_fields)
    if (
        not isinstance(document, dict)
        or type(document.get("schema_version")) is not int
        or document["schema_version"] != signum.SCHEMA_VERSION
        or not isinstance(document.get("events"), list)
    ):
        raise ResumeError(f"Unsupported or invalid journal format: {name}.")
    for event in document["events"]:
        journal.append(event)


def proposal_origin(record):
    if not (record["topic"].startswith(TOPIC_PREFIX) and record["source"].startswith(SOURCE_PREFIX)):
        return None
    if record["kind"] != "permission" or not all(signum.nonempty(record.get(field)) for field in TERMS):
        raise ResumeError("The repair proposal must be a permission with all three terms.")

    origin = json.loads(record["source"][len(SOURCE_PREFIX):], object_pairs_hook=unique_fields)
    if not isinstance(origin, dict) or origin.keys() != ORIGIN_FIELDS:
        raise ResumeError("The repair proposal has invalid observation fields.")
    if not signum.matches(signum.ID_PATTERN, origin["delivery_id"]):
        raise ResumeError("The repair proposal has an invalid delivery ID.")
    for field in ("report_path", "path"):
        delivery.path_parts(origin[field])
    expected, actual = origin["expected_sha256"], origin["actual_sha256"]
    reason = origin["reason"]
    if not delivery.expected_hash(expected) or not (
        (reason in ("missing", "unreadable") and actual is None)
        or (reason == "hash_mismatch" and delivery.expected_hash(actual) and actual != expected)
    ):
        raise ResumeError("The repair proposal has an invalid finding reason or digest.")

    key = {field: origin[field] for field in ("delivery_id", "reason", "path", "expected_sha256")}
    compact = json.dumps(key, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(compact.encode("ascii")).hexdigest()
    if origin["fingerprint"] != fingerprint or record["topic"] != TOPIC_PREFIX + fingerprint[:48]:
        raise ResumeError("The repair proposal has an invalid observation fingerprint.")
    return origin


def inspect_file(project, path, expected):
    try:
        actual = delivery.file_entry(project, path)["sha256"]
    except FileNotFoundError:
        return "missing", None
    except (OSError, delivery.DeliveryError):
        return "unreadable", None
    return ("verified" if actual == expected else "hash_mismatch"), actual


def delivery_report(journal, deliveries, identifier):
    version = deliveries.versions.get(identifier)
    decision = deliveries.decisions.get(identifier)
    if version is None or not decision or decision["event"] != "accepted":
        return None, "The cited delivery has no recorded owner acceptance."
    permission_id = version["permission"]
    if deliveries.latest_version[permission_id] != identifier:
        return None, f"A later delivery version exists: {deliveries.latest_version[permission_id]}."
    if permission_id not in journal.accepted or journal.record(permission_id)["kind"] != "permission":
        raise ResumeError("The accepted delivery does not have a confirmed permission in the local journal.")
    permission = journal.record(permission_id)
    if journal.confirmed[permission["topic"]] != permission_id or journal.basis_issues(permission):
        return None, "The accepted delivery's permission or its basis has changed."
    evidence = version["evidence"]
    if evidence["status"] != "pass" or evidence["criteria"] != permission.get("verification"):
        raise ResumeError("The accepted delivery has inconsistent verification criteria or status.")
    return evidence["artifact"], None


def manifest(project, artifact):
    path, expected, content = artifact["path"], artifact["sha256"], artifact["text"]
    delivery.path_parts(path)
    if not delivery.expected_hash(expected) or hashlib.sha256(content.encode("utf-8")).hexdigest() != expected:
        raise ResumeError("The embedded delivery report does not match the registered digest.")
    issue = delivery.report_issue(content)
    if issue:
        raise ResumeError(issue)
    reason, _ = inspect_file(project, path, expected)
    if reason != "verified":
        raise ResumeError(f"The pinned report is {reason}: {path}.")

    supporting = json.loads(content, object_pairs_hook=delivery.unique_fields).get("supporting_files", {})
    if not isinstance(supporting, dict):
        raise ResumeError("The supporting-files manifest is invalid.")
    for path, digest in supporting.items():
        delivery.path_parts(path)
        if not delivery.expected_hash(digest):
            raise ResumeError(f"Invalid supporting SHA-256: {path}.")
    return supporting


def classify(project, journal, deliveries, origin, manifests):
    if deliveries is None:
        return "unreadable", "The project delivery journal is missing.", None
    identifier = origin["delivery_id"]
    path, expected = origin["path"], origin["expected_sha256"]
    try:
        artifact, stale_reason = delivery_report(journal, deliveries, identifier)
        if stale_reason:
            return "stale", stale_reason, None
        if artifact["path"] != origin["report_path"]:
            return "stale", "The proposal does not name the accepted delivery's pinned report.", None
        if identifier not in manifests:
            manifests[identifier] = manifest(project, artifact)
        if manifests[identifier].get(path) != expected:
            return "stale", "The accepted manifest does not support the recorded path and digest.", None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        return "unreadable", str(error), None

    reason, actual = inspect_file(project, path, expected)
    finding = {"path": path, "expected_sha256": expected, "reason": reason, "actual_sha256": actual}
    if reason == "unreadable":
        return "unreadable", "The supporting file cannot be read safely.", finding
    if reason == "verified":
        return "cleared", "The supporting file matches the accepted digest.", finding
    if (reason, actual) != (origin["reason"], origin["actual_sha256"]):
        return "stale", "The observed reason or actual digest has changed.", finding
    return "current", None, {**finding, "fingerprint": origin["fingerprint"]}


def short_text(value, limit):
    text = " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def choice_list(candidates):
    choices = []
    for candidate in candidates:
        similar = [
            item for item in candidates
            if (item["delivery_id"], item["path"]) == (candidate["delivery_id"], candidate["path"])
        ]
        choice = {key: value for key, value in candidate.items() if key not in TERMS}
        choice["summary"] = short_text(candidate["summary"], 160)
        differences = {}
        for term in TERMS:
            values = [" ".join(item[term].split()) for item in similar]
            if len(set(values)) > 1:
                prefix = os.path.commonprefix(values)
                differences[term] = short_text(" ".join(candidate[term].split())[len(prefix):], 96)
        if differences:
            choice["differing_terms"] = differences
        choices.append(choice)
    return choices


def current_repairs(project, journal, deliveries, only_id, excluded):
    status = journal.status()
    by_id = {entry["id"]: entry for entry in status["confirmed"] + status["proposals"]}
    identifiers = [only_id] if only_id else journal.proposed
    current = []
    manifests = {}

    for identifier in identifiers:
        if identifier not in journal.proposed:
            excluded.append({"id": identifier, "reason": "not_found"})
            continue
        record = journal.record(identifier)
        entry = by_id.get(identifier)
        try:
            origin = proposal_origin(record)
        except (ValueError, signum.SignumError, RecursionError) as error:
            if entry and entry["state"] not in {"stale", "conflict"}:
                excluded.append({"id": identifier, "reason": "invalid_proposal", "detail": str(error)})
            elif only_id:
                reason = "withdrawn" if identifier in journal.withdrawn else "stale"
                excluded.append({"id": identifier, "reason": reason})
            continue
        if origin is None:
            if only_id:
                excluded.append({"id": identifier, "reason": "not_evidence_repair"})
            continue
        context = {
            "id": identifier, "topic": record["topic"], "delivery_id": origin["delivery_id"],
            "report_path": origin["report_path"], "path": origin["path"],
        }
        if identifier in journal.withdrawn:
            excluded.append({**context, "reason": "withdrawn"})
            continue
        if entry is None or entry["state"] in {"stale", "conflict"}:
            if entry and entry["issues"]:
                context["issues"] = entry["issues"]
            excluded.append({**context, "reason": "stale"})
            continue

        state, detail, finding = classify(project, journal, deliveries, origin, manifests)
        if state != "current":
            if finding is not None:
                context["finding"] = finding
            excluded.append({**context, "reason": state, "detail": detail})
            continue
        current.append({
            **context, "state": entry["state"], "summary": record["summary"], "finding": finding,
            "proposed_at": journal.proposed[identifier]["at"],
            **{field: record[field] for field in TERMS},
        })

    return current


def resolve(project, journal, deliveries, only_id, result):
    excluded = result["excluded"]
    current = current_repairs(project, journal, deliveries, only_id, excluded)
    if len(current) > 1:
        result.update(status="ambiguous", choices=choice_list(current))
    elif any(item["reason"] in {"invalid_proposal", "unreadable"} for item in excluded):
        result.update(status="unknown", choices=choice_list(current),
                      error="One or more repair proposals could not be verified safely.")
    elif not only_id and current and current[0]["state"] == "competing":
        result.update(status="unknown", choices=choice_list(current),
                      error="The remaining proposal has competing records in the journal. Use a full proposal ID to inspect it.")
    elif current:
        result.update(status="ready", proposal=current[0])
    else:
        result["status"] = "none"


def resolve_pair(project, journal, deliveries, pair, result):
    excluded = result["excluded"]
    current = current_repairs(project, journal, deliveries, None, excluded)
    if any(item["reason"] in {"invalid_proposal", "unreadable"} for item in excluded):
        result["error"] = "One or more repair proposals could not be verified safely."
        return None
    by_id = {proposal["id"]: proposal for proposal in current}
    if by_id.keys() != set(pair) or any(proposal["state"] != "pending" for proposal in current):
        result["error"] = "The supplied pair does not match exactly two current pending repair proposals."
        return None
    first, second = (by_id[identifier] for identifier in pair)
    if first["delivery_id"] != second["delivery_id"] or first["path"] == second["path"]:
        result["error"] = "The pair must name two distinct paths in one delivery."
        return None
    return first, second


def recorded_source(scope):
    # Existing proposals store the source as the final sentence pair in the scope.
    prefix, suffix = " Verified source: ", ". Reverify its digest before use."
    if scope.count(prefix) == 1 and scope.endswith(suffix):
        return Path(scope.split(prefix)[1][:-len(suffix)])
    return None


def project_revision(project, path):
    try:
        with delivery.parent_directory(project, path) as (directory, name):
            return file_revision(directory, name)
    except FileNotFoundError:
        return None


def copy_evidence(source_root, source_path, draft_path):
    with delivery.open_file(source_root, source_path) as source:
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        with draft_path.open("xb") as output:
            shutil.copyfileobj(source, output)


def trial_evidence(project, journal, deliveries, proposals, sources):
    if any(source is None or not source.is_absolute() for source in sources):
        raise ResumeError("A local source file with an absolute path is needed for the trial.")
    identifier = proposals[0]["delivery_id"]
    evidence = deliveries.versions[identifier]["evidence"]
    supporting = manifest(project, evidence["artifact"])
    paths = {entry["path"] for entry in evidence["files"]} | set(supporting) | {evidence["artifact"]["path"]}
    repaired_paths = {proposal["path"] for proposal in proposals}
    revisions = {key: project_revision(project, key) for key in paths}
    for proposal in proposals:
        path, finding = proposal["path"], proposal["finding"]
        if inspect_file(project, path, finding["expected_sha256"]) != (finding["reason"], finding["actual_sha256"]):
            raise ResumeError("The original finding changed before the trial.")
    with tempfile.TemporaryDirectory(prefix="signum-rehearsal-") as temporary:
        draft = Path(temporary)
        # Only materialize inputs used by the accepted card and its pinned report.
        for key in sorted(paths - repaired_paths):
            if revisions[key] is not None:
                copy_evidence(project, key, draft / key)
        for proposal, source in zip(proposals, sources):
            path = proposal["path"]
            try:
                source_root = source.parent.resolve(strict=True)
            except RuntimeError as error:
                raise ResumeError("The trial source directory cannot be resolved safely.") from error
            copy_evidence(source_root, source.name, draft / path)
        files = []
        for proposal in proposals:
            path, finding = proposal["path"], proposal["finding"]
            digest = delivery.file_entry(draft, path)["sha256"]
            if digest != finding["expected_sha256"]:
                raise ResumeError("The trial source differs from the digest in the accepted report.")
            files.append({"id": proposal["id"], "path": path, "sha256": digest})
        card = deliveries.card(draft, journal, identifier)
        draft_supporting = manifest(draft, evidence["artifact"])
        remaining = [key for key, expected in sorted(draft_supporting.items())
                     if key not in repaired_paths and inspect_file(draft, key, expected)[0] != "verified"]
        blockers = card["acceptance_blockers"]
        if revisions != {key: project_revision(project, key) for key in paths}:
            raise ResumeError("The working evidence changed during the trial.")
    return {"status": "degraded" if remaining or blockers else "healthy", "files": files,
            "remaining": remaining, "acceptance_blockers": blockers}


def rehearse(project, journal, deliveries, proposal, source):
    receipt = {"status": "blocked", "path": proposal["path"], "reason": None, "remaining": []}
    source = source or recorded_source(proposal["scope"])
    try:
        checked = trial_evidence(project, journal, deliveries, (proposal,), (source,))
        receipt.update(sha256=checked.pop("files")[0]["sha256"], **checked)
    except (OSError, ValueError) as error:
        receipt["reason"] = str(error)
    return receipt


def rehearse_pair(project, journal, deliveries, proposals):
    receipt = {"status": "blocked", "delivery_id": proposals[0]["delivery_id"], "reason": None,
               "files": [], "remaining": [], "acceptance_blockers": []}
    sources = tuple(recorded_source(proposal["scope"]) for proposal in proposals)
    try:
        receipt.update(trial_evidence(project, journal, deliveries, proposals, sources))
    except (OSError, ValueError) as error:
        receipt["reason"] = str(error)
    return receipt


def resume(project, only_id, result, *, trial=False, source=None, pair=None):
    if only_id is not None and not signum.matches(signum.ID_PATTERN, only_id):
        raise ResumeError("Use the full ID of a native repair proposal.")
    if pair is not None:
        if only_id is not None or source is not None or not trial:
            raise ResumeError("--pair requires --rehearse and cannot be combined with --id or --source.")
        if (len(pair) != 2 or not all(signum.matches(signum.ID_PATTERN, identifier) for identifier in pair)
                or pair[0] == pair[1]):
            raise ResumeError("Use two distinct full IDs of native repair proposals.")
    project = project.resolve(strict=True)
    root = os.open(project, DIRECTORY_FLAGS)
    try:
        try:
            directory = os.open(".signum", DIRECTORY_FLAGS, dir_fd=root)
        except FileNotFoundError:
            if pair is not None:
                result["error"] = "No project journal contains the selected pair."
            else:
                resolve(project, signum.Journal(), delivery.Deliveries(), only_id, result)
            return
        try:
            journal, deliveries = signum.Journal(), delivery.Deliveries()
            names = ("state.json", "delivery.json")
            before = [file_revision(directory, name) for name in names]
            for name, target in zip(names, (journal, deliveries)):
                read_journal(directory, name, target)
            if before[0] is None and before[1] is not None:
                raise ResumeError("The project intent journal is missing.")
            if before[1] is None:
                deliveries = None
            if pair is not None:
                proposals = resolve_pair(project, journal, deliveries, pair, result)
                if proposals is not None:
                    rehearsal = rehearse_pair(project, journal, deliveries, proposals)
                    result.update(status="ready", rehearsal=rehearsal)
            else:
                resolve(project, journal, deliveries, only_id, result)
                if trial and result["status"] == "ready":
                    proposal = result["proposal"]
                    result["rehearsal"] = rehearse(project, journal, deliveries, proposal, source)
                    result["proposal"] = {key: proposal[key] for key in ("id", "delivery_id", "path", "state", "authority_limit")}
            if (
                not os.path.samestat(os.fstat(root), os.stat(project, follow_symlinks=False))
                or not os.path.samestat(os.fstat(directory), os.stat(".signum", dir_fd=root, follow_symlinks=False))
                or before != [file_revision(directory, name) for name in names]
            ):
                raise ResumeError("The project journals changed while being read.")
        finally:
            os.close(directory)
    finally:
        os.close(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="existing project directory")
    parser.add_argument("--id", help="full ID of one native repair proposal")
    parser.add_argument("--pair", action="append", nargs=2, metavar=("ID1", "ID2"),
                        help="full IDs of both current repair proposals")
    parser.add_argument("--rehearse", action="store_true", help="test the selected repairs in a disposable directory")
    parser.add_argument("--source", type=Path, help="explicit local source for the rehearsal if absent from the proposal")
    args = parser.parse_args()
    if args.source and not args.rehearse:
        parser.error("--source requires --rehearse")
    result = {"status": "unknown", "proposal": None, "choices": [], "excluded": [], "error": None}
    try:
        if args.pair and len(args.pair) != 1:
            raise ResumeError("Supply exactly one pair of proposal IDs.")
        pair = args.pair[0] if args.pair else None
        resume(args.project, args.id, result, trial=args.rehearse, source=args.source, pair=pair)
    except (OSError, ValueError, signum.SignumError, RecursionError) as error:
        result.pop("rehearsal", None)
        result.update(status="unknown", proposal=None, choices=[], excluded=[], error=str(error))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 2 if result["status"] == "unknown" else 0


if __name__ == "__main__":
    sys.exit(main())
