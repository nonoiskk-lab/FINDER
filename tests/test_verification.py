"""The six-point verification gate — the last defence against a bad report."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.classifier import ItClassifier
from gem_intel.analyze.deadline import DeadlineAnalyzer
from gem_intel.analyze.geo import JharkhandGeoFilter
from gem_intel.models import OpportunityScore, Urgency
from gem_intel.validate.rules import Rejection, VerificationGate

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 6, 8, 30, tzinfo=IST)


@pytest.fixture
def gate(settings):
    return VerificationGate(settings)


@pytest.fixture
def analysed(settings):
    def build(**overrides):
        overrides.setdefault("bid_end_at", NOW + timedelta(days=4))
        tender = make_tender(**overrides)
        DeadlineAnalyzer(settings).apply(tender, NOW)
        tender.classification = ItClassifier(settings).classify(tender)
        tender.geo = JharkhandGeoFilter(settings).assess(tender)
        return tender
    return build


def test_a_clean_tender_is_accepted(gate, analysed):
    result = gate.verify([analysed()])
    assert len(result.accepted) == 1
    assert not result.rejected


@pytest.mark.parametrize("url", [
    "https://tenders.example.com/bid/123",
    "https://gem-tenders.co.in/bid/123",
    "https://bidplus.gem.gov.in.evil.com/bid/123",
    "",
])
def test_non_official_sources_are_rejected(gate, analysed, url):
    """The single-source rule. A lookalike host must not slip through."""
    result = gate.verify([analysed(source_url=url)])
    assert not result.accepted
    assert result.rejected == {Rejection.NOT_OFFICIAL_SOURCE.value: 1}


@pytest.mark.parametrize("url", [
    "https://bidplus.gem.gov.in/showbidDocument/1",
    "https://gem.gov.in/bid/1",
    "https://mkp.gem.gov.in/bid/1",
])
def test_official_hosts_and_subdomains_are_accepted(gate, analysed, url):
    assert gate.verify([analysed(source_url=url)]).accepted


def test_expired_tender_is_rejected(gate, analysed):
    result = gate.verify([analysed(bid_end_at=NOW - timedelta(days=2))])
    assert result.rejected == {Rejection.EXPIRED.value: 1}


def test_unknown_closing_date_is_rejected(gate, analysed):
    """We cannot promise a tender is open if we cannot read its deadline."""
    result = gate.verify([analysed(bid_end_at=None)])
    assert result.rejected == {Rejection.NO_CLOSING_DATE.value: 1}


def test_outside_window_without_a_score_is_rejected_not_watchlisted(gate, analysed):
    tender = analysed(bid_end_at=NOW + timedelta(days=25))
    result = gate.verify([tender])
    assert not result.watchlist
    assert result.rejected == {Rejection.OUTSIDE_WINDOW.value: 1}


def test_outside_window_with_a_high_score_goes_to_the_watchlist(gate, analysed):
    tender = analysed(bid_end_at=NOW + timedelta(days=25))
    tender.score = OpportunityScore(total=85.0)
    result = gate.verify([tender])
    assert result.watchlist == [tender]
    assert tender.urgency is Urgency.WATCHLIST
    assert not result.rejected


def test_cancelled_tender_is_rejected(gate, analysed):
    tender = analysed()
    tender.raw_fields["detail.status"] = "Bid Cancelled"
    assert gate.verify([tender]).rejected == {Rejection.NOT_ACTIVE.value: 1}


def test_non_it_and_non_jharkhand_are_rejected(gate, analysed):
    furniture = analysed(title="Supply of Office Furniture",
                         item_description="Office chairs and tables",
                         category="Furniture")
    kerala = analysed(delivery_location="Kochi, Kerala",
                      buyer_address="Kochi, Kerala",
                      buyer_organization="Government of Kerala")
    result = gate.verify([furniture, kerala])
    assert result.rejected[Rejection.NOT_IT_RELATED.value] == 1
    assert result.rejected[Rejection.NOT_JHARKHAND.value] == 1


def test_summary_line_is_human_readable(gate, analysed):
    result = gate.verify([analysed(bid_end_at=NOW - timedelta(days=1))])
    assert "already closed" in result.summary_line()
