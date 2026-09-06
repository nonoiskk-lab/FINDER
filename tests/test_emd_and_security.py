"""EMD and performance-security extraction.

The through-line of every test here: the system may not guess. Silence in the
source must produce NOT_STATED, which the report renders as a warning.
"""

from __future__ import annotations

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.emd import EmdAnalyzer
from gem_intel.analyze.security import SecurityAnalyzer, render_security_line
from gem_intel.models import Confidence, MoneyClaim, TernaryFlag

URL = "https://bidplus.gem.gov.in/showbidDocument/9000001"


@pytest.fixture
def emd():
    return EmdAnalyzer()


@pytest.fixture
def security():
    return SecurityAnalyzer()


# -- EMD ---------------------------------------------------------------
def test_emd_amount_and_status_from_document(emd):
    tender = make_tender()
    text = "The bidder shall deposit EMD of Rs. 75,000 through online payment."
    result = emd.analyze(tender, text, URL)
    assert result.status is TernaryFlag.REQUIRED
    assert result.amount.value == 75000.0
    assert result.amount.evidence[0].quote


def test_explicit_nil_emd_is_not_required(emd):
    tender = make_tender()
    result = emd.analyze(tender, "EMD: NIL. No earnest money is required.", URL)
    assert result.status is TernaryFlag.NOT_REQUIRED
    assert result.headline() == "✅ NO EMD REQUIRED"


def test_silence_produces_not_stated_not_zero(emd):
    """The single most important behaviour in this module."""
    tender = make_tender()
    result = emd.analyze(tender, "Bidders must submit a technical datasheet.", URL)
    assert result.status is TernaryFlag.NOT_STATED
    assert "MANUAL VERIFICATION REQUIRED" in result.headline()


def test_emd_mentioned_without_an_amount_raises_a_flag(emd):
    tender = make_tender()
    emd.analyze(tender, "EMD shall be as per the terms of the bid.", URL)
    assert any("emd" in flag.lower() for flag in tender.verification_flags)


def test_portal_value_wins_and_a_conflict_is_flagged(emd):
    tender = make_tender()
    tender.emd.status = TernaryFlag.REQUIRED
    tender.emd.amount = MoneyClaim(value=75000.0, confidence=Confidence.CONFIRMED)
    emd.analyze(tender, "EMD of Rs. 50,000 shall be paid.", URL)
    assert tender.emd.amount.value == 75000.0       # portal figure retained
    assert any("differs" in flag for flag in tender.verification_flags)


def test_msme_and_startup_exemptions_are_detected(emd):
    tender = make_tender()
    text = ("EMD of Rs. 1,00,000 is payable. MSME registered bidders are exempted "
            "from EMD on submission of a valid Udyam Registration Certificate. "
            "DPIIT recognised startups are also exempted.")
    result = emd.analyze(tender, text, URL)
    assert result.msme_exemption is TernaryFlag.REQUIRED
    assert result.startup_exemption is TernaryFlag.REQUIRED
    assert "Udyam Registration Certificate" in result.exemption_proof_required


def test_payment_mode_and_last_date_are_extracted(emd):
    tender = make_tender()
    text = ("EMD of Rs. 20,000 shall be paid online through NEFT on or before "
            "12-09-2026.")
    result = emd.analyze(tender, text, URL)
    assert result.payment_mode.known
    assert "12-09-2026" in str(result.last_date.value)


def test_no_document_text_leaves_a_flag(emd):
    tender = make_tender()
    result = emd.analyze(tender, "", URL)
    assert result.status is TernaryFlag.NOT_STATED
    assert tender.verification_flags


# -- Performance security ---------------------------------------------
def test_epbg_percentage_and_validity(security):
    tender = make_tender()
    text = ("The successful bidder shall submit ePBG of 3% of the contract value, "
            "valid for 60 days beyond the contract completion date.")
    result = security.analyze(tender, text, URL)
    assert result.epbg is TernaryFlag.REQUIRED
    assert result.percentage.value == 3.0
    line = render_security_line(result)
    assert "3%" in line and "Required" in line


def test_security_silence_is_verification_required(security):
    tender = make_tender()
    result = security.analyze(tender, "Delivery within 30 days.", URL)
    assert result.headline() == "⚠ Verification Required"
    assert "Verification Required" in render_security_line(result)


def test_explicit_nil_performance_security(security):
    tender = make_tender()
    result = security.analyze(
        tender, "Performance Security: Not applicable for this bid.", URL)
    assert result.performance_security is TernaryFlag.NOT_REQUIRED
    assert result.headline() == "Not Required"


def test_partial_answer_keeps_the_caveat(security):
    """Portal says ePBG 0% but the documents are silent on other instruments."""
    tender = make_tender()
    tender.security.epbg = TernaryFlag.NOT_REQUIRED
    line = render_security_line(tender.security)
    assert line.startswith("ePBG: Not Required")
    assert "confirm in the bid document" in line
