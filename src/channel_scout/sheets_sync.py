import json

from .integrations import ApiError, LEAD_HEADERS, RUN_HEADERS


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


def lead_row(lead):
    data = lead["metadata"]
    matches = lead["matches"]
    draft_ready = bool(data and lead["draft_status"] == "Pending Approval"
                       and lead["contact_email"] == lead["recipient"])
    draft_id = str(lead["draft_id"]) if draft_ready else ""
    send = {"Gmail Accepted": "Sent"}.get(lead["send_status"], lead["send_status"])
    if not send and draft_ready:
        send = "Pending Approval"
    return [
        lead["id"], data.get("title", ""), f"https://www.youtube.com/channel/{lead['id']}" if data else "",
        ", ".join(sorted({match["niche"] for match in matches})),
        ", ".join(sorted({match["query"] for match in matches})),
        ", ".join(sorted({match["mode"].title() + " search" for match in matches})),
        data.get("content_format", ""), data.get("upload_format", ""),
        data.get("subscribers") if data.get("subscribers") is not None else "", data.get("country", ""),
        data.get("activity") or ", ".join(date[:10] for date in data.get("publications", [])),
        lead["selection"], lead["selection_reason"], lead["contact_status"], lead["contact_email"] or "",
        lead["contact_source"] or "", lead["draft_status"], send or "",
        lead["subject"] if draft_ready else "", lead["body"] if draft_ready else "",
        draft_id, lead["review_token"] if draft_ready else "",
        lead["sent_at"].isoformat() if send == "Sent" and lead["sent_at"] else "",
    ]


def sync_sheets(db, sheets, limit, report):
    db.expire_metadata()
    leads = sheets.rows("'Leads'!A1:W1")
    runs = sheets.rows("'Run Log'!A1:M1")
    if leads != [LEAD_HEADERS] or runs != [RUN_HEADERS]:
        raise ApiError("sheets", "run_setup_sheets_or_repair_headers")
    lead_index = row_index(sheets.rows("'Leads'!A2:W"))
    for lead in db.pending_channels(limit):
        # Persist an exclusion in the Sheet only if the channel already had a lead row.
        if lead["selection"] != "Excluded" or lead["id"] in lead_index:
            upsert_row(sheets, "Leads", "W", lead_index, lead_row(lead))
            report["leads_synced"] = report.get("leads_synced", 0) + 1
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
