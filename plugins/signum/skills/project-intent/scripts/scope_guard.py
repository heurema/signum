"""Compare an isolated local workspace with an exact file scope."""

import hashlib
import json
import os
import stat


MARKER_PREFIX = "Exact file scope"
LEGACY_MARKER = MARKER_PREFIX + " v1\n"
MARKER = MARKER_PREFIX + " v2\n"
ADMIN = {".git", ".signum"}


class ScopeError(ValueError):
    pass


def path_parts(path):
    if not isinstance(path, str) or any(ord(char) < 32 or char in "\\*?[]" for char in path):
        raise ScopeError(f"Use an exact project-relative file path: {path!r}.")
    parts = path.split("/")
    if any(part.casefold() in ADMIN or part in {"", ".", ".."} for part in parts):
        raise ScopeError(f"Use an exact project-relative file path outside administrative directories: {path!r}.")
    return parts


def ancestors(path):
    parts = path_parts(path)
    return {"/".join(parts[:index]) for index in range(1, len(parts))}


def exact_scope(files, report):
    if not isinstance(files, list) or not files:
        raise ScopeError("Provide at least one exact implementation file path.")
    paths = [*files, report]
    parents = set().union(*(ancestors(path) for path in paths))
    if len(set(paths)) != len(paths) or parents.intersection(paths):
        raise ScopeError("The implementation and report paths must be distinct files.")
    return {"files": sorted(files), "report": report}


def encode(files, report):
    return MARKER + json.dumps(exact_scope(files, report), sort_keys=True, separators=(",", ":"))


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ScopeError("Exact file scope has duplicate fields.")
    return fields


def parse(scope):
    if not isinstance(scope, str) or not scope.lstrip().startswith(MARKER_PREFIX):
        return None
    marker, _, content = scope.partition("\n")
    if marker + "\n" not in (LEGACY_MARKER, MARKER):
        raise ScopeError("Invalid exact file scope marker.")
    try:
        document = json.loads(content, object_pairs_hook=unique_fields)
    except json.JSONDecodeError as error:
        raise ScopeError("Exact file scope must contain valid JSON.") from error
    if not isinstance(document, dict) or document.keys() != {"files", "report"}:
        raise ScopeError("Exact file scope requires files and report.")
    return exact_scope(document["files"], document["report"])


def requires_bound_report(scope):
    return isinstance(scope, str) and scope.startswith(MARKER)


def check_touches(scope, paths):
    if paths and scope is None:
        raise ScopeError("--touch requires confirmed exact file scope.")
    for path in paths:
        path_parts(path)
        if path not in scope["files"]:
            raise ScopeError(f"Intended edit is outside exact file scope: {path}.")


def revision(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def file_hash(directory, name, info, path):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(name, flags, dir_fd=directory), "rb") as stream:
        if revision(os.fstat(stream.fileno())) != revision(info):
            raise ScopeError(f"Workspace file changed during the scope check: {path}.")
        digest = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture(project, scope):
    allowed = {*scope["files"], scope["report"]}
    parents = set().union(*(ancestors(path) for path in allowed))
    files = {}
    directories = {}
    outside = hashlib.sha256()
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def scan(directory, prefix=""):
        before = os.fstat(directory)
        for name in sorted(os.listdir(directory)):
            if not prefix and name in ADMIN:
                continue
            path = prefix + name
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            mode = info.st_mode
            if path in allowed and (not stat.S_ISREG(mode) or info.st_nlink != 1):
                raise ScopeError(f"Exact file scope requires a regular, unlinked file at {path}.")
            if path in parents and not stat.S_ISDIR(mode):
                raise ScopeError(f"Exact file scope requires a real parent directory at {path}.")
            if stat.S_ISDIR(mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    if revision(os.fstat(child)) != revision(info):
                        raise ScopeError(f"Workspace directory changed during the scope check: {path}.")
                    scan(child, path + "/")
                finally:
                    os.close(child)
                if path in parents:
                    directories[path] = mode
                    continue
                content = None
            elif stat.S_ISREG(mode):
                content = file_hash(directory, name, info, path)
            elif stat.S_ISLNK(mode):
                content = os.readlink(name, dir_fd=directory)
            else:
                raise ScopeError(f"Unsupported workspace entry during the scope check: {path}.")
            if revision(os.stat(name, dir_fd=directory, follow_symlinks=False)) != revision(info):
                raise ScopeError(f"Workspace entry changed during the scope check: {path}.")
            fingerprint = [mode, content]
            if path in allowed:
                files[path] = fingerprint
            else:
                outside.update(json.dumps([path, fingerprint], ensure_ascii=True).encode("ascii") + b"\n")
        if revision(os.fstat(directory)) != revision(before):
            raise ScopeError(f"Workspace directory changed during the scope check: {prefix or '.'}.")

    directory = os.open(project, flags)
    try:
        outside.update(f"{os.fstat(directory).st_mode}\n".encode("ascii"))
        scan(directory)
    finally:
        os.close(directory)
    return {"outside": outside.hexdigest(), "files": files, "directories": directories}


def baseline(project, scope):
    before = capture(project, scope)
    if scope["report"] in before["files"]:
        raise ScopeError("The exact scope check report path must be absent at start.")
    return before


def implementation_manifest(scope, before, observed):
    files = []
    for path in scope["files"]:
        current = observed["files"].get(path)
        if before["files"].get(path) != current:
            change = {"sha256": current[1]} if current is not None else {"status": "deleted"}
            files.append({"path": path, **change})
    return files


def verify(project, scope, before, evidence):
    if not isinstance(before, dict) or before.keys() != {"outside", "files", "directories"}:
        raise ScopeError("Exact file scope has no baseline captured before work.")
    report = scope["report"]
    if evidence["artifact"]["path"] != report:
        raise ScopeError(f"Evidence must use the agreed check report path: {report}.")
    observed = capture(project, scope)
    if observed["outside"] != before["outside"]:
        raise ScopeError("Workspace contains changes outside the exact file scope.")
    directories = observed["directories"]
    for path, mode in before["directories"].items():
        if directories.get(path) != mode:
            raise ScopeError(f"Workspace parent directory was changed outside the exact file scope: {path}.")
    needed = set().union(*(ancestors(path) for path in observed["files"]))
    extra = directories.keys() - before["directories"].keys() - needed
    if extra:
        raise ScopeError(f"Workspace contains unexpected empty directories: {', '.join(sorted(extra))}.")
    current_files = observed["files"]
    changed = {entry["path"] for entry in implementation_manifest(scope, before, observed)}
    named = {entry["path"]: entry.get("sha256") for entry in evidence["files"]}
    if named.keys() != changed:
        paths = ", ".join(sorted(named.keys() ^ changed))
        raise ScopeError(f"Evidence paths differ from the observed implementation changes: {paths}.")
    for path, expected in [*named.items(), (report, evidence["artifact"]["sha256"])]:
        if current_files.get(path, [None, None])[1] != expected:
            raise ScopeError(f"Workspace file differs from the checked evidence: {path}.")
    return {"files": current_files, "directories": directories}
