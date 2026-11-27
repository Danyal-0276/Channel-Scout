import csv
import secrets
from uuid import UUID

from .domain import normalize_email
from .integrations import ApiError, LEAD_HEADERS, LEAD_LAST
from .sheets_sync import lead_row


REVIEW_COLUMNS = (
    "Priority", "Channel", "Channel URL", "Target Area", "Location Match", "Contact Email",
    "Contact Source URL", "Subject", "Draft Body", "Draft ID", "Review Decision", "Notes",
)


def outreach_review_rows(db):
    for lead in db.pending_channels(None, export=True):
        record = dict(zip(LEAD_HEADERS, lead_row(lead)))
        if record["Outreach Status"] == "Pending Approval":
            yield {field: record.get(field, "") for field in REVIEW_COLUMNS}


def export_review_csv(db, path):
    rows = list(outreach_review_rows(db))
    with path.open("x", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=REVIEW_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: "'" + value if value.lstrip()[:1] in {"=", "+", "-", "@"} else value
                             for field, value in row.items()})
    return len(rows)


def decisions(rows):
    approved = {}
    blocked = set()
    for row in rows:
        if not any(row):
            continue
        try:
            draft_id = UUID(str(row[0]))
        except (ValueError, TypeError, IndexError):
            raise ApiError("sheets", "invalid_approval_row") from None
        fields = [str(field).strip() for field in row[1:]]
        if len(fields) != 3 or any(field.startswith("=") for field in fields):
            blocked.add(draft_id)
            continue
        token, decision, evidence = fields
        value = {"token": token, "evidence": evidence}
        if (decision != "Approve" or not token or not 5 <= len(evidence) <= 1000
                or (draft_id in approved and approved[draft_id] != value)):
            blocked.add(draft_id)
        else:
            approved[draft_id] = value
    return {key: value for key, value in approved.items() if key not in blocked}


def reviewed_snapshot(sheets, draft, decision):
    rows = sheets.rows(f"'Leads'!A1:{LEAD_LAST}")
    if not rows or rows[0] != LEAD_HEADERS:
        raise ApiError("sheets", "unexpected_headers")
    matches = [row for row in rows[1:] if row and row[0] == draft["channel_id"]]
    if len(matches) != 1:
        return False
    row = dict(zip(LEAD_HEADERS, matches[0]))
    try:
        valid_recipient = normalize_email(draft["recipient"]) == draft["recipient"]
    except ValueError:
        return False
    return (
        valid_recipient and row.get("Selection Status") == "Qualified"
        and row.get("Contact Email") == draft["recipient"]
        and row.get("Subject") == draft["subject"] and row.get("Draft Body") == draft["body"]
        and row.get("Draft ID") == str(draft["id"]) and row.get("Review Token") == draft["review_token"]
        and secrets.compare_digest(decision["token"].encode(), draft["review_token"].encode())
    )


def run_sender(db, sheets, gmail, live, cap, days, report):
    report["mode"] = "live" if live else "dry_run"
    report["stale_claims"] = db.expire_claims()
    candidates = decisions(sheets.verified_approvals())
    db.import_approvals(candidates)
    report["approved_rows"] = len(candidates)
    actions = 0
    for draft_id, decision in candidates.items():
        if actions >= cap:
            break
        draft = db.eligible(draft_id, days)
        if not draft or not reviewed_snapshot(sheets, draft, decision):
            report["blocked"] = report.get("blocked", 0) + 1
            continue
        report["eligible"] = report.get("eligible", 0) + 1
        if not live:
            actions += 1
            continue
        # Resolve OAuth before claiming so failure to authenticate can't consume a send slot.
        headers = gmail.token.headers()
        current = decisions(sheets.verified_approvals())
        db.import_approvals(current)
        if current.get(draft_id) != decision or not reviewed_snapshot(sheets, draft, decision):
            report["blocked"] = report.get("blocked", 0) + 1
            continue
        claim = db.claim(draft_id, days, cap)
        if not claim:
            report["blocked"] = report.get("blocked", 0) + 1
            continue
        actions += 1
        approved_draft, message_id = claim
        try:
            gmail_id = gmail.submit(approved_draft, message_id, headers)
        except ApiError as error:
            definitive = error.status in {400, 401, 403, 404, 429}
            db.finish_send(draft_id, "Rejected" if definitive else "Needs Review", reason=error.code)
            metric = "rejected" if definitive else "uncertain"
            report[metric] = report.get(metric, 0) + 1
            # Pause this run on auth, quota, transient provider, or ambiguous failures.
            if error.status != 400:
                report["errors"] = report.get("errors", 0) + 1
                break
            continue
        if not gmail_id:
            db.finish_send(draft_id, "Needs Review", reason="missing_gmail_id")
            report["uncertain"] = report.get("uncertain", 0) + 1
            report["errors"] = report.get("errors", 0) + 1
            break
        db.finish_send(draft_id, "Gmail Accepted", gmail_id=gmail_id)
        report["accepted"] = report.get("accepted", 0) + 1
