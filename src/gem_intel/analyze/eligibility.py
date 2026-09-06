"""Can we potentially participate?

Hard constraint from the brief: never claim guaranteed eligibility. The
vocabulary this module is allowed to use is fixed:

    Likely Suitable  |  Potentially Suitable  |  Requires Verification  |
    Major Requirement Gap

Anything the company profile does not answer becomes a verification point,
not a match. A missing profile therefore produces "Requires Verification"
across the board — which is the honest answer.
"""

from __future__ import annotations

import re

from gem_intel.analyze.profile import CompanyProfile
from gem_intel.models import (
    Confidence,
    EligibilityAssessment,
    MatchLevel,
    Tender,
    TernaryFlag,
)

VERDICT_LIKELY = "Likely Suitable"
VERDICT_POTENTIAL = "Potentially Suitable"
VERDICT_VERIFY = "Requires Verification"
VERDICT_GAP = "Major Requirement Gap"

TURNOVER_RE = re.compile(
    r"(?:average\s+)?annual\s+turnover[^.\n]{0,60}?"
    r"(?:rs\.?|inr|₹)?\s*([\d,]+(?:\.\d+)?)\s*(lakh?s?|crores?|cr)?",
    re.IGNORECASE,
)
EXPERIENCE_YEARS_RE = re.compile(
    r"(\d{1,2})\s*(?:\+)?\s*years?\s+of\s+(?:past\s+)?experience", re.IGNORECASE
)
SCALES = {"lakh": 100_000, "lakhs": 100_000, "crore": 10_000_000,
          "crores": 10_000_000, "cr": 10_000_000}


class EligibilityAnalyzer:
    def __init__(self, profile: CompanyProfile) -> None:
        self.profile = profile

    def assess(self, tender: Tender) -> EligibilityAssessment:
        assessment = EligibilityAssessment()
        supporting: list[str] = []
        gaps: list[str] = []
        verify: list[str] = []

        # --- category fit ------------------------------------------------
        level, reason = self.profile.capability_for(tender.classification.categories)
        if level == "strong":
            supporting.append(f"Product/service category matches our core business — {reason}.")
        elif level == "moderate":
            supporting.append(f"Product/service category is workable for us — {reason}.")
        elif level == "weak":
            gaps.append(f"Category is outside our usual scope — {reason}.")
        else:
            verify.append(
                "Whether we can supply this category (company profile does not cover it)."
            )

        # --- location ----------------------------------------------------
        if tender.geo.is_jharkhand and tender.geo.confidence is Confidence.CONFIRMED:
            where = ", ".join(tender.geo.districts) or "Jharkhand"
            supporting.append(f"Work location is in our operating area ({where}).")
            serves = self.profile.serves_district(tender.geo.districts)
            if serves is False:
                verify.append(
                    f"Service coverage in {', '.join(tender.geo.districts)} — "
                    "outside the districts listed in our profile."
                )
        elif tender.geo.is_jharkhand:
            verify.append(f"Actual work location — {tender.geo.reason}.")

        # --- OEM authorisation ------------------------------------------
        needs_oem = any(d.canonical == "oem_authorization" for d in tender.required_documents)
        if needs_oem:
            if self.profile.oem_held:
                verify.append(
                    "OEM Authorization (MAF) for the exact make/model this bid asks for — "
                    f"we hold authorisations for {', '.join(self.profile.oem_held)}."
                )
            elif self.profile.oem_obtainable:
                verify.append(
                    "OEM Authorization (MAF) must be arranged before bidding — "
                    f"obtainable from {', '.join(self.profile.oem_obtainable)}."
                )
            else:
                gaps.append(
                    "OEM Authorization Letter is required and no OEM authorisation is "
                    "recorded in our profile."
                )

        # --- turnover ----------------------------------------------------
        turnover_required = self._required_turnover(tender)
        if turnover_required is not None:
            if self.profile.annual_turnover_inr is None:
                verify.append(
                    f"Turnover requirement of about ₹{turnover_required:,.0f} — "
                    "our turnover is not recorded in the company profile."
                )
            elif self.profile.annual_turnover_inr >= turnover_required:
                supporting.append(
                    f"Stated turnover requirement (₹{turnover_required:,.0f}) appears met."
                )
            else:
                gaps.append(
                    f"Turnover requirement of ₹{turnover_required:,.0f} exceeds our "
                    f"recorded turnover (₹{self.profile.annual_turnover_inr:,.0f})."
                )

        # --- experience --------------------------------------------------
        years_required = self._required_years(tender)
        if years_required is not None:
            if self.profile.years_in_business is None:
                verify.append(f"Requirement of {years_required} years' experience.")
            elif self.profile.years_in_business >= years_required:
                supporting.append(
                    f"{years_required}-year experience requirement appears met."
                )
            else:
                gaps.append(
                    f"{years_required} years' experience required; profile records "
                    f"{self.profile.years_in_business}."
                )

        if any(d.canonical in ("work_order", "completion_certificate", "experience")
               for d in tender.required_documents):
            if self.profile.government_supply_experience is None:
                verify.append(
                    "Past government supply experience (work orders / completion "
                    "certificates) — not recorded in our profile."
                )
            elif self.profile.government_supply_experience is False:
                gaps.append(
                    "Past work orders / completion certificates are required and we "
                    "have no recorded government supply experience."
                )
            else:
                supporting.append("We have prior government supply experience on record.")

        # --- money we must front ----------------------------------------
        if tender.emd.status is TernaryFlag.REQUIRED and tender.emd.amount.known:
            emd = float(tender.emd.amount.value)
            cap = self.profile.can_arrange_emd_upto_inr
            if cap is None:
                verify.append(f"Ability to fund EMD of ₹{emd:,.0f}.")
            elif emd > cap:
                gaps.append(
                    f"EMD of ₹{emd:,.0f} exceeds the ₹{cap:,.0f} we have recorded as "
                    "arrangeable."
                )
            elif (self.profile.msme_registered
                  and tender.emd.msme_exemption is TernaryFlag.REQUIRED):
                supporting.append("MSME EMD exemption appears available to us.")
        elif tender.emd.status is TernaryFlag.NOT_REQUIRED:
            supporting.append("No EMD is required.")
        else:
            verify.append("EMD requirement is not stated clearly in the source.")

        if tender.security.headline() == "Required" and tender.security.percentage.known:
            verify.append(
                f"Performance security of {tender.security.percentage.value:g}% must be "
                "arranged if we win."
            )
        elif tender.security.headline() == "⚠ Verification Required":
            verify.append("Performance security / ePBG terms are not stated clearly.")

        # --- avoid list --------------------------------------------------
        blob = f"{tender.title} {tender.item_description}".lower()
        for keyword in self.profile.avoid_keywords:
            if keyword.lower() in blob:
                gaps.append(f"Matches an avoid-list keyword: '{keyword}'.")

        if not self.profile.configured:
            verify.insert(0, (
                "Everything below is unverified: config/company_profile.yaml has not "
                "been filled in, so the system has no basis to judge our capability."
            ))

        assessment.supporting_points = supporting
        assessment.gaps = gaps
        assessment.verification_points = verify
        assessment.match_level, assessment.verdict = self._verdict(
            supporting, gaps, verify, level
        )
        return assessment

    # ------------------------------------------------------------------
    def _verdict(self, supporting: list[str], gaps: list[str],
                 verify: list[str], capability: str) -> tuple[MatchLevel, str]:
        if not self.profile.configured:
            return MatchLevel.UNKNOWN, VERDICT_VERIFY
        if gaps:
            # A single hard gap (OEM, turnover) is enough to demote.
            if len(gaps) >= 2 or capability == "weak":
                return MatchLevel.LOW, VERDICT_GAP
            return MatchLevel.MEDIUM, VERDICT_VERIFY
        if capability == "strong" and len(supporting) >= 3 and len(verify) <= 2:
            return MatchLevel.HIGH, VERDICT_LIKELY
        if capability in ("strong", "moderate") and supporting:
            return MatchLevel.MEDIUM, VERDICT_POTENTIAL
        return MatchLevel.UNKNOWN, VERDICT_VERIFY

    @staticmethod
    def _required_turnover(tender: Tender) -> float | None:
        for line in tender.eligibility_requirements:
            match = TURNOVER_RE.search(line)
            if not match:
                continue
            try:
                value = float(match.group(1).replace(",", ""))
            except ValueError:
                continue
            scale = (match.group(2) or "").lower()
            return value * SCALES.get(scale, 1)
        return None

    @staticmethod
    def _required_years(tender: Tender) -> int | None:
        for line in tender.eligibility_requirements:
            match = EXPERIENCE_YEARS_RE.search(line)
            if match:
                return int(match.group(1))
        return None
