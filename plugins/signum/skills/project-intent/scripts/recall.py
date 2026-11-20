"""Recall saved decisions from the local Signum journal."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys
import unicodedata

from signum import Journal, SignumError, journal_directory


MAX_CANDIDATES = 3
MAX_QUERY_TERMS = 20
INDEX_SUMMARY_LENGTH = 120
CYRILLIC_WORD = re.compile(r"[а-я]+")
QUERY_STOP_WORDS = frozenset("""
    about from have does given into should that their them then there these this was were what when where which
    will with would current status project remember decision decisions permission permissions allowed adopted
    было была были будет будут ведь вот где для если еще или как когда кого какие какой куда ли мы надо
    нас наше него нее они она оно при про раз свой так там тот уже чем что чтобы это этот этой этих
    вспомни можно проект проекта решение решения разрешение разрешено текущий текущая принято
""".split())
DETAIL_FIELDS = (
    "id", "kind", "state", "summary", "source", "scope", "authority_limit", "verification", "questions",
    "basis", "replaces", "issues",
)
SEARCH_FIELDS = ("summary", "scope", "authority_limit", "verification", "questions")


def words(text):
    normalized = unicodedata.normalize("NFC", text).casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+", normalized)


def matches_word(term, word):
    if term == word:
        return True
    if not (CYRILLIC_WORD.fullmatch(term) and CYRILLIC_WORD.fullmatch(word)):
        return False
    prefix_length = max(5, len(term) - 5, len(word) - 5)
    return min(len(term), len(word)) >= prefix_length and term[:prefix_length] == word[:prefix_length]


def search_terms(query):
    terms = list(dict.fromkeys(
        word for word in words(query) if len(word) > 1 and word not in QUERY_STOP_WORDS
    ))
    if len(terms) > MAX_QUERY_TERMS:
        raise SignumError(f"Use at most {MAX_QUERY_TERMS} distinctive query words.")
    return terms


def record_matches(record, terms):
    searchable = set(words(record["topic"]))
    for field in SEARCH_FIELDS:
        value = record.get(field, "")
        searchable.update(words(" ".join(value) if isinstance(value, list) else value))
    return [term for term in terms if any(matches_word(term, word) for word in searchable)]


def index_entry(entry):
    summary = " ".join(entry["summary"].split())
    if len(summary) > INDEX_SUMMARY_LENGTH:
        summary = summary[:INDEX_SUMMARY_LENGTH - 1].rstrip() + "…"
    return {"kind": entry["kind"], "state": entry["state"], "summary": summary}


def topic_view(journal, topic, entries, historical, *, detailed):
    view = {"topic": topic}
    if detailed:
        view["entries"] = [
            {field: entry[field] for field in DETAIL_FIELDS if entry.get(field)}
            for entry in entries
        ]
        contract = journal.contract(topic)
        draft = contract["draft"]
        if draft["blocked"]:
            view["blocked"] = draft["blocked"]
        if draft["missing_verification"]:
            view["missing_verification"] = True
        checks = contract.get("related_checks", [])
        if checks:
            view["related_checks"] = checks[-MAX_CANDIDATES:]
            view["related_check_count"] = len(checks)
    else:
        view["entries"] = [index_entry(entry) for entry in entries]
    if historical:
        view["history_versions"] = len(historical)
    return view


def recall(journal, *, topic=None, query=None):
    status = journal.status()
    entries = defaultdict(list)
    for entry in status["confirmed"] + status["proposals"]:
        entries[entry["topic"]].append(entry)
    versions = defaultdict(list)
    historical = defaultdict(list)
    current_ids = {entry["id"] for topic_entries in entries.values() for entry in topic_entries}
    for identifier, event in journal.proposed.items():
        record = event["record"]
        versions[record["topic"]].append((identifier, record))
        if identifier not in current_ids:
            historical[record["topic"]].append(identifier)

    if not versions:
        return {"result": "empty"}
    if query is not None and query.strip() in versions:
        topic = query.strip()
    if topic is not None:
        if topic not in versions:
            return {"result": "unknown", "topic": topic}
        return {
            "result": "matched" if entries[topic] else "historical",
            "topics": [topic_view(journal, topic, entries[topic], historical[topic], detailed=True)],
        }
    if query is None:
        return {
            "result": "index",
            "topics": [
                topic_view(journal, name, entries[name], historical[name], detailed=False) for name in sorted(versions)
            ],
        }

    terms = search_terms(query)
    candidates = []
    for name, records in versions.items():
        matches = [(identifier, record_matches(record, terms)) for identifier, record in records]
        count = max(len(matched) for _, matched in matches)
        if count:
            strongest = [(identifier, matched) for identifier, matched in matches if len(matched) == count]
            candidates.append((count, name, strongest))
    if not candidates:
        return {"result": "unknown"}
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    best = candidates[0][0]
    leaders = [candidate for candidate in candidates if candidate[0] == best]
    if len(leaders) > 1 or best < min(2, len(terms)):
        return {
            "result": "ambiguous",
            "topics": [
                topic_view(journal, name, entries[name], historical[name], detailed=False)
                for _, name, _ in leaders[:MAX_CANDIDATES]
            ],
            "candidate_count": len(leaders),
        }
    _, name, matches = leaders[0]
    view = topic_view(journal, name, entries[name], historical[name], detailed=True)
    view["matched_terms"] = [term for term in terms if any(term in matched for _, matched in matches)]
    prior_matches = [identifier for identifier, _ in matches if identifier not in current_ids]
    if prior_matches:
        view["historical_matches"] = prior_matches[:MAX_CANDIDATES]
        view["historical_match_count"] = len(prior_matches)
    active_match = any(identifier in current_ids for identifier, _ in matches)
    return {"result": "matched" if active_match else "historical", "topics": [view]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path, help="existing project directory")
    selector = parser.add_mutually_exclusive_group()
    selector.add_argument("--topic", help="exact topic key")
    selector.add_argument("--query", help="distinctive words from the request")
    args = parser.parse_args()
    try:
        project = args.project.resolve(strict=True)
        if not project.is_dir():
            raise SignumError("--project must be an existing directory.")
        directory = journal_directory(project)
        journal = Journal.read(directory / "state.json")
        result = recall(journal, topic=args.topic, query=args.query)
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    except (SignumError, OSError, ValueError) as error:
        print(f"signum recall: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
