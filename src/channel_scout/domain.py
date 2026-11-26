import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
BUSINESS_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9][A-Z0-9_.+-]{0,63}@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)*\.[A-Z]{2,24}(?![\w-])",
    re.I | re.ASCII,
)
BUSINESS_CONTEXT = re.compile(r"\b(business|contact|inquiries|enquiries|sponsor\w*|collab\w*|partnership\w*)\b", re.I)
URL = re.compile(r"https?://[^\s<>\x22]+", re.I)


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 254 or not BUSINESS_EMAIL.fullmatch(normalized) or ".." in normalized:
        raise ValueError("invalid email address")
    return normalized


def parse_keywords(rows: list[list]) -> tuple[list[dict], int]:
    names = [str(field).strip().lower() for field in rows[0]] if rows else []
    if len(set(names)) != len(names) or not {"niche", "keyword", "enabled"} <= set(names):
        raise ValueError("missing or duplicate Keywords headers")
    records = {}
    invalid = 0
    for values in rows[1:]:
        if not any(values):
            continue
        row = dict(zip(names, values))
        enabled = str(row.get("enabled", "")).strip().lower()
        if enabled in {"", "false", "no", "0"}:
            continue
        niche = text(row.get("niche"), 100)
        query = text(row.get("keyword"), 200)
        mode = str(row.get("search mode") or "channel").strip().lower()
        mode = {"channel search": "channel", "video search": "video"}.get(mode, mode)
        if enabled not in {"true", "yes", "1"} or not niche or not query or mode not in {"channel", "video"}:
            invalid += 1
            continue
        target = text(row.get("target format"), 60)
        key = fingerprint(niche.casefold(), query.casefold(), mode)
        records[key] = dict(id=key, niche=niche, query=query, mode=mode, target_format=target)
    return list(records.values()), invalid


def classify(subscribers, hidden: bool, country: str, activity: str, activity_enabled: bool) -> tuple[str, str]:
    if country.upper() in {"IN", "PK", "BD"}:
        return "Excluded", "excluded_country"
    if hidden or subscribers is None:
        return "Needs Review", "subscriber_count_unknown"
    if not 1_000 <= subscribers <= 1_000_000:
        return "Excluded", "subscriber_count_out_of_range"
    if activity_enabled and activity == "Inactive":
        return "Excluded", "no_recent_uploads"
    if activity_enabled and activity == "Unknown":
        return "Needs Review", "upload_history_incomplete"
    return "Qualified", "passed_filters"


def as_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def classify_activity(publications: list[str], complete: bool, now: datetime) -> str:
    cutoff = now - timedelta(days=90)
    count = sum(cutoff <= as_time(value) <= now for value in publications)
    if count >= 4:
        return "Active"
    if not complete:
        return "Unknown"
    return "Slow Uploads" if count else "Inactive"


def business_emails(content: str, trusted_contact_page: bool = False) -> set[str]:
    result = set()
    for match in BUSINESS_EMAIL.finditer(content):
        context = content[max(0, match.start() - 100):match.end() + 50]
        if trusted_contact_page or BUSINESS_CONTEXT.search(context):
            try:
                result.add(normalize_email(match.group()))
            except ValueError:
                pass
    return result


class DraftOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["draft", "abstain"]
    subject: str = Field(max_length=100)
    body: str = Field(max_length=1600)
    evidence_ids: list[str] = Field(max_length=4)


def validate_draft(raw: str, evidence: dict[str, str]) -> DraftOutput:
    result = DraftOutput.model_validate_json(raw)
    if result.status == "abstain":
        if result.subject or result.body or result.evidence_ids:
            raise ValueError("invalid_abstention")
        return result
    if (
        not result.subject.strip() or not result.body.strip() or not result.evidence_ids
        or any(character in result.subject for character in "\r\n\x00")
        or any(ord(c) < 32 and c not in "\r\n\t" for c in result.body)
        or not set(result.evidence_ids) <= evidence.keys()
        or BUSINESS_EMAIL.search(result.subject + " " + result.body)
        or URL.search(result.subject + " " + result.body)
    ):
        raise ValueError("invalid_draft_content")
    return result
