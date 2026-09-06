"""Google Search discovery adapter and CompositeSource merging.

The load-bearing guarantee tested here: Google's ranking is never trusted as
a source-verification signal. A result that scores well but does not point
at an official GeM host must be dropped exactly as if it had never appeared.
"""

from __future__ import annotations

import pytest

from gem_intel.http_client import GemHttpClient
from gem_intel.models import Tender
from gem_intel.sources.base import CompositeSource, SourceAdapter, SourceResult
from gem_intel.sources.google_search import GoogleSearchGemSource


class FakeGoogleResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or "{}"

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        return self._payload


def item(link, title="", snippet=""):
    return {"link": link, "title": title, "snippet": snippet}


@pytest.fixture
def enabled_settings(settings, monkeypatch):
    settings.raw["source"]["google_search"]["enabled"] = True
    settings.raw["source"]["google_search"]["site_filters"] = ["site:bidplus.gem.gov.in"]
    settings.raw["source"]["google_search"]["location_hints"] = ["Jharkhand"]
    settings.raw["source"]["min_delay_seconds"] = 0
    settings.raw["source"]["max_delay_seconds"] = 0
    monkeypatch.setenv("GOOGLE_SEARCH_API_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_SEARCH_CSE_ID", "test-cse")
    return settings


@pytest.fixture
def client(settings):
    return GemHttpClient(settings)


def test_disabled_by_default_returns_empty(settings, client):
    adapter = GoogleSearchGemSource(settings, client)
    result = adapter.search(["Laptop"])
    assert result.tenders == []
    assert not adapter.enabled


def test_missing_credentials_records_an_issue_not_a_crash(settings, client, monkeypatch):
    settings.raw["source"]["google_search"]["enabled"] = True
    monkeypatch.delenv("GOOGLE_SEARCH_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_SEARCH_CSE_ID", raising=False)
    adapter = GoogleSearchGemSource(settings, client)
    result = adapter.search(["Laptop"])
    assert result.tenders == []
    assert result.access_issues[0].error_type == "GOOGLE_SEARCH_NOT_CONFIGURED"


def test_official_result_becomes_a_tender(enabled_settings, client, monkeypatch):
    adapter = GoogleSearchGemSource(enabled_settings, client)
    monkeypatch.setattr(adapter, "_call_google", lambda q: [
        item("https://bidplus.gem.gov.in/showbidDocument/8800001",
             title="GEM/2026/B/8800001 - Supply of Laptop",
             snippet="Supply of 30 laptops for Ranchi office, Jharkhand"),
    ])
    result = adapter.search(["Laptop"])
    assert len(result.tenders) == 1
    tender = result.tenders[0]
    assert tender.bid_number == "GEM/2026/B/8800001"
    assert tender.source_url.startswith("https://bidplus.gem.gov.in/")
    assert tender.raw_fields["discovered_via"] == adapter.name


def test_non_official_result_is_dropped_even_if_it_ranks_well(enabled_settings, client,
                                                               monkeypatch):
    """The core compliance guarantee of this adapter."""
    adapter = GoogleSearchGemSource(enabled_settings, client)
    monkeypatch.setattr(adapter, "_call_google", lambda q: [
        item("https://gem-tenders-aggregator.example.com/bid/8800001",
             title="GEM/2026/B/8800001 mirrored here", snippet="..."),
        item("https://bidplus.gem.gov.in.evil.com/bid/1", title="fake", snippet="..."),
    ])
    result = adapter.search(["Laptop"])
    assert result.tenders == []


def test_bid_number_extracted_from_title_or_snippet_or_url(enabled_settings, client,
                                                            monkeypatch):
    adapter = GoogleSearchGemSource(enabled_settings, client)
    monkeypatch.setattr(adapter, "_call_google", lambda q: [
        item("https://bidplus.gem.gov.in/showbidDocument/1", snippet="Bid GEM/2026/B/1234567"),
    ])
    result = adapter.search(["Laptop"])
    assert result.tenders[0].bid_number == "GEM/2026/B/1234567"


def test_result_without_a_bid_number_still_becomes_a_candidate(enabled_settings, client,
                                                                monkeypatch):
    """No bid number yet is fine — fetch_detail fills it in from the real page."""
    adapter = GoogleSearchGemSource(enabled_settings, client)
    monkeypatch.setattr(adapter, "_call_google", lambda q: [
        item("https://bidplus.gem.gov.in/showbidDocument/99", title="Supply of Printers"),
    ])
    result = adapter.search(["Printer"])
    assert len(result.tenders) == 1
    assert result.tenders[0].bid_number == ""


def test_duplicate_results_across_queries_are_merged(enabled_settings, client, monkeypatch):
    adapter = GoogleSearchGemSource(enabled_settings, client)
    monkeypatch.setattr(adapter, "_call_google", lambda q: [
        item("https://bidplus.gem.gov.in/showbidDocument/1", title="GEM/2026/B/1111111"),
    ])
    result = adapter.search(["Laptop", "Desktop"])
    assert len(result.tenders) == 1
    assert len(result.tenders[0].raw_fields["matched_queries"]) >= 1


def test_daily_query_budget_is_enforced(enabled_settings, client, monkeypatch):
    enabled_settings.raw["source"]["google_search"]["daily_query_budget"] = 1
    adapter = GoogleSearchGemSource(enabled_settings, client)
    calls = []

    def fake_call(q):
        calls.append(q)
        return []

    monkeypatch.setattr(adapter, "_call_google", fake_call)
    adapter.search(["Laptop", "Desktop", "Printer"])
    assert len(calls) == 1
    assert any(i.error_type == "GOOGLE_SEARCH_BUDGET_EXHAUSTED" for i in client.access_issues)


def test_quota_exceeded_is_recorded_not_raised(enabled_settings, client, monkeypatch):
    adapter = GoogleSearchGemSource(enabled_settings, client)

    def fake_get(*args, **kwargs):
        return FakeGoogleResponse(status_code=429)

    monkeypatch.setattr("gem_intel.sources.google_search.requests.get", fake_get)
    result = adapter.search(["Laptop"])
    assert result.tenders == []
    assert result.access_issues[0].error_type == "GOOGLE_SEARCH_QUOTA_EXCEEDED"


def test_network_failure_is_recorded_not_raised(enabled_settings, client, monkeypatch):
    import requests

    adapter = GoogleSearchGemSource(enabled_settings, client)

    def fake_get(*args, **kwargs):
        raise requests.ConnectionError("dns failure")

    monkeypatch.setattr("gem_intel.sources.google_search.requests.get", fake_get)
    result = adapter.search(["Laptop"])
    assert result.tenders == []
    assert result.access_issues[0].error_type == "GOOGLE_SEARCH_NETWORK_FAILURE"


def test_real_google_call_shape(enabled_settings, client, monkeypatch):
    """Confirm the request is built the way the Custom Search API expects."""
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return FakeGoogleResponse(payload={"items": []})

    monkeypatch.setattr("gem_intel.sources.google_search.requests.get", fake_get)
    adapter = GoogleSearchGemSource(enabled_settings, client)
    adapter.search(["Laptop"])
    assert captured["url"] == "https://www.googleapis.com/customsearch/v1"
    assert captured["params"]["key"] == "test-key"
    assert captured["params"]["cx"] == "test-cse"
    assert "site:bidplus.gem.gov.in" in captured["params"]["q"]
    assert "Jharkhand" in captured["params"]["q"]


# -- CompositeSource ------------------------------------------------------
class StubAdapter(SourceAdapter):
    def __init__(self, name, tenders):
        self.name = name
        self._tenders = tenders
        self.fetch_calls = []

    def search(self, queries):
        return SourceResult(tenders=list(self._tenders), listings_seen=len(self._tenders))

    def fetch_detail(self, tender):
        self.fetch_calls.append(tender.identity_key)
        return tender


def test_composite_merges_distinct_tenders():
    a = StubAdapter("A", [Tender(bid_number="GEM/2026/B/1", source_url="https://gem.gov.in/1")])
    b = StubAdapter("B", [Tender(bid_number="GEM/2026/B/2", source_url="https://gem.gov.in/2")])
    composite = CompositeSource([a, b])
    result = composite.search(["x"])
    assert {t.bid_number for t in result.tenders} == {"GEM/2026/B/1", "GEM/2026/B/2"}


def test_composite_deduplicates_the_same_bid_found_twice():
    shared_url = "https://gem.gov.in/showbidDocument/9"
    a = StubAdapter("A", [Tender(bid_number="GEM/2026/B/9", source_url=shared_url)])
    b = StubAdapter("B", [Tender(bid_number="GEM/2026/B/9", source_url=shared_url)])
    composite = CompositeSource([a, b])
    result = composite.search(["x"])
    assert len(result.tenders) == 1


def test_composite_routes_fetch_detail_to_the_discovering_adapter():
    only_in_b = Tender(bid_number="GEM/2026/B/3", source_url="https://gem.gov.in/3")
    a = StubAdapter("A", [])
    b = StubAdapter("B", [only_in_b])
    composite = CompositeSource([a, b])
    composite.search(["x"])
    composite.fetch_detail(only_in_b)
    assert only_in_b.identity_key in b.fetch_calls
    assert only_in_b.identity_key not in a.fetch_calls


def test_composite_requires_at_least_one_adapter():
    with pytest.raises(ValueError):
        CompositeSource([])


def test_composite_propagates_an_abort_reason():
    aborted = StubAdapter("A", [])
    aborted.search = lambda queries: SourceResult(aborted=True, abort_reason="CAPTCHA")
    ok = StubAdapter("B", [])
    result = CompositeSource([aborted, ok]).search(["x"])
    assert result.aborted
    assert "CAPTCHA" in result.abort_reason


# -- build_source factory --------------------------------------------------
def test_build_source_portal_mode_returns_gem_bidplus(settings, client):
    from gem_intel.sources import build_source
    from gem_intel.sources.gem_bidplus import GemBidPlusSource

    assert isinstance(build_source(settings, client, "portal"), GemBidPlusSource)


def test_build_source_google_search_mode(settings, client):
    from gem_intel.sources import build_source

    assert isinstance(build_source(settings, client, "google_search"), GoogleSearchGemSource)


def test_build_source_both_mode_is_composite(settings, client):
    from gem_intel.sources import build_source
    from gem_intel.sources.gem_bidplus import GemBidPlusSource

    source = build_source(settings, client, "both")
    assert isinstance(source, CompositeSource)
    kinds = {type(a) for a in source.adapters}
    assert kinds == {GemBidPlusSource, GoogleSearchGemSource}


def test_build_source_rejects_unknown_mode(settings, client):
    from gem_intel.sources import build_source

    with pytest.raises(ValueError):
        build_source(settings, client, "nonsense")
