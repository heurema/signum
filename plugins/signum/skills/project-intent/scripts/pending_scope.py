"""Review one pending turn against one confirmed permission, keeping the project inbox intact."""
import argparse
import json
from pathlib import Path
import re
import sqlite3
import sys

import chat_source
import decision_capture


def dependency_sources(journal, permission, receipts):
    """Identify known turns of the declared decisions, including older turn-only links."""
    dependencies = list(permission.get("depends_on", ()))
    if permission.get("basis"):
        dependencies.append(permission["basis"])
    versions = {d["topic"]: d["id"] for d in dependencies}
    if not versions:
        return {}
    sources = {}
    for key, review in journal.reviewed.items():
        for topic, part in review["choices"].items():
            if topic not in versions:
                continue
            version = part["id"]
            pending_revision = (version not in journal.accepted and version not in journal.withdrawn
                                and journal.record(version)["replaces"] == versions[topic]
                                and (key in receipts or review["owner_seen"]))
            if version == versions[topic] or pending_revision:
                sources.setdefault(key, set()).add(topic)
    for dep in dependencies:
        version, topic = dep["id"], dep["topic"]
        if version not in journal.proposed:
            continue
        refs = [journal.record(version)["source"], journal.owner_evidence.get(version, "")]
        for ref in refs:
            ref = ref.partition("\n")[0]
            for thread, turn in re.findall(r"codex:thread:([a-f0-9-]{36}) turn:([a-f0-9-]{36})", ref):
                sources.setdefault((thread, turn), set()).add(topic)
            legacy = set(re.findall(r"codex:turn:([a-f0-9-]{36})", ref))
            if legacy:
                for key in receipts:
                    if key[1] in legacy:
                        sources.setdefault(key, set()).add(topic)
    return sources


def fingerprint(item):
    if (not item["allow_owner"] or not item.get("complete_user_text")
            or item["status"] not in {"completed", "interrupted"}):
        return None
    return decision_capture.digest([item[name] for name in
                                    ("thread", "turn", "user_id", "user", "assistant_id", "assistant", "status")])


def exclusions(db, permission_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scope_reviews'").fetchone():
        return {}
    return {(thread, turn): signature for thread, turn, signature in db.execute(
        "SELECT thread, turn, fingerprint FROM scope_reviews WHERE permission=?", (permission_id,))}


def review(project, journal, version, pair, expected=None, reason=None):
    import capture_inbox
    import delivery
    permission = delivery.require_permission(journal, version)
    key = tuple(pair.split("/"))
    if len(key) != 2 or not all(capture_inbox.ID.fullmatch(k) for k in key):
        raise ValueError("Use the exact THREAD/TURN pair.")
    item = chat_source.read_turn(project, *key)
    signature = fingerprint(item)
    if not signature:
        raise ValueError("The complete, terminal source turn must match its native receipt.")
    topics = dependency_sources(journal, permission, [key]).get(key)
    if topics:
        raise ValueError("Source turn belongs to a declared decision: " + ", ".join(sorted(topics)))
    if expected is None:
        return {"permission": version, "topic": permission["topic"], "outcome": permission["summary"],
                "scope": permission.get("scope"), "limits": permission.get("authority_limit"),
                "depends_on": permission.get("depends_on", []),
                "basis": permission.get("basis"), "turn": pair, "user": item["user"], "assistant": item["assistant"],
                "fingerprint": signature}
    if signature != expected:
        raise ValueError("The source differs from the displayed turn. Review its complete text again.")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Explain why this turn does not affect the named permission.")
    with capture_inbox.database(project, write=True) as db:
        db.execute("CREATE TABLE IF NOT EXISTS scope_reviews (permission TEXT, thread TEXT, turn TEXT,"
                   " fingerprint TEXT NOT NULL, reason TEXT NOT NULL, PRIMARY KEY(permission, thread, turn))")
        db.execute("INSERT OR REPLACE INTO scope_reviews VALUES (?,?,?,?,?)", (version, *key, signature, reason.strip()))
    return {"permission": version, "unrelated_turn": pair, "fingerprint": signature, "reason": reason.strip()}


def main():
    import signum
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--permission", required=True)
    parser.add_argument("--turn", required=True, metavar="THREAD/TURN")
    parser.add_argument("--unrelated", metavar="DISPLAYED_FINGERPRINT")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if (args.unrelated is None) != (args.reason is None):
        parser.error("--unrelated and --reason must be supplied together.")
    try:
        project = chat_source.project_root(args.project)
        with signum.locked_store(project / ".signum"):
            journal = signum.Journal.read(project / ".signum/state.json", required=True)
            result = review(project, journal, args.permission, args.turn, args.unrelated, args.reason)
        print(json.dumps(result, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error, signum.SignumError) as error:
        print(f"Signum scope review unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
