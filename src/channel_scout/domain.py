import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal
from unicodedata import normalize

import pycountry
from pydantic import BaseModel, ConfigDict, Field


CHANNEL_ID = re.compile(r"UC[A-Za-z0-9_-]{22}\Z")
BUSINESS_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9][A-Z0-9_.+-]{0,63}@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)*\.[A-Z]{2,24}(?![\w-])",
    re.I | re.ASCII,
)
BUSINESS_CONTEXT = re.compile(r"\b(business|contact|inquiries|enquiries|sponsor\w*|collab\w*|partnership\w*)\b", re.I)
URL = re.compile(r"https?://[^\s<>\x22]+", re.I)
DEFAULT_EXCLUSIONS = ("BD", "IN", "PK")
COUNTRY_ALIASES = {"uk": "GB", "uae": "AE", "usa": "US", "south korea": "KR", "north korea": "KP"}


def fingerprint(*values) -> str:
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def normalize_email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 254 or not BUSINESS_EMAIL.fullmatch(normalized) or ".." in normalized:
        raise ValueError("invalid email address")
    return normalized


def country_code(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.casefold() in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[value.casefold()]
    try:
        return pycountry.countries.lookup(value).alpha_2
    except LookupError:
        raise ValueError("unrecognized country") from None


def subscriber_bound(value, default):
    if value is None or str(value).strip() == "":
        return default
    if isinstance(value, bool):
        raise ValueError("subscriber bound must be a whole number")
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    number = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[0-9]{1,12}", number):
        raise ValueError("subscriber bound must be a nonnegative whole number")
    return int(number)


def read_criteria(row: dict) -> dict:
    country = country_code(str(row.get("country") or ""))
    city = text(row.get("city / town"), 100)
    mode = str(row.get("location policy") or "review uncertain").strip().lower()
    if mode not in {"review uncertain", "confirmed only"}:
        raise ValueError("invalid location policy")
    low = subscriber_bound(row.get("min subscribers"), 1_000)
    high = subscriber_bound(row.get("max subscribers"), 1_000_000)
    if low > high:
        raise ValueError("minimum exceeds maximum")
    raw = str(row.get("excluded countries") or "").strip()
    if not raw:
        excluded = sorted(set(DEFAULT_EXCLUSIONS) - {country})
    elif raw.casefold() == "none":
        excluded = []
    else:
        excluded = sorted({country_code(entry) for entry in raw.split(",") if entry.strip()})
        if not excluded or country in excluded:
            raise ValueError("invalid or contradictory country exclusions")
    options = {}
    for name, value, default in (
        ("country", country, ""), ("city", city, ""), ("location_policy", mode, "review uncertain"),
        ("min_subscribers", low, 1_000), ("max_subscribers", high, 1_000_000),
        ("excluded_countries", excluded, list(DEFAULT_EXCLUSIONS)),
    ):
        if value != default:
            options[name] = value
    return options


def place_text(value: str) -> str:
    return " ".join(normalize("NFKC", value).casefold().replace("’", "'").split())


def search_topic(query: str, criteria: dict) -> str:
    country = pycountry.countries.get(alpha_2=criteria.get("country", ""))
    parts = [query]
    for place in (criteria.get("city", ""), country.name if country else ""):
        if place and not re.search(rf"(?<!\w){re.escape(place_text(place))}(?!\w)", place_text(" ".join(parts))):
            parts.append(place)
    return " ".join(parts)


def location_result(country: str, description: str, criteria: dict) -> tuple[str, str]:
    target, city = criteria.get("country"), criteria.get("city")
    if not target and not city:
        return "Any location", ""
    if target and country and country != target:
        return "Outside target", "country_mismatch"
    if target and country == target and not city:
        return "Country matches", "channel_country_matches"
    explicit = re.compile(r"^(?:(?:we are|we're|i am|i'm|our (?:team|channel|studio) is)\s+)?"
                          r"(?:based|located|headquartered)\s+in\s+(.+)$")
    claims = []
    for sentence in re.split(r"[.!?;\n|—]+", description):
        wording = place_text(sentence)
        match = explicit.match(wording)
        if match and not re.search(r"\b(?:until|formerly|previously)\b", wording):
            claims.append(match.group(1))
    if target and not country:
        name = re.escape(place_text(pycountry.countries.get(alpha_2=target).name))
        if not city:
            if any(re.search(rf"(?:^|,\s*){name}$", value) for value in claims):
                return "Country stated", "explicit_country_statement"
            return "Unverified", "country_not_confirmed"
        place = re.escape(place_text(city))
        combined = rf"{place}(?:\s+|,\s*|,\s*[^,]{{1,50}},\s*){name}"
        if any(re.fullmatch(combined, value) for value in claims):
            return "City stated", "explicit_city_country_statement"
        return "Unverified", "country_not_confirmed"
    if city and any(re.match(rf"{re.escape(place_text(city))}(?!\w)", value) for value in claims):
        return "City stated", "explicit_city_statement"
    return "Unverified", "city_not_confirmed"


def lead_priority(selection: str, contact: str, draft: str) -> str:
    if selection == "Excluded":
        return "Excluded"
    if selection == "Needs Review":
        return "Review"
    if contact == "Verified public contact" and draft == "Pending Approval":
        return "High"
    return "Standard"


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
        try:
            criteria = read_criteria(row)
        except ValueError:
            invalid += 1
            continue
        target = text(row.get("target format"), 60)
        identity = (niche.casefold(), query.casefold(), mode)
        key = fingerprint(*identity, criteria) if criteria else fingerprint(*identity)
        records[key] = dict(id=key, niche=niche, query=query, mode=mode, target_format=target, criteria=criteria)
    return list(records.values()), invalid


def classify(subscribers, hidden: bool, country: str, activity: str, activity_enabled: bool, criteria=None) -> tuple[str, str]:
    criteria = criteria or {}
    if country.upper() in criteria.get("excluded_countries", DEFAULT_EXCLUSIONS):
        return "Excluded", "excluded_country"
    if hidden or subscribers is None:
        return "Needs Review", "subscriber_count_unknown"
    if not criteria.get("min_subscribers", 1_000) <= subscribers <= criteria.get("max_subscribers", 1_000_000):
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
    copy = place_text(result.subject + " " + result.body)
    if (
        not result.subject.strip() or not result.body.strip() or not result.evidence_ids
        or any(character in result.subject for character in "\r\n\x00")
        or any(ord(c) < 32 and c not in "\r\n\t" for c in result.body)
        or not set(result.evidence_ids) <= evidence.keys()
        or re.search(r"\bsource-\d+\b", copy)
        or re.search(r"\bi(?:'ve| have)?\s+(?:recently\s+)?(?:watched|listened|followed|subscribed|enjoyed)\b", copy)
        or BUSINESS_EMAIL.search(result.subject + " " + result.body)
        or URL.search(result.subject + " " + result.body)
    ):
        raise ValueError("invalid_draft_content")
    return result
