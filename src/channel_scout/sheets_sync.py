import json

from .domain import lead_priority
from .integrations import ApiError, LEAD_HEADERS, LEAD_LAST, RUN_HEADERS


def row_index(rows):
    index = {}
    for number, row in enumerate(rows, 2):
        if not row:
            continue
        key = str(row[0]).strip()
        if not key or key in index:
            raise ApiError("sheets", "missing_or_duplicate_record_id")
        index[key] = number
    return index


def upsert_row(sheets, tab, end, index, row):
    key = row[0]
    if key not in index:
        response = sheets.append(f"'{tab}'!A:{end}", [row])
        updated_range = response.get("updates", {}).get("updatedRange", "")
        number = updated_range.rsplit("!", 1)[-1].split(":", 1)[0].removeprefix("A")
        if not number.isdecimal():
            raise ApiError("sheets", "append_result_unclear")
        index[key] = int(number)
    else:
        number = index[key]
        current = sheets.rows(f"'{tab}'!A{number}")
        if current != [[key]]:
            raise ApiError("sheets", "row_moved_during_sync")
        sheets.put(f"'{tab}'!A{number}:{end}{number}", [row])
    number = index[key]
    if sheets.rows(f"'{tab}'!A{number}") != [[key]]:
        raise ApiError("sheets", "projection_verification_failed")


def append_leads(sheets, leads):
    if not leads:
        return
    response = sheets.append(f"'Leads'!A:{LEAD_LAST}", [lead_row(lead) for lead in leads])
    updated_range = response.get("updates", {}).get("updatedRange", "")
    first = updated_range.rsplit("!", 1)[-1].split(":", 1)[0].removeprefix("A")
    if not first.isdecimal():
        raise ApiError("sheets", "append_result_unclear")
    last = int(first) + len(leads) - 1
    expected = [[lead["id"]] for lead in leads]
    if sheets.rows(f"'Leads'!A{first}:A{last}") != expected:
        raise ApiError("sheets", "projection_verification_failed")


def lead_row(lead):
    data = lead["metadata"]
    matches = lead["matches"]
    draft_ready = bool(data and lead["draft_status"] == "Pending Approval"
                       and lead["contact_email"] == lead["recipient"])
    draft_id = str(lead["draft_id"]) if draft_ready else ""
    send = {"Gmail Accepted": "Sent"}.get(lead["send_status"], lead["send_status"])
    if not send and draft_ready:
        send = "Pending Approval"
    area_status = set()
    for match in matches:
        criteria = match.get("criteria", {})
        area = ", ".join(value for value in (criteria.get("city"), criteria.get("country")) if value)
        if area:
            area_status.add((area, match.get("location_match", "Unverified")))
    areas = ", ".join(sorted({area for area, _ in area_status}))
    geography = " · ".join(f"{area}: {status}" for area, status in sorted(area_status))
    return [
        lead["id"], data.get("title", ""), f"https://www.youtube.com/channel/{lead['id']}" if data else "",
        ", ".join(sorted({match["niche"] for match in matches})),
        ", ".join(sorted({match["query"] for match in matches})),
        ", ".join(sorted({match["mode"].title() + " search" for match in matches})),
        lead["reviewed_content_format"] or data.get("content_format", ""),
        lead["reviewed_upload_format"] or data.get("upload_format", ""),
        data.get("subscribers") if data.get("subscribers") is not None else "", data.get("country", ""),
        data.get("activity") or ", ".join(date[:10] for date in data.get("publications", [])),
        lead["selection"], lead["selection_reason"], lead["contact_status"], lead["contact_email"] or "",
        lead["contact_source"] or "", lead["draft_status"], send or "",
        lead["subject"] if draft_ready else "", lead["body"] if draft_ready else "",
        draft_id, lead["review_token"] if draft_ready else "",
        lead["sent_at"].isoformat() if send == "Sent" and lead["sent_at"] else "",
        areas, geography or ("Any location" if matches else ""),
        lead_priority(lead["selection"], lead["contact_status"], lead["draft_status"]),
    ]


def sync_sheets(db, sheets, limit, report):
    db.expire_metadata()
    leads = sheets.rows(f"'Leads'!A1:{LEAD_LAST}1")
    runs = sheets.rows("'Run Log'!A1:M1")
    if leads != [LEAD_HEADERS] or runs != [RUN_HEADERS]:
        raise ApiError("sheets", "run_setup_sheets_or_repair_headers")
    lead_index = row_index(sheets.rows(f"'Leads'!A2:{LEAD_LAST}"))
    pending = db.pending_channels(limit)
    new_leads = [lead for lead in pending if lead["id"] not in lead_index and lead["selection"] != "Excluded"]
    existing = [lead for lead in pending if lead["id"] in lead_index]
    append_leads(sheets, new_leads)
    if existing:
        check_index = row_index(sheets.rows("'Leads'!A2:A"))
        if any(check_index.get(lead["id"]) != lead_index[lead["id"]] for lead in existing):
            raise ApiError("sheets", "row_moved_during_sync")
        updates = []
        for lead in existing:
            index = lead_index[lead["id"]]
            updates.append({"range": f"'Leads'!A{index}:{LEAD_LAST}{index}", "values": [lead_row(lead)]})
        sheets.put_batch(updates)
        check_index = row_index(sheets.rows("'Leads'!A2:A"))
        if any(check_index.get(lead["id"]) != lead_index[lead["id"]] for lead in existing):
            raise ApiError("sheets", "projection_verification_failed")
    report["leads_synced"] = len(new_leads) + len(existing)
    for lead in pending:
        db.acknowledge_channel(lead["id"], lead["sheet_revision"])
    run_index = row_index(sheets.rows("'Run Log'!A2:M"))
    for run in db.pending_runs(limit):
        data = run["report"]
        row = [
            str(run["id"]), run["job"], run["started_at"].isoformat(), run["finished_at"].isoformat(), run["status"],
            *[data.get(field, "") for field in ("raw_hits", "unique_channels", "qualified", "needs_review",
                                               "excluded", "new_leads", "verified_contacts")],
            json.dumps(data, ensure_ascii=False)[:4000],
        ]
        upsert_row(sheets, "Run Log", "M", run_index, row)
        db.execute("UPDATE job_runs SET sheet_synced=true WHERE id=%s", (run["id"],))
        report["runs_synced"] = report.get("runs_synced", 0) + 1
