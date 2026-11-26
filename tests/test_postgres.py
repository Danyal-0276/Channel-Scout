import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from channel_scout.config import Settings
from channel_scout.db import Database
from channel_scout.domain import DraftOutput
from channel_scout.integrations import ApiError, LEAD_HEADERS, RUN_HEADERS
from channel_scout.sheets_sync import sync_sheets
from channel_scout.workflows import run_discovery


@pytest.fixture
def postgres():
    if os.environ.get("CHANNELSCOUT_TEST_POSTGRES") != "1":
        pytest.skip("Set CHANNELSCOUT_TEST_POSTGRES=1 to run PostgreSQL integration tests.")
    parameters = Settings(Path.cwd()).postgres(maintenance=True)
    schema = f"scout_test_{uuid4().hex}"
    with psycopg.connect(**parameters, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        config = {**parameters, "options": f"-c search_path={schema}"}
        try:
            with Database(config) as db:
                db.migrate(Path("migrations").glob("*.sql"))
                yield db, config
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def lead(id_):
    return {"id": id_, "metadata": {"title": "Synthetic channel", "subscribers": 2000},
            "selection": "Qualified", "reason": "passed_filters", "match": None}


def add_draft(db, channel, recipient, version):
    output = DraftOutput(status="draft", subject=f"Subject {version}", body="Synthetic draft", evidence_ids=["source-1"])
    draft_id = db.save_draft(channel, recipient, output, version, "fixture", "stub")
    db.draft_result(channel, recipient, "https://example.com", "Verified public contact", "Pending Approval",
                    datetime.now(timezone.utc))
    return draft_id


@pytest.mark.postgres
def test_replay_approvals_and_concurrent_claims(postgres):
    db, settings = postgres
    keyword = {"id": "keyword", "niche": "Business", "query": "podcast", "mode": "channel", "target_format": ""}
    db.keywords([keyword])
    one, two = "UC" + "A"*22, "UC" + "B"*22
    assert db.ingest(keyword, [lead(one), lead(two)]) == {one, two}
    assert db.ingest(keyword, [lead(one)]) == set()
    assert db.execute("SELECT count(*) AS count FROM channel_matches").fetchone()["count"] == 2
    recipient = "test@example.com"
    obsolete = add_draft(db, one, recipient, "old")
    one_id = add_draft(db, one, recipient, "new")
    two_id = add_draft(db, two, recipient, "first")
    first = db.latest_draft(one)
    second = db.latest_draft(two)
    approve = {item["id"]: {"token": item["review_token"], "evidence": "Synthetic opt-in"}
               for item in [first, second]}
    db.import_approvals(approve)
    approved_at = db.execute("SELECT approved_at FROM approvals WHERE draft_id=%s", (one_id,)).fetchone()["approved_at"]
    db.import_approvals(approve)
    assert db.execute("SELECT approved_at FROM approvals WHERE draft_id=%s", (one_id,)).fetchone()["approved_at"] == approved_at
    assert db.eligible(obsolete, 7) is None
    assert db.claim(one_id, 7, 5) is None
    db.enable_sender(True)

    def attempt(draft_id):
        with Database(settings) as worker:
            return worker.claim(draft_id, 7, 5)

    with ThreadPoolExecutor(2) as executor:
        claims = [claim for claim in executor.map(attempt, (one_id, two_id)) if claim]
    assert len(claims) == 1
    winning_id = claims[0][0]["id"]
    db.finish_send(winning_id, "Needs Review", reason="network_error")
    assert db.claim(one_id, 7, 5) is None and db.claim(two_id, 7, 5) is None
    db.resolve_send(winning_id, "confirmed-gmail-id")
    assert db.execute("SELECT status FROM send_attempts").fetchone()["status"] == "Gmail Accepted"
    db.import_approvals({})
    assert db.execute("SELECT count(*) AS count FROM approvals WHERE revoked_at IS NOT NULL").fetchone()["count"] == 2


class SheetFixture:
    def __init__(self):
        self.tabs = {"Keywords": [["Niche", "Keyword", "Enabled"], ["Finance", "Podcast", True]],
                     "Leads": [LEAD_HEADERS], "Run Log": [RUN_HEADERS]}
        self.fail_append = False

    def rows(self, address):
        tab, cells = address.replace("'", "").split("!")
        start = int("".join(x for x in cells.split(":")[0] if x.isdecimal()) or 1)
        if ":" not in cells:
            rows = self.tabs[tab][start-1:start]
            return [row[:1] for row in rows]
        if cells.split(":")[1].endswith("1"):
            return self.tabs[tab][:1]
        return self.tabs[tab][start-1:]

    def put(self, address, values):
        tab, cells = address.replace("'", "").split("!")
        index = int("".join(x for x in cells.split(":")[0] if x.isdecimal())) - 1
        self.tabs[tab][index] = values[0]

    def append(self, address, values):
        tab, _ = address.replace("'", "").split("!")
        self.tabs[tab].extend(values)
        if self.fail_append:
            self.fail_append = False
            raise ApiError("sheets", "network_error")
        return {"updates": {"updatedRange": f"'{tab}'!A{len(self.tabs[tab])}:W{len(self.tabs[tab])}"}}


class YouTubeFixture:
    def search(self, keyword, pages):
        assert pages == 3
        return {"UC" + "C"*22: None}, 1

    def channels(self, ids):
        return [{"id": ids[0], "snippet": {"title": "Finance Talk"}, "statistics": {"subscriberCount": "2000"}}]


@pytest.mark.postgres
def test_end_to_end_discovery_with_ambiguous_sheet_write(postgres):
    db, _ = postgres
    sheet = SheetFixture()
    with db.job("discover") as report:
        run_discovery(db, sheet, YouTubeFixture(), False, 10, report)
    assert report["new_leads"] == 1
    sheet.fail_append = True
    with pytest.raises(ApiError, match="network_error"):
        sync_sheets(db, sheet, 10, {})
    sync_sheets(db, sheet, 10, {})
    assert len(sheet.tabs["Leads"]) == len(sheet.tabs["Run Log"]) == 2
    assert sheet.tabs["Leads"][1][11] == "Qualified"
    with db.job("discover") as report:
        run_discovery(db, sheet, YouTubeFixture(), False, 10, report)
    assert report["raw_hits"] == 0
    assert db.pending_channels(10) == []


@pytest.mark.postgres
def test_quota_bucket_blocks_at_configured_cap(postgres):
    db, _ = postgres
    db.reserve("search", 1)
    with pytest.raises(ApiError, match="daily_quota"):
        db.reserve("search", 1)
    db.reserve("general", 1)
