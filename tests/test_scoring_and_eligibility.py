"""Opportunity scoring, ranking and the eligibility vocabulary."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.classifier import ItClassifier
from gem_intel.analyze.deadline import DeadlineAnalyzer
from gem_intel.analyze.eligibility import (
    VERDICT_GAP,
    VERDICT_LIKELY,
    VERDICT_POTENTIAL,
    VERDICT_VERIFY,
    EligibilityAnalyzer,
)
from gem_intel.analyze.geo import JharkhandGeoFilter
from gem_intel.analyze.profile import CompanyProfile, load_profile
from gem_intel.analyze.scoring import OpportunityScorer, rank_tenders
from gem_intel.models import (
    Confidence,
    MatchLevel,
    MoneyClaim,
    RequiredDocument,
    TernaryFlag,
    Urgency,
)

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 6, 8, 30, tzinfo=IST)
ALLOWED_VERDICTS = {VERDICT_LIKELY, VERDICT_POTENTIAL, VERDICT_VERIFY, VERDICT_GAP}


@pytest.fixture
def profile(settings):
    return load_profile(settings.config_dir / "company_profile.yaml")


@pytest.fixture
def prepared(settings, profile):
    """A fully analysed laptop tender in Ranchi closing in 4 days."""
    def build(**overrides):
        overrides.setdefault("bid_end_at", NOW + timedelta(days=4))
        tender = make_tender(**overrides)
        DeadlineAnalyzer(settings).apply(tender, NOW)
        tender.classification = ItClassifier(settings).classify(tender)
        tender.geo = JharkhandGeoFilter(settings).assess(tender)
        tender.eligibility = EligibilityAnalyzer(profile).assess(tender)
        tender.score = OpportunityScorer(settings, profile).score(tender)
        return tender
    return build


def test_score_is_bounded_and_banded(prepared):
    tender = prepared()
    assert 0 <= tender.score.total <= 100
    assert tender.score.band_label
    assert tender.score.rank_emoji


def test_every_component_explains_itself(prepared):
    tender = prepared()
    assert len(tender.score.components) == 7
    for component in tender.score.components:
        assert component.reason, component.name
        assert 0 <= component.points <= component.max_points


def test_component_weights_match_the_brief(settings, profile):
    scorer = OpportunityScorer(settings, profile)
    expected = {"Product Match": 20, "Location Match": 15, "Business Capability": 20,
                "Document Complexity": 10, "Deadline Urgency": 10,
                "Commercial Potential": 15, "Procurement Complexity": 10}
    tender = make_tender()
    tender.classification = ItClassifier(settings).classify(tender)
    tender.geo = JharkhandGeoFilter(settings).assess(tender)
    got = {c.name: c.max_points for c in scorer.score(tender).components}
    assert got == expected
    assert sum(expected.values()) == 100


def test_a_non_jharkhand_tender_loses_the_location_points(settings, profile, prepared):
    local = prepared()
    remote = prepared(delivery_location="Kochi, Kerala",
                      buyer_address="Kochi, Kerala 682001",
                      buyer_organization="Government of Kerala")
    assert remote.score.total < local.score.total


def test_unpublished_value_scores_neutrally_not_zero(prepared):
    """GeM often omits the value; inventing one — or zeroing it — would mislead."""
    tender = prepared()
    component = next(c for c in tender.score.components
                     if c.name == "Commercial Potential")
    assert 0 < component.points < component.max_points
    assert "not published" in component.reason.lower()


def test_a_published_value_in_range_scores_full(settings, profile, prepared):
    tender = prepared()
    tender.estimated_value = MoneyClaim(value=3_150_000.0,
                                        confidence=Confidence.CONFIRMED)
    rescored = OpportunityScorer(settings, profile).score(tender)
    component = next(c for c in rescored.components if c.name == "Commercial Potential")
    assert component.points == component.max_points


def test_tight_deadline_reduces_the_preparation_score(settings, profile):
    scorer = OpportunityScorer(settings, profile)
    analyzer = DeadlineAnalyzer(settings)
    scores = {}
    for days in (1, 8):
        tender = make_tender(bid_end_at=NOW + timedelta(days=days))
        analyzer.apply(tender, NOW)
        tender.classification = ItClassifier(settings).classify(tender)
        tender.geo = JharkhandGeoFilter(settings).assess(tender)
        scores[days] = next(c.points for c in scorer.score(tender).components
                            if c.name == "Deadline Urgency")
    assert scores[1] < scores[8]


def test_ranking_prefers_score_then_urgency(settings, profile, prepared):
    high = prepared()
    high.score.total = 88
    high.urgency = Urgency.ACTION_REQUIRED
    high.days_remaining = 8
    low = prepared()
    low.score.total = 60
    low.urgency = Urgency.URGENT
    low.days_remaining = 1
    tie = prepared()
    tie.score.total = 88
    tie.urgency = Urgency.URGENT
    tie.days_remaining = 1
    ordered = rank_tenders([low, high, tie])
    assert ordered[0] is tie      # same score, closes sooner
    assert ordered[1] is high
    assert ordered[2] is low


# -- eligibility --------------------------------------------------------
def test_verdict_vocabulary_never_promises_eligibility(prepared):
    tender = prepared()
    assert tender.eligibility.verdict in ALLOWED_VERDICTS
    blob = " ".join([tender.eligibility.verdict] +
                    tender.eligibility.supporting_points).lower()
    assert "is eligible" not in blob
    assert "guaranteed" not in blob


def test_unconfigured_profile_yields_requires_verification():
    analyzer = EligibilityAnalyzer(CompanyProfile())
    assessment = analyzer.assess(make_tender())
    assert assessment.match_level is MatchLevel.UNKNOWN
    assert assessment.verdict == VERDICT_VERIFY
    assert any("company_profile.yaml" in point
               for point in assessment.verification_points)


def test_missing_oem_authorization_is_a_gap(profile):
    tender = make_tender()
    tender.required_documents = [
        RequiredDocument(name="OEM Authorization Letter", canonical="oem_authorization")
    ]
    assessment = EligibilityAnalyzer(profile).assess(tender)
    assert any("OEM" in gap for gap in assessment.gaps)


def test_turnover_shortfall_is_a_gap():
    profile = CompanyProfile(strong=["Computer Hardware"], annual_turnover_inr=2_000_000)
    tender = make_tender()
    tender.eligibility_requirements = [
        "The bidder should have a minimum average annual turnover of Rs. 5 Crore."
    ]
    assessment = EligibilityAnalyzer(profile).assess(tender)
    assert any("Turnover requirement" in gap for gap in assessment.gaps)


def test_unclear_emd_becomes_a_verification_point(profile):
    tender = make_tender()
    tender.emd.status = TernaryFlag.NOT_STATED
    assessment = EligibilityAnalyzer(profile).assess(tender)
    assert any("EMD" in point for point in assessment.verification_points)
