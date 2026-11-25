"""Record a sanitized pilot observation and acknowledge it against a designated Signum backlog."""

import argparse
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from uuid import uuid4

import signum


QUEUE = "feedback.json"
BACKLOG = "plugin-feedback.md"
ID = r"SGF-[0-9]{3,}"
ISSUE_LINE = re.compile(rf"^(?:\| ({ID}) \||### ({ID})$)", re.M)
MARKER = re.compile(rf"<!-- signum-feedback ([0-9a-f]{{64}}) ([a-z0-9.-]+) ({ID}) -->")
KINDS = {"bug": "Сбой", "friction": "Неудобство", "idea": "Предложение"}
TEXT_FIELDS = ("project", "symptom", "source", "step", "expected", "observed", "impact", "kind")


def validate_record(record):
    if not isinstance(record, dict) or set(record) - {"issue"} != set(TEXT_FIELDS):
        raise ValueError("Expected project, symptom, source, step, expected, observed, impact, kind and optional issue.")
    for field in TEXT_FIELDS:
        value = record[field]
        if (not isinstance(value, str) or not value.strip() or len(value) > 600 or
                value != value.strip() or any(unicodedata.category(c).startswith("C") for c in value)):
            raise ValueError(f"Expected short single-line text in {field}.")
    if not re.fullmatch("[a-z0-9][a-z0-9.-]{0,79}", record["symptom"]):
        raise ValueError("Use a stable lowercase slug for the symptom.")
    if record["kind"] not in KINDS:
        raise ValueError("Use bug, friction or idea for kind.")
    if "issue" in record and not (isinstance(record["issue"], str) and re.fullmatch(ID, record["issue"])):
        raise ValueError("Invalid existing issue number.")
    return record


def episode_key(record):
    identity = [record["project"].casefold(), record["source"], record["symptom"]]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


def cell(value):
    # Keep supplied text inside its own cell even when it contains Markdown or HTML.
    return "".join(f"&#{ord(c)};" if c in "&<>|\\[]()`!*_~" else c for c in value)


def add_episode(document, key, record):
    rows = document.splitlines(keepends=True)
    existing = {first or second for first, second in ISSUE_LINE.findall(document)}
    matches = MARKER.findall(document)
    prior = {issue for episode, _, issue in matches if episode == key}
    family = {issue for _, symptom, issue in matches if symptom == record["symptom"]}
    numbers = prior or family
    if "issue" in record:
        numbers = numbers | {record["issue"]}
    if len(numbers) > 1 or numbers - existing:
        raise ValueError("The observation's existing central issue is missing or ambiguous.")
    if prior:
        return document, next(iter(prior))

    heading = rows.index("## Открыто\n") if "## Открыто\n" in rows else None
    if heading is None:
        raise ValueError("Central feedback table missing.")
    top = next((i for i in range(heading + 1, len(rows)) if rows[i].startswith("| ID |")), None)
    if top is None or top + 1 >= len(rows) or rows[top + 1].strip() != "|---|---|---|---|---|":
        raise ValueError("Central feedback table missing.")
    issue = next(iter(numbers)) if numbers else f"SGF-{1 + max((int(n[4:]) for n in existing), default=0):03d}"
    marker = f"<!-- signum-feedback {key} {record['symptom']} {issue} -->"
    where = f"{cell(record['project'])}, {cell(record['step'])}"
    observation = (f"Ожидалось: {cell(record['expected'])}. Наблюдалось: {cell(record['observed'])}. "
                   f"Последствие: {cell(record['impact'])}.")
    source = cell(record["source"])
    if issue in existing:
        entry = f"{where}. {observation} {source} {marker}"
        for i, row in enumerate(rows):
            if row.startswith(f"| {issue} |"):
                if not row.rstrip().endswith("|"):
                    raise ValueError("The existing feedback row is malformed.")
                rows[i] = row.rstrip()[:-1].rstrip() + "<br>" + entry + " |\n"
                break
            if row.rstrip() == f"### {issue}":
                rows.insert(i + 1, f"\n{entry}\n")
                break
    else:
        end = top + 2
        while end < len(rows) and rows[end].startswith("|"):
            end += 1
        rows.insert(end, f"| {issue} | {KINDS[record['kind']]} | {where} | {observation} | {source} {marker} |\n")
    return "".join(rows), issue


@contextmanager
def open_directory(path, parent=None):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def read_file(directory, name):
    with os.fdopen(signum.no_follow(name, os.O_RDONLY, dir_fd=directory), "rb") as stream:
        return stream.read().decode(), os.fstat(stream.fileno())


def replace_file(directory, name, content, previous):
    temporary = ".feedback-" + uuid4().hex
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            if previous is not None:
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
            stream.write(content.encode())
            stream.flush()
            os.fsync(stream.fileno())
        try:
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current != previous:
            # Reading can update access time; only compare file identity and relevant revision fields.
            if (current is None or previous is None or not os.path.samestat(current, previous)
                    or (current.st_size, current.st_mtime_ns, current.st_ctime_ns)
                    != (previous.st_size, previous.st_mtime_ns, previous.st_ctime_ns)):
                raise ValueError(f"{name} changed while recording feedback.")
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def deliver(root, key, record):
    with ExitStack() as stack:
        directory = stack.enter_context(open_directory(root))
        for name in ("docs", "operations"):
            directory = stack.enter_context(open_directory(name, directory))
        # All pilot projects lock the same directory inode across atomic backlog replacements.
        fcntl.flock(directory, fcntl.LOCK_EX)
        document, previous = read_file(directory, BACKLOG)
        updated, issue = add_episode(document, key, record)
        if updated != document:
            # Atomic replacement must also respect write access on the existing file.
            with os.fdopen(signum.no_follow(BACKLOG, os.O_WRONLY, dir_fd=directory), "wb") as target:
                if not os.path.samestat(previous, os.fstat(target.fileno())):
                    raise ValueError("Central backlog changed while recording feedback.")
            replace_file(directory, BACKLOG, updated, previous)
        return issue


def read_queue(directory):
    try:
        text, revision = read_file(directory, QUEUE)
    except FileNotFoundError:
        return {"schema_version": 1, "central_root": None, "episodes": {}}, None
    queue = json.loads(text)
    if (not isinstance(queue, dict) or queue.get("schema_version") != 1
            or not isinstance(queue.get("episodes"), dict) or "central_root" not in queue
            or queue["central_root"] is not None and not isinstance(queue["central_root"], str)):
        raise ValueError("Invalid feedback queue.")
    for key, item in queue["episodes"].items():
        if (not isinstance(item, dict) or set(item) != {"record", "id"}
                or episode_key(validate_record(item["record"])) != key
                or item["id"] is not None and not (isinstance(item["id"], str) and re.fullmatch(ID, item["id"]))):
            raise ValueError("Invalid feedback queue entry.")
    return queue, revision


def run(project, root, command):
    directory = signum.journal_directory(project.resolve(strict=True))
    if not signum.Journal.read(directory / "state.json").enabled:
        return {"status": "disabled", "receipts": []}
    record = validate_record(json.load(sys.stdin)) if command == "record" else None
    route_problem = None
    if root is not None:
        try:
            if not root.is_absolute() or root.is_symlink():
                raise ValueError("Supply the authorized absolute central checkout directory.")
            root = str(root.resolve())
        except (OSError, RuntimeError, ValueError) as error:
            route_problem = str(error)
            root = None
    with signum.locked_store(directory), open_directory(directory) as store:
        queue, revision = read_queue(store)
        entries = queue["episodes"]
        if root and queue["central_root"] not in (None, root):
            route_problem = "This queue belongs to a different central checkout."
            root = None
        if record:
            key = episode_key(record)
            keys = [key]
        else:
            keys = list(entries)
        if not keys:
            return {"status": "empty", "receipts": []}

        def save():
            nonlocal revision
            replace_file(store, QUEUE, json.dumps(queue, ensure_ascii=False, indent=2) + "\n", revision)
            revision = os.stat(QUEUE, dir_fd=store, follow_symlinks=False)

        new_binding = root and queue["central_root"] is None
        new_episode = record is not None and key not in entries
        if new_binding or new_episode:
            if new_binding:
                queue["central_root"] = root
            if new_episode:
                entries[key] = {"record": record, "id": None}
            save()  # A crash after this point leaves a retryable observation.
        receipts = []
        for key in keys:
            item = entries[key]
            receipt = {"episode": key, "id": item["id"]}
            if item["id"] is None:
                if root is None:
                    receipt["reason"] = route_problem or "Central write access was not supplied."
                else:
                    try:
                        issue = deliver(root, key, item["record"])
                    except (OSError, ValueError, signum.SignumError) as error:
                        receipt["reason"] = str(error)
                    else:
                        item["id"] = receipt["id"] = issue
                        save()  # A retry can always recover the committed central marker.
            if route_problem and receipt["id"] is not None:
                receipt["recorded_central_root"] = queue["central_root"]
            receipts.append(receipt)
        return {"status": "pending" if any(r["id"] is None for r in receipts) else "delivered", "receipts": receipts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("record", "flush"):
        commands.add_parser(name).add_argument("--central-root", type=Path)
    args = parser.parse_args()
    try:
        result = run(args.project, args.central_root, args.command)
    except (ValueError, OSError, signum.SignumError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
