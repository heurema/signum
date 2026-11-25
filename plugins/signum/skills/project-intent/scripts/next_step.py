"""Describe the next step under an existing permission without changing project state."""

import os
import sqlite3

import capture_inbox
import delivery
import recall
import scope_guard


def closed(deliveries, identifier):
    version = deliveries.latest_version.get(identifier)
    return (deliveries.decisions.get(version, {}).get("event") == "accepted"
            and not deliveries.open_start(identifier))


def select(project, journal, deliveries, query):
    status = journal.status()
    cards = [item for item in status["confirmed"] if item["kind"] == "permission"]
    pending = [item for item in status["proposals"] if item["kind"] == "permission" and item["state"] != "conflict"]
    candidates = cards + [item for item in pending if item["topic"] not in journal.confirmed]
    navigation = {"продолжи", "продолжим", "продолжить", "continue", "resume", "next", "шаг", "дальше"}
    terms = [t for t in recall.search_terms(query) if t not in navigation] if query else []
    if query:
        exact = [item for item in candidates if item["topic"] == query.strip() or item["id"] == query.strip()]
        if exact:
            candidates = exact
        else:
            scored = []
            for item in candidates:
                main = set(recall.words(item["topic"]) + recall.words(item["summary"]))
                primary = sum(any(recall.matches_word(term, word) for word in main) for term in terms)
                coverage = len(recall.record_matches(item, terms))
                score = (primary, coverage)
                # An incidental mention in limits or evidence cannot identify the work.
                if primary and coverage * 2 >= len(terms):
                    scored.append((score, item))
            strongest = max((score for score, _ in scored), default=(0, 0))
            candidates = [item for score, item in scored if score == strongest]
            if candidates and strongest[1] < min(2, len(terms)):
                return choices(candidates)
    elif candidates:
        unfinished = [item for item in candidates if not closed(deliveries, item["id"])]
        if not unfinished and len(candidates) > 1:
            issues = [describe(project, journal, deliveries, item) for item in candidates]
            investigations = [item for item in issues if item["result"] != "complete"]
            if investigations:
                return {"result": "investigate", "reason": "Review the evidence for these accepted deliveries.",
                        "deliveries": investigations}
            return {"result": "complete", "reason": "All current permissions have accepted deliveries with matching evidence.",
                    "accepted_permissions": len(candidates)}
        candidates = unfinished or candidates
    if not candidates:
        return {"result": "no_permission", "reason": "No matching current permission or pending permission proposal."}
    if len(candidates) > 1:
        return choices(candidates)
    return candidates[0]


def choices(candidates):
    return {"result": "ambiguous", "candidate_count": len(candidates),
            "candidates": [{key: item[key] for key in ("topic", "id", "summary", "state")}
                           for item in sorted(candidates, key=lambda item: (item["topic"], item["id"]))[:5]]}


def started_step(project, permission, scope, start, deliveries, journal):
    before = start.get("scope_baseline")
    observed = scope_guard.capture(project, scope)
    scope_guard.check_workspace(scope, before, observed)
    changed = scope_guard.implementation_manifest(scope, before, observed)
    report = scope["report"]
    if report not in observed["files"]:
        if scope_guard.requires_bound_report(permission.get("scope")):
            delivery.verification_context(report, journal, deliveries)
        if changed:
            return {"result": "verify", "step": "Finish the agreed edits and run the agreed verification.",
                    "changed": [item["path"] for item in changed]}
        return {"result": "implement", "step": "Carry out the agreed change within the listed files."}
    artifact = delivery.artifact_entry(project, report)
    issue = delivery.report_issue(artifact["text"])
    if issue:
        return {"result": "blocked", "reason": f"The verification report {report} cannot establish a pass. {issue}"}
    evidence = {"files": changed, "artifact": {"path": report, "sha256": artifact["sha256"]},
                "criteria": permission["verification"], "result": "Inspect the existing check report.", "status": "pass"}
    checked = delivery.read_evidence(project, evidence, permission, start)
    scope_guard.verify(project, scope, before, checked)
    return {"result": "register_evidence", "step": "Record the verified files and the existing check report."}


def describe(project, journal, deliveries, card):
    identifier = card["id"]
    result = {"topic": card["topic"], "permission": identifier, "summary": card["summary"]}
    if identifier not in journal.accepted:
        return {**result, "result": "unconfirmed", "reason": "The proposed permission has no owner confirmation."}
    latest = deliveries.latest_version.get(identifier)
    decision = deliveries.decisions.get(latest)
    start = deliveries.open_start(identifier)
    if latest and (not start or start["id"] == deliveries.versions[latest]["start"]):
        result["delivery"] = latest
        if decision:
            result["delivery_state"] = decision["event"]
            result["owner_reason"] = decision["reason"]
            if decision["event"] == "accepted":
                issues = deliveries.card(project, journal, latest)["acceptance_blockers"]
                if issues:
                    return {**result, "result": "investigate", "reason": " ".join(issues)}
                return {**result, "result": "complete", "reason": "The owner accepted this delivery version."}
        else:
            review = deliveries.card(project, journal, latest)
            result["delivery_state"] = review["state"]
            if review["acceptance_blockers"]:
                return {**result, "result": "blocked", "reason": " ".join(review["acceptance_blockers"])}
            return {**result, "result": "awaiting_acceptance", "step": "The owner can review and accept or return this version."}

    try:
        permission = delivery.require_permission(journal, identifier)
        scope = scope_guard.parse(permission.get("scope"))
        if not scope:
            return {**result, "result": "blocked", "reason": "This permission has no exact file scope for a bounded next step."}
        result.update(scope=scope, verification=permission["verification"])
        if permission.get("authority_limit"):
            result["authority_limit"] = permission["authority_limit"]
        dependencies = list(permission.get("depends_on", []))
        if permission.get("basis") and permission["basis"] not in dependencies:
            dependencies.append(permission["basis"])
        if dependencies:
            result["decisions"] = [{"topic": item["topic"], "id": item["id"], "summary": journal.record(item["id"])["summary"]}
                                   for item in dependencies]
        checks = [entry for entry in journal.contract(card["topic"]).get("related_checks", []) if entry["relation"] == "current"]
        if checks:
            result["recorded_observations"] = [{"topic": item["topic"], "observation": item["observation"]} for item in checks]
        inbox = capture_inbox.pending_decisions(project, journal, permission, advance=False,
                                                request_thread=os.environ.get("CODEX_THREAD_ID")) or {}
        if inbox.get("history_gap_count"):
            result["history_gap_count"] = inbox["history_gap_count"]
        if start:
            result["start"] = start["id"]
            step = started_step(project, permission, scope, start, deliveries, journal)
        else:
            scope_guard.baseline(project, scope)
            step = {"result": "start", "step": "Start the confirmed change within the listed files."}
        current = inbox.get("current_request")
        if current and step["result"] != "blocked":
            return {**result, "result": "request_pending", "request": {"thread": current["thread"], "turn": current["turn"]},
                    "reason": "Interpret the current message against the recorded scope. It is still unreviewed.",
                    "if_scope_unchanged": step}
        return {**result, **step}
    except (OSError, ValueError, sqlite3.Error) as error:
        return {**result, "result": "blocked", "reason": str(error)}


def store_revision(directory):
    # Journal updates use atomic replacement. Receipts can change in place.
    revision = []
    for name in ("state.json", "delivery.json", capture_inbox.DB, capture_inbox.DB + "-wal"):
        try:
            info = (directory / name).lstat()
        except FileNotFoundError:
            revision.append(None)
        else:
            revision.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
    return revision


def run(project, directory, core, query):
    before = store_revision(directory)
    try:
        journal = core.Journal.read(directory / "state.json", required=True)
        deliveries = delivery.Deliveries.read(directory / "delivery.json", core)
        selection = select(project, journal, deliveries, query)
        result = selection if "result" in selection else describe(project, journal, deliveries, selection)
    except (core.SignumError, recall.SignumError, OSError, ValueError, sqlite3.Error, KeyError) as error:
        result = {"result": "blocked", "reason": str(error)}
    if before != store_revision(directory):
        return {"result": "blocked", "reason": "The project journal or chat receipts changed during this read. Retry."}
    return result
