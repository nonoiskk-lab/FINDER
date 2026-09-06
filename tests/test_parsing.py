"""GeM HTML parsing, including the drift guard."""

from __future__ import annotations

import pytest
from tests.conftest import make_tender

from gem_intel.extract.gem_html import (
    GemDetailParser,
    GemListingParser,
    ParserDrift,
    Selectors,
    classify_document,
)
from gem_intel.models import TernaryFlag
from gem_intel.sources.gem_bidplus import render_relative_dates


@pytest.fixture
def selectors(settings):
    return Selectors.load(settings.config_dir / "selectors.yaml")


@pytest.fixture
def listing_parser(selectors, settings):
    return GemListingParser(selectors, "https://bidplus.gem.gov.in", settings.timezone)


@pytest.fixture
def detail_parser(selectors, settings):
    return GemDetailParser(selectors, "https://bidplus.gem.gov.in", settings.timezone)


@pytest.fixture
def listing_html(fixtures_dir):
    return render_relative_dates((fixtures_dir / "listing1.html").read_text())


def test_listing_cards_are_parsed(listing_parser, listing_html):
    tenders = listing_parser.parse(listing_html, "https://bidplus.gem.gov.in/all-bids")
    assert len(tenders) == 7
    first = tenders[0]
    assert first.bid_number == "GEM/2026/B/7100011"
    assert first.source_url.startswith("https://bidplus.gem.gov.in/")
    assert first.quantity == 45
    assert first.bid_end_at is not None
    assert "Ranchi" in first.buyer_address


def test_drift_is_raised_not_silently_empty(listing_parser):
    """A markup change must never look like 'no tenders today'."""
    html = ("<html><body><div class='newcard'>"
            "Bid Number: GEM/2026/B/1234567 Bid End Date: 10-09-2026"
            "</div></body></html>")
    with pytest.raises(ParserDrift):
        listing_parser.parse(html, "https://bidplus.gem.gov.in/all-bids")


def test_a_genuinely_empty_page_returns_empty(listing_parser):
    html = "<html><body><p>No results found for your search.</p></body></html>"
    assert listing_parser.parse(html, "https://bidplus.gem.gov.in/all-bids") == []


def test_detail_fields_are_mapped(detail_parser, fixtures_dir, settings):
    tender = make_tender(bid_number="GEM/2026/B/7100011")
    html = render_relative_dates(
        (fixtures_dir / "detail_GEM_2026_B_7100011.html").read_text())
    detail_parser.parse(tender, html, "https://bidplus.gem.gov.in/showbidDocument/7100011")

    assert tender.ministry == "Government of Jharkhand"
    assert tender.buyer_organization == "Directorate of Higher Education, Jharkhand"
    assert tender.emd.status is TernaryFlag.REQUIRED
    assert tender.emd.amount.value == 75000.0
    assert tender.security.percentage.value == 3.0
    assert tender.security.epbg is TernaryFlag.REQUIRED
    assert tender.estimated_value.value == 3_150_000.0
    assert tender.msme_exemption_portal_flag is TernaryFlag.REQUIRED
    assert len(tender.documents) == 2


def test_zero_epbg_is_not_required_not_unknown(detail_parser, fixtures_dir):
    tender = make_tender(bid_number="GEM/2026/B/7100012")
    html = render_relative_dates(
        (fixtures_dir / "detail_GEM_2026_B_7100012.html").read_text())
    detail_parser.parse(tender, html, "https://bidplus.gem.gov.in/showbidDocument/7100012")
    assert tender.emd.status is TernaryFlag.NOT_REQUIRED
    assert tender.security.epbg is TernaryFlag.NOT_REQUIRED


def test_portal_evidence_is_recorded(detail_parser, fixtures_dir):
    tender = make_tender(bid_number="GEM/2026/B/7100011")
    html = render_relative_dates(
        (fixtures_dir / "detail_GEM_2026_B_7100011.html").read_text())
    url = "https://bidplus.gem.gov.in/showbidDocument/7100011"
    detail_parser.parse(tender, html, url)
    assert tender.emd.amount.evidence[0].source_url == url


def test_detail_quantity_does_not_erase_the_listing_unit(detail_parser, fixtures_dir):
    tender = make_tender(bid_number="GEM/2026/B/7100012",
                         quantity=1.0, quantity_unit="Contract")
    html = render_relative_dates(
        (fixtures_dir / "detail_GEM_2026_B_7100012.html").read_text())
    detail_parser.parse(tender, html, "https://bidplus.gem.gov.in/x")
    assert tender.quantity_unit == "Contract"


@pytest.mark.parametrize("name,expected", [
    ("Buyer Added Bid Specific ATC", "atc"),
    ("Corrigendum 1", "corrigendum"),
    ("Technical Specification", "technical_spec"),
    ("Scope of Work", "scope_of_work"),
    ("Bid Document", "bid_document"),
    ("random.bin", "unknown"),
])
def test_document_classification(name, expected):
    assert classify_document(name) == expected


def test_relative_date_tokens_expand():
    from datetime import date

    out = render_relative_dates("{{+4d 15:00}}", date(2026, 9, 6))
    assert out == "10-09-2026 15:00:00"
