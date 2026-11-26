"""Link a recipient to a project task and recover its current decision and delivery."""
import json
import sqlite3

import delivery
import next_step
import scope_guard


STORE = "handoffs.json"


def validate(core, topic, link):
    fields = {"previous_task", "assignee", "decision_topic", "question"}
    if (not core.matches(core.TOPIC_PATTERN, topic) or not isinstance(link, dict) or link.keys() != fields
            or not all(core.nonempty(value) for value in link.values())
            or not core.matches(core.TOPIC_PATTERN, link["decision_topic"]) or topic == link["decision_topic"]):
        raise ValueError("A handoff needs two distinct valid topics, a predecessor, a recipient and a question.")


def read_links(path, core):
    try:
        with open(path, encoding="utf-8", opener=core.no_follow) as stream:
            doc = json.load(stream, object_pairs_hook=lambda pairs: core.unique_json_object(pairs, operation="Handoff"))
    except FileNotFoundError:
        return {}
    if (not isinstance(doc, dict) or type(doc.get("schema_version")) is not int or doc["schema_version"] != 1
            or not isinstance(doc.get("links"), dict)):
        raise ValueError("Unsupported or invalid handoff file.")
    return doc["links"]


def link_id(topic, link):
    return delivery.version_id({"topic": topic, **link})


def dependencies(permission):
    refs = list(permission.get("depends_on", []))
    if permission.get("basis") and permission["basis"] not in refs:
        refs.append(permission["basis"])
    return refs


def topic_entries(journal, topic, kind):
    status = journal.status(topic)
    pending = [p for p in status["proposals"] if p["state"] != "conflict"]
    if any(item["kind"] != kind for item in status["confirmed"] + pending):
        raise ValueError(f"The linked topic {topic} must contain {kind} records.")
    return status["confirmed"], pending


def permission_issue(journal, permission, directory, core):
    links = read_links(directory / STORE, core)
    if permission["topic"] not in links:
        return None
    link = links[permission["topic"]]
    validate(core, permission["topic"], link)
    topic = link["decision_topic"]
    confirmed, pending = topic_entries(journal, topic, "decision")
    if pending:
        return f"The linked task is waiting for the owner's decision on {topic}."
    if not confirmed or {"topic": topic, "id": confirmed[0]["id"]} not in dependencies(permission):
        return f"The permission needs the current linked decision on {topic}."
    return None


def link_task(args, directory, core):
    link = {"previous_task": args.after, "assignee": args.assignee,
            "decision_topic": args.decision, "question": args.question}
    validate(core, args.topic, link)
    # Refuse an unconnected folder before creating a lock.
    core.Journal.read(directory / "state.json", required=True)
    with core.locked_store(directory):
        journal = core.Journal.read(directory / "state.json", required=True)
        for topic, kind in ((args.decision, "decision"), (args.topic, "permission")):
            confirmed, proposals = topic_entries(journal, topic, kind)
            if kind == "decision" and not (confirmed or proposals):
                raise ValueError(f"Expected a {kind} topic: {topic}.")
        links = read_links(directory / STORE, core)
        current = links.get(args.topic)
        if args.topic in links:
            validate(core, args.topic, current)
        identifier = link_id(args.topic, link)
        if current == link:
            return {"result": "linked", "topic": args.topic, "id": identifier}
        previous = link_id(args.topic, current) if current is not None else None
        if previous != args.expected:
            raise ValueError(f"Task link changed. Read the handoff and supply its current ID: {previous}.")
        links[args.topic] = link
        core.write_document(directory / STORE, {"schema_version": 1, "links": links})
        return {"result": "linked", "topic": args.topic, "id": identifier}


def inspect(project, directory, core, topic):
    journal = core.Journal.read(directory / "state.json")
    if not journal.enabled:
        return {"result": "disabled"}
    links = read_links(directory / STORE, core)
    if topic is None and len(links) > 1:
        for key, link in links.items():
            validate(core, key, link)
        return {"result": "ambiguous", "candidates": [
            {"topic": key, "previous_task": links[key]["previous_task"], "recipient": links[key]["assignee"]}
            for key in sorted(links)]}
    topic = topic if topic is not None else next(iter(links), None)
    if topic not in links:
        return {"result": "none"}
    link = links[topic]
    validate(core, topic, link)
    choice = link["decision_topic"]
    card = {"topic": topic, "id": link_id(topic, link), "previous_task": link["previous_task"],
            "recipient": link["assignee"], "assignee": "coordinator", "decision_topic": choice}
    confirmed, pending = topic_entries(journal, choice, "decision")
    permissions, proposals = topic_entries(journal, topic, "permission")
    if pending:
        return {**card, "result": "waiting_for_owner", "assignee": "owner", "question": link["question"],
                "choices": [decision_info(journal, p["id"]) for p in pending]}
    if not confirmed:
        return {**card, "result": "blocked", "reason": "The linked decision is missing or was withdrawn."}
    decision = confirmed[0]
    card["decisions"] = [decision_info(journal, decision["id"])]
    selected = {"topic": choice, "id": decision["id"]}
    if not permissions:
        if proposals:
            if any(p["kind"] != "permission" or selected not in dependencies(p) for p in proposals):
                return {**card, "result": "blocked", "reason": "Review the proposed contract and its decision dependency."}
            return {**card, "result": "unconfirmed", "assignee": "owner", "proposals": [p["id"] for p in proposals],
                    "reason": "Review the proposed permission before starting the change."}
        return {**card, "result": "needs_contract", "reason": "Prepare a scoped permission using the confirmed decision."}
    permission = permissions[0]
    deps = dependencies(permission)
    if permission["kind"] != "permission" or selected not in deps:
        return {**card, "result": "blocked", "permission": permission["id"],
                "reason": "The current contract does not depend on the current version of the linked decision."}
    scope = scope_guard.parse(permission.get("scope"))
    if not scope:
        return {**card, "result": "blocked", "permission": permission["id"],
                "reason": "A handoff requires a contract with an exact file scope and report."}
    deliveries = delivery.Deliveries.read(directory / "delivery.json", core)
    step = next_step.describe(project, journal, deliveries, permission)
    card.update(step)
    if step["result"] in ("start", "implement", "verify", "register_evidence"):
        card["assignee"] = link["assignee"]
    if step["result"] in ("awaiting_acceptance", "complete"):
        evidence = deliveries.versions[step["delivery"]]["evidence"]
        card.update(assignee="owner", scope=scope,
                    verification=permission["verification"], authority_limit=permission.get("authority_limit"),
                    evidence={"status": evidence["status"], "result": evidence["result"], "files": evidence["files"],
                              "artifact": {key: evidence["artifact"][key] for key in ("path", "sha256")}})
    card["decisions"] = [decision_info(journal, dep["id"]) for dep in deps]
    return card


def decision_info(journal, identifier):
    record = journal.record(identifier)
    fields = ("topic", "summary", "source", "reason", "scope", "authority_limit", "verification", "questions")
    info = {"id": identifier, **{key: record[key] for key in fields if key in record}}
    if identifier in journal.owner_evidence:
        info["owner_evidence"] = journal.owner_evidence[identifier]
    return info


def run(args, project, directory, core):
    if args.command == "link-task":
        return link_task(args, directory, core)
    before = next_step.store_revision(directory)
    try:
        card = inspect(project, directory, core, args.topic)
    except (ValueError, core.SignumError, OSError, sqlite3.Error, KeyError) as error:
        return {"result": "blocked", "reason": str(error)}
    if before != next_step.store_revision(directory):
        return {"result": "blocked", "reason": "The handoff or journal changed while reading. Retry."}
    return card
