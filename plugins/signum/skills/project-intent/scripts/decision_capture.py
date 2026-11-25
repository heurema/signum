"""Record source-checked decisions and a replay receipt in the project journal."""

from datetime import datetime
import hashlib
import json
import math
import re


UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
DIGEST = re.compile(r"[0-9a-f]{64}")
# Only self-contained, unqualified declarations enter the automatic path.
# Other natural-language choices remain proposals for review.
DECLARATION = re.compile(
    r"(?:я выбираю|выбираем|бер[её]м|используем|оставляем|переходим на|храним|"
    r"i (?:choose|select)|we (?:choose|select|use)|let's use) "
    r"[^,;:!?\n]+|[\w][\w ./+-]* (?:подходит|устраивает|works for me)", re.I,
)
QUALIFIED = re.compile(
    r"""[?«»“”„"\x60<>]|(?:^|\n)\s*>|\b(?:не|нет|если|бы|пока|может|наверно|наверное|"""
    r"""например|допустим|передай|цитата|хотя|возможно|но|только|лишь|ли|или|якобы|"""
    r"""but|if|not|maybe|perhaps|only|or|example|quote)\b""", re.I,
)
DEFERRED = re.compile(
    r"(?:[\w][\w /+-]* )?(?:(?:пока )?(?:оставим открытым|оставим открытой|отложим)|"
    r"(?:обсудим|решим|уточним) (?:потом|позже|отдельно)|потом|позже|later|remains open)", re.I,
)


def digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def parse_request(core, payload):
    if not isinstance(payload, dict) or payload.keys() != {"thread", "turn", "choices"}:
        raise core.SignumError("Capture requires thread, turn and choices.")
    if not all(core.matches(UUID, payload[key]) for key in ("thread", "turn")):
        raise core.SignumError("Capture needs the exact Codex thread and turn IDs.")
    choices = payload["choices"]
    if not isinstance(choices, list):
        raise core.SignumError("Capture choices must be an array.")
    topics = set()
    for item in choices:
        if not isinstance(item, dict) or item.keys() != {"topic", "summary", "state", "quote", "expected"}:
            raise core.SignumError("Each choice needs topic, summary, state, quote and expected.")
        if not core.matches(core.TOPIC_PATTERN, item["topic"]) or item["topic"] in topics:
            raise core.SignumError("Each choice needs a distinct valid topic.")
        if item["state"] not in ("open", "chosen") or not all(core.nonempty(item[k]) for k in ("summary", "quote")):
            raise core.SignumError("A choice needs open or chosen, a summary and a verbatim quote.")
        if item["expected"] is not None and not core.matches(core.ID_PATTERN, item["expected"]):
            raise core.SignumError("Expected must name the current version or be null.")
        topics.add(item["topic"])


def direct_choice(owner, quote, summary):
    # Never promote a paraphrase into an owner-authored commitment. A clause
    # left out of the quote may change its meaning. Only an independent,
    # explicitly deferred companion can be set aside.
    clause = quote.strip().rstrip(".!").strip()
    if clause != summary.strip().rstrip(".!").strip() or not DECLARATION.fullmatch(clause) or QUALIFIED.search(clause):
        return False
    parts = [part.strip().rstrip(".!").strip() for part in re.split(r"[,;\n]", owner.strip())]
    if parts.count(clause) != 1:
        return False
    words = set(re.findall(r"\w+", clause.casefold()))
    for part in parts:
        if part == clause:
            continue
        deferred = re.sub(r"\bпока (?=оставим|отложим)", "", part, flags=re.I)
        if not DEFERRED.fullmatch(part) or QUALIFIED.search(deferred) or words.intersection(re.findall(r"\w+", part.casefold())):
            return False
    return True


def load_reviews(core, journal, entries):
    if not isinstance(entries, list):
        raise core.SignumError("Invalid capture receipts.")
    for entry in entries:
        keys = {"thread", "turn", "user_id", "assistant_id", "user_hash", "fingerprint", "choices", "completed", "started_at", "owner_seen"}
        if not isinstance(entry, dict) or entry.keys() != keys:
            raise core.SignumError("Invalid capture receipt.")
        key = (entry["thread"], entry["turn"])
        if not all(core.matches(UUID, v) for v in key) or key in journal.reviewed:
            raise core.SignumError("Invalid or duplicated reviewed turn.")
        if entry["started_at"] is not None and (
            type(entry["started_at"]) not in (int, float) or not math.isfinite(entry["started_at"])
        ):
            raise core.SignumError("Invalid capture timestamp.")
        if not isinstance(entry["choices"], dict) or not all(type(entry[k]) is bool for k in ("completed", "owner_seen")):
            raise core.SignumError("Invalid capture state.")
        if not all(isinstance(entry[k], str) for k in ("user_id", "assistant_id")):
            raise core.SignumError("Invalid source message identity.")
        if not all(core.matches(DIGEST, entry[k]) for k in ("user_hash", "fingerprint")):
            raise core.SignumError("Invalid capture fingerprint.")
        for topic, part in entry["choices"].items():
            if not isinstance(part, dict) or part.keys() != {"id", "state", "input_hash"}:
                raise core.SignumError("Invalid captured decision.")
            identifier = part["id"]
            if (not core.matches(core.ID_PATTERN, identifier) or identifier not in journal.proposed
                    or journal.record(identifier)["topic"] != topic or journal.record(identifier)["kind"] != "decision"
                    or part["state"] not in ("open", "chosen") or not core.matches(DIGEST, part["input_hash"])
                    or (part["state"] == "chosen" and identifier not in journal.accepted)):
                raise core.SignumError("Capture refers to an inconsistent decision.")
        journal.reviewed[key] = entry


def displayed_rule(text):
    # Support ordinary presentation while excluding unrelated surrounding
    # questions, exceptions or executable actions from a bare assent.
    text = re.sub(r"\*\*([^*\n]+)\*\*|__([^_\n]+)__|`([^`\n]+)`",
                  lambda m: next(v for v in m.groups() if v is not None), text.strip())
    text = re.sub(r"^(?:предлагаю|предложение|правило|proposed rule|proposal|rule)\s*:\s*", "", text, flags=re.I)
    text = re.sub(r"\s+(?:согласны|подходит|подтверждаете|фиксируем|agreed|confirm)\?\s*$", "", text, flags=re.I)
    return " ".join(text.split())


def whole_statement(text, quote):
    if not quote or text.count(quote) != 1:
        return False
    before, _, after = text.partition(quote)
    starts = not before.strip() or before.rstrip().endswith((".", "!", "?")) or before.endswith("\n")
    ends = not after.strip() or quote.endswith((".", "!", "?")) or after.startswith("\n")
    return starts and ends and not re.match(
        r"\s*(?:но|только|при условии|если|кроме|однако|but|only|unless|except|provided|if)\b", after, re.I)


def previous_offer(core, journal, source, previous, item):
    if previous is None or previous["thread"] != source["thread"] or previous["turn"] != source.get("previous_turn"):
        return None
    review = journal.reviewed.get((previous["thread"], previous["turn"]))
    if not review or not previous["completed"] or not previous["assistant_id"]:
        return None
    part = review["choices"].get(item["topic"])
    if not part or part["state"] != "open" or part["id"] in journal.accepted or part["id"] in journal.withdrawn:
        return None
    record = journal.record(part["id"])
    user_hash = digest([previous["user_id"] or "", previous["user"]])
    fingerprint = digest([user_hash, previous["assistant_id"], previous["assistant"]])
    if (review["user_hash"] != user_hash
            or (review["assistant_id"] and review["fingerprint"] != fingerprint)
            or record["summary"] != item["summary"] or record["replaces"] != item["expected"]):
        return None
    # A generic yes can only address one visibly self-contained rule. Extra
    # prose may contain another question or a qualification.
    if displayed_rule(previous["assistant"]) != core.assent_text(record["summary"]):
        return None
    origin, _, original_quote = record["source"].partition("\n")
    if (origin.endswith(f" message:{previous['user_id']}") and original_quote in previous["user"]
            and not whole_statement(previous["user"], original_quote)):
        return None
    if sum(core.assent_text(journal.record(p["id"])["summary"]) == core.assent_text(record["summary"])
           for p in review["choices"].values()) != 1:
        return None
    pending = [p for p in journal.status(item["topic"])["proposals"] if p["state"] != "conflict"]
    return part["id"] if len(pending) == 1 else None


def assign_choice(core, journal, source, item, previous):
    topic, summary, quote = item["topic"], item["summary"], item["quote"]
    current = journal.confirmed.get(topic)
    if current != item["expected"]:
        raise core.SignumError(f"Current version for {topic} changed. Inspect its history.")
    if current is not None and journal.record(current)["kind"] != "decision":
        raise core.SignumError("Capture applies only to project decisions.")
    in_owner = quote in source["user"]
    if not in_owner and quote not in source["assistant"]:
        raise core.SignumError("Decision quote is absent from the source turn.")
    identifier = source["user_id"] if in_owner else source["assistant_id"]
    if not core.nonempty(identifier):
        raise core.SignumError("The quoted message has no source identity.")
    source_ref = f"codex:thread:{source['thread']} turn:{source['turn']} message:{identifier}"
    chosen = (item["state"] == "chosen" and source["allow_owner"] is True and in_owner
              and direct_choice(source["user"], quote, summary))

    if (item["state"] == "chosen" and source["allow_owner"] is True and quote == source["user"].strip()
            and core.assent_text(quote).lower().rstrip(".!") in core.ASSENT_REPLIES):
        chosen = previous_offer(core, journal, source, previous, item) is not None

    if current is not None and chosen and journal.record(current)["summary"] != summary:
        confirmed_at = next(e["at"] for e in journal.events if e["event"] == "confirmed" and e["id"] == current)
        prior_time = datetime.fromisoformat(confirmed_at).timestamp()
        origin = journal.owner_evidence.get(current, "").split("\n", 1)[0]
        if origin:
            for review in journal.reviewed.values():
                ref = f"codex:thread:{review['thread']} turn:{review['turn']} message:{review['user_id']}"
                if ref == origin and review["started_at"] is not None:
                    prior_time = review["started_at"]
                    break
        started_at = source.get("started_at")
        if (type(started_at) not in (int, float) or not math.isfinite(started_at)
                or started_at <= prior_time):
            raise core.SignumError("An older or undated turn cannot replace the current decision.")

    if current is not None and journal.record(current)["summary"] == summary and chosen:
        return current, "chosen"
    candidates = [p for p in journal.status(topic)["proposals"]
                  if p["replaces"] == current and p["kind"] == "decision" and p["summary"] == summary
                  and journal.record(p["id"]).keys() == core.REQUIRED]
    if len(candidates) > 1:
        raise core.SignumError("Several proposals match this wording. Resolve the competing versions.")
    if candidates:
        version = candidates[0]["id"]
    else:
        record = {"topic": topic, "kind": "decision", "summary": summary, "replaces": current,
                  "source": f"{source_ref}\n{quote}", "reason": "Captured during project discussion."}
        version = journal.propose(record)["id"]
    if chosen:
        journal.confirm(version, owner_evidence=f"{source_ref}\n{quote}")
    return version, "chosen" if chosen else "open"


def capture(core, project, directory, payload, read_turn):
    parse_request(core, payload)
    key = (payload["thread"], payload["turn"])
    source = read_turn(project, *key)
    if (source.get("thread"), source.get("turn")) != key:
        raise core.SignumError("The source reader returned a different turn.")
    user_id, assistant_id = source.get("user_id") or "", source.get("assistant_id") or ""
    if not user_id and not assistant_id and not source.get("complete_user_text"):
        raise core.SignumError("No source messages are available for this turn.")
    user_hash = digest([user_id, source["user"]])
    fingerprint = digest([user_hash, assistant_id, source["assistant"]])
    previous = None
    if (source.get("allow_owner") is True and source.get("previous_turn")
            and core.assent_text(source["user"]).lower().rstrip(".!") in core.ASSENT_REPLIES
            and any(i["state"] == "chosen" for i in payload["choices"])):
        previous = read_turn(project, key[0], source["previous_turn"])
    path = directory / "state.json"
    with core.locked_store(directory):
        journal = core.Journal.read(path, required=True)
        old = journal.reviewed.get(key)
        if old:
            # Desktop can append another user message before the turn ends.
            # An empty, unfinished review still has a verifiable original message.
            extended = (not old["completed"] and not old["assistant_id"] and old["user_id"] and not old["choices"]
                        and source.get("complete_user_text")
                        and next(iter(source.get("user_message_hashes", ())), None) == old["user_hash"])
            if ((old["user_hash"] != user_hash and not extended)
                    or (old["assistant_id"] and old["fingerprint"] != fingerprint)):
                raise core.SignumError("Published source content changed; review the source again.")
            if not old["owner_seen"] and source["allow_owner"]:
                submitted = {item["topic"] for item in payload["choices"]}
                missing = {topic for topic, part in old["choices"].items() if part["state"] == "open"} - submitted
                if missing:
                    raise core.SignumError("The owner receipt arrived after review. Recheck the original choices: "
                                           + ", ".join(sorted(missing)) + ".")
        parts = dict(old["choices"]) if old else {}
        for item in payload["choices"]:
            topic = item["topic"]
            existing = parts.get(topic)
            if existing:
                if existing["input_hash"] != digest(item):
                    raise core.SignumError("This turn already recorded a different interpretation of that choice.")
                newly_attested = not old["owner_seen"] and source["allow_owner"] is True
                if not (newly_attested and existing["state"] == "open" and item["state"] == "chosen"):
                    continue
            version, state = assign_choice(core, journal, source, item, previous)
            parts[topic] = {"id": version, "state": state, "input_hash": digest(item)}
        receipt = {"thread": key[0], "turn": key[1], "user_id": user_id, "assistant_id": assistant_id,
                   "user_hash": user_hash, "fingerprint": fingerprint, "completed": source.get("completed") is True,
                   "started_at": source.get("started_at"), "owner_seen": source["allow_owner"] is True, "choices": parts}
        if receipt != old:
            journal.reviewed[key] = receipt
            core.write_journal(path, journal)
    result = {"result": "reviewed", "thread": key[0], "turn": key[1],
              "choices": {topic: {"id": part["id"], "state": part["state"]} for topic, part in parts.items()}}
    if source["allow_owner"]:
        for topic, part in parts.items():
            if part["state"] != "open":
                continue
            record = journal.record(part["id"])
            ref, _, quote = record["source"].partition("\n")
            if (ref == f"codex:thread:{key[0]} turn:{key[1]} message:{user_id}"
                    and quote == record["summary"] and whole_statement(source["user"], quote)
                    and record["replaces"] == journal.confirmed.get(topic)
                    and part["id"] not in journal.accepted and part["id"] not in journal.withdrawn):
                result["choices"][topic]["wording"] = quote
    return result
