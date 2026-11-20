"""Export and restore explicitly selected Signum project files on POSIX."""

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import unicodedata
import zipfile
import zlib


FORMAT = "signum-project-backup"
VERSION = 1
FILES = {
    "README.md", ".gitignore", ".agents/plugins/marketplace.json",
    ".signum/state.json", ".signum/delivery.json",
}
TREES = {"docs", "plugins", "tests", "tools", ".signum/review"}
IGNORED = {".git", ".hg", ".svn", "__pycache__", ".cache", ".pytest_cache",
           ".mypy_cache", ".ruff_cache", ".DS_Store", "write.lock"}
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
DIGEST = re.compile(r"[0-9a-f]{64}")


class BackupError(Exception):
    pass


def ignored(name):
    name = name.casefold()
    return name in IGNORED or name.endswith((".pyc", ".pyo")) or name.startswith(".journal-")


def validate_path(path):
    if (not isinstance(path, str) or not path or any(
        part in {"", ".", ".."} or part.endswith((".", " "))
        or any(ord(c) < 32 or c in '\\:<>"|?*' for c in part)
        for part in path.split("/")
    )):
        raise BackupError(f"Invalid project path: {path!r}")
    return PurePosixPath(path)


def within(path, root):
    return path == root or path.startswith(root + "/")


def allowed(path):
    parts = validate_path(path).parts
    return (not any(ignored(part) for part in parts)
            and (path in FILES or any(within(path, root) for root in TREES)))


def location(path):
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def source_lock(project_fd, use_journals, create=False):
    if not use_journals:
        yield True
        return
    with ExitStack() as resources:
        try:
            directory_fd = os.open(".signum", DIR_FLAGS, dir_fd=project_fd)
        except (FileNotFoundError, NotADirectoryError):
            yield False
            return
        resources.callback(os.close, directory_fd)
        flags = os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            lock_fd = os.open("write.lock", flags | os.O_RDONLY, dir_fd=directory_fd)
        except FileNotFoundError:
            if not create:
                yield True
                return
            lock_fd = os.open("write.lock", flags | os.O_WRONLY | os.O_CREAT, 0o600,
                              dir_fd=directory_fd)
        resources.callback(os.close, lock_fd)
        if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
            raise BackupError("The journal lock is not a regular file.")
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        yield True


def fingerprint(info):
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def transfer(source, destination=None):
    checksum = hashlib.sha256()
    size = 0
    for block in iter(lambda: source.read(1024 * 1024), b""):
        checksum.update(block)
        size += len(block)
        if destination is not None:
            destination.write(block)
    return size, checksum.hexdigest()


def snapshot_entry(parent_fd, name, path, entries, skipped, archive=None):
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    mode = info.st_mode
    if stat.S_ISDIR(mode):
        directory_fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
        try:
            entries[path] = {"kind": "directory"}
            for child in sorted(os.listdir(directory_fd)):
                if not ignored(child):
                    child_path = f"{path}/{child}"
                    validate_path(child_path)
                    snapshot_entry(directory_fd, child, child_path, entries, skipped, archive)
            if fingerprint(info) != fingerprint(os.fstat(directory_fd)):
                raise BackupError(f"Directory changed during backup or comparison: {path}")
        finally:
            os.close(directory_fd)
    elif stat.S_ISREG(mode):
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        with os.fdopen(os.open(name, flags, dir_fd=parent_fd), "rb") as source:
            if fingerprint(info) != fingerprint(os.fstat(source.fileno())):
                raise BackupError(f"File changed during backup or comparison: {path}")
            with ExitStack() as resources:
                output = resources.enter_context(archive.open(f"files/{path}", "w", force_zip64=True)) if archive else None
                size, checksum = transfer(source, output)
            if fingerprint(info) != fingerprint(os.fstat(source.fileno())) or size != info.st_size:
                raise BackupError(f"File changed during backup or comparison: {path}")
        entries[path] = {"kind": "file", "size": size, "sha256": checksum,
                         "executable": bool(mode & stat.S_IXUSR)}
    else:
        skipped[path] = "symbolic link" if stat.S_ISLNK(mode) else "unsupported file type"


def snapshot_selection(project_fd, path, entries, skipped, archive):
    parts = validate_path(path).parts
    with ExitStack() as resources:
        parent_fd = project_fd
        for part in parts[:-1]:
            try:
                parent_fd = os.open(part, DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                skipped[path] = "missing"
                return
            except NotADirectoryError:
                skipped[path] = "non-directory or symbolic link in path"
                return
            resources.callback(os.close, parent_fd)
        try:
            os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            skipped[path] = "missing"
            return
        snapshot_entry(parent_fd, parts[-1], path, entries, skipped, archive)


def unique_paths(entries):
    seen = {}
    for path in entries:
        for component in (validate_path(path), *PurePosixPath(path).parents):
            name = component.as_posix()
            key = unicodedata.normalize("NFC", name).casefold()
            if seen.setdefault(key, name) != name:
                raise BackupError(f"Paths collide on case-insensitive filesystems: {seen[key]} and {name}")
            if name != path and entries.get(name, {}).get("kind") == "file":
                raise BackupError(f"File is also used as a directory: {name}")


def export(args):
    project = args.project.resolve(strict=True)
    output = args.archive.parent.resolve(strict=True) / args.archive.name
    requested = sorted(args.include + args.optional)
    if not requested or any(not allowed(path) for path in requested):
        raise BackupError("Select one or more allowed paths using --include or --optional.")
    for index, path in enumerate(requested):
        if any(within(path, previous) for previous in requested[:index]):
            raise BackupError(f"Overlapping selections: {path}")
        if output == project / path or project / path in output.parents:
            raise BackupError("The archive cannot be written inside an exported path.")
    if os.path.lexists(output):
        raise BackupError(f"Archive already exists: {output}")

    entries, skipped = {}, {}
    with ExitStack() as resources:
        project_fd = os.open(project, DIR_FLAGS)
        resources.callback(os.close, project_fd)
        info = os.fstat(project_fd)
        directory = resources.enter_context(tempfile.TemporaryDirectory(dir=output.parent, prefix=".signum-export-"))
        temporary = Path(directory) / "archive.zip"
        use_journals = any(path.startswith(".signum/") for path in requested)
        with source_lock(project_fd, use_journals, create=True) as has_journals:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path in requested:
                    if not has_journals and path.startswith(".signum/"):
                        skipped[path] = "missing or inaccessible .signum directory"
                    else:
                        snapshot_selection(project_fd, path, entries, skipped, archive)
                    if path in skipped and path in args.include:
                        raise BackupError(f"Required path {path}: {skipped[path]}")
                    if path in entries:
                        validate_selection_kind(path, entries[path])
                unique_paths(entries)
                manifest = {"format": FORMAT, "version": VERSION, "created_at": timestamp(),
                            "source_location": location(project), "source_identity": [info.st_dev, info.st_ino],
                            "required": sorted(args.include), "optional": sorted(args.optional),
                            "entries": entries, "skipped": skipped}
                archive.writestr("manifest.json", json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        temporary.chmod(0o600)
        os.link(temporary, output)
    return {"status": "exported", "archive": str(output), "entries": len(entries), "skipped": skipped}


def reject_duplicate_keys(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BackupError(f"Duplicate manifest key: {key}")
        result[key] = value
    return result


def validate_selection_kind(path, entry):
    if ((path in FILES and entry.get("kind") != "file")
            or (path in TREES and entry.get("kind") != "directory")):
        raise BackupError(f"Unexpected file type for selected path: {path}")


def read_manifest(archive):
    names = archive.namelist()
    if len(names) != len(set(names)) or "manifest.json" not in names:
        raise BackupError("Missing manifest or duplicate ZIP members.")
    manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=reject_duplicate_keys)
    if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT
            or type(manifest.get("version")) is not int or manifest["version"] != VERSION):
        raise BackupError("Unsupported backup format or version.")
    try:
        required, optional = manifest["required"], manifest["optional"]
        entries, skipped = manifest["entries"], manifest["skipped"]
        identity, source_location = manifest["source_identity"], manifest["source_location"]
        if (not isinstance(required, list) or not isinstance(optional, list)
                or not isinstance(entries, dict) or not isinstance(skipped, dict)
                or not isinstance(identity, list) or len(identity) != 2
                or not all(type(value) is int and value >= 0 for value in identity)
                or not isinstance(source_location, str) or not DIGEST.fullmatch(source_location)):
            raise BackupError("Invalid manifest metadata.")
        requested = required + optional
        if not requested or not all(allowed(path) for path in requested):
            raise BackupError("Invalid manifest selections.")
        requested.sort()
        if any(any(within(path, other) for other in requested[:index])
               for index, path in enumerate(requested)):
            raise BackupError("Invalid manifest selections.")
        if (not all(path in entries for path in required)
                or not all((path in entries) != (path in skipped) for path in optional)
                or any(not allowed(path) or path in entries or not isinstance(reason, str)
                       or not any(within(path, root) for root in requested)
                       for path, reason in skipped.items())):
            raise BackupError("Invalid manifest coverage.")
        expected_names = {"manifest.json"}
        for path, entry in entries.items():
            roots = [root for root in requested if root in entries and within(path, root)]
            if not allowed(path) or not roots or not isinstance(entry, dict):
                raise BackupError(f"Invalid manifest entry: {path}")
            validate_selection_kind(path, entry)
            if path != roots[0] and entries.get(str(PurePosixPath(path).parent)) != {"kind": "directory"}:
                raise BackupError(f"Missing parent directory: {path}")
            if entry == {"kind": "directory"} and path not in FILES:
                continue
            if (entry.keys() != {"kind", "size", "sha256", "executable"} or entry["kind"] != "file"
                    or type(entry["size"]) is not int or entry["size"] < 0
                    or not isinstance(entry["sha256"], str) or not DIGEST.fullmatch(entry["sha256"])
                    or type(entry["executable"]) is not bool):
                raise BackupError(f"Invalid file entry: {path}")
            expected_names.add(f"files/{path}")
        unique_paths(entries)
        if set(names) != expected_names:
            raise BackupError("ZIP members do not match the manifest.")
        for item in archive.infolist():
            mode = stat.S_IFMT(item.external_attr >> 16)
            if item.orig_filename != item.filename or mode not in {0, stat.S_IFREG} or item.is_dir():
                raise BackupError(f"Invalid ZIP member: {item.filename}")
    except KeyError as error:
        raise BackupError(f"Missing manifest field: {error}") from error
    return manifest


def materialize(archive, manifest, destination=None):
    for path, entry in sorted(manifest["entries"].items()):
        output = destination / path if destination is not None else None
        if entry["kind"] == "directory":
            if output is not None:
                output.mkdir(parents=True, exist_ok=True, mode=0o700)
            continue
        member = archive.getinfo(f"files/{path}")
        if member.file_size != entry["size"]:
            raise BackupError(f"Size mismatch: {path}")
        with ExitStack() as resources:
            source = resources.enter_context(archive.open(member))
            sink = None
            if output is not None:
                output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                sink = resources.enter_context(output.open("xb"))
            if transfer(source, sink) != (entry["size"], entry["sha256"]):
                raise BackupError(f"Checksum mismatch: {path}")
            if output is not None:
                output.chmod(0o700 if entry["executable"] else 0o600)


def compare_target(target, manifest):
    entries, skipped = {}, {}
    target_fd = os.open(target, DIR_FLAGS)
    try:
        with source_lock(target_fd, True):
            for child in os.listdir(target_fd):
                if not ignored(child):
                    validate_path(child)
                    snapshot_entry(target_fd, child, child, entries, skipped)
            if skipped:
                return False
            expected = manifest["entries"].copy()
            for path in manifest["entries"]:
                for parent in PurePosixPath(path).parents:
                    if parent.parts:
                        expected.setdefault(parent.as_posix(), {"kind": "directory"})
            return entries == expected
    finally:
        os.close(target_fd)


def check_target(target, manifest, source):
    if source is not None:
        origin = source.resolve(strict=True)
        if target == origin or origin in target.parents:
            raise BackupError("Restore target cannot be inside the source project.")
    for ancestor in (target, *target.parents):
        if location(ancestor) == manifest["source_location"]:
            raise BackupError("Restore target cannot be inside the source project.")
        if ancestor.exists():
            info = ancestor.stat()
            if [info.st_dev, info.st_ino] == manifest["source_identity"]:
                raise BackupError("Restore target cannot be inside the source project.")


def restore(args, archive, manifest):
    if args.target.name in {"", ".."}:
        raise BackupError("Specify a named project directory as the restore target.")
    target = args.target.parent.resolve(strict=True) / args.target.name
    if target.is_symlink():
        raise BackupError("Restore target is a symbolic link.")
    check_target(target, manifest, args.source)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix=".signum-restore-") as temporary:
        staging = Path(temporary) / "project"
        staging.mkdir(mode=0o700)
        materialize(archive, manifest, staging)
        if target.exists() and (any(target.iterdir()) or not manifest["entries"]):
            if not compare_target(target, manifest):
                raise BackupError(f"Restore target differs from the archive: {target}")
            status = "already-restored"
        else:
            os.rename(staging, target)
            status = "restored"
    return {"status": status, "target": str(target), "entries": len(manifest["entries"]),
            "skipped": manifest["skipped"]}


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("export", "inspect", "restore"):
        commands.add_parser(command).add_argument("--archive", required=True, type=Path)
    commands.choices["export"].add_argument("--project", required=True, type=Path)
    commands.choices["export"].add_argument("--include", action="append", default=[], metavar="PATH")
    commands.choices["export"].add_argument("--optional", action="append", default=[], metavar="PATH")
    commands.choices["restore"].add_argument("--target", required=True, type=Path)
    commands.choices["restore"].add_argument("--source", type=Path)
    return parser.parse_args()


def main():
    args = arguments()
    try:
        if args.command == "export":
            result = export(args)
        else:
            with zipfile.ZipFile(args.archive) as archive:
                manifest = read_manifest(archive)
                if args.command == "restore":
                    result = restore(args, archive, manifest)
                else:
                    materialize(archive, manifest)
                    result = manifest
        print(json.dumps(result, indent=2, sort_keys=True))
    except (BackupError, OSError, ValueError, RuntimeError, zipfile.BadZipFile, zlib.error) as error:
        print(f"project_backup: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
