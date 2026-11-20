"""Read original project chat messages from the local Codex App Server."""
import argparse
import json
import heapq
import math
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import time


class ChatSourceError(ValueError):
    pass


DESKTOP_BINARIES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
)


def codex_executable(project):
    """Prefer the signed app's known installation path; never run a project-local shim."""
    bundled = os.environ.get("CODEX_APP_RUNTIME_CLI_PATH", "")
    candidates = [path for path in DESKTOP_BINARIES if str(path) == bundled and path.resolve() == path]
    for folder in os.get_exec_path():
        if folder and Path(folder).is_absolute():
            candidates.append(Path(folder) / "codex")
    excluded = (project.resolve(), Path.cwd().resolve())
    for path in candidates:
        target = path.resolve()
        if any(root != Path("/") and (root in path.parents or root in target.parents) for root in excluded):
            continue
        if target.is_file() and os.access(target, os.X_OK):
            return str(target)
    raise ChatSourceError("No installed Codex executable is available outside the project.")


class AppServer:
    """One short-lived, read-only stdio connection. No thread is started or resumed."""

    def __init__(self, project):
        self.executable = codex_executable(project)

    def __enter__(self):
        self.serial = 0
        self.deadline = time.monotonic() + 16
        self.buffer = bytearray()
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"], cwd="/",
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            self.call("initialize", {
                "clientInfo": {"name": "signum_history", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            self.send({"method": "initialized", "params": {}})
        except BaseException:
            self.close()
            raise
        return self

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def call(self, method, params):
        self.serial += 1
        self.send({"id": self.serial, "method": method, "params": params})
        deadline = min(self.deadline, time.monotonic() + 8)
        fd = self.process.stdout.fileno()
        while time.monotonic() < deadline:
            newline = self.buffer.find(b"\n")
            if newline == -1:
                if len(self.buffer) > 16 * 1024 * 1024:
                    raise ChatSourceError("History page exceeds the local read limit.")
                ready = select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]
                if not ready:
                    break
                chunk = os.read(fd, 65536)
                if not chunk:
                    raise ChatSourceError("Codex closed the history connection.")
                self.buffer.extend(chunk)
                continue
            response = json.loads(self.buffer[:newline])
            del self.buffer[:newline + 1]
            if response.get("id") != self.serial:
                continue
            if not isinstance(response.get("result"), dict):
                raise ChatSourceError("Codex could not read history with " + method + ".")
            return response["result"]
        raise ChatSourceError("Codex history read timed out.")

    def close(self):
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        finally:
            self.process.stdin.close()
            self.process.stdout.close()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def project_root(project):
    root = Path(project).resolve(strict=True)
    journal = root / ".signum"
    if not stat.S_ISDIR(journal.lstat().st_mode) or not stat.S_ISREG((journal / "state.json").lstat().st_mode):
        raise ChatSourceError("Reading history requires a project with a local Signum journal.")
    return root


def interactive(thread, project):
    cwd = thread.get("cwd")
    return (
        isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).resolve() == project
        and thread.get("source") in ("cli", "vscode") and not thread.get("ephemeral")
        and not any(thread.get(field) for field in ("parentThreadId", "forkedFromId", "agentNickname", "agentRole"))
    )


def checked_thread(server, project, thread_id):
    thread = server.call("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
    if thread.get("id") != thread_id or not interactive(thread, project):
        raise ChatSourceError("The chat is outside this project's interactive history.")
    return thread


def page_rows(server, method, params, size):
    """Yield pages lazily so a bounded caller never requests the next page needlessly."""
    cursor = None
    seen = set()
    while True:
        request = dict(params, limit=size)
        if cursor:
            request["cursor"] = cursor
        page = server.call(method, request)
        rows = page.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ChatSourceError("Invalid history page.")
        yield from rows
        cursor = page.get("nextCursor")
        if not cursor:
            return
        if cursor in seen or not rows:
            raise ChatSourceError("History pagination did not advance.")
        seen.add(cursor)


def turn_rows(server, thread_id, size):
    return page_rows(server, "thread/turns/list", {
        "threadId": thread_id, "sortDirection": "desc", "itemsView": "notLoaded",
    }, size)


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def with_previous(rows):
    current = next(rows, None)
    while current is not None:
        previous = next(rows, None)
        yield current, previous["id"] if previous else None
        current = previous


def messages(server, project, thread_id, turn, previous_turn):
    turn_id = turn["id"]
    entries = page_rows(server, "thread/items/list", {
        "threadId": thread_id, "turnId": turn_id, "sortDirection": "asc",
    }, 20)
    users = []
    final_text, final_id = "", ""
    for index, entry in enumerate(entries):
        if index >= 600:
            raise ChatSourceError("Turn exceeds the local message limit.")
        if entry.get("turnId") != turn_id or not isinstance(entry.get("item"), dict):
            raise ChatSourceError("Codex returned a message from a different turn.")
        item = entry["item"]
        if item.get("type") == "userMessage":
            users.append(item)
        elif item.get("type") == "agentMessage" and item.get("phase") == "final_answer" and not item.get("delivery"):
            final_id, final_text = item["id"], item["text"]
    text, user_id, allow_owner = "", "", False
    if users:
        parts = [
            [part["text"] for part in item["content"]
             if part.get("type") == "text" and isinstance(part.get("text"), str)]
            for item in users
        ]
        text = "\n\n".join("\n".join(chunks) for chunks in parts)
        if len(users) == 1:
            user_id = users[0]["id"]
            if text and len(users[0]["content"]) == len(parts[0]) == 1 and user_id:
                # Relayed tool input is a functionCallOutput. A userMessage alone
                # cannot establish whether an API client acted for the owner.
                from capture_inbox import native_prompt_seen
                allow_owner = native_prompt_seen(project, thread_id, turn_id, text)
    return {
        "thread": thread_id, "turn": turn_id, "user": text, "user_id": user_id,
        "assistant": final_text, "assistant_id": final_id, "allow_owner": allow_owner,
        "started_at": turn.get("startedAt"), "completed": turn.get("status") == "completed",
        "previous_turn": previous_turn,
    }


def read_turn(project, thread_id, turn_id):
    root = project_root(project)
    with AppServer(root) as server:
        checked_thread(server, root, thread_id)
        for index, (turn, previous) in enumerate(with_previous(turn_rows(server, thread_id, 100))):
            if index >= 2000:
                raise ChatSourceError("Turn lookup exceeds the local history limit.")
            if turn["id"] == turn_id:
                return messages(server, root, thread_id, turn, previous)
    raise ChatSourceError("This turn is unavailable in the selected chat.")


def indexed_threads(server, root, since, size):
    """Merge active and archived pages in recency order for the exact project."""
    def list_chats(archived):
        rows = page_rows(server, "thread/list", {
            "cwd": str(root), "useStateDbOnly": True, "sourceKinds": ["cli", "vscode"],
            "sortKey": "updated_at", "sortDirection": "desc", "archived": archived,
        }, size)
        for row in rows:
            updated = row.get("updatedAt")
            if since is not None and number(updated) and updated < since:
                return
            if interactive(row, root):
                yield row

    seen = set()
    for row in heapq.merge(list_chats(False), list_chats(True),
                           key=lambda item: item.get("updatedAt") or 0, reverse=True):
        if row["id"] not in seen:
            seen.add(row["id"])
            yield row


def recent_turns(project, *, thread_id=None, since=None, thread_limit=6, turn_limit=4, reviewed=()):
    """Read bounded unreviewed turns. Completeness applies only to the local index."""
    if not 1 <= thread_limit <= 30 or not 1 <= turn_limit <= 50 or (since is not None and not number(since)):
        raise ValueError("Invalid history window.")
    root = project_root(project)
    reviewed = set(reviewed)
    turns, warnings = [], []
    active_count = 0
    with AppServer(root) as server:
        if thread_id:
            threads = [checked_thread(server, root, thread_id)]
        else:
            threads = indexed_threads(server, root, since, max(20, thread_limit + 1))
        try:
            for thread in threads:
                count = 0
                rows = turn_rows(server, thread["id"], max(20, turn_limit + 2))
                try:
                    for turn, previous in with_previous(rows):
                        stamp = turn.get("startedAt")
                        if since is not None and number(stamp) and stamp < since:
                            break
                        if turn.get("status") == "completed" and (thread["id"], turn["id"]) in reviewed:
                            continue
                        if since is not None and not number(stamp):
                            warnings.append("A turn has no reliable start time.")
                        if count == 0:
                            active_count += 1
                            if active_count > thread_limit:
                                warnings.append("More project chats may be available.")
                                break
                        if count >= turn_limit:
                            warnings.append("More project turns may be available.")
                            break
                        count += 1
                        turns.append(messages(server, root, thread["id"], turn, previous))
                except (ChatSourceError, OSError, ValueError, KeyError) as error:
                    warnings.append(f"Could not finish reading indexed chat {thread['id']}: {type(error).__name__}.")
                if active_count > thread_limit:
                    break
        except (ChatSourceError, OSError, ValueError, KeyError) as error:
            warnings.append(f"Could not finish listing indexed chats: {type(error).__name__}.")
    warnings = list(dict.fromkeys(warnings))
    turns.sort(key=lambda row: (row["started_at"] or 0, row["thread"], row["turn"]))
    return {"turns": turns, "complete": not warnings, "warnings": warnings, "indexed_only": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--thread")
    parser.add_argument("--turn")
    parser.add_argument("--since", type=float)
    parser.add_argument("--thread-limit", type=int, default=6)
    parser.add_argument("--turn-limit", type=int, default=4)
    args = parser.parse_args()
    if args.turn and not args.thread:
        parser.error("--turn requires --thread")
    try:
        if args.turn:
            result = read_turn(args.project, args.thread, args.turn)
        else:
            result = recent_turns(args.project, thread_id=args.thread, since=args.since,
                                  thread_limit=args.thread_limit, turn_limit=args.turn_limit)
        json.dump(result, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    except (OSError, ValueError, KeyError, ChatSourceError) as error:
        print(f"Signum history unavailable: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
