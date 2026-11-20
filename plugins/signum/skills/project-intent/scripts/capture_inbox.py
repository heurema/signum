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

ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DB = "chat-inbox.sqlite3"


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
    mode = "rwc" if write else "ro"
    db = sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=2)
    try:
        if write:
            db.execute("CREATE TABLE IF NOT EXISTS tracking (id INTEGER PRIMARY KEY, since INTEGER NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS prompts (thread TEXT, turn TEXT, digest TEXT NOT NULL,"
                       " PRIMARY KEY(thread, turn))")
            db.execute("INSERT OR IGNORE INTO tracking VALUES (1, ?)", (int(time.time()),))
        yield db
        if write:
            db.commit()
    finally:
        db.close()


def digest(text):
    return hashlib.sha256(text.rstrip("\r\n").encode("utf-8")).hexdigest()


def native_prompt_seen(project, thread, turn, user_text):
    if not active(project):
        return False
    with database(project) as db:
        row = db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone()
    return row == (digest(user_text),)


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
            db.execute("INSERT OR IGNORE INTO prompts VALUES (?, ?, ?)", (thread, turn, digest(prompt)))
            if db.execute("SELECT digest FROM prompts WHERE thread=? AND turn=?", (thread, turn)).fetchone() != (digest(prompt),):
                raise ValueError("A different prompt already occupies this source turn.")
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": (
        f"Signum {thread}/{turn}. Before choosing an interface, check the scope and prior choices. "
        "When the outcome is known but its supported entry points are still open, ask which should run that same operation before drafting. "
        f"For scope, decisions or continuation see {Path(__file__).resolve().parents[1] / 'references/chat-decisions.md'}. "
        "Routine chat proceeds normally."
    )}}


def inspect(project, journal, *, thread_limit=10, turn_limit=12, include_text=False):
    """Read only messages since activation/checkpoint. A partial source never certifies a start."""
    with database(project) as db:
        since = db.execute("SELECT since FROM tracking WHERE id=1").fetchone()[0]
        native = {tuple(row) for row in db.execute("SELECT thread,turn FROM prompts")}
    before = int(time.time())
    reviewed = {key for key, value in journal.reviewed.items() if value["completed"] and value["assistant_id"]}
    result = chat_source.recent_turns(project, since=since, thread_limit=thread_limit, turn_limit=turn_limit,
                                      reviewed=reviewed)
    pending, seen = [], set()
    warnings = list(result["warnings"])
    def check(item):
        key = (item["thread"], item["turn"])
        seen.add(key)
        review = journal.reviewed.get(key)
        if not item["user_id"] and not item["assistant_id"]:
            if key in native or not item["completed"]:
                warnings.append("A submitted or running turn has no readable source yet.")
            return
        user_hash = decision_capture.digest([item["user_id"], item["user"]])
        fingerprint = decision_capture.digest([user_hash, item["assistant_id"], item["assistant"]])
        if review and review["fingerprint"] == fingerprint and review["completed"] == item["completed"]:
            return
        entry = {"thread": key[0], "turn": key[1]}
        if include_text:
            entry.update({name: item[name] for name in ("allow_owner", "completed")})
            for name in ("user", "assistant"):
                entry[name] = item[name][:1200]
            entry["excerpt"] = any(len(item[name]) > 1200 for name in ("user", "assistant"))
        pending.append(entry)
    for item in result["turns"]:
        check(item)
    # A receipt can arrive before its chat is indexed or before its start timestamp window.
    for key in sorted(native - seen):
        previous = journal.reviewed.get(key)
        if previous and previous["completed"] and previous["assistant_id"]:
            continue
        try:
            check(chat_source.read_turn(project, *key))
        except (OSError, ValueError, KeyError, chat_source.ChatSourceError):
            warnings.append(f"Source turn {key[0]}/{key[1]} is unavailable.")
    complete = bool(result["complete"]) and not warnings
    if complete and not pending and all(item["completed"] for item in result["turns"]):
        with database(project, write=True) as db:
            db.execute("UPDATE tracking SET since=max(since,?) WHERE id=1", (before - 1,))
    return {"complete": complete, "pending": pending[:4], "pending_count": len(pending),
            "warnings": list(dict.fromkeys(warnings))}


def check_start(project, journal, permission):
    if not active(project):
        return
    if "depends_on" not in permission and not permission.get("basis"):
        raise ValueError("This earlier permission has no decision declaration. Revise it with depends_on before a new start.")
    if not permission.get("basis") and not permission.get("depends_on"):
        return
    state = inspect(project, journal)
    if not state["complete"]:
        raise ValueError("Project chat history is incomplete for this directory. Inspect the warnings with "
                         "capture_inbox.py --project ROOT. For a copied journal, check the original project "
                         "directory where the chats were recorded before starting new decision-dependent work.")
    if state["pending"]:
        raise ValueError("Review the pending project chat turns before starting decision-dependent work (capture_inbox.py --project ROOT).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hook", action="store_true")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--thread-limit", type=int, default=10)
    parser.add_argument("--turn-limit", type=int, default=12)
    args = parser.parse_args()
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
