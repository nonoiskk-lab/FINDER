"""EMD (Earnest Money Deposit) extraction.

The governing rule for this module, from the project brief: **the system must
never guess whether EMD is required.** So:

* The portal's structured EMD field, when present, wins outright.
* Otherwise we look for explicit statements in the document text and require a
  matching sentence as evidence.
* "Nil", "₹0", "not applicable" and "exempted" are the *only* things that
  produce NOT_REQUIRED. Silence produces NOT_STATED, which renders as
  "⚠ EMD INFORMATION NOT CLEAR – MANUAL VERIFICATION REQUIRED".
"""

from __future__ import annotations

import re

from gem_intel.extract.text import clean, parse_amount, snippet
from gem_intel.models import (
    Claim,
    Confidence,
    EMDAnalysis,
    Evidence,
    MoneyClaim,
    Tender,
    TernaryFlag,
)


def _clause(width: int) -> str:
    """Up to ``width`` characters within the same sentence.

    A plain ``[^.\n]`` run would stop at the period in "Rs. 20,000" and miss
    everything after it, which is exactly where GeM puts the payment mode. This
    treats a period as a sentence break only when a capital letter follows it.
    """
    return rf"(?:(?!\.\s+[A-Z])[^\n]){{0,{width}}}?"


EMD_LABEL = r"(?:emd|earnest\s+money(?:\s+deposit)?|bid\s+security)"

EMD_CONTEXT = re.compile(
    r"(earnest\s+money(?:\s+deposit)?|\bemd\b|bid\s+security(?!\s+declaration))",
    re.IGNORECASE,
)
NOT_REQUIRED = re.compile(
    rf"\b{EMD_LABEL}\b{_clause(80)}"
    r"\b(?:nil|not\s+applicable|not\s+required|no\s+emd|exempt(?:ed|ion)?|"
    r"zero|rs\.?\s*0(?:\.00)?\b|₹\s*0(?:\.00)?\b)",
    re.IGNORECASE,
)
AMOUNT_NEAR_EMD = re.compile(
    rf"\b{EMD_LABEL}\b{_clause(120)}(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
AMOUNT_BEFORE_EMD = re.compile(
    rf"(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?){_clause(60)}\b(?:as\s+)?{EMD_LABEL}\b",
    re.IGNORECASE,
)
PAYMENT_MODE = re.compile(
    rf"\b{EMD_LABEL}\b{_clause(160)}"
    r"\b(online|net\s?banking|neft|rtgs|demand\s+draft|dd|bank\s+guarantee|bg|"
    r"fixed\s+deposit|fdr|insurance\s+surety\s+bond|through\s+gem\s+pool\s+account)\b",
    re.IGNORECASE,
)
LAST_DATE = re.compile(
    rf"\b{EMD_LABEL}\b{_clause(160)}"
    rf"(?:on\s+or\s+)?(?:before|by|up\s?to|last\s+date{_clause(20)})\s*"
    r"(\d{1,2}[-/\s][A-Za-z0-9]{2,9}[-/\s]\d{2,4}(?:\s+\d{1,2}:\d{2})?)",
    re.IGNORECASE,
)
MSME_EXEMPT = re.compile(
    rf"\b(?:msme|mse|micro\s+and\s+small|udyam|nsic)\b{_clause(120)}"
    rf"\bexempt(?:ed|ion)?\b|"
    rf"\bexempt(?:ed|ion)?\b{_clause(120)}"
    r"\b(?:msme|mse|micro\s+and\s+small|udyam|nsic)\b",
    re.IGNORECASE,
)
STARTUP_EXEMPT = re.compile(
    rf"\b(?:startup|start-up|dpiit)\b{_clause(120)}\bexempt(?:ed|ion)?\b|"
    rf"\bexempt(?:ed|ion)?\b{_clause(120)}\b(?:startup|start-up|dpiit)\b",
    re.IGNORECASE,
)
PROOF_HINTS = (
    ("Udyam Registration Certificate", re.compile(r"udyam", re.IGNORECASE)),
    ("MSME / MSE Certificate", re.compile(r"\bms(?:me|e)\s+certificate\b", re.IGNORECASE)),
    ("NSIC Registration Certificate", re.compile(r"\bnsic\b", re.IGNORECASE)),
    ("DPIIT Startup Recognition Certificate", re.compile(r"dpiit|startup\s+recognition",
                                                        re.IGNORECASE)),
)


class EmdAnalyzer:
    def analyze(self, tender: Tender, document_text: str, source_url: str) -> EMDAnalysis:
        analysis = tender.emd
        portal_decided = analysis.status is not TernaryFlag.NOT_STATED

        text = clean(document_text)
        if not text:
            if not portal_decided:
                tender.flag("EMD could not be determined: no document text was available.")
            self._exemptions_from_portal(tender, analysis)
            return analysis

        # 1. Explicit "no EMD" statements.
        no_emd = NOT_REQUIRED.search(text)
        # 2. An amount stated next to an EMD label.
        amount, amount_match = self._find_amount(text)

        if not portal_decided:
            if amount is not None and amount > 0:
                analysis.status = TernaryFlag.REQUIRED
                analysis.evidence.append(self._evidence(source_url, text, amount_match))
            elif no_emd is not None:
                analysis.status = TernaryFlag.NOT_REQUIRED
                analysis.evidence.append(self._evidence(source_url, text, no_emd))
            elif amount is not None and amount == 0:
                analysis.status = TernaryFlag.NOT_REQUIRED
                analysis.evidence.append(self._evidence(source_url, text, amount_match))
            else:
                analysis.status = TernaryFlag.NOT_STATED
                if EMD_CONTEXT.search(text):
                    tender.flag(
                        "EMD is mentioned in the tender documents but no amount or "
                        "exemption could be read — verify the EMD clause manually."
                    )

        if amount is not None and not analysis.amount.known:
            analysis.amount = MoneyClaim(
                value=amount,
                confidence=Confidence.CONFIRMED,
                evidence=[self._evidence(source_url, text, amount_match)],
            )

        # An amount from documents that contradicts the portal is a red flag,
        # not something to silently overwrite.
        if (portal_decided and amount is not None and analysis.amount.known
                and abs(float(analysis.amount.value) - amount) > 1):
            tender.flag(
                f"EMD amount differs between the GeM listing "
                f"(₹{float(analysis.amount.value):,.0f}) and the bid document "
                f"(₹{amount:,.0f}) — confirm which applies before paying."
            )

        analysis.payment_mode = self._claim(PAYMENT_MODE, text, source_url, group=1)
        analysis.last_date = self._claim(LAST_DATE, text, source_url, group=1)

        if MSME_EXEMPT.search(text):
            analysis.msme_exemption = TernaryFlag.REQUIRED
            analysis.exemption_available = TernaryFlag.REQUIRED
            analysis.evidence.append(self._evidence(source_url, text, MSME_EXEMPT.search(text)))
        if STARTUP_EXEMPT.search(text):
            analysis.startup_exemption = TernaryFlag.REQUIRED
            analysis.exemption_available = TernaryFlag.REQUIRED

        if analysis.exemption_available is TernaryFlag.REQUIRED:
            analysis.exemption_proof_required = [
                label for label, pattern in PROOF_HINTS if pattern.search(text)
            ]

        self._exemptions_from_portal(tender, analysis)
        return analysis

    # ------------------------------------------------------------------
    @staticmethod
    def _exemptions_from_portal(tender: Tender, analysis: EMDAnalysis) -> None:
        """The GeM listing's own MSE/Startup exemption flags, when present."""
        if analysis.msme_exemption is TernaryFlag.NOT_STATED:
            analysis.msme_exemption = tender.msme_exemption_portal_flag
        if analysis.startup_exemption is TernaryFlag.NOT_STATED:
            analysis.startup_exemption = tender.startup_exemption_portal_flag

    @staticmethod
    def _find_amount(text: str) -> tuple[float | None, re.Match[str] | None]:
        for pattern in (AMOUNT_NEAR_EMD, AMOUNT_BEFORE_EMD):
            match = pattern.search(text)
            if match:
                return parse_amount(match.group(1)), match
        return None, None

    @staticmethod
    def _evidence(source_url: str, text: str, match: re.Match[str] | None) -> Evidence:
        if match is None:
            return Evidence(source_url=source_url, extractor="rule:emd")
        return Evidence(
            source_url=source_url,
            locator="tender documents",
            quote=snippet(text, match.start()),
            extractor="rule:emd",
        )

    def _claim(self, pattern: re.Pattern[str], text: str,
               source_url: str, group: int = 0) -> Claim:
        match = pattern.search(text)
        if not match:
            return Claim(confidence=Confidence.NOT_FOUND)
        return Claim(
            value=clean(match.group(group)),
            confidence=Confidence.CONFIRMED,
            evidence=[self._evidence(source_url, text, match)],
        )
