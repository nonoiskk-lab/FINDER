"""Jharkhand relevance.

The business rule that drives the design: a tender qualifies if the *work*
lands in Jharkhand, regardless of where the buyer's head office sits. So the
assessment looks at delivery / service / installation / consignee fields
first, and only uses the buyer's address as corroboration.

Pan-India tenders that merely include Jharkhand are kept but marked, because
they compete very differently from a Ranchi-only bid.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gem_intel.config import Settings
from gem_intel.extract.text import clean
from gem_intel.models import Confidence, GeoAssessment, Tender

PAN_INDIA_MARKERS = (
    "all india", "pan india", "pan-india", "across india", "all states",
    "multiple states", "nationwide", "all over india", "various locations across",
)

# States whose presence *without* Jharkhand means the work is elsewhere.
OTHER_STATES = (
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "karnataka", "kerala",
    "madhya pradesh", "maharashtra", "manipur", "meghalaya", "mizoram",
    "nagaland", "odisha", "orissa", "punjab", "rajasthan", "sikkim",
    "tamil nadu", "telangana", "tripura", "uttar pradesh", "uttarakhand",
    "west bengal", "delhi", "jammu", "kashmir", "ladakh", "puducherry",
    "chandigarh", "andaman",
)

# Place names that exist in Jharkhand *and* elsewhere. Matching one of these
# alone is not enough — it must be corroborated by a state or district token.
AMBIGUOUS_PLACES = {"mango", "chas", "barhi", "nala", "kandra", "bermo"}


@dataclass
class _Field:
    name: str
    text: str


class JharkhandGeoFilter:
    def __init__(self, settings: Settings) -> None:
        terms = settings.geo_terms
        self.state_aliases = [t.lower() for t in terms["state_aliases"]]
        self.districts = [t.lower() for t in terms["districts"]]
        self.localities = [t.lower() for t in terms["localities"]]
        self.org_hints = [t.lower() for t in terms["local_org_hints"]]
        self.accept_fields = list(
            settings.get("geography.accept_fields", [])
        ) or ["delivery_location", "service_location", "installation_location", "buyer_address"]
        self.keep_pan_india = bool(settings.get("geography.keep_pan_india", True))

        self._state_re = self._compile(self.state_aliases, require_word=True)
        self._district_re = self._compile(self.districts)
        self._locality_re = self._compile(self.localities)
        self._org_re = self._compile(self.org_hints)

    @staticmethod
    def _compile(terms: list[str], require_word: bool = True) -> re.Pattern[str]:
        if not terms:
            return re.compile(r"(?!x)x")           # never matches
        alternation = "|".join(
            re.escape(t).replace(r"\ ", r"\s+") for t in sorted(terms, key=len, reverse=True)
        )
        boundary = r"(?<![a-z0-9])(?:%s)(?![a-z0-9])" if require_word else r"(?:%s)"
        return re.compile(boundary % alternation, re.IGNORECASE)

    # ------------------------------------------------------------------
    def assess(self, tender: Tender, document_text: str = "") -> GeoAssessment:
        fields = self._fields(tender, document_text)
        assessment = GeoAssessment()

        matched_terms: list[str] = []
        matched_fields: list[str] = []
        districts: list[str] = []
        strong_hit = False

        for field in fields:
            if not field.text:
                continue
            hits: list[str] = []

            state_hits = [m.group(0) for m in self._state_re.finditer(field.text)]
            # "JH" alone is too weak to act on; require the full state name.
            state_hits = [h for h in state_hits if len(h) > 2]
            district_hits = [m.group(0) for m in self._district_re.finditer(field.text)]
            locality_hits = [m.group(0) for m in self._locality_re.finditer(field.text)]

            unambiguous_localities = [
                h for h in locality_hits if h.lower() not in AMBIGUOUS_PLACES
            ]
            ambiguous_localities = [
                h for h in locality_hits if h.lower() in AMBIGUOUS_PLACES
            ]

            hits.extend(state_hits + district_hits + unambiguous_localities)
            if ambiguous_localities and (state_hits or district_hits):
                hits.extend(ambiguous_localities)

            if hits:
                matched_terms.extend(hits)
                matched_fields.append(field.name)
                districts.extend(district_hits)
                if field.name in self.accept_fields:
                    strong_hit = True

        assessment.matched_terms = _dedupe_ci(matched_terms)
        assessment.matched_fields = _dedupe_ci(matched_fields)
        assessment.districts = _title(_dedupe_ci(districts))

        blob = " ".join(f.text for f in fields).lower()
        assessment.pan_india = any(marker in blob for marker in PAN_INDIA_MARKERS)

        org_hit = bool(self._org_re.search(blob))

        if strong_hit:
            assessment.is_jharkhand = True
            assessment.confidence = Confidence.CONFIRMED
            assessment.reason = (
                f"Jharkhand found in {', '.join(assessment.matched_fields)}: "
                f"{', '.join(assessment.matched_terms[:5])}"
            )
        elif assessment.matched_terms:
            assessment.is_jharkhand = True
            assessment.confidence = Confidence.INFERRED
            assessment.reason = (
                "Jharkhand mentioned, but not in a delivery/service/buyer-address field "
                f"({', '.join(assessment.matched_fields)}) — confirm the actual work location"
            )
        elif assessment.pan_india and self.keep_pan_india:
            assessment.is_jharkhand = True
            assessment.confidence = Confidence.UNCLEAR
            assessment.reason = (
                "Pan-India / multi-state tender: Jharkhand is likely in scope but is "
                "not named — confirm the consignee list before bidding"
            )
        elif org_hit:
            assessment.is_jharkhand = True
            assessment.confidence = Confidence.UNCLEAR
            assessment.reason = (
                "Buyer is an organisation that operates mainly in Jharkhand, but no "
                "Jharkhand location is stated — confirm the delivery location"
            )
        else:
            assessment.is_jharkhand = False
            assessment.confidence = Confidence.CONFIRMED
            other = [s for s in OTHER_STATES if s in blob]
            assessment.reason = (
                f"No Jharkhand reference; locations point to {', '.join(other[:3])}"
                if other else "No Jharkhand reference found in any location field"
            )
        return assessment

    @staticmethod
    def _fields(tender: Tender, document_text: str) -> list[_Field]:
        return [
            _Field("delivery_location", clean(tender.delivery_location)),
            _Field("service_location", clean(tender.service_location)),
            _Field("installation_location", clean(tender.installation_location)),
            _Field("consignee_locations", clean(" ".join(tender.consignee_locations))),
            _Field("buyer_address", clean(tender.buyer_address)),
            _Field("buyer_organization",
                   clean(f"{tender.buyer_organization} {tender.department} "
                         f"{tender.ministry} {tender.office}")),
            _Field("title", clean(tender.title)),
            _Field("listing_text", clean(tender.raw_fields.get("listing_text", ""))),
            _Field("documents", clean(document_text[:200_000])),
        ]


def _dedupe_ci(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(value.strip())
    return out


def _title(values: list[str]) -> list[str]:
    return [v.title() for v in values]
