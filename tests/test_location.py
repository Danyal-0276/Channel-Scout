from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from channel_scout.domain import (
    classify, fingerprint, lead_priority, location_result, parse_keywords, search_topic,
)
from channel_scout.integrations import YouTube
from channel_scout.workflows import discovery_graph


HEADERS = [
    "Niche", "Keyword", "Enabled", "Country", "City / Town", "Location Policy",
    "Min Subscribers", "Max Subscribers", "Excluded Countries",
]
CHANNEL = "UC" + "A" * 22


def test_old_keyword_keeps_id_and_new_areas_and_filters_are_distinct():
    records, invalid = parse_keywords([
        HEADERS,
        ["Property", "Property podcast", True],
        ["Property", "Property podcast", True, "Canada", "Toronto", "", 500, "2,000,000", "none"],
        ["Property", "Property podcast", True, "CA", "Ottawa"],
    ])
    assert len(records) == 3 and invalid == 0
    assert records[0]["id"] == fingerprint("property", "property podcast", "channel")
    assert records[1]["criteria"] == {"country": "CA", "city": "Toronto", "min_subscribers": 500,
                                      "max_subscribers": 2_000_000, "excluded_countries": []}


def test_explicit_country_can_replace_a_default_exclusion():
    records, invalid = parse_keywords([
        HEADERS, ["Video", "Podcast", True, "Pakistan"], ["Video", "Podcast", True, "PK", "", "", "", "", "PK"],
        ["Video", "Podcast", True, "Atlantis"], ["Video", "Podcast", True, "", "", "", 1000, 50],
    ])
    assert invalid == 3 and len(records) == 1
    assert classify(2000, False, "PK", "", False, records[0]["criteria"])[0] == "Qualified"
    assert classify(500, False, "US", "", False, {"min_subscribers": 400})[0] == "Qualified"
    assert classify(None, True, "US", "", False, {"min_subscribers": 0})[0] == "Needs Review"


@pytest.mark.parametrize(("country", "description", "expected"), [
    ("US", "We are based in Toronto.", "Outside target"),
    ("", "We are based in Toronto.", "Unverified"),
    ("CA", "We’re based in Toronto. We interview local founders.", "City stated"),
    ("CA", "Our studio is located in Toronto.", "City stated"),
    ("CA", "We make Toronto real estate videos.", "Unverified"),
    ("CA", "We are not based in Toronto.", "Unverified"),
    ("CA", "Based in Torontoville.", "Unverified"),
    ("CA", "Based in Toronto until 2020.", "Unverified"),
    ("CA", "We used to be based in Toronto.", "Unverified"),
])
def test_city_and_country_require_location_evidence(country, description, expected):
    assert location_result(country, description, {"country": "CA", "city": "Toronto"})[0] == expected


def test_country_only_and_city_only_are_both_supported():
    assert location_result("GB", "", {"country": "GB"})[0] == "Country matches"
    assert location_result("", "Based in Cambridge, Massachusetts.", {"city": "Cambridge, Massachusetts"})[0] == "City stated"
    assert search_topic("Toronto housing podcast", {"country": "CA", "city": "Toronto"}) == "Toronto housing podcast Canada"


def test_geo_search_uses_query_text_without_video_region_or_fake_channel_geofence():
    def respond(request):
        assert request.url.params["q"] == "real estate podcast Toronto Canada"
        assert request.url.params["type"] == "channel"
        assert not {"regionCode", "location", "locationRadius"} & request.url.params.keys()
        return httpx.Response(200, json={"items": []})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert YouTube(client, "fixture", lambda *_: None).search(
            {"query": "real estate podcast", "mode": "channel", "criteria": {"country": "CA", "city": "Toronto"}}, 3
        ) == ({}, 0)


@pytest.mark.parametrize(("policy", "expected"), [("review uncertain", "Needs Review"), ("confirmed only", "Excluded")])
def test_discovery_does_not_qualify_city_mentions(policy, expected):
    api = SimpleNamespace(search=lambda *_: ({CHANNEL: None}, 1), channels=lambda *_: [{
        "id": CHANNEL, "snippet": {"country": "CA", "description": "We cover Toronto property sales."},
        "statistics": {"subscriberCount": "6400"},
    }])
    db = SimpleNamespace(ingest=lambda *_: set())
    graph = discovery_graph(db, api, False, datetime.now(timezone.utc))
    result = graph.invoke({"keyword": {"criteria": {"country": "CA", "city": "Toronto", "location_policy": policy}},
                           "pages": 1})
    assert result["records"][0]["selection"] == expected
    assert result["records"][0]["reason"] == "city_not_confirmed"


@pytest.mark.parametrize(("selection", "contact", "draft", "expected"), [
    ("Qualified", "Verified public contact", "Pending Approval", "High"),
    ("Qualified", "No public email", "Needs Review", "Standard"),
    ("Needs Review", "Verified public contact", "Pending Approval", "Review"),
    ("Excluded", "Verified public contact", "Pending Approval", "Excluded"),
])
def test_priority_explains_workflow_readiness(selection, contact, draft, expected):
    assert lead_priority(selection, contact, draft) == expected
