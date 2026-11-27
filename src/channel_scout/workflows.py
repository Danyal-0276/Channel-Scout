import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .domain import (
    BUSINESS_EMAIL,
    URL,
    business_emails,
    classify,
    classify_activity,
    fingerprint,
    parse_keywords,
    text,
    validate_draft,
)
from .integrations import (
    ApiError,
    SYSTEM_PROMPT,
    public_contact_urls,
    read_contact_page,
)


class DiscoveryState(TypedDict, total=False):
    keyword: dict
    pages: int
    matches: dict
    raw_hits: int
    candidates: list[dict]
    records: list[dict]
    new_ids: set[str]


def discovery_graph(db, youtube, activity_enabled, now):
    def search(state):
        matches, hits = youtube.search(state["keyword"], state["pages"])
        return {"matches": matches, "raw_hits": hits}

    def metadata(state):
        return {"candidates": youtube.channels(list(state["matches"]))}

    def filter_channels(state):
        records = []
        for item in state["candidates"]:
            channel_id = item["id"]
            if channel_id not in state["matches"]:
                continue
            snippet = item.get("snippet", {})
            statistics = item.get("statistics", {})
            count = statistics.get("subscriberCount")
            subscribers = int(count) if str(count).isdecimal() else None
            country = text(snippet.get("country"), 2).upper()
            selection, reason = classify(
                subscribers,
                statistics.get("hiddenSubscriberCount", False),
                country,
                "",
                False,
            )
            playlist = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            activity = "Unknown"
            publishes = []
            if selection == "Qualified" and playlist:
                try:
                    publishes, complete = youtube.uploads(playlist)
                    if activity_enabled:
                        activity = classify_activity(publishes, complete, now)
                except ApiError as error:
                    if error.code in {
                        "daily_quota",
                        "job_deadline",
                    } or error.status in {401, 403}:
                        raise
                except ValueError:
                    publishes = []
            if selection == "Qualified" and activity_enabled:
                selection, reason = classify(
                    subscribers, False, country, activity, True
                )
            details = {
                "title": text(snippet.get("title"), 150),
                "description": str(snippet.get("description", ""))[:5000],
                "country": country,
                "subscribers": subscribers,
                "activity": activity if activity_enabled else "",
                "publications": publishes[:4],
                "content_format": "Unknown",
                "upload_format": "Unknown",
            }
            records.append(
                dict(
                    id=channel_id,
                    metadata=details,
                    selection=selection,
                    reason=reason,
                    match=state["matches"][channel_id],
                )
            )
        return {"records": records}

    def persist(state):
        return {"new_ids": db.ingest(state["keyword"], state["records"])}

    graph = StateGraph(DiscoveryState)
    for name, node in (
        ("search", search),
        ("metadata", metadata),
        ("filter", filter_channels),
        ("persist", persist),
    ):
        graph.add_node(name, node)
    graph.add_edge(START, "search")
    graph.add_edge("search", "metadata")
    graph.add_edge("metadata", "filter")
    graph.add_edge("filter", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def run_discovery(db, sheets, youtube, activity_enabled, limit, report):
    now = datetime.now(timezone.utc)
    keywords, invalid = parse_keywords(sheets.rows("'Keywords'!A1:E"))
    due = db.keywords(keywords)
    modes = Counter(
        (row["niche"].casefold(), row["query"].casefold()) for row in keywords
    )
    graph = discovery_graph(db, youtube, activity_enabled, now)
    totals = {}
    raw = deferred = errors = 0
    new = set()
    details = []
    for keyword in due[:limit]:
        pair = (keyword["niche"].casefold(), keyword["query"].casefold())
        pages = 3 if modes[pair] == 1 else 2 if keyword["mode"] == "channel" else 1
        try:
            result = graph.invoke({"keyword": keyword, "pages": pages})
        except ApiError as error:
            if error.code in {"daily_quota", "job_deadline"}:
                deferred += 1
                break
            if error.status in {400, 401, 403} and not error.retryable:
                raise
            errors += 1
            delay = max(
                error.delay, min(3600, 15 * 60 * 2 ** min(keyword["failures"], 3))
            )
            db.schedule_keyword(keyword["id"], now + timedelta(seconds=delay), False)
            details.append({"keyword_id": keyword["id"], "error": error.code})
            continue
        db.schedule_keyword(keyword["id"], now + timedelta(days=1), True)
        raw += result["raw_hits"]
        new.update(result["new_ids"])
        results = Counter(row["selection"] for row in result["records"])
        for row in result["records"]:
            totals[row["id"]] = row["selection"]
        details.append(
            {
                "niche": keyword["niche"],
                "keyword": keyword["query"],
                "mode": keyword["mode"],
                "raw_hits": result["raw_hits"],
                "unique_candidates": len(result["matches"]),
                "not_available": len(result["matches"]) - len(result["records"]),
                "reasons": dict(Counter(row["reason"] for row in result["records"])),
                **results,
            }
        )
    result = Counter(totals.values())
    report.update(
        raw_hits=raw,
        unique_channels=len(totals),
        qualified=result["Qualified"],
        needs_review=result["Needs Review"],
        excluded=result["Excluded"],
        new_leads=len(new),
        already_known=max(0, result["Qualified"] - len(new)),
        errors=errors + invalid,
        deferred=deferred + max(0, len(due) - limit),
        keywords=details,
    )


class DraftState(TypedDict, total=False):
    lead: dict
    email: str
    source: str
    contact_status: str
    evidence: dict[str, str]
    draft_key: str
    status: str
    output: dict
    provider: str
    model: str
    delay_hours: float
    deferred: bool


def evidence_packet(description):
    clean = BUSINESS_EMAIL.sub(
        "[address removed]", URL.sub("[link removed]", description)
    )
    result = {}
    for snippet in re.split(r"[\n]+", clean):
        snippet = text(snippet, 320)
        if len(snippet.split()) >= 6:
            result[f"source-{len(result) + 1}"] = snippet
        if len(result) >= 4:
            break
    return result


def drafting_graph(db, primary, fallback, offer, hosts, now, fetch=read_contact_page):
    def contact(state):
        lead = state["lead"]
        description = lead["metadata"].get("description", "")
        channel_url = f"https://www.youtube.com/channel/{lead['id']}"
        found = {email: channel_url for email in business_emails(description)}
        contact_lookup_failed = False
        for url in public_contact_urls(description, hosts) if not found else []:
            try:
                for email in fetch(url):
                    found[email] = url
            except ApiError:
                contact_lookup_failed = True
        if len(found) != 1:
            label = (
                "Conflicting contacts"
                if found
                else (
                    "Contact lookup failed"
                    if contact_lookup_failed
                    else "No public email"
                )
            )
            return {
                "contact_status": label,
                "status": "Needs Review",
                "delay_hours": 1 if contact_lookup_failed else 168,
                "deferred": contact_lookup_failed,
            }
        email, source = next(iter(found.items()))
        evidence = evidence_packet(description)
        previous = db.latest_draft(lead["id"])
        key = fingerprint(
            lead["id"],
            email,
            offer,
            evidence,
            SYSTEM_PROMPT,
            primary.provider,
            primary.model,
            fallback.model if fallback else "",
        )
        reviewed_key = fingerprint(lead["id"], email, evidence) + ":"
        unchanged = previous and (
            previous["fingerprint"] == key
            or (
                previous["provider"] == "human"
                and previous["fingerprint"].startswith(reviewed_key)
            )
        )
        status = (
            "Needs Review" if not evidence else "Pending Approval" if unchanged else ""
        )
        return {
            "email": email,
            "source": source,
            "contact_status": "Verified public contact",
            "evidence": evidence,
            "draft_key": key,
            "status": status,
            "delay_hours": 24,
        }

    def generate(state):
        model = primary
        channel = text(
            BUSINESS_EMAIL.sub(
                "[address removed]",
                URL.sub("[link removed]", state["lead"]["metadata"].get("title", "")),
            ),
            150,
        )
        tries = 0
        while tries < 2:
            tries += 1
            try:
                raw = model.draft(offer, channel, state["evidence"])
            except ApiError as error:
                if not error.retryable:
                    raise
                if tries == 1 and fallback:
                    model = fallback
                    continue
                return {
                    "status": "Draft deferred",
                    "deferred": True,
                    "delay_hours": max(1, error.delay / 3600),
                }
            try:
                output = validate_draft(raw, state["evidence"])
            except ValidationError:
                if tries == 1:
                    continue
                return {"status": "Needs Review"}
            except ValueError:
                return {"status": "Needs Review"}
            if output.status == "abstain":
                return {"status": "Needs Review"}
            return {
                "output": output.model_dump(),
                "provider": model.provider,
                "model": model.model,
                "status": "Pending Approval",
            }
        return {"status": "Needs Review"}

    def persist(state):
        from .domain import DraftOutput

        channel_id = state["lead"]["id"]
        with db.conn.transaction():
            if "output" in state:
                db.save_draft(
                    channel_id,
                    state["email"],
                    DraftOutput.model_validate(state["output"]),
                    state["draft_key"],
                    state["provider"],
                    state["model"],
                )
            db.draft_result(
                channel_id,
                state.get("email"),
                state.get("source"),
                state["contact_status"],
                state["status"],
                now + timedelta(hours=state.get("delay_hours", 24)),
            )
        return {}

    graph = StateGraph(DraftState)
    graph.add_node("contact", contact)
    graph.add_node("generate", generate)
    graph.add_node("persist", persist)
    graph.add_edge(START, "contact")
    graph.add_conditional_edges(
        "contact",
        lambda state: "persist" if state["status"] else "generate",
        {"persist": "persist", "generate": "generate"},
    )
    graph.add_edge("generate", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def run_drafting(db, primary, fallback, offer, approved_hosts, limit, report):
    graph = drafting_graph(
        db, primary, fallback, offer, approved_hosts, datetime.now(timezone.utc)
    )
    for lead in db.drafting_queue(limit):
        result = graph.invoke({"lead": lead})
        for name, matches in (
            (
                "verified_contacts",
                result["contact_status"] == "Verified public contact",
            ),
            ("drafts_created", "output" in result),
            ("needs_review", result["status"] == "Needs Review"),
            ("deferred", result.get("deferred", False)),
        ):
            report[name] = report.get(name, 0) + int(matches)
