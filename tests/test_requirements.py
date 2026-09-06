"""Required-document and requirement extraction."""

from __future__ import annotations

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.requirements import RequirementsExtractor
from gem_intel.models import Confidence

URL = "https://bidplus.gem.gov.in/showbidDocument/9000001"


@pytest.fixture
def extractor():
    return RequirementsExtractor()


def _canonicals(tender):
    return {d.canonical for d in tender.required_documents}


def test_documents_behind_a_demand_verb_are_captured(extractor):
    tender = make_tender()
    text = ("The bidder shall upload a scanned copy of the PAN Card, GST "
            "Registration Certificate and Udyam Registration. An OEM "
            "Authorization Letter must be submitted with the bid.")
    extractor.extract(tender, text, URL)
    assert {"pan", "gst", "msme", "oem_authorization"} <= _canonicals(tender)


def test_a_bare_mention_is_not_a_requirement(extractor):
    """Rule from the brief: do not assume documents are required."""
    tender = make_tender()
    text = "Payments will be released against a valid GST invoice raised by the vendor."
    extractor.extract(tender, text, URL)
    assert "gst" not in _canonicals(tender)


def test_hedged_requirements_are_marked_unclear(extractor):
    tender = make_tender()
    text = "Bidders shall submit an ISO Certificate, if applicable."
    extractor.extract(tender, text, URL)
    iso = next(d for d in tender.required_documents if d.canonical == "iso")
    assert iso.confidence is Confidence.UNCLEAR
    assert "Verification Required" in iso.display


def test_a_firm_statement_upgrades_a_hedged_one(extractor):
    tender = make_tender()
    text = ("Bidders shall submit an ISO Certificate, if applicable. "
            "The ISO 9001 certificate must be uploaded with the technical bid.")
    extractor.extract(tender, text, URL)
    iso = next(d for d in tender.required_documents if d.canonical == "iso")
    assert iso.confidence is Confidence.CONFIRMED


def test_every_document_carries_evidence(extractor):
    tender = make_tender()
    extractor.extract(tender, "The bidder shall upload the PAN Card copy.", URL)
    for document in tender.required_documents:
        assert document.evidence and document.evidence[0].quote


def test_eligibility_and_technical_lines_are_collected(extractor):
    tender = make_tender()
    text = ("Eligibility criteria: the bidder should have a minimum average annual "
            "turnover of Rs. 50,00,000 in the last three financial years. "
            "Technical specification: Intel Core i5 processor, 16 GB RAM, 512 GB SSD, "
            "3 years onsite warranty.")
    extractor.extract(tender, text, URL)
    assert tender.eligibility_requirements
    assert tender.technical_requirements
    assert any("warrant" in line.lower() for line in tender.warranty_requirements)


def test_no_text_leaves_an_explicit_flag(extractor):
    tender = make_tender()
    extractor.extract(tender, "", URL)
    assert not tender.required_documents
    assert any("Required-documents list is unavailable" in f
               for f in tender.verification_flags)


def test_silent_document_section_is_flagged_not_assumed(extractor):
    tender = make_tender()
    extractor.extract(tender, "Delivery shall be completed within 30 days.", URL)
    assert not tender.required_documents
    assert any("No specific document requirements" in f
               for f in tender.verification_flags)
