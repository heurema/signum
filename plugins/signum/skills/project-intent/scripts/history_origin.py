"""Bind a copy to the exact chat receipts it inherited from an original project."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

from decision_capture import digest


def registration(project):
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not state.is_absolute():
        raise ValueError("The user state directory must be absolute.")
    directory = (state / "signum/history-origins").resolve()
    if directory == project or project in directory.parents:
        raise ValueError("History origin registrations must be outside the project.")
    return directory / (digest(str(project)) + ".json")


def load(project):
    path = registration(project)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    directory = path.parent.stat()
    if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
        raise ValueError("The history origin registry must be private to its owner.")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The history origin registration must be a private, owner-controlled file.")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("version") != 1 or record.get("copy") != str(project):
        raise ValueError("The history origin registration belongs to a different copy.")
    return record


def directory_identity(root):
    info = (root / ".signum").lstat()
    return [info.st_dev, info.st_ino]


def check_separate_stores(project, source):
    import capture_inbox
    for name in ("state.json", capture_inbox.DB):
        local = (project / ".signum" / name).lstat()
        remote = (source / ".signum" / name).lstat()
        if not stat.S_ISREG(local.st_mode) or not stat.S_ISREG(remote.st_mode) or os.path.samestat(local, remote):
            raise ValueError("The source and copy must have separate regular journals and receipt databases.")


def events(project):
    import signum
    try:
        return signum.Journal.read(project / ".signum/state.json", required=True).events
    except signum.SignumError as error:
        raise ValueError(f"Invalid project journal: {error}") from error


def check_lineage(project, record):
    import chat_source
    source = chat_source.project_root(record["source"])
    if (str(source) != record["source"] or source == project
            or directory_identity(project) != record["copy_identity"]
            or directory_identity(source) != record["source_identity"]):
        raise ValueError("The registered source or copy directory has changed.")
    if load(source) is not None:
        raise ValueError("A registered copy cannot supply another copy's history.")
    check_separate_stores(project, source)
    length = record["event_count"]
    if type(length) is not int or length < 1:
        raise ValueError("The registered journal lineage is empty or invalid.")
    for root in (project, source):
        prefix = events(root)[:length]
        if len(prefix) != length or digest(prefix) != record["event_digest"]:
            raise ValueError("The project journal no longer matches the registered lineage.")
    return source


def receipt(db, key):
    return db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", key).fetchone()


def bind(project, source):
    import capture_inbox
    import chat_source
    project, source = chat_source.project_root(project), chat_source.project_root(source)
    if project == source or project in source.parents or source in project.parents:
        raise ValueError("Choose separate source and copy project directories.")
    if load(source) is not None:
        raise ValueError("Choose the original project, not another registered copy.")
    target = registration(project)
    if source in target.parents:
        raise ValueError("History origin registrations must be outside the source project.")
    existing = load(project)
    if existing:
        if check_lineage(project, existing) != source:
            raise ValueError("This copy is already bound to a different origin.")
        _, refused = resolve(project, [tuple(key.split("/")) for key in existing["receipts"]])
        if refused:
            raise ValueError(next(iter(refused.values())))
        return {"source": str(source), "copy": str(project), "pinned": len(existing["receipts"])}
    check_separate_stores(project, source)
    original, copied = events(source), events(project)
    length = min(len(original), len(copied))
    if not length or copied[:length] != original[:length]:
        raise ValueError("The copy and source lack a matching non-empty journal lineage.")
    pinned = {}
    with capture_inbox.database(project) as local, capture_inbox.database(source) as remote:
        for thread, turn, fingerprint in local.execute("SELECT thread,turn,digest FROM prompts"):
            row = receipt(remote, (thread, turn))
            if row is None:  # Native to the copy; never route this key to the origin.
                continue
            if row != (fingerprint,):
                raise ValueError(f"Receipt digest differs at registration for {thread}/{turn}.")
            pinned[f"{thread}/{turn}"] = fingerprint
    if not pinned:
        raise ValueError("The source and copy have no matching active receipts.")
    record = {"version": 1, "source": str(source), "copy": str(project), "source_identity": directory_identity(source),
              "copy_identity": directory_identity(project), "event_count": length,
              "event_digest": digest(original[:length]), "receipts": pinned}
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = target.parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("The history origin registry must be private to its owner.")
    with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, encoding="utf-8") as stream:
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
        os.link(stream.name, target)  # Publish the complete registration without replacing an existing one.
    return {"source": str(source), "copy": str(project), "pinned": len(pinned)}


def resolve(project, keys):
    """Map only the requested inherited IDs. Revalidate both active receipts before a read."""
    import capture_inbox
    record = load(project)
    if record is None:
        return {}, {}
    pins = record["receipts"]
    inherited = {key: pins["/".join(key)] for key in keys if "/".join(key) in pins}
    if not inherited:
        return {}, {}
    routes, refused = {}, {}
    try:
        source = check_lineage(project, record)
        with capture_inbox.database(project) as local, capture_inbox.database(source) as remote:
            for key, expected in inherited.items():
                if receipt(local, key) == receipt(remote, key) == (expected,):
                    routes[key] = (source, expected)
                else:
                    refused[key] = "The active receipts no longer match the registered origin digest."
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        return {}, dict.fromkeys(inherited, str(error))
    return routes, refused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(bind(args.project, args.source)))
    except (ValueError, OSError, KeyError, sqlite3.Error) as error:
        print(f"Signum could not register the history origin: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
