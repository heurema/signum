"""Record and review a version of a local Signum delivery."""

from argparse import REMAINDER
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from time import perf_counter
from uuid import uuid4

import scope_guard


COMMANDS = {"start", "snapshot", "verify", "evidence", "delivery", "accept", "return"}
MEASURES = {"elapsed_seconds", "input_tokens", "output_tokens"}
SHA256 = re.compile(r"[0-9a-f]{64}")


class DeliveryError(ValueError):
    pass


def add_commands(commands):
    start = commands.add_parser("start", help="record the start of work under a confirmed permission")
    start.add_argument("permission")
    start.add_argument("--started-at", help="externally recorded UTC start time")
    start.add_argument("--source", help="source of the start time")
    start.add_argument("--touch", action="append", default=[], metavar="PATH",
                       help="preflight an intended edit against the confirmed exact file scope")
    snapshot = commands.add_parser("snapshot", help="capture hashes of the files to be checked")
    snapshot.add_argument("files", nargs="*", metavar="PATH", help="changed project files")
    snapshot.add_argument("--deleted", action="append", default=[], metavar="PATH", help="removed project file")
    snapshot.add_argument("--artifact", metavar="PATH", help="UTF-8 text report from the check")
    verify = commands.add_parser("verify", help="run an explicit check and save its exit code")
    verify.add_argument("--report", required=True, help="new project-relative JSON report file")
    verify.add_argument("--name", required=True, help="label for this check")
    verify.add_argument("argv", nargs=REMAINDER, metavar="-- COMMAND")
    evidence = commands.add_parser("evidence", help="record a version of the checked result")
    evidence.add_argument("permission")
    evidence.add_argument("--input", default="-", help="JSON file or - for stdin")
    commands.add_parser("delivery", help="show a version or list deliveries").add_argument("id", nargs="?")
    for name, description in (
        ("accept", "record the owner's acceptance of a displayed version"),
        ("return", "record the owner's request to rework a displayed version"),
    ):
        command = commands.add_parser(name, help=description)
        command.add_argument("id")
        command.add_argument("--reason", required=True)
        command.add_argument("--source", required=True, help="owner's statement or its source")


def now():
    return datetime.now(timezone.utc)


def utc(value, field):
    try:
        instant = datetime.fromisoformat(value.removesuffix("Z") + "+00:00" if value.endswith("Z") else value)
    except (AttributeError, ValueError):
        instant = None
    if instant is None or instant.utcoffset() != timedelta():
        raise DeliveryError(f"{field} must be an ISO 8601 UTC timestamp.")
    return instant


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DeliveryError(f"{field} must be non-empty text.")


def version_id(event):
    body = json.dumps(event, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def permission_issues(journal, permission_id):
    if permission_id not in journal.accepted or journal.record(permission_id)["kind"] != "permission":
        return ["Use a confirmed permission ID."]
    permission = journal.record(permission_id)
    if journal.confirmed[permission["topic"]] != permission_id:
        return [f"Permission {permission_id} is no longer current."]
    issues = journal.basis_issues(permission)
    if issues:
        return issues
    draft = journal.contract(permission["topic"])["draft"]
    if draft["blocked"]:
        return ["Resolve competing proposals for the permission or its basis first."]
    if not permission.get("verification"):
        return ["The permission has no agreed verification criteria."]
    if scope_guard.parse(permission.get("scope")) is not None and permission.get("questions"):
        return ["Resolve the open questions in the permission before working under exact file scope."]
    handoff_issue = journal.handoff_issue(permission)
    return [handoff_issue] if handoff_issue else []


def require_permission(journal, permission_id):
    issues = permission_issues(journal, permission_id)
    if issues:
        raise DeliveryError(" ".join(issues))
    return journal.record(permission_id)


def path_parts(value):
    require_text(value, "Path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] == ".signum" or "\x00" in value:
        raise DeliveryError(f"Use a relative path inside the project and outside .signum: {value!r}.")
    return parts


def file_revision(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def parent_directory(project, relative_path):
    parts = path_parts(relative_path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(project, directory_flags)
    try:
        for part in parts[:-1]:
            child = os.open(part, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory, parts[-1]
    finally:
        os.close(directory)


@contextmanager
def open_file(project, relative_path):
    with parent_directory(project, relative_path) as (directory, name):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise DeliveryError(f"Expected a regular file: {relative_path}.")
            yield stream
            after = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if file_revision(before) != file_revision(after):
                raise DeliveryError(f"File changed while being read: {relative_path}.")


def file_entry(project, path, deleted=False):
    try:
        with open_file(project, path) as stream:
            if deleted:
                raise DeliveryError(f"Deleted file is still present: {path}.")
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except FileNotFoundError:
        if deleted:
            return {"path": path, "status": "deleted"}
        raise
    return {"path": path, "sha256": digest.hexdigest()}


def artifact_entry(project, path):
    with open_file(project, path) as stream:
        raw = stream.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise DeliveryError("The check artifact must be a UTF-8 text file.") from None
    if "\x00" in content or not content.strip():
        raise DeliveryError("The check artifact must contain non-empty text.")
    return {"path": path, "sha256": hashlib.sha256(raw).hexdigest(), "text": content}


def write_check_report(directory, name, report):
    temporary = f".signum-check-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.fsync(directory)
    finally:
        os.unlink(temporary, dir_fd=directory)


def verification_context(report, journal, deliveries):
    matching = []
    for identifier in journal.confirmed.values():
        permission = journal.record(identifier)
        scope_text = permission.get("scope")
        if permission["kind"] != "permission" or not scope_guard.requires_bound_report(scope_text):
            continue
        scope = scope_guard.parse(scope_text)
        if scope["report"] == report:
            matching.append((identifier, scope))
    if not matching:
        return None
    active = [(identifier, scope) for identifier, scope in matching if deliveries.open_start(identifier)]
    if not active:
        raise DeliveryError("Start work under the confirmed permission before running its verification.")
    if len(active) != 1:
        raise DeliveryError("The check report matches multiple active permissions. Use distinct report paths.")
    identifier, scope = active[0]
    require_permission(journal, identifier)
    start = deliveries.open_start(identifier)
    if "scope_baseline" not in start:
        raise DeliveryError("Exact file scope has no baseline captured before work.")
    return scope, start


def run_check(project, args, context):
    require_text(args.name, "Check name")
    if len(args.argv) < 2 or args.argv[0] != "--":
        raise DeliveryError("Specify the explicit check command after --.")
    if args.report.partition("/")[0].casefold() in scope_guard.ADMIN:
        raise DeliveryError("Save the check report outside the project's .git and .signum directories.")
    with parent_directory(project, args.report) as (directory, name):
        try:
            os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise DeliveryError(f"The check report already exists: {args.report}.")
        if context is not None:
            scope, start = context
            before = scope_guard.capture(project, scope)
            if before["outside"] != start["scope_baseline"]["outside"]:
                raise DeliveryError("Workspace contains changes outside the exact file scope.")
        started = perf_counter()
        completed = subprocess.run(args.argv[1:], cwd=project, check=False)
        elapsed = perf_counter() - started
        report = {
            "checked_at": now().isoformat(),
            "checks": [{"name": args.name, "exit_code": completed.returncode, "elapsed_seconds": elapsed}],
        }
        if context is not None:
            if scope_guard.capture(project, scope) != before:
                raise DeliveryError("The workspace changed during verification. Run the check again on stable files.")
            report["verification"] = {
                "start": start["id"],
                "files": scope_guard.implementation_manifest(scope, start["scope_baseline"], before),
            }
        write_check_report(directory, name, report)
    return completed.returncode if completed.returncode >= 0 else 128 - completed.returncode


def snapshot(project, args):
    if not (args.files or args.deleted or args.artifact is not None):
        raise DeliveryError("Specify at least one file, deleted file or check artifact.")
    files = args.files + args.deleted
    if len(set(files)) != len(files):
        raise DeliveryError("Snapshot file paths must be distinct.")
    result = {}
    if files:
        removed = set(args.deleted)
        result["files"] = [file_entry(project, path, path in removed) for path in sorted(files)]
    if args.artifact is not None:
        artifact = artifact_entry(project, args.artifact)
        result["artifact"] = {key: artifact[key] for key in ("path", "sha256")}
    return result


def expected_hash(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def verify_files(project, files):
    if not isinstance(files, list) or not files:
        raise DeliveryError("Provide the snapshot of at least one changed project file.")
    checked = {}
    for expected in files:
        if not isinstance(expected, dict):
            raise DeliveryError("Each file must be a snapshot entry with path and sha256 or status deleted.")
        deleted = expected.keys() == {"path", "status"} and expected["status"] == "deleted"
        hashed = expected.keys() == {"path", "sha256"} and expected_hash(expected["sha256"])
        if not (deleted or hashed):
            raise DeliveryError("Each file must be a snapshot entry with path and sha256 or status deleted.")
        path = expected["path"]
        path_parts(path)
        if path in checked:
            raise DeliveryError(f"Duplicate changed file: {path}.")
        if file_entry(project, path, deleted) != expected:
            raise DeliveryError(f"Project file differs from the snapshot: {path}.")
        checked[path] = expected
    return [checked[path] for path in sorted(checked)]


def verify_artifact(project, expected):
    if (
        not isinstance(expected, dict)
        or expected.keys() != {"path", "sha256"}
        or not expected_hash(expected["sha256"])
    ):
        raise DeliveryError("The check artifact must be a snapshot entry with path and sha256.")
    actual = artifact_entry(project, expected["path"])
    if actual["sha256"] != expected["sha256"]:
        raise DeliveryError(f"Check artifact differs from the snapshot: {expected['path']}.")
    return actual


def unique_fields(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Repeated fields in the check artifact.")
    return result


def report_issue(content):
    try:
        report = json.loads(content, object_pairs_hook=unique_fields)
    except (ValueError, RecursionError):
        return "A passed verification requires a JSON check artifact with unambiguous fields."
    checks = report.get("checks") if isinstance(report, dict) else None
    if not isinstance(checks, list) or not checks:
        return "A passed verification requires a non-empty checks list in the artifact."
    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("name"), str)
            or not check["name"].strip()
            or type(check.get("exit_code")) is not int
        ):
            return "Every check needs a non-empty name and an integer exit_code."
        if check["exit_code"] != 0:
            return f"Check exit code {check['exit_code']} prevents a passed verification."
    return None


def validate_measurements(measurements):
    if not isinstance(measurements, dict) or measurements.keys() - MEASURES - {"source"}:
        raise DeliveryError("Use elapsed_seconds, input_tokens or output_tokens with a measurement source.")
    require_text(measurements.get("source"), "Measurement source")
    if not measurements.keys() & MEASURES:
        raise DeliveryError("Provide at least one measured value.")
    for field, value in measurements.items():
        if field == "source":
            continue
        if field == "elapsed_seconds":
            valid = (
                (type(value) is int and value >= 0)
                or (type(value) is float and math.isfinite(value) and value >= 0)
            )
        else:
            valid = type(value) is int and value >= 0
        if not valid:
            raise DeliveryError(f"{field} must be a finite, non-negative measurement.")


def read_evidence(project, data, permission, start):
    required = {"files", "artifact", "criteria", "result", "status"}
    optional = {"checked_at", "measurements"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - optional:
        raise DeliveryError("Evidence requires files, artifact, criteria, result and status.")
    if data["criteria"] != permission["verification"]:
        raise DeliveryError("Evidence must quote the exact verification criteria from the permission.")
    require_text(data["result"], "Result")
    if data["status"] not in ("pass", "fail"):
        raise DeliveryError("Use pass or fail for the verification status.")
    evidence = {
        "files": verify_files(project, data["files"]),
        "artifact": verify_artifact(project, data["artifact"]),
        "criteria": data["criteria"],
        "result": data["result"],
        "status": data["status"],
    }
    checked_at = utc(data["checked_at"], "checked_at") if "checked_at" in data else None
    if evidence["status"] == "pass":
        issue = report_issue(evidence["artifact"]["text"])
        if issue:
            raise DeliveryError(issue)
        if scope_guard.requires_bound_report(permission.get("scope")):
            report = json.loads(evidence["artifact"]["text"])
            if report.get("verification") != {"start": start["id"], "files": evidence["files"]}:
                raise DeliveryError("The check report does not match this start and the changed file snapshot. Run verify again.")
            reported_at = utc(report.get("checked_at"), "Report checked_at")
            if checked_at is not None and checked_at != reported_at:
                raise DeliveryError("Evidence checked_at differs from the check report.")
            checked_at = reported_at
    if checked_at is not None:
        if not utc(start["started_at"], "started_at") <= checked_at <= now():
            raise DeliveryError("The actual check time must be between the start and now.")
        evidence["checked_at"] = checked_at.isoformat()
    if "measurements" in data:
        validate_measurements(data["measurements"])
        evidence["measurements"] = data["measurements"]
    return evidence


def evidence_issues(project, evidence):
    issues = []
    for expected in evidence["files"]:
        path = expected["path"]
        try:
            actual = file_entry(project, path, expected.get("status") == "deleted")
        except (OSError, DeliveryError):
            actual = None
        if actual != expected:
            issues.append(f"Changed or unavailable project file: {path}.")
    expected = evidence["artifact"]
    path = expected["path"]
    try:
        actual = artifact_entry(project, path)
    except (OSError, DeliveryError):
        actual = None
    if actual != expected:
        issues.append(f"Changed or unavailable check artifact: {path}.")
    return issues


class Deliveries:
    def __init__(self):
        self.events = []
        self.starts = {}
        self.latest_start = {}
        self.versions = {}
        self.latest_version = {}
        self.decisions = {}

    @classmethod
    def read(cls, path, core):
        deliveries = cls()
        try:
            with open(path, encoding="utf-8", opener=core.no_follow) as stream:
                document = json.load(stream)
        except FileNotFoundError:
            return deliveries
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != core.SCHEMA_VERSION
            or not isinstance(document.get("events"), list)
        ):
            raise DeliveryError("Unsupported or invalid delivery journal format.")
        for event in document["events"]:
            deliveries.append(event)
        return deliveries

    def append(self, event):
        try:
            kind = event["event"]
            identifier = event["id"]
            utc(event["at"], "Delivery event time")
            body = {key: value for key, value in event.items() if key != "id"}
            if kind in ("started", "registered") and identifier != version_id(body):
                raise DeliveryError(f"Delivery journal content does not match its ID: {identifier}.")
            if kind == "started":
                permission_id = event["permission"]
                if identifier in self.starts or self.open_start(permission_id):
                    raise DeliveryError("Invalid duplicate start in delivery journal.")
                utc(event["started_at"], "Start time")
                require_text(event["source"], "Start source")
                self.starts[identifier] = event
                self.latest_start[permission_id] = identifier
            elif kind == "registered":
                permission_id = event["permission"]
                start = self.open_start(permission_id)
                if identifier in self.versions or start is None or start["id"] != event["start"]:
                    raise DeliveryError("Invalid registration in delivery journal.")
                self.versions[identifier] = event
                self.latest_version[permission_id] = identifier
            elif kind in ("accepted", "returned"):
                version = self.versions[identifier]
                if identifier in self.decisions or self.latest_version[version["permission"]] != identifier:
                    raise DeliveryError("Invalid decision in delivery journal.")
                require_text(event["reason"], "Owner's reason")
                require_text(event["source"], "Owner's decision source")
                self.decisions[identifier] = event
            else:
                raise DeliveryError("Unknown delivery journal event.")
        except (AttributeError, KeyError, TypeError):
            raise DeliveryError("Invalid delivery journal event.") from None
        self.events.append(event)

    def open_start(self, permission_id):
        identifier = self.latest_start.get(permission_id)
        if identifier is None:
            return None
        latest = self.versions.get(self.latest_version.get(permission_id))
        if latest and latest["start"] == identifier and latest["id"] in self.decisions:
            return None
        return self.starts[identifier]

    def start(self, project, journal, args):
        import capture_inbox
        permission = require_permission(journal, args.permission)
        capture_inbox.check_start(project, journal, permission)
        scope = scope_guard.parse(permission.get("scope"))
        scope_guard.check_touches(scope, args.touch)
        recorded_at = now()
        started_at = recorded_at
        source = args.source if args.source is not None else "Signum clock at start"
        require_text(source, "Start source")
        if args.started_at is not None:
            if scope is not None:
                raise DeliveryError("Exact file scope requires a fresh start before any work.")
            require_text(args.source, "Externally recorded start source")
            started_at = utc(args.started_at, "started_at")
            confirmed = next(
                event for event in journal.events
                if event["event"] == "confirmed" and event["id"] == args.permission
            )
            if not utc(confirmed["at"], "Permission confirmation time") <= started_at <= recorded_at:
                raise DeliveryError("The external start time must be between permission confirmation and now.")
        existing = self.open_start(args.permission)
        if existing:
            if scope is not None and "scope_baseline" not in existing:
                raise DeliveryError("Exact file scope has no baseline captured before work.")
            if existing["source"] == source and (
                args.started_at is None or existing["started_at"] == started_at.isoformat()
            ):
                return existing
            raise DeliveryError("Work already started for this permission.")
        event = {
            "event": "started",
            "permission": args.permission,
            "at": recorded_at.isoformat(),
            "started_at": started_at.isoformat(),
            "source": source,
        }
        if scope is not None:
            event["scope_baseline"] = scope_guard.baseline(project, scope)
        event["id"] = version_id(event)
        self.append(event)
        return event

    def register(self, project, journal, permission_id, data):
        permission = require_permission(journal, permission_id)
        start = self.open_start(permission_id)
        if start is None:
            raise DeliveryError("Start work under this permission before recording evidence.")
        evidence = read_evidence(project, data, permission, start)
        scope = scope_guard.parse(permission.get("scope"))
        result = None if scope is None else scope_guard.verify(project, scope, start.get("scope_baseline"), evidence)
        latest = self.versions.get(self.latest_version.get(permission_id))
        if (latest and latest["start"] == start["id"] and latest["evidence"] == evidence
                and latest.get("scope_result") == result):
            return self.card(project, journal, latest["id"], scope_checked=True)
        event = {
            "event": "registered",
            "permission": permission_id,
            "start": start["id"],
            "at": now().isoformat(),
            "evidence": evidence,
        }
        if scope is not None:
            event["scope_result"] = result
        event["id"] = version_id(event)
        self.append(event)
        return self.card(project, journal, event["id"], scope_checked=True)

    def card(self, project, journal, identifier, scope_checked=False):
        version = self.versions.get(identifier)
        if version is None:
            raise DeliveryError("Use the full ID of a recorded delivery version.")
        permission_id = version["permission"]
        current_version = self.latest_version[permission_id]
        decision = self.decisions.get(identifier)
        evidence = version["evidence"]
        start = self.starts[version["start"]]
        permission = journal.record(permission_id) if permission_id in journal.accepted else None
        issues = permission_issues(journal, permission_id)
        if identifier != current_version:
            issues.append(f"A later delivery version exists: {current_version}.")
        if permission_id in journal.accepted and evidence["criteria"] != journal.record(permission_id).get("verification"):
            issues.append("The recorded criteria do not match the permission.")
        if evidence["status"] != "pass":
            issues.append("The recorded verification did not pass.")
        else:
            issue = report_issue(evidence["artifact"]["text"])
            if issue:
                issues.append(issue)
        if decision and decision["event"] == "returned":
            issues.append("The owner returned this version for rework.")
        if identifier == current_version:
            issues.extend(evidence_issues(project, evidence))
            scope = scope_guard.parse(permission.get("scope")) if permission else None
            if scope is not None and decision is None and not scope_checked:
                try:
                    result = scope_guard.verify(project, scope, start.get("scope_baseline"), evidence)
                    if result != version.get("scope_result"):
                        issues.append("Exact scope file metadata changed since registration.")
                except (scope_guard.ScopeError, OSError) as error:
                    issues.append(str(error))
        if decision:
            state = decision["event"]
        else:
            state = "review" if identifier == current_version else "superseded"
        return {
            "id": identifier,
            "permission": {"id": permission_id, **permission} if permission else {"id": permission_id},
            "start": start,
            "recorded_at": version["at"],
            "evidence": evidence,
            "state": state,
            "owner_decision": decision,
            "acceptance_blockers": issues,
        }

    def decide(self, project, journal, args):
        require_text(args.reason, "Owner's reason")
        require_text(args.source, "Owner's decision source")
        card = self.card(project, journal, args.id)
        kind = "accepted" if args.command == "accept" else "returned"
        existing = self.decisions.get(args.id)
        if existing and (existing["event"], existing["reason"], existing["source"]) != (
            kind, args.reason, args.source
        ):
            raise DeliveryError("This delivery version already has an owner decision.")
        if card["state"] == "superseded":
            raise DeliveryError("A later delivery version exists. Review the current version.")
        if args.command == "accept" and card["acceptance_blockers"]:
            raise DeliveryError(" ".join(card["acceptance_blockers"]))
        if existing:
            return existing
        event = {
            "event": kind,
            "id": args.id,
            "at": now().isoformat(),
            "reason": args.reason,
            "source": args.source,
        }
        self.append(event)
        return event


def run(args, project, directory, core):
    intent = directory / "state.json"
    path = directory / "delivery.json"
    if args.command == "snapshot":
        return snapshot(project, args)
    if args.command == "verify":
        journal = core.Journal.read(intent, required=True)
        deliveries = Deliveries.read(path, core)
        context = verification_context(args.report, journal, deliveries)
        raise SystemExit(run_check(project, args, context))
    if args.command == "delivery":
        if not path.exists() and not path.is_symlink():
            if args.id is not None:
                raise DeliveryError("Use the full ID of a recorded delivery version.")
            return {"starts": [], "deliveries": []}
        deliveries = Deliveries.read(path, core)
        journal = core.Journal.read(intent, required=True)
        if args.id is not None:
            return deliveries.card(project, journal, args.id)
        return {
            "starts": list(deliveries.starts.values()),
            "deliveries": [deliveries.card(project, journal, key) for key in deliveries.versions],
        }
    core.Journal.read(intent, required=True)
    if args.command == "evidence":
        if args.input == "-":
            data = json.load(sys.stdin)
        else:
            with Path(args.input).open(encoding="utf-8") as stream:
                data = json.load(stream)
    with core.locked_store(directory):
        deliveries = Deliveries.read(path, core)
        journal = core.Journal.read(intent, required=True)
        before = len(deliveries.events)
        if args.command == "start":
            result = deliveries.start(project, journal, args)
        elif args.command == "evidence":
            result = deliveries.register(project, journal, args.permission, data)
        else:
            result = deliveries.decide(project, journal, args)
        if len(deliveries.events) != before:
            core.write_journal(path, deliveries)
        return result
