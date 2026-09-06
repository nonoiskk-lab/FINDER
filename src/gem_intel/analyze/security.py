"""Performance Security / ePBG / Security Deposit extraction.

Three distinct instruments that GeM tenders use interchangeably in prose:

* **Performance Security** — the umbrella obligation (usually 3–10% of value).
* **ePBG** — an electronic Performance Bank Guarantee, GeM's usual mechanism.
* **Security Deposit** — occasionally used by state buyers for the same thing.

Each gets its own three-valued flag so the report can say exactly which one
the tender named, and the percentage/validity are only reported when a
sentence actually states them.
"""

from __future__ import annotations

import re

from gem_intel.extract.text import clean, parse_amount, parse_percentage, snippet
from gem_intel.models import (
    Claim,
    Confidence,
    Evidence,
    MoneyClaim,
    SecurityAnalysis,
    Tender,
    TernaryFlag,
)


def _clause(width: int) -> str:
    """Up to ``width`` characters within the same sentence — see analyze/emd.py."""
    return rf"(?:(?!\.\s+[A-Z])[^\n]){{0,{width}}}?"


SECURITY_LABEL = (r"(?:performance\s+security|e-?pbg|"
                  r"performance\s+bank\s+guarantee|security\s+deposit)")

PERF_SECURITY = re.compile(r"performance\s+security", re.IGNORECASE)
EPBG = re.compile(r"\be-?pbg\b|performance\s+bank\s+guarantee", re.IGNORECASE)
SECURITY_DEPOSIT = re.compile(r"security\s+deposit", re.IGNORECASE)

INSTRUMENTS = (
    ("performance_security", PERF_SECURITY),
    ("epbg", EPBG),
    ("security_deposit", SECURITY_DEPOSIT),
)

NOT_REQUIRED = re.compile(
    rf"{SECURITY_LABEL}{_clause(80)}"
    r"\b(?:nil|not\s+applicable|not\s+required|no\s+performance|"
    r"zero|0\s*%|rs\.?\s*0\b)",
    re.IGNORECASE,
)
PERCENTAGE = re.compile(
    rf"{SECURITY_LABEL}{_clause(140)}(\d{{1,2}}(?:\.\d+)?)\s*(?:%|percent|per\s?cent)"
    rf"|(\d{{1,2}}(?:\.\d+)?)\s*(?:%|percent|per\s?cent){_clause(80)}"
    r"(?:performance\s+security|e-?pbg|performance\s+bank\s+guarantee)",
    re.IGNORECASE,
)
AMOUNT = re.compile(
    rf"{SECURITY_LABEL}{_clause(120)}(?:rs\.?|inr|₹)\s*([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)
VALIDITY = re.compile(
    r"(?:valid|validity)[^.\n]{0,80}?(\d{1,4})\s*(days?|months?|years?)"
    r"[^.\n]{0,60}?(?:beyond|after|from)?[^.\n]{0,60}",
    re.IGNORECASE,
)
VALIDITY_NEAR = re.compile(
    r"(?:performance\s+security|e-?pbg|performance\s+bank\s+guarantee)"
    r"[^.]{0,240}?(\d{1,4}\s*(?:days?|months?|years?)[^.\n]{0,80})",
    re.IGNORECASE,
)
TIMELINE = re.compile(
    rf"(?:performance\s+security|e-?pbg){_clause(160)}"
    rf"within\s+(\d{{1,3}}\s*(?:days?|weeks?){_clause(60)})",
    re.IGNORECASE,
)
EXEMPTION = re.compile(
    rf"(?:performance\s+security|e-?pbg|security\s+deposit){_clause(160)}"
    rf"exempt(?:ed|ion)?{_clause(120)}",
    re.IGNORECASE,
)


class SecurityAnalyzer:
    def analyze(self, tender: Tender, document_text: str, source_url: str) -> SecurityAnalysis:
        analysis = tender.security
        text = clean(document_text)
        if not text:
            return analysis

        not_required_match = NOT_REQUIRED.search(text)

        for attribute, pattern in INSTRUMENTS:
            if getattr(analysis, attribute) is not TernaryFlag.NOT_STATED:
                continue                       # portal already told us
            match = pattern.search(text)
            if not match:
                continue
            # A named instrument that the same sentence nils out is not required.
            if not_required_match and pattern.search(not_required_match.group(0)):
                setattr(analysis, attribute, TernaryFlag.NOT_REQUIRED)
                analysis.evidence.append(
                    self._evidence(source_url, text, not_required_match))
            else:
                setattr(analysis, attribute, TernaryFlag.REQUIRED)
                analysis.evidence.append(self._evidence(source_url, text, match))

        if not analysis.percentage.known:
            match = PERCENTAGE.search(text)
            if match:
                raw = match.group(1) or match.group(2)
                percentage = parse_percentage(f"{raw}%")
                if percentage is not None:
                    analysis.percentage = Claim(
                        value=percentage, confidence=Confidence.CONFIRMED,
                        evidence=[self._evidence(source_url, text, match)],
                    )

        if not analysis.amount.known:
            match = AMOUNT.search(text)
            if match:
                amount = parse_amount(match.group(1))
                if amount is not None:
                    analysis.amount = MoneyClaim(
                        value=amount, confidence=Confidence.CONFIRMED,
                        evidence=[self._evidence(source_url, text, match)],
                    )

        analysis.validity = self._claim(VALIDITY_NEAR, text, source_url, 1) or \
            self._claim(VALIDITY, text, source_url, 0) or analysis.validity
        analysis.submission_timeline = self._claim(TIMELINE, text, source_url, 1) or \
            analysis.submission_timeline

        exemption = EXEMPTION.search(text)
        if exemption:
            analysis.exemptions = [clean(exemption.group(0))[:300]]

        # Percentage without a named instrument still means something is owed.
        if (analysis.percentage.known
                and all(getattr(analysis, a) is TernaryFlag.NOT_STATED
                        for a, _ in INSTRUMENTS)):
            analysis.performance_security = TernaryFlag.REQUIRED

        if analysis.headline() == "⚠ Verification Required" and (
                PERF_SECURITY.search(text) or EPBG.search(text)):
            tender.flag(
                "Performance security / ePBG is mentioned but its amount or "
                "percentage is not stated clearly — verify before bidding."
            )
        return analysis

    # ------------------------------------------------------------------
    @staticmethod
    def _evidence(source_url: str, text: str, match: re.Match[str]) -> Evidence:
        return Evidence(
            source_url=source_url, locator="tender documents",
            quote=snippet(text, match.start()), extractor="rule:security",
        )

    def _claim(self, pattern: re.Pattern[str], text: str,
               source_url: str, group: int) -> Claim | None:
        match = pattern.search(text)
        if not match:
            return None
        return Claim(
            value=clean(match.group(group)),
            confidence=Confidence.CONFIRMED,
            evidence=[self._evidence(source_url, text, match)],
        )


def render_security_line(analysis: SecurityAnalysis) -> str:
    """One-line summary, e.g. 'ePBG: 3% of contract value, valid 60 days'."""
    if analysis.headline() == "⚠ Verification Required":
        return "⚠ Verification Required — the tender documents do not state this clearly"

    if analysis.headline() == "Not Required":
        named = [
            label for label, flag in (
                ("ePBG", analysis.epbg),
                ("Performance Security", analysis.performance_security),
                ("Security Deposit", analysis.security_deposit),
            ) if flag is TernaryFlag.NOT_REQUIRED
        ]
        line = f"{' / '.join(named)}: Not Required"
        if not analysis.fully_stated:
            line += (" — ⚠ no separate performance-security clause was found in the "
                     "text we could read; confirm in the bid document")
        return line

    names = []
    if analysis.epbg is TernaryFlag.REQUIRED:
        names.append("ePBG")
    if analysis.performance_security is TernaryFlag.REQUIRED:
        names.append("Performance Security")
    if analysis.security_deposit is TernaryFlag.REQUIRED:
        names.append("Security Deposit")
    label = " / ".join(names) or "Performance Security"

    parts = [f"{label}: Required"]
    if analysis.percentage.known:
        parts.append(f"{analysis.percentage.value:g}% of contract value")
    if analysis.amount.known:
        parts.append(analysis.amount.render())
    if analysis.validity.known:
        parts.append(f"validity {analysis.validity.value}")
    if analysis.submission_timeline.known:
        parts.append(f"submit within {analysis.submission_timeline.value}")
    return " — ".join(parts)
