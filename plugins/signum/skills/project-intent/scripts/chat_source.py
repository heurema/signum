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
from decision_capture import digest
import history_origin


class ChatSourceError(ValueError):
    pass


class MissingTurn(ChatSourceError):
    """Desktop could answer the lookup but has no readable history for this turn."""


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
        if time.monotonic() >= self.deadline:
            raise ChatSourceError("Codex history read timed out.")
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
                error = response.get("error")
                if isinstance(error, dict) and method == "thread/read" and error.get("code") == -32600:
                    if error.get("message") == f"thread not loaded: {params['threadId']}":
                        raise MissingTurn("Desktop has not loaded this chat. Its contents are unknown.")
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


def messages(server, project, thread_id, turn, previous_turn, *, origin_digest=None):
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
    from capture_inbox import native_prompt_seen
    if origin_digest is not None:
        from capture_inbox import digest as prompt_digest
    texts, user_hashes = [], []
    receipt_matches = False
    plain_user_text = False
    for user in users:
        chunks = [p["text"] for p in user["content"] if p.get("type") == "text" and isinstance(p.get("text"), str)]
        body = "\n".join(chunks)
        texts.append(body)
        if not body.strip() or not user["id"] or len(chunks) != len(user["content"]):
            continue
        user_hashes.append(digest([user["id"], body]))
        if len(chunks) == 1:
            plain_user_text = len(users) == 1
            if not receipt_matches and (origin_digest is None or prompt_digest(body) == origin_digest):
                receipt_matches = native_prompt_seen(project, thread_id, turn_id, body)
    # The receipt authenticates one intact message. It cannot authorize
    # the combined text of a turn that later gained more messages.
    user_id = users[0]["id"] if len(users) == 1 else ""
    return {
        "thread": thread_id, "turn": turn_id, "user": "\n\n".join(texts), "user_id": user_id,
        "assistant": final_text, "assistant_id": final_id, "allow_owner": plain_user_text and receipt_matches,
        "receipt_matches": receipt_matches, "user_message_hashes": user_hashes,
        "empty_turn": not users and not final_id,
        "complete_user_text": bool(users) and len(user_hashes) == len(users),
        "started_at": turn.get("startedAt"), "completed": turn.get("status") == "completed",
        "status": turn.get("status"), "plain_user_text": plain_user_text,
        "previous_turn": previous_turn,
    }


def lookup_receipts(project, keys, *, verify_origin=()):
    """Read the requested turns and revalidate active inherited receipts, including reviewed ones."""
    keys = list(dict.fromkeys(keys))
    checks = dict.fromkeys([*keys, *verify_origin])
    if not checks:
        return
    try:
        root = project_root(project)
        origins, refused = history_origin.resolve(root, checks)
    except (ValueError, OSError, KeyError) as error:
        for key in checks:
            yield key, None, error
        return
    for key, reason in refused.items():
        yield key, None, ChatSourceError(reason)
    remaining = {}
    for key in keys:
        if key not in refused:
            cwd, fingerprint = origins.get(key, (root, None))
            remaining.setdefault((cwd, key[0]), {})[key[1]] = fingerprint
    if not remaining:
        return
    try:
        with AppServer(root) as server:
            for (cwd, thread_id), wanted in remaining.items():
                reason = MissingTurn("This turn is unavailable in the selected chat.")
                try:
                    checked_thread(server, cwd, thread_id)
                    for index, (turn, previous) in enumerate(with_previous(turn_rows(server, thread_id, 100))):
                        if index >= 2000:
                            raise ChatSourceError("Turn lookup exceeds the local history limit.")
                        turn_id = turn["id"]
                        if turn_id not in wanted:
                            continue
                        fingerprint = wanted.pop(turn_id)
                        try:
                            item = messages(server, root, thread_id, turn, previous, origin_digest=fingerprint)
                            item["latest_in_thread"] = index == 0
                            if fingerprint is not None:
                                if not item["receipt_matches"] or not item["complete_user_text"]:
                                    raise ChatSourceError("Original message does not match the inherited receipt.")
                                item["history_origin"] = str(cwd)
                        except (ValueError, OSError, KeyError) as error:
                            yield (thread_id, turn_id), None, error
                        else:
                            yield (thread_id, turn_id), item, None
                        if not wanted:
                            break
                except (ValueError, OSError, KeyError) as error:
                    reason = error
                for turn_id in sorted(wanted):
                    yield (thread_id, turn_id), None, reason
                wanted.clear()
    except (ValueError, OSError, KeyError) as error:
        for (_, thread_id), wanted in remaining.items():
            for turn_id in sorted(wanted):
                yield (thread_id, turn_id), None, error


def read_turn(project, thread_id, turn_id):
    for _, item, reason in lookup_receipts(project, [(thread_id, turn_id)]):
        if reason is not None:
            raise ChatSourceError(str(reason)) from reason
        return item
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


def recent_turns(project, *, thread_id=None, since=None, thread_limit=6, turn_limit=4, reviewed=(), skip_turn=None):
    """Read bounded unreviewed turns. Completeness applies only to the local index."""
    if not 1 <= thread_limit <= 30 or not 1 <= turn_limit <= 50 or (since is not None and not number(since)):
        raise ValueError("Invalid history window.")
    root = project_root(project)
    reviewed = set(reviewed)
    turns, warnings, skipped = [], [], []
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
                    for index, (turn, previous) in enumerate(with_previous(rows)):
                        stamp = turn.get("startedAt")
                        if since is not None and number(stamp) and stamp < since:
                            break
                        if turn.get("status") == "completed" and (thread["id"], turn["id"]) in reviewed:
                            continue
                        if since is not None and not number(stamp):
                            warnings.append("A turn has no reliable start time.")
                        item = None
                        if skip_turn is not None:
                            item = messages(server, root, thread["id"], turn, previous)
                            if skip_turn(item):
                                skipped.append((thread["id"], turn["id"]))
                                continue
                        if count == 0:
                            active_count += 1
                            if active_count > thread_limit:
                                warnings.append("More project chats may be available.")
                                break
                        if count >= turn_limit:
                            warnings.append("More project turns may be available.")
                            break
                        count += 1
                        if item is None:
                            item = messages(server, root, thread["id"], turn, previous)
                        item["latest_in_thread"] = index == 0
                        turns.append(item)
                except (ChatSourceError, OSError, ValueError, KeyError) as error:
                    warnings.append(f"Could not finish reading indexed chat {thread['id']}: {error}")
                if active_count > thread_limit:
                    break
        except (ChatSourceError, OSError, ValueError, KeyError) as error:
            warnings.append(f"Could not finish listing indexed chats: {error}")
    warnings = list(dict.fromkeys(warnings))
    turns.sort(key=lambda row: (row["started_at"] or 0, row["thread"], row["turn"]))
    return {"turns": turns, "complete": not warnings, "warnings": warnings, "indexed_only": True, "skipped": skipped}


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
