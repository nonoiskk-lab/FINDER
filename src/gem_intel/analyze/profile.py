"""The bidding company's own profile.

Kept separate from settings because it answers a different question: not
"how should the crawler behave" but "what can we actually do". Every field
defaults to *unknown*, and unknown never scores as a match.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class CompanyProfile:
    legal_name: str | None = None
    base_city: str = ""
    base_state: str = ""
    gem_seller_registered: bool | None = None
    years_in_business: int | None = None
    msme_registered: bool | None = None
    startup_dpiit_recognised: bool | None = None
    iso_certified: bool | None = None
    gst_registered: bool | None = None

    annual_turnover_inr: float | None = None
    can_arrange_emd_upto_inr: float | None = None
    can_arrange_epbg_upto_inr: float | None = None

    strong: list[str] = field(default_factory=list)
    moderate: list[str] = field(default_factory=list)
    weak: list[str] = field(default_factory=list)

    oem_held: list[str] = field(default_factory=list)
    oem_obtainable: list[str] = field(default_factory=list)

    government_supply_experience: bool | None = None
    largest_single_order_inr: float | None = None
    service_districts: list[str] = field(default_factory=list)

    min_attractive_value_inr: float | None = None
    max_comfortable_value_inr: float | None = None
    avoid_keywords: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        """Has anyone actually filled this in?"""
        return bool(self.strong or self.moderate or self.legal_name)

    def capability_for(self, categories: list[str]) -> tuple[str, str]:
        """Return ``(level, reason)`` for a tender's categories."""
        if not self.configured:
            return "unknown", "company profile not configured"
        lowered = {c.lower() for c in categories}
        if lowered & {c.lower() for c in self.strong}:
            hit = sorted(lowered & {c.lower() for c in self.strong})
            return "strong", f"core category ({', '.join(hit)})"
        if lowered & {c.lower() for c in self.moderate}:
            hit = sorted(lowered & {c.lower() for c in self.moderate})
            return "moderate", f"adjacent category ({', '.join(hit)})"
        if lowered & {c.lower() for c in self.weak}:
            hit = sorted(lowered & {c.lower() for c in self.weak})
            return "weak", f"outside usual scope ({', '.join(hit)})"
        return "unknown", "category not listed in the company profile"

    def serves_district(self, districts: list[str]) -> bool | None:
        if not self.service_districts:
            return None
        mine = {d.lower() for d in self.service_districts}
        if not districts:
            return None
        return any(d.lower() in mine for d in districts)


def load_profile(path: Path | str) -> CompanyProfile:
    path = Path(path)
    if not path.exists():
        return CompanyProfile()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    company = data.get("company", {}) or {}
    financials = data.get("financials", {}) or {}
    capability = data.get("capability", {}) or {}
    oem = data.get("oem_authorizations", {}) or {}
    experience = data.get("experience", {}) or {}
    preferences = data.get("preferences", {}) or {}

    return CompanyProfile(
        legal_name=company.get("legal_name"),
        base_city=company.get("base_city") or "",
        base_state=company.get("base_state") or "",
        gem_seller_registered=company.get("gem_seller_registered"),
        years_in_business=company.get("years_in_business"),
        msme_registered=company.get("msme_registered"),
        startup_dpiit_recognised=company.get("startup_dpiit_recognised"),
        iso_certified=company.get("iso_certified"),
        gst_registered=company.get("gst_registered"),
        annual_turnover_inr=financials.get("annual_turnover_inr"),
        can_arrange_emd_upto_inr=financials.get("can_arrange_emd_upto_inr"),
        can_arrange_epbg_upto_inr=financials.get("can_arrange_epbg_upto_inr"),
        strong=list(capability.get("strong", []) or []),
        moderate=list(capability.get("moderate", []) or []),
        weak=list(capability.get("weak", []) or []),
        oem_held=list(oem.get("held", []) or []),
        oem_obtainable=list(oem.get("obtainable", []) or []),
        government_supply_experience=experience.get("government_supply_experience"),
        largest_single_order_inr=experience.get("largest_single_order_inr"),
        service_districts=list(experience.get("service_districts", []) or []),
        min_attractive_value_inr=preferences.get("min_attractive_value_inr"),
        max_comfortable_value_inr=preferences.get("max_comfortable_value_inr"),
        avoid_keywords=list(preferences.get("avoid_keywords", []) or []),
    )
