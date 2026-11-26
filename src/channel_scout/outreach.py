import secrets
from uuid import UUID

from .domain import normalize_email
from .integrations import ApiError, LEAD_HEADERS


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
    rows = sheets.rows("'Leads'!A1:W")
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
    for draft_id, decision in list(candidates.items())[:cap]:
        draft = db.eligible(draft_id, days)
        if not draft or not reviewed_snapshot(sheets, draft, decision):
            report["blocked"] = report.get("blocked", 0) + 1
            continue
        report["eligible"] = report.get("eligible", 0) + 1
        if not live:
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
