"""Jharkhand relevance."""

from __future__ import annotations

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.geo import JharkhandGeoFilter
from gem_intel.models import Confidence


@pytest.fixture
def geo(settings):
    return JharkhandGeoFilter(settings)


def test_delivery_location_in_jharkhand_is_confirmed(geo):
    tender = make_tender(delivery_location="Nepal House, Doranda, Ranchi, Jharkhand")
    result = geo.assess(tender)
    assert result.is_jharkhand
    assert result.confidence is Confidence.CONFIRMED
    assert "Ranchi" in result.districts


def test_buyer_hq_elsewhere_does_not_disqualify_jharkhand_delivery(geo):
    """The brief's key rule: the work location decides, not the head office."""
    tender = make_tender(
        buyer_organization="Ministry of Coal, Shastri Bhawan, New Delhi",
        buyer_address="Shastri Bhawan, New Delhi 110001",
        delivery_location="Koyla Bhawan, Dhanbad, Jharkhand 826005",
    )
    result = geo.assess(tender)
    assert result.is_jharkhand
    assert result.confidence is Confidence.CONFIRMED
    assert "delivery_location" in result.matched_fields


def test_other_state_is_rejected(geo):
    tender = make_tender(
        delivery_location="Thiruvananthapuram, Kerala 695001",
        buyer_address="Directorate of Technical Education, Kerala",
        buyer_organization="Government of Kerala",
        title="Supply of Desktop Computer",
    )
    result = geo.assess(tender)
    assert not result.is_jharkhand
    assert "kerala" in result.reason.lower()


def test_pan_india_is_kept_but_flagged_unclear(geo):
    tender = make_tender(
        delivery_location="Multiple states across India",
        buyer_address="New Delhi",
        buyer_organization="National Informatics Centre",
        title="Supply of Laptop across India",
    )
    result = geo.assess(tender)
    assert result.is_jharkhand          # kept for review
    assert result.pan_india
    assert result.confidence is Confidence.UNCLEAR


def test_ambiguous_place_name_alone_is_not_enough(geo):
    """'Mango' is a Jamshedpur locality and also a fruit — needs corroboration."""
    tender = make_tender(
        delivery_location="Mango Market Complex",
        buyer_address="Mango Market Complex",
        buyer_organization="Municipal Corporation",
        title="Supply of Computer",
    )
    result = geo.assess(tender)
    assert not result.is_jharkhand


def test_ambiguous_place_with_district_is_accepted(geo):
    tender = make_tender(
        delivery_location="Mango, East Singhbhum, Jharkhand",
        buyer_address="Mango, East Singhbhum",
    )
    result = geo.assess(tender)
    assert result.is_jharkhand
    assert result.confidence is Confidence.CONFIRMED


def test_local_org_hint_alone_is_unclear_not_confirmed(geo):
    tender = make_tender(
        delivery_location="", buyer_address="",
        buyer_organization="Bharat Coking Coal Limited",
        title="Supply of Printer",
    )
    result = geo.assess(tender)
    assert result.is_jharkhand
    assert result.confidence is Confidence.UNCLEAR
    assert "confirm" in result.reason.lower()


def test_jharkhand_only_in_document_text_is_inferred(geo):
    tender = make_tender(delivery_location="", buyer_address="",
                         buyer_organization="Central Procurement Cell", title="Laptops")
    result = geo.assess(tender, document_text="Delivery shall be made at Hazaribagh.")
    assert result.is_jharkhand
    assert result.confidence is Confidence.INFERRED
