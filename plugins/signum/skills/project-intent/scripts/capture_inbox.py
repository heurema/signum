"""Remember native Codex submissions and reconcile decisions in an opted-in project."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

import chat_source
import decision_capture
import history_origin
import pending_scope
import scope_guard

ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DB = "chat-inbox.sqlite3"
CONNECTION_PROBES = {"ping", "проверка связи", "проверяем связь"}


def regular(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except FileNotFoundError:
        return False


def active(project):
    return regular(project / ".signum" / DB)


@contextmanager
def database(project, write=False):
    folder = chat_source.project_root(project) / ".signum"
    path = folder / DB
    for candidate in (path, *(folder / (DB + suffix) for suffix in ("-journal", "-shm", "-wal"))):
        if candidate.is_symlink() or (candidate.exists() and not regular(candidate)):
            raise ValueError("The chat receipt store must be a local regular file.")
        if write and candidate.exists() and candidate.stat().st_nlink > 1:
            raise ValueError("Cannot write to a receipt store shared by hard link.")
    mode = "rwc" if write else "ro"
    db = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=2)
    try:
        if write:
            db.execute("CREATE TABLE IF NOT EXISTS tracking (id INTEGER PRIMARY KEY, since INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS prompts (thread TEXT, turn TEXT, digest TEXT NOT NULL,"
                       " PRIMARY KEY(thread, turn))")
            db.execute("CREATE TABLE IF NOT EXISTS archived_prompts (thread TEXT, turn TEXT, digest TEXT NOT NULL,"
                       " reason TEXT NOT NULL, archived_at INTEGER NOT NULL, PRIMARY KEY(thread, turn))")
            db.execute("INSERT OR IGNORE INTO tracking VALUES (1, ?)", (int(time.time()),))
        yield db
        if write:
            db.commit()
    finally:
        db.close()


def digest(text):
    return hashlib.sha256(text.rstrip("\r\n").encode("utf-8")).hexdigest()


def has_archive(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archived_prompts'").fetchone() is not None


def native_prompt_seen(project, thread, turn, user_text):
    if not active(project):
        return False
    with database(project) as db:
        row = db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone()
        if row is None and has_archive(db):
            row = db.execute("SELECT digest FROM archived_prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone()
    return row == (digest(user_text),)


def archive_receipts(project, pairs, reason):
    keys = [tuple(pair.split("/")) for pair in pairs]
    if not reason or not reason.strip() or any(len(key) != 2 or not all(ID.fullmatch(x) for x in key) for key in keys):
        raise ValueError("Specify THREAD/TURN and the owner's reason for archiving these receipts.")
    with database(project, write=True) as db:
        active_keys = {}
        for key in dict.fromkeys(keys):
            if db.execute("SELECT 1 FROM archived_prompts WHERE thread=? AND turn=?", key).fetchone():
                continue
            row = db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", key).fetchone()
            if row is None:
                raise ValueError(f"No receipt for {'/'.join(key)}.")
            active_keys[key] = row[0]
        for key, item, _ in chat_source.lookup_receipts(project, active_keys):
            if item is not None:
                raise ValueError(f"Source {'/'.join(key)} is readable. Review the message before starting.")
        for key, fingerprint in active_keys.items():
            db.execute("INSERT INTO archived_prompts (thread,turn,digest,reason,archived_at) VALUES (?,?,?,?,?)",
                       (*key, fingerprint, reason.strip(), int(time.time())))
            db.execute("DELETE FROM prompts WHERE thread=? AND turn=?", key)
    return {"archived": list(dict.fromkeys(pairs))}


def receive(event):
    if event.get("hook_event_name") != "UserPromptSubmit" or event.get("agent_id") or event.get("agent_type"):
        return {}
    # Ephemeral Codex turns have no persistent history to reconcile on resume.
    transcript = event.get("transcript_path")
    if not isinstance(transcript, str) or not Path(transcript).is_absolute():
        return {}
    cwd, thread, turn, prompt = (event.get(k) for k in ("cwd", "session_id", "turn_id", "prompt"))
    if not all(isinstance(x, str) for x in (cwd, thread, turn, prompt)) or not Path(cwd).is_absolute():
        return {}
    if not ID.fullmatch(thread) or not ID.fullmatch(turn) or not prompt.strip():
        return {}
    project = Path(cwd).resolve()
    folder = project / ".signum"
    if folder.is_symlink() or not regular(folder / "state.json"):
        return {}
    import signum
    with signum.locked_store(folder):
        with database(project, write=True) as db:
            archived = db.execute("SELECT digest FROM archived_prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone()
            if archived:
                if archived != (digest(prompt),):
                    raise ValueError("A different prompt already occupies this archived source turn.")
                return {}
            db.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, ?)", (thread, turn, digest(prompt)))
            if db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone() != (digest(prompt),):
                raise ValueError("A different prompt already occupies this source turn.")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        f"Signum {thread}/{turn}. Routine edits proceed directly. To resume agreed work use python3 -B "
        f"{Path(__file__).resolve().with_name('signum.py')} --project . next --query TOPIC. "
        "Check scope before advice. If only the method is open, ask about that method."
    )}}


def inspect(project, journal, *, thread_limit=10, turn_limit=12, include_text=False, advance=True, request_thread=None,
            permission=None):
    """Inspect the current history window and exact receipts still awaiting review."""
    with database(project) as db:
        since = db.execute("SELECT since FROM tracking WHERE id=1").fetchone()[0]
        native = {tuple(row) for row in db.execute("SELECT thread,turn FROM prompts")}
        receipts = native.union(db.execute("SELECT thread,turn FROM archived_prompts")) if has_archive(db) else native
        version = journal.confirmed.get(permission.get("topic")) if permission else None
        exclusions = pending_scope.exclusions(db, version) if version else {}
    sources = pending_scope.dependency_sources(journal, permission, receipts) if permission else {}
    origin = history_origin.load(project)
    inherited = {tuple(key.split("/")) for key in origin["receipts"]} if origin else set()
    before = int(time.time())
    # History can be reviewed before the hook supplies evidence of who submitted it.
    reviewed = {key for key, value in journal.reviewed.items()
                if value["completed"] and value["assistant_id"] and (key not in receipts or value["owner_seen"])}
    def already_reviewed(item, review):
        if not review or (item["allow_owner"] and not review["owner_seen"]):
            return False
        user_hash = decision_capture.digest([item["user_id"], item["user"]])
        fingerprint = decision_capture.digest([user_hash, item["assistant_id"], item["assistant"]])
        return review["fingerprint"] == fingerprint and review["completed"] == item["completed"]

    def resolved_interruption(item):
        if item.get("status") != "interrupted" or not item.get("complete_user_text", item.get("plain_user_text")):
            return False
        key = item["thread"], item["turn"]
        if key in sources:
            return False
        if key in receipts and not item.get("receipt_matches", item["allow_owner"]):
            return False
        review = journal.reviewed.get(key)
        if review:
            return already_reviewed(item, review)
        return (item.get("plain_user_text") and not item["assistant_id"]
                and item["user"].strip().casefold() in CONNECTION_PROBES)

    try:
        result = chat_source.recent_turns(project, since=since, thread_limit=thread_limit, turn_limit=turn_limit,
                                          reviewed=reviewed, skip_turn=resolved_interruption)
    except (chat_source.ChatSourceError, OSError) as error:
        outstanding = sorted(native - reviewed)
        label = f" while checking {'/'.join(outstanding[0])}" if outstanding else ""
        blockers = [f"Desktop history could not be read{label}: {error}"]
        return {"complete": False, "pending": [], "pending_count": 0, "warnings": blockers, "start_blockers": blockers}
    blockers = list(result["warnings"])
    gaps = []
    for key in sorted(inherited - receipts):
        blockers.append(f"Source turn {'/'.join(key)}: the inherited receipt is missing from this copy.")
    seen = set(result.get("skipped", ()))
    pending = []
    deferred = []
    origin_recovered = []

    def check(item):
        key = (item["thread"], item["turn"])
        seen.add(key)
        review = journal.reviewed.get(key)
        label = f"Source turn {key[0]}/{key[1]}"
        if key in sources:
            label += " for decision " + ", ".join(sorted(sources[key]))
            if not item["allow_owner"] or (review and not already_reviewed(item, review)):
                blockers.append(f"{label}: the submitted source or its recorded review could not be verified.")
                return
        interrupted = item.get("status") == "interrupted"
        if key in receipts and (not item.get("receipt_matches", item["allow_owner"])
                                or not item.get("complete_user_text", True)):
            blockers.append(f"{label}: the submitted text is missing or does not match the receipt.")
            return
        if interrupted and not item.get("complete_user_text", item.get("plain_user_text")):
            problems = gaps if item.get("empty_turn") else blockers
            problems.append(f"{label}: the turn was interrupted and its complete message is unknown.")
            return
        if not item["user_id"] and not item["assistant_id"] and not item.get("complete_user_text"):
            if key in native or not item["completed"]:
                blockers.append(f"{label}: no readable message is available yet.")
            return
        if already_reviewed(item, review) or resolved_interruption(item):
            return
        if key not in sources and key in exclusions and pending_scope.fingerprint(item) == exclusions[key]:
            deferred.append(key)
            return
        entry = {"thread": key[0], "turn": key[1]}
        if (request_thread == key[0] and key not in inherited and not item.get("history_origin")
                and item.get("latest_in_thread") and item.get("status") == "inProgress"
                and item.get("plain_user_text") and not item["assistant_id"]):
            entry["current_request"] = True
        if include_text:
            entry.update({name: item[name] for name in ("allow_owner", "completed")})
            entry["status"] = item.get("status")
            if review and item["allow_owner"] and not review["owner_seen"]:
                entry["recheck_topics"] = sorted(review["choices"])
            for name in ("user", "assistant"):
                entry[name] = item[name][:1200]
            entry["excerpt"] = any(len(item[name]) > 1200 for name in ("user", "assistant"))
        pending.append(entry)
    for item in result["turns"]:
        check(item)
    # A receipt can arrive before its chat is indexed or before its start timestamp window.
    missing = sorted((((native | exclusions.keys()) - reviewed) | sources.keys()) - seen)
    for key, item, error in chat_source.lookup_receipts(project, missing, verify_origin=sorted(inherited & native)):
        if error is not None:
            gap = isinstance(error, chat_source.MissingTurn) and key not in sources and key not in exclusions
            problems = gaps if gap else blockers
            dependency = " for decision " + ", ".join(sorted(sources[key])) if key in sources else ""
            problems.append(f"Source turn {key[0]}/{key[1]}{dependency} is unavailable: {error}")
        else:
            if item.get("history_origin"):
                origin_recovered.append({"thread": key[0], "turn": key[1], "source": item["history_origin"],
                                         "allow_owner": item["allow_owner"]})
            check(item)
    warnings = list(dict.fromkeys([*blockers, *gaps]))
    complete = bool(result["complete"]) and not warnings
    if advance and complete and not pending and not deferred and all(item["completed"] or item.get("status") == "interrupted"
                                        for item in result["turns"]):
        with database(project, write=True) as db:
            db.execute("UPDATE tracking SET since=max(since,?) WHERE id=1", (before - 1,))
    data = {"complete": complete, "pending": pending[:4], "pending_count": len(pending),
            "warnings": warnings, "start_blockers": blockers}
    if origin_recovered:
        data["origin_recovered"] = origin_recovered
    return data


def pending_decisions(project, journal, permission, *, advance, request_thread=None):
    if not active(project):
        sources = pending_scope.dependency_sources(journal, permission, ())
        if sources:
            key, topics = next(iter(sources.items()))
            raise ValueError(f"Start of {permission['topic']} is waiting. Source turn {'/'.join(key)} for decision "
                             f"{', '.join(sorted(topics))} cannot be verified: the receipt store is missing.")
        if (permission.get("depends_on") or permission.get("basis")) and history_origin.load(project):
            raise ValueError(f"Start of {permission['topic']} is waiting: this copy's receipt store is missing.")
        return
    if "depends_on" not in permission and not permission.get("basis"):
        raise ValueError("This earlier permission has no decision declaration. Revise it with depends_on before a new start.")
    if not permission.get("basis") and not permission.get("depends_on"):
        return
    state = inspect(project, journal, advance=advance, request_thread=request_thread, permission=permission)
    step = permission.get("topic", "decision-dependent work")
    problems = state["warnings"] if scope_guard.parse(permission.get("scope")) is None else state["start_blockers"]
    if problems:
        reasons = " ".join(problems[:3])
        raise ValueError(f"Start of {step} is waiting. Project chat history is incomplete: {reasons} "
                         "Check the original project history and retry. capture_inbox.py --project ROOT lists all warnings.")
    current = next((item for item in state["pending"] if item.get("current_request")), None)
    if state["pending_count"] > int(current is not None):
        first = next(item for item in state["pending"] if item != current)
        raise ValueError(f"Start of {step} is waiting. Review the pending message {first['thread']}/{first['turn']} "
                         "for decisions used by this step (capture_inbox.py --project ROOT). "
                         "Use pending_scope.py for a turn verified as unrelated to this permission.")

    return {"current_request": current, "history_gap_count": len(state["warnings"])}


def check_start(project, journal, permission):
    pending_decisions(project, journal, permission, advance=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--thread-limit", type=int, default=10)
    parser.add_argument("--turn-limit", type=int, default=12)
    parser.add_argument("--archive-receipt", action="append", metavar="THREAD/TURN")
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.archive_receipt or args.reason is not None:
        if args.hook or not args.project or not args.archive_receipt or not args.reason:
            parser.error("Archiving requires --project, --archive-receipt and --reason together.")
    try:
        if args.hook:
            data = receive(json.load(sys.stdin))
        elif args.project:
            import signum
            project = chat_source.project_root(args.project)
            with signum.locked_store(project / ".signum"):
                if not active(project):
                    # A skill can still recover old sources when requested explicitly via chat_source.
                    raise ValueError("No native capture receipt yet. Check plugin hook trust or use chat_source.py for historical review.")
                if args.archive_receipt:
                    data = archive_receipts(project, args.archive_receipt, args.reason)
                else:
                    journal = signum.Journal.read(project / ".signum/state.json", required=True)
                    data = inspect(project, journal, thread_limit=args.thread_limit, turn_limit=args.turn_limit, include_text=True)
        else:
            parser.error("Supply --project or --hook.")
        if data:
            print(json.dumps(data, ensure_ascii=False))
    except (sqlite3.Error, OSError, ValueError, KeyError, chat_source.ChatSourceError) as error:
        print(f"Signum chat receipt unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
