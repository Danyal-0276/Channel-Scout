import base64
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from channel_scout.domain import classify, classify_activity, parse_keywords, validate_draft
from channel_scout.integrations import ApiError, Gmail, LEAD_HEADERS, Model, Sheets, YouTube, public_contact_urls
from channel_scout.outreach import decisions, run_sender
from channel_scout.sheets_sync import row_index, upsert_row
from channel_scout.workflows import discovery_graph, drafting_graph


CHANNEL = "UC" + "A" * 22
NOW = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
DESCRIPTION = "We interview founders about real estate lending and property management.\nBusiness inquiries: booking@example.com"
OFFER = "Audio editing for podcasts"
DRAFT = json.dumps({"status": "draft", "subject": "Audio editing for your founder interviews",
                    "body": "Your interviews cover real estate lending. Would an editing sample help?",
                    "evidence_ids": ["source-1"]})


@pytest.mark.parametrize("count,hidden,country,expected", [
    (999, False, "", "Excluded"), (1000, False, "", "Qualified"),
    (1000000, False, "US", "Qualified"), (1000001, False, "US", "Excluded"),
    (2000, False, "IN", "Excluded"), (2000, False, "PK", "Excluded"),
    (2000, False, "BD", "Excluded"), (2000, True, "US", "Needs Review"),
    (None, False, "", "Needs Review"), (2000, False, "GB", "Qualified"),
])
def test_channel_rules(count, hidden, country, expected):
    assert classify(count, hidden, country, "Unknown", False)[0] == expected


@pytest.mark.parametrize("count,complete,expected", [
    (0, True, "Inactive"), (1, True, "Slow Uploads"), (3, True, "Slow Uploads"),
    (4, False, "Active"), (3, False, "Unknown"), (0, False, "Unknown"),
])
def test_activity_and_cutoff(count, complete, expected):
    matched = [(NOW - timedelta(days=90, seconds=-i)).isoformat() for i in range(count)]
    old = (NOW - timedelta(days=90, seconds=1)).isoformat()
    assert classify_activity([*matched, old], complete, NOW) == expected


def test_keywords_are_distinct_by_kind_and_duplicate_rows_do_not_repeat():
    keywords, invalid = parse_keywords([
        ["Niche", "Keyword", "Enabled", "Search Mode"],
        ["Finance", "Money Podcast", True, "Video"],
        ["Finance", "Money Podcast", "TRUE", "Video search"],
        ["Finance", "Money Podcast", "TRUE", "Channel search"],
        ["Finance", "Money Podcast", False],
        ["", "bad", True],
    ])
    assert {item["mode"] for item in keywords} == {"channel", "video"}
    assert len(keywords) == 2 and invalid == 1


def test_discovery_graph_keeps_match_source_and_honors_incomplete_upload_history():
    item = {"id": CHANNEL, "snippet": {"title": "Talk Show", "description": DESCRIPTION},
            "statistics": {"subscriberCount": "9000"},
            "contentDetails": {"relatedPlaylists": {"uploads": "playlist"}}}
    db = SimpleNamespace(ingest=lambda keyword, rows: {row["id"] for row in rows})
    api = SimpleNamespace(search=lambda keyword, pages: ({CHANNEL: "video-id"}, 2),
                          channels=lambda ids: [item], uploads=lambda _: ([], False))
    result = discovery_graph(db, api, True, NOW).invoke({"keyword": {"mode": "video"}, "pages": 1})
    assert result["raw_hits"] == 2
    assert result["records"][0]["selection"] == "Needs Review"
    assert result["records"][0]["match"] == "video-id"


def test_youtube_search_keeps_one_candidate_per_channel():
    def reply(request):
        params = request.url.params
        if params.get("pageToken"):
            return httpx.Response(200, json={"items": [
                {"id": {"videoId": "other"}, "snippet": {"channelId": CHANNEL}}]})
        return httpx.Response(200, json={"nextPageToken": "next", "items": [
            {"id": {"videoId": "first"}, "snippet": {"channelId": CHANNEL}}]})
    charges = []
    with httpx.Client(transport=httpx.MockTransport(reply)) as client:
        result = YouTube(client, "synthetic-key", charges.append).search({"mode": "video", "query": "Podcast"}, 3)
    assert result == ({CHANNEL: "first"}, 2)
    assert charges == ["search", "search"]


@pytest.mark.parametrize("value", [
    "https://localhost/about", "https://contact.example.com.evil.invalid/", "https://contact.example.com:8443/",
    "http://contact.example.com/", "https://contact.example.com@evil.invalid/",
    "https://contact.example.com/#fragment", "https://contact.example.com/?data=x",
])
def test_contact_url_policy(value):
    assert public_contact_urls(value, {"contact.example.com"}) == []


@pytest.mark.parametrize("key,value", [
    ("subject", "Subject\r\nBcc: x@example.com"), ("subject", ""), ("body", ""),
    ("body", "Visit https://example.com"), ("body", "Reply to a@example.com"), ("body", "\u0000"),
    ("evidence_ids", ["made-up"]), ("evidence_ids", []), ("recipient", "a@example.com"),
    ("status", "send"), ("status", "abstain"), ("evidence_ids", [1]), ("subject", "x" * 101),
])
def test_adversarial_model_outputs(key, value):
    payload = json.loads(DRAFT)
    payload[key] = value
    with pytest.raises(ValueError):
        validate_draft(json.dumps(payload), {"source-1": "synthetic evidence"})


class FixtureModel:
    def __init__(self, provider, output):
        self.provider, self.model, self.output = provider, "fixture", output
        self.calls = []

    def draft(self, offer, channel, evidence):
        self.calls.append((offer, channel, evidence))
        if isinstance(self.output, Exception):
            raise self.output
        return self.output


def test_drafting_fallback_validates_and_does_not_pass_recipient():
    persisted = []
    database = SimpleNamespace(conn=SimpleNamespace(transaction=nullcontext), latest_draft=lambda _: None,
                               save_draft=lambda *fields: persisted.append(fields), draft_result=lambda *_: None)
    primary = FixtureModel("groq", ApiError("groq", "rate_limited", True))
    fallback = FixtureModel("ollama", DRAFT)
    result = drafting_graph(database, primary, fallback, OFFER, set(), NOW).invoke(
        {"lead": {"id": CHANNEL, "metadata": {"title": "Talk Show", "description": DESCRIPTION}}})
    assert result["status"] == "Pending Approval" and result["provider"] == "ollama"
    assert len(primary.calls) == len(fallback.calls) == len(persisted) == 1
    assert "booking@example.com" not in str(primary.calls)


def test_invalid_content_does_not_trigger_fallback():
    db = SimpleNamespace(conn=SimpleNamespace(transaction=nullcontext), latest_draft=lambda _: None,
                         draft_result=lambda *_: None)
    primary = FixtureModel("groq", DRAFT.replace("source-1", "invented-1"))
    fallback = FixtureModel("ollama", DRAFT)
    graph = drafting_graph(db, primary, fallback, OFFER, set(), NOW)
    assert graph.invoke({"lead": {"id": CHANNEL, "metadata": {"description": DESCRIPTION}}})["status"] == "Needs Review"
    assert len(primary.calls) == 1 and fallback.calls == []


def test_model_adapter_requests_strict_schema_without_tools():
    def handle(request):
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert "tools" not in body
        return httpx.Response(200, json={"choices": [{"message": {"content": DRAFT}}]})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert Model(client, "groq", "fixture", "key").draft(OFFER, "title", {"source-1": "facts"}) == DRAFT


def test_protection_is_required_before_approval_reads():
    sheets = Sheets(None, "test-id", None)
    sheets.metadata = lambda: {"sheets": [{"properties": {"title": "Approvals", "sheetId": 7},
        "protectedRanges": [{"range": {"sheetId": 7}, "requestingUserCanEdit": True}]}]}
    with pytest.raises(ApiError, match="approval_tab_not_protected"):
        sheets.verified_approvals()


@pytest.mark.parametrize("range_info,exceptions,allowed", [
    ({"sheetId": 7}, [], True),
    ({"sheetId": 7, "startRowIndex": 0, "startColumnIndex": 0}, [], True),
    ({"sheetId": 7, "endRowIndex": 1000}, [], False),
    ({"sheetId": 7}, [{"sheetId": 7, "startRowIndex": 1, "endRowIndex": 2}], False),
])
def test_approval_protection_covers_future_rows(range_info, exceptions, allowed):
    sheets = Sheets(None, "test-id", None)
    sheets.metadata = lambda: {"sheets": [{"properties": {"title": "Approvals", "sheetId": 7},
        "protectedRanges": [{"range": range_info, "requestingUserCanEdit": False, "unprotectedRanges": exceptions}]}]}
    sheets.rows = lambda *_args, **_kwargs: [["Draft ID", "Review Token", "Decision", "Eligibility Evidence"]]
    if allowed:
        assert sheets.verified_approvals() == []
    else:
        with pytest.raises(ApiError, match="approval_tab_not_protected"):
            sheets.verified_approvals()


def test_sheets_service_account_response_omits_false_edit_permission():
    sheets = Sheets(None, "test-id", None)
    sheets.metadata = lambda: {"sheets": [{"properties": {"title": "Approvals", "sheetId": 7},
        "protectedRanges": [{"range": {"sheetId": 7}}]}]}
    sheets.rows = lambda *_args, **_kwargs: [["Draft ID", "Review Token", "Decision", "Eligibility Evidence"]]
    assert sheets.verified_approvals() == []


def test_revocation_conflict_and_formula_approvals_are_blocked():
    identifiers = [uuid4() for _ in range(3)]
    one, two, three = map(str, identifiers)
    rows = [[one, "token", "Approve", "Creator opt-in"], [one, "token", "Revoke"],
            [two, "=TOKEN()", "Approve", "Creator opt-in"], [three, "token", "Approve", "Creator opt-in"]]
    assert set(decisions(rows)) == {identifiers[2]}


@pytest.mark.parametrize("live,error,expected", [
    (False, None, None), (True, None, "Gmail Accepted"),
    (True, ApiError("gmail", "network_error", True), "Needs Review"),
    (True, ApiError("gmail", "http_500", True, status=500), "Needs Review"),
    (True, ApiError("gmail", "http_429", status=429), "Rejected"),
])
def test_sender_is_one_submission_or_dry_run(live, error, expected):
    draft_id = uuid4()
    draft = {"id": draft_id, "channel_id": CHANNEL, "recipient": "booking@example.com",
             "subject": "Hello", "body": "Body", "review_token": "opaque-token"}
    approval = [str(draft_id), "opaque-token", "Approve", "Creator opted in"]
    by_header = {"Channel ID": CHANNEL, "Selection Status": "Qualified", "Contact Email": draft["recipient"],
                 "Subject": "Hello", "Draft Body": "Body", "Draft ID": str(draft_id), "Review Token": "opaque-token"}
    rows = [LEAD_HEADERS, [by_header.get(header, "") for header in LEAD_HEADERS]]
    sheets = SimpleNamespace(verified_approvals=lambda: [approval], rows=lambda _: rows)
    attempts = []
    submissions = []
    database = SimpleNamespace(expire_claims=lambda: 0, import_approvals=lambda _: None,
                               eligible=lambda *args: draft, claim=lambda *args: (draft, "<test@example.com>"),
                               finish_send=lambda _, status, **kwargs: attempts.append(status))

    def submit(*args):
        submissions.append(args)
        if error:
            raise error
        return "gmail-id"

    gmail = SimpleNamespace(token=SimpleNamespace(headers=lambda: {}), submit=submit)
    run_sender(database, sheets, gmail, live, 5, 7, {})
    assert attempts == ([expected] if expected else [])
    assert len(submissions) == int(live)


def test_sync_refuses_a_moved_or_duplicated_row():
    with pytest.raises(ApiError, match="duplicate"):
        row_index([["row"], ["row"]])
    sheets = SimpleNamespace(rows=lambda _: [["different-id"]])
    with pytest.raises(ApiError, match="row_moved"):
        upsert_row(sheets, "Leads", "W", {"row": 2}, ["row", "payload"])


def test_gmail_submission_is_bound_to_configured_sender_and_approved_mime():
    def handle(request):
        assert "/users/sender%40example.com/messages/send" in str(request.url)
        raw = base64.urlsafe_b64decode(json.loads(request.content)["raw"])
        mail = BytesParser(policy=policy.default).parsebytes(raw)
        assert str(mail["To"]) == "receiver@example.com"
        assert str(mail["From"]) == "sender@example.com"
        assert str(mail["Subject"]) == "Reviewed subject"
        assert mail.get_content().strip() == "Reviewed body"
        assert mail["Cc"] is None and mail["Bcc"] is None
        return httpx.Response(200, json={"id": "fixture-id"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        gateway = Gmail(client, None, "sender@example.com")
        draft = {"recipient": "receiver@example.com", "subject": "Reviewed subject", "body": "Reviewed body"}
        assert gateway.submit(draft, "<stable@example.com>", {}) == "fixture-id"
