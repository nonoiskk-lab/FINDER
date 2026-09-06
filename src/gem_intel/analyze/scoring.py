"""Opportunity scoring, 0–100.

Seven components with the weights from the brief. Two design rules:

* **Unknown never scores full marks.** A component we cannot evaluate gets a
  neutral-to-low share and says so in its reason, so a data-poor tender cannot
  float to the top on optimism.
* **Every component states its reasoning**, and the reasons are what the
  report prints. A score with no explanation is not actionable.
"""

from __future__ import annotations

from gem_intel.analyze.profile import CompanyProfile
from gem_intel.config import Settings
from gem_intel.models import (
    Confidence,
    MatchLevel,
    OpportunityScore,
    ScoreComponent,
    Tender,
    TenderKind,
    TernaryFlag,
    Urgency,
)


class OpportunityScorer:
    def __init__(self, settings: Settings, profile: CompanyProfile) -> None:
        self.weights = settings.score_weights
        self.bands = settings.score_bands
        self.profile = profile
        self.max_days = settings.max_days_remaining

    def score(self, tender: Tender) -> OpportunityScore:
        components = [
            self._product_match(tender),
            self._location_match(tender),
            self._capability(tender),
            self._document_complexity(tender),
            self._deadline(tender),
            self._commercial(tender),
            self._procurement_complexity(tender),
        ]
        total = round(sum(c.points for c in components), 1)
        result = OpportunityScore(components=components, total=total)
        band = self._band_for(total)
        if band:
            result.band_label = band["label"]
            result.band_emoji = band["emoji"]
            result.rank_label = band["rank"]
            result.rank_emoji = band["rank_emoji"]
        return result

    # -- components -----------------------------------------------------
    def _product_match(self, tender: Tender) -> ScoreComponent:
        maximum = self.weights.get("product_match", 20)
        classification = tender.classification
        if not classification.is_it_related:
            return ScoreComponent("Product Match", 0, maximum,
                                  "Not an IT product or service")
        base = maximum if classification.confidence is Confidence.CONFIRMED else maximum * 0.7
        # A tender that hits several of our categories is a better fit than one
        # that grazes a single signal.
        if len(classification.categories) >= 2:
            base = min(maximum, base + maximum * 0.1)
        if len(classification.matched_signals) == 1:
            base *= 0.85
        kind = {
            TenderKind.PRODUCT_SUPPLY: "product supply",
            TenderKind.SERVICE: "service",
            TenderKind.SUPPLY_AND_SERVICE: "supply + service",
            TenderKind.UNKNOWN: "type unclear",
        }[classification.kind]
        return ScoreComponent(
            "Product Match", round(base, 1), maximum,
            f"{', '.join(classification.categories)} ({kind})",
        )

    def _location_match(self, tender: Tender) -> ScoreComponent:
        maximum = self.weights.get("location_match", 15)
        geo = tender.geo
        if not geo.is_jharkhand:
            return ScoreComponent("Location Match", 0, maximum, geo.reason)
        if geo.confidence is Confidence.CONFIRMED:
            points = maximum
            reason = f"Jharkhand confirmed ({', '.join(geo.districts) or 'state-level'})"
            serves = self.profile.serves_district(geo.districts)
            if serves is False:
                points = maximum * 0.75
                reason += " — outside our listed service districts"
        elif geo.confidence is Confidence.INFERRED:
            points = maximum * 0.7
            reason = "Jharkhand mentioned but not in a location field"
        else:
            points = maximum * 0.4
            reason = "Pan-India / inferred only — confirm Jharkhand is in scope"
        return ScoreComponent("Location Match", round(points, 1), maximum, reason)

    def _capability(self, tender: Tender) -> ScoreComponent:
        maximum = self.weights.get("business_capability", 20)
        level, reason = self.profile.capability_for(tender.classification.categories)
        share = {"strong": 1.0, "moderate": 0.7, "weak": 0.3, "unknown": 0.4}[level]
        points = maximum * share
        if tender.eligibility.match_level is MatchLevel.LOW:
            points *= 0.5
            reason += "; eligibility gaps identified"
        elif tender.eligibility.match_level is MatchLevel.HIGH:
            points = min(maximum, points * 1.1)
        if level == "unknown":
            reason = "Company profile does not cover this category — scored neutrally"
        return ScoreComponent("Business Capability", round(points, 1), maximum, reason)

    def _document_complexity(self, tender: Tender) -> ScoreComponent:
        """More points = *easier* paperwork."""
        maximum = self.weights.get("document_complexity", 10)
        documents = tender.required_documents
        if not documents:
            return ScoreComponent(
                "Document Complexity", round(maximum * 0.5, 1), maximum,
                "Document requirements unknown — scored neutrally",
            )
        count = len(documents)
        hard = {"oem_authorization", "iso", "balance_sheet", "turnover",
                "completion_certificate", "labour_licence"}
        hard_count = sum(1 for d in documents if d.canonical in hard)
        points = maximum
        points -= min(maximum * 0.5, max(0, count - 6) * (maximum * 0.06))
        points -= hard_count * (maximum * 0.12)
        points = max(0.0, points)
        return ScoreComponent(
            "Document Complexity", round(points, 1), maximum,
            f"{count} documents required"
            + (f", {hard_count} of them hard to arrange" if hard_count else ""),
        )

    def _deadline(self, tender: Tender) -> ScoreComponent:
        """More points = *more* time to prepare a good bid."""
        maximum = self.weights.get("deadline_urgency", 10)
        days = tender.days_remaining
        if days is None:
            return ScoreComponent("Deadline Urgency", round(maximum * 0.3, 1), maximum,
                                  "Closing date unknown — verify on GeM")
        if days < 0:
            return ScoreComponent("Deadline Urgency", 0, maximum, "Closed")
        if days <= 1:
            points, reason = maximum * 0.25, f"{days} day left — very little preparation time"
        elif days <= 2:
            points, reason = maximum * 0.45, f"{days} days left — tight"
        elif days <= 5:
            points, reason = maximum * 0.8, f"{days} days left — workable"
        else:
            points, reason = maximum, f"{days} days left — comfortable"
        return ScoreComponent("Deadline Urgency", round(points, 1), maximum, reason)

    def _commercial(self, tender: Tender) -> ScoreComponent:
        """Value-based, and explicitly neutral when no value is published.

        GeM frequently omits the estimated value. Inventing one would be a
        fabrication, so an unpublished value scores at the midpoint and the
        reason says the value is unknown.
        """
        maximum = self.weights.get("commercial_potential", 15)
        if not tender.estimated_value.known:
            quantity = tender.quantity
            if quantity and quantity >= 25:
                return ScoreComponent(
                    "Commercial Potential", round(maximum * 0.65, 1), maximum,
                    f"Value not published; quantity of {quantity:g} "
                    f"{tender.quantity_unit or 'units'} suggests a worthwhile order",
                )
            return ScoreComponent(
                "Commercial Potential", round(maximum * 0.5, 1), maximum,
                "Estimated value not published on GeM — scored neutrally",
            )

        value = float(tender.estimated_value.value)
        floor = self.profile.min_attractive_value_inr
        ceiling = self.profile.max_comfortable_value_inr
        if floor is not None and value < floor:
            points = maximum * 0.3
            reason = f"₹{value:,.0f} is below our ₹{floor:,.0f} threshold"
        elif ceiling is not None and value > ceiling:
            points = maximum * 0.6
            reason = f"₹{value:,.0f} exceeds our ₹{ceiling:,.0f} comfort ceiling"
        else:
            points = maximum
            reason = f"₹{value:,.0f} sits in our target range"
        return ScoreComponent("Commercial Potential", round(points, 1), maximum, reason)

    def _procurement_complexity(self, tender: Tender) -> ScoreComponent:
        """More points = *simpler* procurement / less crowded field."""
        maximum = self.weights.get("procurement_complexity", 10)
        points = maximum
        notes: list[str] = []

        if tender.emd.status is TernaryFlag.REQUIRED:
            points -= maximum * 0.15
            notes.append("EMD payable")
        elif tender.emd.status is TernaryFlag.NOT_REQUIRED:
            # No EMD lowers the barrier for everyone, so expect more bidders.
            points -= maximum * 0.1
            notes.append("no EMD — expect more bidders")
        else:
            points -= maximum * 0.2
            notes.append("EMD terms unclear")

        if tender.security.headline() == "Required":
            points -= maximum * 0.15
            notes.append("performance security required")

        blob = " ".join(tender.special_conditions).lower()
        ra_flag = tender.raw_fields.get("detail.ra_enabled", "").lower()
        if "reverse auction" in blob or ra_flag == "yes":
            points -= maximum * 0.2
            notes.append("reverse auction — price pressure")
        if any(k in blob for k in ("liquidated damages", "penalt")):
            points -= maximum * 0.1
            notes.append("penalty clauses")
        if tender.classification.kind is TenderKind.SERVICE:
            notes.append("service contract — recurring revenue")
            points += maximum * 0.05

        points = max(0.0, min(maximum, points))
        return ScoreComponent(
            "Procurement Complexity", round(points, 1), maximum,
            "; ".join(notes) or "No unusual procurement conditions found",
        )

    # ------------------------------------------------------------------
    def _band_for(self, total: float) -> dict | None:
        for band in self.bands:
            if band["min"] <= total <= band["max"]:
                return band
        return None


def rank_tenders(tenders: list[Tender]) -> list[Tender]:
    """Sort for the report: score first, then urgency, then closing time.

    Two tenders with the same score are ordered by which one closes sooner,
    because that is the one a human must act on first.
    """
    urgency_order = {
        Urgency.URGENT: 0,
        Urgency.HIGH_PRIORITY: 1,
        Urgency.ACTION_REQUIRED: 2,
        Urgency.WATCHLIST: 3,
        Urgency.UNKNOWN: 4,
        Urgency.EXPIRED: 5,
    }
    return sorted(
        tenders,
        key=lambda t: (
            -t.score.total,
            urgency_order.get(t.urgency, 9),
            t.days_remaining if t.days_remaining is not None else 999,
            t.title.lower(),
        ),
    )
