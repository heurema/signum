"""Local intent journal and contract draft for the Signum skill."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import unicodedata
from uuid import uuid4

import fcntl
import decision_capture
import delivery
import scope_guard


TOPIC_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
ID_PATTERN = re.compile(r"[0-9a-f]{24}")
NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")
OWNER_TURN_PATTERN = re.compile(r"codex:turn:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
KINDS = {"decision", "permission", "check"}
REQUIRED = {"topic", "kind", "summary", "source", "reason", "replaces"}
OPTIONAL = {"questions", "scope", "verification", "authority_limit", "basis", "depends_on"}
BASIS_KIND = {"permission": "decision", "check": "permission"}
ASSENT_REPLIES = {"да", "согласен", "согласна", "подтверждаю", "yes", "agree", "i agree", "i confirm"}
SCHEMA_VERSION = 1
PROJECT_POINTER = (
    "This project uses Signum. Use the installed project-intent skill on project kickoff, "
    "consequential decisions and scoped changes. Orient from current files and prior owner "
    "decisions before choosing work. For next-step questions run the installed project-intent "
    "skill's read-only scripts/signum.py --project ROOT next --query \"topic words\" and answer "
    "from the result. Keep routine edits in ordinary chat.\n"
)


class SignumError(Exception):
    pass


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def matches(pattern, value):
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def validate_record(record):
    if not isinstance(record, dict):
        raise SignumError("Expected a JSON object for the proposal.")
    missing = REQUIRED - record.keys()
    unknown = record.keys() - REQUIRED - OPTIONAL
    if missing:
        raise SignumError(f"Missing proposal fields: {', '.join(sorted(missing))}.")
    if unknown:
        raise SignumError(f"Unknown proposal fields: {', '.join(sorted(unknown))}.")
    if not matches(TOPIC_PATTERN, record["topic"]):
        raise SignumError("Use 1–64 ASCII letters, digits, dots, underscores or hyphens for the topic, starting with a letter or digit.")
    kind = record["kind"]
    if not isinstance(kind, str) or kind not in KINDS:
        raise SignumError("Use decision, permission or check for kind.")
    for field in ("summary", "source", "reason", "scope", "verification", "authority_limit"):
        if field in record and not nonempty(record[field]):
            raise SignumError(f"{field} must be non-empty text.")
    if record["replaces"] is not None and not matches(ID_PATTERN, record["replaces"]):
        raise SignumError("replaces must be null or the ID of a confirmed version.")
    questions = record.get("questions", [])
    if not isinstance(questions, list) or not all(nonempty(item) for item in questions):
        raise SignumError("questions must be a list of non-empty strings.")
    if "depends_on" in record:
        dependencies = record["depends_on"]
        if kind != "permission" or not isinstance(dependencies, list):
            raise SignumError("depends_on must be a list of decision versions on a permission.")
        topics = set()
        for item in dependencies:
            if (not isinstance(item, dict) or item.keys() != {"topic", "id"}
                    or not matches(TOPIC_PATTERN, item["topic"]) or not matches(ID_PATTERN, item["id"])
                    or item["topic"] == record["topic"] or item["topic"] in topics):
                raise SignumError("Each dependency must name a distinct decision topic and version.")
            topics.add(item["topic"])
    if "basis" in record:
        basis = record["basis"]
        if kind not in BASIS_KIND:
            raise SignumError("A decision cannot have a basis version.")
        if (
            not isinstance(basis, dict)
            or basis.keys() != {"topic", "id"}
            or not matches(TOPIC_PATTERN, basis["topic"])
            or not matches(ID_PATTERN, basis["id"])
            or basis["topic"] == record["topic"]
        ):
            raise SignumError("basis must name another topic and its confirmed version ID.")
    elif kind == "check":
        raise SignumError("A check requires a basis identifying the permission it checked.")


def proposal_input(record):
    if not isinstance(record, dict) or "owner_turn" not in record:
        validate_record(record)
        return record

    record = record.copy()
    owner_turn = record.pop("owner_turn")
    validate_record(record)
    if record["kind"] != "permission" or not matches(OWNER_TURN_PATTERN, owner_turn):
        raise SignumError("owner_turn must identify a specific permission request as codex:turn:<lowercase UUID>.")
    record["source"] = f"{owner_turn}\n\n{record['source']}"
    return record


def assent_text(value):
    return " ".join(unicodedata.normalize("NFC", value).split())


def unique_json_object(pairs, *, operation="Assent"):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SignumError(f"{operation} input contains a duplicate JSON field.")
        result[key] = value
    return result


def read_prepare(stream, topic):
    terms = json.load(stream, object_pairs_hook=lambda pairs: unique_json_object(pairs, operation="Prepare"))
    fields = {"summary", "source", "reason"}
    if not isinstance(terms, dict) or terms.keys() not in (fields, fields | {"expected"}):
        raise SignumError("Prepare requires summary, source and reason, with optional expected.")
    expected_supplied = "expected" in terms
    expected = terms.pop("expected", None)
    if expected is not None and not matches(ID_PATTERN, expected):
        raise SignumError("expected must be null or the full ID of the current confirmed version.")
    record = {"topic": topic, "kind": "decision", "replaces": expected, **terms}
    validate_record(record)
    return record, expected_supplied


def read_draft(stream, topic):
    terms = json.load(stream, object_pairs_hook=lambda pairs: unique_json_object(pairs, operation="Draft"))
    required = {"request", "outcome", "rationale", "excludes", "files", "report", "verification"}
    if not isinstance(terms, dict) or not required <= terms.keys() <= required | {"expected", "basis", "owner_turn", "depends_on"}:
        raise SignumError("Draft requires request, outcome, rationale, excludes, files, report and verification.")
    record = {
        "kind": "permission", "topic": topic, "replaces": terms.get("expected"),
        "source": terms["request"], "summary": terms["outcome"], "reason": terms["rationale"],
        "authority_limit": terms["excludes"], "verification": terms["verification"],
        "scope": scope_guard.encode(terms["files"], terms["report"]),
    }
    for field in ("basis", "owner_turn", "depends_on"):
        if field in terms:
            record[field] = terms[field]
    return proposal_input(record)


def read_assent(stream):
    evidence = json.load(stream, object_pairs_hook=unique_json_object)
    if not isinstance(evidence, dict) or evidence.keys() not in (
        {"reply", "shown", "id"}, {"reply", "shown", "record"},
    ):
        raise SignumError("Assent requires reply and shown, and exactly one of id or record.")
    if not nonempty(evidence["reply"]):
        raise SignumError("Assent requires the owner's reply.")
    reply = assent_text(evidence["reply"]).lower()
    if reply.endswith((".", "!")):
        reply = reply[:-1]
    if reply not in ASSENT_REPLIES:
        raise SignumError("The reply is not a supported standalone expression of assent.")
    if not nonempty(evidence["shown"]):
        raise SignumError("Assent requires the exact displayed decision text.")
    if "id" in evidence:
        if not matches(ID_PATTERN, evidence["id"]):
            raise SignumError("Assent requires the full ID of the displayed proposal.")
    else:
        validate_record(evidence["record"])
    return evidence


def validate_assent_terms(record, shown):
    if record["kind"] != "decision" or record.keys() != REQUIRED:
        raise SignumError("Assent only supports decisions with the complete rule in summary.")
    if assent_text(record["summary"]) != assent_text(shown):
        raise SignumError("The displayed text differs from the decision summary.")


def proposal_id(record, nonce=None):
    encoded = json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    if nonce is not None:
        encoded = f"{nonce}\n{encoded}"
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self):
        self.enabled = False
        self.events = []
        self.proposed = {}
        self.confirmed = {}
        self.accepted = set()
        self.withdrawn = {}
        self.reviewed = {}
        self.owner_evidence = {}

    @classmethod
    def read(cls, path, *, required=False):
        journal = cls()
        try:
            with open(path, encoding="utf-8", opener=no_follow) as stream:
                document = json.load(
                    stream, object_pairs_hook=lambda pairs: unique_json_object(pairs, operation="Journal"),
                )
        except FileNotFoundError:
            if required:
                raise SignumError("Signum is disabled for this project. Run init to enable it.") from None
            return journal
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != SCHEMA_VERSION
            or not isinstance(document.get("events"), list)
        ):
            raise SignumError("Unsupported or invalid journal format.")
        for event in document["events"]:
            journal.append(event)
        decision_capture.load_reviews(sys.modules[__name__], journal, document.get("capture_reviews", []))
        journal.enabled = True
        return journal

    def append(self, event):
        if (
            not isinstance(event, dict)
            or not matches(ID_PATTERN, event.get("id"))
            or not nonempty(event.get("at"))
        ):
            raise SignumError("Invalid journal event.")
        identifier = event["id"]
        if event.get("event") == "proposed":
            record = event.get("record")
            validate_record(record)
            nonce = event.get("nonce")
            if "nonce" in event and not matches(NONCE_PATTERN, nonce):
                raise SignumError(f"Invalid proposal nonce for {identifier}.")
            if identifier != proposal_id(record, nonce) or identifier in self.proposed:
                raise SignumError(f"Proposal content does not match its ID, or ID is duplicated: {identifier}.")
            self.validate_references(record)
            self.proposed[identifier] = event
        elif event.get("event") == "confirmed":
            if identifier not in self.proposed or identifier in self.accepted or identifier in self.withdrawn:
                raise SignumError(f"Invalid confirmation in journal: {identifier}.")
            record = self.record(identifier)
            issues = self.confirmation_issues(record)
            if issues:
                raise SignumError(f"Cannot confirm {identifier}. " + " ".join(issues))
            self.confirmed[record["topic"]] = identifier
            self.accepted.add(identifier)
            if "owner_evidence" in event:
                if record["kind"] != "decision" or not nonempty(event["owner_evidence"]):
                    raise SignumError("Invalid decision confirmation source.")
                self.owner_evidence[identifier] = event["owner_evidence"]
        elif event.get("event") == "withdrawn":
            if (
                identifier not in self.proposed
                or identifier in self.accepted
                or identifier in self.withdrawn
                or not nonempty(event.get("reason"))
            ):
                raise SignumError(f"Invalid withdrawal in journal: {identifier}.")
            self.withdrawn[identifier] = event
        else:
            raise SignumError("Unknown journal event.")
        self.events.append(event)

    def record(self, identifier):
        return self.proposed[identifier]["record"]

    def validate_references(self, record):
        current = self.confirmed.get(record["topic"])
        if current is not None and self.record(current)["kind"] != record["kind"]:
            raise SignumError("A topic keeps its original kind.")

        replaced = record["replaces"]
        if replaced is not None and (
            replaced not in self.accepted or self.record(replaced)["topic"] != record["topic"]
        ):
            raise SignumError("replaces must identify a confirmed version of this topic.")

        basis = record.get("basis")
        if basis is not None and (
            basis["id"] not in self.accepted
            or self.record(basis["id"])["topic"] != basis["topic"]
            or self.record(basis["id"])["kind"] != BASIS_KIND[record["kind"]]
        ):
            raise SignumError(f"basis must identify a confirmed {BASIS_KIND[record['kind']]} of its topic.")

        for dependency in record.get("depends_on", []):
            topic, version = dependency["topic"], dependency["id"]
            if (version not in self.accepted or self.record(version)["kind"] != "decision"
                    or self.record(version)["topic"] != topic):
                raise SignumError(f"Unknown confirmed decision dependency: {topic}.")

    def basis_issues(self, record):
        dependencies = record.get("depends_on", [])
        issues = [f"Decision {d['topic']} changed from {d['id']} to {self.confirmed.get(d['topic'])}."
                  for d in dependencies if self.confirmed.get(d["topic"]) != d["id"]]
        basis = record.get("basis")
        if basis is not None:
            current = self.confirmed[basis["topic"]]
            if current != basis["id"]:
                issues.append(f"Basis {basis['topic']} changed from {basis['id']} to {current}.")
            elif record["kind"] == "check":
                issues.extend(self.basis_issues(self.record(current)))
        return issues

    def confirmation_issues(self, record):
        current = self.confirmed.get(record["topic"])
        issues = self.basis_issues(record)
        if current != record["replaces"]:
            issues.insert(0, f"Topic {record['topic']} changed. Current version: {current}.")
        return issues

    def require_permission_origin(self, record):
        # Enforce on new writes so previously confirmed records can still be replayed.
        if record["kind"] != "permission":
            return
        owner_turn, _, request = record["source"].partition("\n\n")
        if "basis" not in record and not (matches(OWNER_TURN_PATTERN, owner_turn) and nonempty(request)):
            raise SignumError(
                "A permission requires a current confirmed basis or an owner_turn "
                "with the complete verbatim owner request."
            )
        self.validate_references(record)
        issues = self.basis_issues(record)
        if issues:
            raise SignumError(" ".join(issues))

    def propose(self, record):
        self.require_permission_origin(record)
        for identifier, event in self.proposed.items():
            if identifier in self.withdrawn or event["record"] != record:
                continue
            if identifier in self.accepted:
                raise SignumError(f"Version {identifier} already has a confirmation.")
            break
        else:
            nonce = uuid4().hex
            identifier = proposal_id(record, nonce)
            self.append({
                "event": "proposed",
                "id": identifier,
                "record": record,
                "at": timestamp(),
                "nonce": nonce,
            })
        return next(item for item in self.status(record["topic"])["proposals"] if item["id"] == identifier)

    def prepare(self, record, expected_supplied):
        topic = record["topic"]
        current = self.confirmed.get(topic)
        if expected_supplied and record["replaces"] != current:
            raise SignumError(f"Topic {topic} changed. Current version: {current}.")
        if current is None and not expected_supplied:
            raise SignumError(f"Topic {topic} has no confirmed version. Use expected: null to prepare a new decision.")

        summary = record["summary"]
        current_card = None
        if current is not None:
            confirmed = self.record(current)
            if confirmed["kind"] != "decision" or confirmed.keys() != REQUIRED:
                raise SignumError(f"Prepare only supports plain decisions. Current version: {current}.")
            current_card = {"id": current, "summary": confirmed["summary"]}
            if confirmed["summary"] == summary:
                return {"result": "already_current", "current": current_card, "proposal": None}

        proposals = self.status(topic)["proposals"]
        alternatives = [item for item in proposals if item["state"] != "conflict"]
        if alternatives:
            identifier = alternatives[0]["id"]
            pending = self.record(identifier)
            if (
                len(alternatives) != 1
                or pending["kind"] != "decision"
                or pending.keys() != REQUIRED
                or pending["replaces"] != current
                or pending["summary"] != summary
            ):
                identifiers = ", ".join(item["id"] for item in alternatives)
                raise SignumError(f"Topic {topic} has other live proposals: {identifiers}.")
        else:
            if not expected_supplied and any(assent_text(item["summary"]) == assent_text(summary) for item in proposals):
                raise SignumError(
                    f"A proposal with this wording has an obsolete basis. Review the current version {current} "
                    "and supply its ID as expected to prepare a new proposal."
                )
            identifier = self.propose({**record, "replaces": current})["id"]

        return {
            "result": "prepared",
            "current": current_card,
            "proposal": {"id": identifier, "summary": summary},
        }

    def confirm(self, identifier, *, owner_evidence=None):
        if identifier not in self.proposed:
            raise SignumError(f"No proposal matches {identifier}. Use the full ID from the displayed proposal.")
        if identifier in self.withdrawn:
            raise SignumError(f"Proposal {identifier} has been withdrawn.")
        record = self.record(identifier)
        if identifier not in self.accepted:
            self.require_permission_origin(record)
            event = {"event": "confirmed", "id": identifier, "at": timestamp()}
            if owner_evidence is not None:
                event["owner_evidence"] = owner_evidence
            self.append(event)
        elif self.confirmed[record["topic"]] != identifier or self.basis_issues(record):
            raise SignumError(f"Confirmed version {identifier} is no longer current.")
        return self.confirmed_entry(identifier)

    def assent(self, evidence):
        if "id" in evidence:
            identifier = evidence["id"]
            if identifier not in self.proposed:
                raise SignumError(f"No proposal matches {identifier}. Use the full ID from the displayed proposal.")
            record = self.record(identifier)
            validate_assent_terms(record, evidence["shown"])
            pending = self.status(record["topic"])["proposals"]
            if any(item["id"] == identifier and item["state"] == "competing" for item in pending):
                raise SignumError("The topic has competing proposals. Confirm an explicit choice after review.")
        else:
            record = evidence["record"]
            validate_assent_terms(record, evidence["shown"])
            identifier = self.late_assent_target(record)

        already_current = identifier in self.accepted
        entry = self.confirm(identifier)
        return {
            "result": "already_current" if already_current else "confirmed",
            "topic": entry["topic"],
            "id": identifier,
        }

    def late_assent_target(self, record):
        topic = record["topic"]
        current = self.confirmed.get(topic)
        pending = self.status(topic)["proposals"]
        same_current = current is not None and self.record(current) == record
        summary = assent_text(record["summary"])
        for identifier in self.accepted | self.withdrawn.keys():
            if same_current and identifier == current:
                continue
            previous = self.record(identifier)
            if previous["topic"] == topic and assent_text(previous["summary"]) == summary:
                raise SignumError("A confirmed or withdrawn version already used this wording. Use a fresh displayed proposal ID.")

        if same_current and not pending:
            return current
        issues = self.confirmation_issues(record)
        if issues:
            raise SignumError(" ".join(issues))
        self.validate_references(record)

        if pending:
            if len(pending) == 1 and self.record(pending[0]["id"]) == record:
                return pending[0]["id"]
            raise SignumError("The topic has other proposals. Use the full ID of the displayed proposal.")
        return self.propose(record)["id"]

    def withdraw(self, identifier, reason):
        if identifier not in self.proposed:
            raise SignumError(f"No proposal matches {identifier}. Use the full ID from the displayed proposal.")
        if identifier in self.accepted:
            raise SignumError(f"Version {identifier} already has a confirmation. Propose a correction instead.")
        if not nonempty(reason):
            raise SignumError("A withdrawal requires a non-empty reason.")
        if identifier not in self.withdrawn:
            self.append({"event": "withdrawn", "id": identifier, "at": timestamp(), "reason": reason})
        event = self.withdrawn[identifier]
        if event["reason"] != reason:
            raise SignumError(f"Proposal {identifier} was already withdrawn with a different reason.")
        return event

    def confirmed_entry(self, identifier):
        record = self.record(identifier)
        issues = self.basis_issues(record)
        entry = {"id": identifier, **record, "state": "stale" if issues else "current", "issues": issues}
        if identifier in self.owner_evidence:
            entry["owner_evidence"] = self.owner_evidence[identifier]
        return entry

    def status(self, topic=None):
        confirmed = [
            self.confirmed_entry(identifier)
            for name, identifier in self.confirmed.items()
            if topic is None or name == topic
        ]
        proposals = []
        by_topic = {}
        for identifier, event in self.proposed.items():
            record = event["record"]
            if (
                identifier in self.accepted
                or identifier in self.withdrawn
                or (topic is not None and topic != record["topic"])
            ):
                continue
            issues = self.confirmation_issues(record)
            state = "conflict" if issues else "pending"
            entry = {"id": identifier, **record, "state": state, "issues": issues}
            proposals.append(entry)
            if not issues:
                by_topic.setdefault(record["topic"], []).append(entry)
        for entries in by_topic.values():
            if len(entries) > 1:
                for entry in entries:
                    entry["state"] = "competing"
                    entry["competing_with"] = [other["id"] for other in entries if other is not entry]
        return {"confirmed": confirmed, "proposals": proposals}

    def history(self, topic=None):
        return {
            "events": [
                event for event in self.events
                if topic is None or self.record(event["id"])["topic"] == topic
            ],
        }

    def contract(self, topic=None):
        status = self.status()
        alternatives = {}
        for proposal in status["proposals"]:
            if proposal["state"] == "competing":
                alternatives.setdefault(proposal["topic"], []).append(proposal["id"])
        if topic is not None:
            status = {
                key: [item for item in entries if item["topic"] == topic]
                for key, entries in status.items()
            }
        scope = []
        blocked = []
        missing_verification = []
        for entry in status["confirmed"]:
            if entry["state"] == "stale":
                required = (
                    "Confirm revised permission for the current basis."
                    if entry["kind"] == "permission"
                    else "Record a fresh check against the current confirmed permission."
                )
                blocked.append({"topic": entry["topic"], "id": entry["id"], "required": required})
            elif entry["kind"] == "permission":
                affected_topics = [entry["topic"]]
                if "basis" in entry:
                    affected_topics.append(entry["basis"]["topic"])
                disputes = {key: alternatives[key] for key in affected_topics if key in alternatives}
                if disputes:
                    blocked.append({
                        "topic": entry["topic"],
                        "id": entry["id"],
                        "required": "Resolve competing proposals before including this permission in the draft.",
                        "alternatives": disputes,
                    })
                    continue
                verification = entry.get("verification")
                if verification is None:
                    missing_verification.append({"topic": entry["topic"], "id": entry["id"]})
                scope.append({
                    "topic": entry["topic"],
                    "id": entry["id"],
                    "summary": entry["summary"],
                    "scope": entry.get("scope"),
                    "verification": verification,
                    "authority_limit": entry.get("authority_limit"),
                })
        status["draft"] = {
            "scope": scope,
            "blocked": blocked,
            "missing_verification": missing_verification,
            "authority_limit": "Use the confirmed scope. When the user's request changes an agreed method or boundary, first ask whether to replace the current choice with the proposed one. Wait for that answer before choosing implementation details or executing the change. New work and releases need their own authorization.",
        }
        if missing_verification:
            status["draft"]["verification_note"] = "Ask how to verify each listed item before judging its result."
        target = self.confirmed.get(topic)
        if target is not None and self.record(target)["kind"] == "permission":
            checks = []
            for event in self.events:
                if event["event"] != "confirmed":
                    continue
                identifier = event["id"]
                record = self.record(identifier)
                if record["kind"] != "check" or record["basis"]["topic"] != topic:
                    continue
                current = self.confirmed[record["topic"]] == identifier and not self.basis_issues(record)
                checks.append({
                    "id": identifier, "topic": record["topic"], "basis_id": record["basis"]["id"],
                    "relation": "current" if current else "historical",
                    "observation": record["summary"], "source": record["source"],
                })
            status["related_checks"] = checks
        return status


def no_follow(path, flags, *, dir_fd=None):
    descriptor = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise SignumError("Expected a regular file: " + str(path))
    return descriptor


def journal_directory(project):
    directory = project / ".signum"
    try:
        mode = directory.lstat().st_mode
    except FileNotFoundError:
        return directory
    if not stat.S_ISDIR(mode):
        raise SignumError("The .signum path must be a real directory inside the project.")
    return directory


@contextmanager
def locked_store(directory, *, create=False):
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    store = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            lock_file = no_follow("write.lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=store)
        except FileExistsError:
            lock_file = no_follow("write.lock", os.O_WRONLY, dir_fd=store)
        with os.fdopen(lock_file, "wb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not os.path.samestat(os.fstat(store), directory.lstat()):
                raise SignumError("The .signum directory changed while being opened.")
            yield
    finally:
        os.close(store)


def write_journal(path, journal):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=".journal-", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            document = {"schema_version": SCHEMA_VERSION, "events": journal.events}
            if getattr(journal, "reviewed", None):
                document["capture_reviews"] = list(journal.reviewed.values())
            write_json(stream, document)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_json(stream, value):
    json.dump(value, stream, ensure_ascii=False, indent=2)
    stream.write("\n")


def write_project_pointer(project):
    path = project / "AGENTS.md"
    try:
        path.lstat()
    except FileNotFoundError:
        pass
    else:
        return "retained"
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=project, prefix=".signum-agents-", delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            stream.write(PROJECT_POINTER)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            return "retained"
        return "created"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def read_initialization_state(directory):
    delivery_path = directory / "delivery.json"
    try:
        delivery_path.lstat()
    except FileNotFoundError:
        return Journal.read(directory / "state.json")
    journal = Journal.read(directory / "state.json")
    if not journal.enabled:
        raise SignumError("The project intent journal is missing while a delivery journal exists.")
    delivery.Deliveries.read(delivery_path, core=sys.modules[__name__])
    return journal


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="existing project directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("next", help="read the next step under an existing permission").add_argument(
        "--query", help="permission topic or distinctive words",
    )
    commands.add_parser("init", help="enable Signum in this project")
    commands.add_parser("capture", help="record source-checked choices from a Codex turn")
    for name in ("status", "history", "contract"):
        commands.add_parser(name).add_argument("--topic", help="show one topic")
    commands.add_parser("draft", help="draft a file-scoped contract from the request and intended outcome").add_argument(
        "--topic", required=True,
    )
    commands.add_parser("propose").add_argument("--input", default="-", help="JSON file or - for stdin")
    commands.add_parser("prepare", help="prepare a plain decision from JSON on stdin").add_argument(
        "--topic", required=True, help="topic of the displayed decision",
    )
    commands.add_parser("confirm").add_argument("id", help="full ID of the displayed proposal")
    commands.add_parser("assent", help="confirm a displayed decision from a JSON transcript excerpt on stdin")
    withdraw = commands.add_parser("withdraw")
    withdraw.add_argument("id", help="full ID of the unconfirmed proposal")
    withdraw.add_argument("--reason", required=True, help="reason for withdrawing the proposal")
    delivery.add_commands(commands)
    return parser.parse_args()


def run(args):
    project = args.project.resolve(strict=True)
    if not project.is_dir():
        raise SignumError("--project must be an existing directory.")
    directory = journal_directory(project)
    path = directory / "state.json"
    if args.command == "next":
        import next_step
        return next_step.run(project, directory, sys.modules[__name__], args.query)
    if args.command == "init":
        read_initialization_state(directory)
        with locked_store(directory, create=True):
            journal = read_initialization_state(directory)
            if not journal.enabled:
                write_journal(path, journal)
            pointer = write_project_pointer(project)
        return {
            "project": str(project), "enabled": True, "initialized": not journal.enabled, "agents_md": pointer,
        }
    if args.command in delivery.COMMANDS:
        return delivery.run(args, project, directory, core=sys.modules[__name__])
    if args.command in ("status", "history", "contract"):
        journal = Journal.read(path)
        result = getattr(journal, args.command)(args.topic)
        if args.command == "status":
            result["enabled"] = journal.enabled
        return result

    Journal.read(path, required=True)
    if args.command == "capture":
        import chat_source
        payload = json.load(sys.stdin, object_pairs_hook=lambda pairs: unique_json_object(pairs, operation="Capture"))
        try:
            return decision_capture.capture(sys.modules[__name__], project, directory, payload, chat_source.read_turn)
        except chat_source.ChatSourceError as error:
            raise SignumError(str(error)) from error
    if args.command == "draft":
        record = read_draft(sys.stdin, args.topic)
    elif args.command == "propose":
        if args.input == "-":
            record = json.load(sys.stdin)
        else:
            with Path(args.input).open(encoding="utf-8") as stream:
                record = json.load(stream)
        record = proposal_input(record)
    elif args.command == "prepare":
        record, expected_supplied = read_prepare(sys.stdin, args.topic)
    elif args.command == "assent":
        evidence = read_assent(sys.stdin)
    with locked_store(directory):
        journal = Journal.read(path, required=True)
        before = len(journal.events)
        if args.command in ("draft", "propose") and record["kind"] == "permission":
            import capture_inbox
            if capture_inbox.active(project) and "depends_on" not in record and "basis" not in record:
                raise SignumError("List the decision versions this permission depends on, or use depends_on: [] for independent work.")
        if args.command == "draft":
            current = journal.confirmed.get(args.topic)
            if current != record["replaces"]:
                raise SignumError(f"Topic {args.topic} changed. Read the current contract first: {current}.")
            result = journal.propose(record)
            result["scope"] = scope_guard.parse(record["scope"])
        elif args.command == "propose":
            result = journal.propose(record)
        elif args.command == "prepare":
            result = journal.prepare(record, expected_supplied)
        elif args.command == "assent":
            result = journal.assent(evidence)
        elif args.command == "withdraw":
            result = journal.withdraw(args.id, args.reason)
        else:
            result = journal.confirm(args.id)
        if len(journal.events) != before:
            write_journal(path, journal)
    return result


def main():
    try:
        write_json(sys.stdout, run(arguments()))
    except (SignumError, OSError, ValueError) as error:
        print(f"signum: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
