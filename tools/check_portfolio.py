"""Inspect a local portfolio and describe repairs without modifying any files."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata


VERSION = 1
IDENTITY = ".portfolio-identity.json"
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
REVIEW_FIELDS = {"proposal_id", "project_id", "owner", "decision_id", "acceptance_id", "disposition"}


def exact_fields(value, fields, label):
    if not isinstance(value, dict) or value.keys() != set(fields):
        raise ValueError(f"Invalid fields in {label}.")
    return value


def text_field(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Invalid {label}.")
    return value


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"Invalid {label}.")
    return value


def relative_path(value, label):
    value = text_field(value, label)
    if "\\" in value or "\0" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError(f"{label} must be a normalized relative path.")
    return value


def digest(value, label):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError(f"Invalid SHA-256 in {label}.")
    return value


def record(value, label):
    exact_fields(value, {"id", "path", "sha256", "scope"}, label)
    text_field(value["id"], f"{label} ID")
    relative_path(value["path"], f"{label} path")
    digest(value["sha256"], label)
    text_field(value["scope"], f"{label} scope")


def registry_entries(values, key):
    if not isinstance(values, list):
        raise ValueError(f"{key} must be a list.")
    indexed = {}
    for value in values:
        if not isinstance(value, dict):
            raise ValueError(f"Invalid {key} entry.")
        entry_id = identifier(value.get("id"), f"{key} ID")
        if entry_id in indexed:
            raise ValueError(f"Duplicate {key} ID {entry_id}.")
        indexed[entry_id] = value
    return dict(sorted(indexed.items()))


def validate_registry(registry):
    exact_fields(registry, {"version", "resources", "projects"}, "registry")
    if type(registry["version"]) is not int or registry["version"] != VERSION:
        raise ValueError("Unsupported registry version.")
    resources = registry_entries(registry["resources"], "resource")
    projects = registry_entries(registry["projects"], "project")
    if not projects:
        raise ValueError("Registry has no projects.")
    for resource in resources.values():
        exact_fields(resource, {"id", "path"}, "resource")
        relative_path(resource["path"], "resource path")
    for project in projects.values():
        exact_fields(project, {"id", "owner", "environment", "root", "decision", "acceptance", "artifact"},
                     "project")
        text_field(project["owner"], "owner")
        text_field(project["environment"], "environment")
        relative_path(project["root"], "project root")
        record(project["decision"], "decision")
        record(project["acceptance"], "acceptance")
        artifact = project["artifact"]
        fields = {"resource", "path", "sha256"}
        if isinstance(artifact, dict) and "source" in artifact:
            fields.add("source")
        exact_fields(artifact, fields, "artifact")
        if identifier(artifact["resource"], "artifact resource") not in resources:
            raise ValueError(f"Unknown artifact resource for {project['id']}.")
        relative_path(artifact["path"], "artifact path")
        digest(artifact["sha256"], "artifact")
        if "source" in artifact:
            relative_path(artifact["source"], "artifact source")
    return projects, resources


def physical_key(path):
    # Reject ambiguous spelling conservatively on both case-sensitive and insensitive volumes.
    return unicodedata.normalize("NFD", path).casefold()


def contains(parent, child):
    return child == parent or child.startswith(parent + "/")


def check_roots(projects, resources):
    project_paths = [physical_key(project["root"]) for project in projects.values()]
    resource_paths = [physical_key(resource["path"]) for resource in resources.values()]
    if len(set(resource_paths)) != len(resource_paths):
        raise ValueError("Resource roots alias each other.")
    for index, project_path in enumerate(project_paths):
        for other_path in project_paths[index + 1:] + resource_paths:
            if contains(project_path, other_path) or contains(other_path, project_path):
                raise ValueError("Project roots must be distinct and disjoint from other roots.")


def directory(root_fd, path):
    """Return a descriptor and state, leaving ownership of root_fd with the caller."""
    fd = os.dup(root_fd)
    traversed = []
    for part in path.split("/") if path else ():
        traversed.append(part)
        try:
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
        except FileNotFoundError:
            os.close(fd)
            return None, {"status": "missing", "missing_at": "/".join(traversed)}
        except OSError:
            os.close(fd)
            return None, {"status": "unknown", "reason": "unreadable_or_aliased_directory"}
        os.close(fd)
        fd = child
    return fd, {"status": "present"}


def file_hash(root_fd, path, read=False):
    parent, _, name = path.rpartition("/")
    parent_fd, state = directory(root_fd, parent)
    if parent_fd is None:
        return state, None
    try:
        fd = os.open(name, FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return {"status": "missing", "missing_at": path}, None
    except OSError:
        return {"status": "unknown", "reason": "unreadable_or_aliased_file"}, None
    finally:
        os.close(parent_fd)
    with os.fdopen(fd, "rb") as data:
        try:
            initial = os.fstat(data.fileno())
            if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
                return {"status": "unknown", "reason": "nonregular_or_aliased_file"}, None
            if read:
                content = data.read()
                sha256 = hashlib.sha256(content).hexdigest()
            else:
                content = None
                hasher = hashlib.sha256()
                for chunk in iter(lambda: data.read(1024 * 1024), b""):
                    hasher.update(chunk)
                sha256 = hasher.hexdigest()
            end = os.fstat(data.fileno())
        except OSError:
            return {"status": "unknown", "reason": "unreadable_file"}, None
    if (initial.st_size, initial.st_mtime_ns, initial.st_ctime_ns) != (end.st_size, end.st_mtime_ns, end.st_ctime_ns):
        return {"status": "unknown", "reason": "file_changed_during_read"}, None
    return {"status": "present", "observed_sha256": sha256}, content


def verification(observation, expected):
    if observation["status"] == "present":
        status = "verified" if observation["observed_sha256"] == expected else "digest_mismatch"
        return {**observation, "status": status}
    return observation


def unique_object(pairs):
    value = {}
    for key, member in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON key {key}.")
        value[key] = member
    return value


def parse_json(content):
    return json.loads(content, object_pairs_hook=unique_object)


def inspect_project(root_fd, project, resource):
    project_id = project["id"]
    artifact = project["artifact"]
    identity_state, identity_content = file_hash(root_fd, f"{project['root']}/{IDENTITY}", read=True)
    expected_identity = {
        "project_id": project_id,
        **{key: project[key] for key in ("owner", "environment", "decision", "acceptance")},
        "evidence": {"source": artifact.get("source"), "sha256": artifact["sha256"]},
    }
    if identity_content is not None:
        try:
            verified = parse_json(identity_content) == expected_identity
        except ValueError:
            verified = False
        identity_state["status"] = "verified" if verified else "identity_mismatch"

    records = {}
    for key in ("decision", "acceptance"):
        declared = project[key]
        observation, _ = file_hash(root_fd, f"{project['root']}/{declared['path']}")
        records[key] = {
            **declared, "project_id": project_id, **verification(observation, declared["sha256"]),
        }
        if identity_state["status"] != "verified" and records[key]["status"] == "verified":
            records[key].update(status="unbound_to_project", bytes_status="verified")
    target = f"{resource['path']}/{artifact['path']}"
    observed, _ = file_hash(root_fd, target)
    recovery = {"status": "undeclared"}
    if "source" in artifact:
        source = f"{project['root']}/{artifact['source']}"
        source_observation, _ = file_hash(root_fd, source)
        recovery = {"path": source, **verification(source_observation, artifact["sha256"])}
    blockers = []
    if identity_state["status"] != "verified":
        blockers.append("identity_unverified")
    for key, value in records.items():
        if value["status"] != "verified":
            blockers.append(f"{key}_unverified")
    observed = verification(observed, artifact["sha256"])
    if observed["status"] == "unknown":
        blockers.append("artifact_unverified")
    if observed["status"] in {"missing", "digest_mismatch"} and recovery["status"] != "verified":
        blockers.append("recovery_source_unverified")

    return {
        "project_id": project_id, "owner": project["owner"], "environment": project["environment"],
        "root": project["root"], "identity": identity_state, **records,
        "artifact": {"resource": resource["id"], "target": target, "sha256": artifact["sha256"], **observed,
                     "recovery": recovery},
        "blockers": blockers,
    }


def inspect_roots(root_fd, projects, resources):
    anchor = os.fstat(root_fd)
    signatures = {(anchor.st_dev, anchor.st_ino): "registry directory"}
    observations = {}
    for kind, entries, field in (("project", projects, "root"), ("resource", resources, "path")):
        for entry_id, entry in entries.items():
            fd, state = directory(root_fd, entry[field])
            if fd is not None:
                try:
                    identity = os.fstat(fd)
                finally:
                    os.close(fd)
                signature = identity.st_dev, identity.st_ino
                label = f"{kind} {entry_id}"
                if signature in signatures:
                    raise ValueError(f"{label} aliases {signatures[signature]}.")
                signatures[signature] = label
            if kind == "resource":
                observations[entry_id] = {**entry, **state}
    return observations


def conflict(project_ids, targets, reason, states):
    for project_id in project_ids:
        states[project_id]["blockers"].append(reason)
    return {"reason": reason, "projects": sorted(project_ids), "targets": sorted(targets)}


def target_groups(states):
    claims = {}
    for project_id, state in states.items():
        artifact = state["artifact"]
        claims.setdefault(physical_key(artifact["target"]), []).append(project_id)

    conflicts = []
    for key, project_ids in sorted(claims.items()):
        targets = {states[project_id]["artifact"]["target"] for project_id in project_ids}
        digests = {states[project_id]["artifact"]["sha256"] for project_id in project_ids}
        if len(digests) > 1:
            conflicts.append(conflict(project_ids, targets, "conflicting_target_digests", states))
        elif len(targets) > 1:
            conflicts.append(conflict(project_ids, targets, "ambiguous_target_alias", states))
        components = key.split("/")
        for length in range(1, len(components)):
            parent_ids = claims.get("/".join(components[:length]))
            if parent_ids:
                related = project_ids + parent_ids
                paths = {states[project_id]["artifact"]["target"] for project_id in related}
                conflicts.append(conflict(related, paths, "file_directory_collision", states))
    return claims, conflicts


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def scope(state):
    return {
        "project_id": state["project_id"], "owner": state["owner"], "environment": state["environment"],
        "root": state["root"], "identity_sha256": state["identity"]["observed_sha256"],
        **{field: {key: state[field][key] for key in ("id", "path", "scope", "sha256")}
           for field in ("decision", "acceptance")},
    }


def plan_repairs(anchor, states, resources, claims):
    missing_roots = {
        physical_key(resource["missing_at"]): resource["missing_at"]
        for resource in resources.values() if resource["status"] == "missing"
    }
    groups = {}
    for key, project_ids in sorted(claims.items()):
        missing = next((missing_roots[parent] for parent in sorted(missing_roots) if contains(parent, key)), None)
        first_target = states[project_ids[0]]["artifact"]["target"]
        group_key = ("restore_resource", missing) if missing else ("restore_artifact", first_target)
        groups.setdefault(group_key, []).append(project_ids)

    proposals = []
    blockers = []
    for (kind, target), target_claims in sorted(groups.items()):
        impacted = sorted({project_id for ids in target_claims for project_id in ids})
        damaged = [project_id for project_id in impacted
                   if states[project_id]["artifact"]["status"] in {"missing", "digest_mismatch"}]
        if not damaged:
            continue
        blocked = [project_id for project_id in impacted if states[project_id]["blockers"]]
        if blocked:
            blockers.append({"reason": "repair_scope_unverified", "target": target,
                             "projects": impacted, "blocked_projects": blocked})
            for project_id in damaged:
                states[project_id]["blockers"].append("repair_scope_unverified")
            continue
        writes = []
        for project_ids in target_claims:
            affected = [project_id for project_id in project_ids if project_id in damaged]
            if not affected:
                continue
            artifact = states[affected[0]]["artifact"]
            writes.append({
                "target": artifact["target"], "sha256": artifact["sha256"],
                "precondition": {
                    key: artifact[key] for key in ("status", "observed_sha256", "missing_at") if key in artifact
                },
                "recovery_sources": [
                    {"project_id": project_id, "path": states[project_id]["artifact"]["recovery"]["path"],
                     "sha256": artifact["sha256"]}
                    for project_id in affected
                ],
            })
        descriptor = {
            "version": VERSION, "kind": kind, "target": target,
            "required_scopes": [scope(states[project_id]) for project_id in impacted],
            "resources": [{"id": resource_id, "path": resources[resource_id]["path"]}
                          for resource_id in sorted({states[project_id]["artifact"]["resource"]
                                                     for project_id in impacted})],
            "writes": sorted(writes, key=lambda write: write["target"]),
        }
        proposal_id = hashlib.sha256(canonical({"anchor": str(anchor), **descriptor}).encode()).hexdigest()
        proposals.append({"id": proposal_id, **descriptor})
    return proposals, blockers


def human_review(proposals, claims):
    """Match local review claims to current proposals without authenticating an owner."""
    valid = {}
    for proposal in proposals:
        for reviewer in proposal["required_scopes"]:
            pair = proposal["id"], reviewer["project_id"]
            valid[pair] = reviewer
    matched = {}
    issues = []
    for claim in claims:
        if (not isinstance(claim, dict) or claim.keys() != REVIEW_FIELDS
                or any(not isinstance(value, str) for value in claim.values())
                or claim["disposition"] not in {"approve", "reject"}):
            issues.append({"reason": "invalid_review"})
            continue
        pair = claim["proposal_id"], claim["project_id"]
        reviewer = valid.get(pair)
        if (reviewer is None or claim["owner"] != reviewer["owner"]
                or claim["decision_id"] != reviewer["decision"]["id"]
                or claim["acceptance_id"] != reviewer["acceptance"]["id"]):
            issues.append({"reason": "review_scope_mismatch", "proposal_id": pair[0], "project_id": pair[1]})
            continue
        matched.setdefault(pair, set()).add(claim["disposition"])

    queue = []
    for pair, reviewer in valid.items():
        dispositions = matched.get(pair, set())
        state = "pending"
        if dispositions == {"approve"}:
            state = "approve_claimed"
        elif dispositions == {"reject"}:
            state = "reject_claimed"
        elif dispositions:
            issues.append({"reason": "contradictory_reviews", "proposal_id": pair[0], "project_id": pair[1]})
        queue.append({"proposal_id": pair[0], "project_id": pair[1], "owner": reviewer["owner"],
                      "decision_id": reviewer["decision"]["id"], "acceptance_id": reviewer["acceptance"]["id"],
                      "status": state})
    return queue, sorted(issues, key=canonical)


def local_json(path):
    anchor = path.parent.resolve(strict=True)
    fd = os.open(anchor, DIRECTORY_FLAGS)
    try:
        state, content = file_hash(fd, path.name, read=True)
        if content is None:
            raise ValueError(f"Input {path.name} is {state['status']}.")
        return anchor, parse_json(content)
    finally:
        os.close(fd)


def run(registry_path, review_path=None):
    try:
        anchor, registry = local_json(registry_path)
        projects, resources = validate_registry(registry)
        check_roots(projects, resources)
        claims = [] if review_path is None else local_json(review_path)[1]
        if not isinstance(claims, list):
            raise ValueError("Review claims must be a JSON list.")

        fd = os.open(anchor, DIRECTORY_FLAGS)
        try:
            observed_resources = inspect_roots(fd, projects, resources)
            states = {
                project_id: inspect_project(fd, project, resources[project["artifact"]["resource"]])
                for project_id, project in projects.items()
            }
        finally:
            os.close(fd)
        targets, blockers = target_groups(states)
        proposals, blocked_repairs = plan_repairs(anchor, states, observed_resources, targets)
        blockers += blocked_repairs
        for state in states.values():
            state["blockers"] = sorted(set(state["blockers"]))
            state["status"] = "blocked" if state["blockers"] else (
                "healthy" if state["artifact"]["status"] == "verified" else "needs_repair"
            )
        queue, issues = human_review(proposals, claims)
        for proposal in proposals:
            dispositions = {row["status"] for row in queue if row["proposal_id"] == proposal["id"]}
            proposal["status"] = "pending"
            if "reject_claimed" in dispositions:
                proposal["status"] = "reject_claimed"
            elif dispositions == {"approve_claimed"}:
                proposal["status"] = "all_approvals_claimed"
        statuses = {state["status"] for state in states.values()}
        status = "blocked" if "blocked" in statuses else "needs_repair" if proposals else "healthy"
        return {
            "version": VERSION, "status": status, "projects": list(states.values()),
            "resources": list(observed_resources.values()), "proposals": proposals, "queue": queue,
            "blockers": blockers, "review_issues": issues,
        }, 0
    except (OSError, ValueError) as error:
        return {
            "version": VERSION, "status": "blocked", "projects": [], "resources": [], "proposals": [],
            "queue": [], "blockers": [{"reason": "invalid_input", "detail": str(error)}], "review_issues": [],
        }, 2


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, required=True, help="Portfolio registry JSON")
    parser.add_argument("--review", type=Path,
                        help="Optional local review claims JSON. Claims are matched, never authenticated.")
    args = parser.parse_args()
    outcome, exit_code = run(args.registry, args.review)
    print(json.dumps(outcome, ensure_ascii=True, indent=2))
    sys.exit(exit_code)
