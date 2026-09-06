"""Is this an IT tender, and is it supply or service?

Deliberately more than keyword matching:

* Signals are matched on word boundaries, so "amc" does not fire on "dynamics".
* Negative signals veto — "AMC of lift" and "printing of books" are the two
  biggest sources of false positives on GeM and both are vetoed here.
* Product vs service is decided from the *shape* of the requirement (supply
  verbs vs service verbs), not from which keyword list matched.
* The result carries the matched signals, so the report can show its working
  and an LLM pass can be asked to overturn a borderline call with evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gem_intel.config import Settings
from gem_intel.extract.text import clean, normalize_key
from gem_intel.models import ClassificationResult, Confidence, Tender, TenderKind

SUPPLY_VERBS = (
    "supply", "supplying", "purchase", "procurement", "provision of",
    "buying", "delivery of", "supply of",
)
SERVICE_VERBS = (
    "amc", "annual maintenance", "maintenance", "repair", "servicing",
    "installation", "commissioning", "support", "outsourcing", "hiring",
    "manpower", "operation and maintenance", "o&m", "rate contract for service",
    "comprehensive maintenance",
)
# Categories that superficially look like IT but are not our business.
HARD_VETOES = (
    "medical oxygen", "ambulance", "x-ray", "ct scan", "ultrasound",
    "furniture", "stationery item", "uniform", "civil work", "construction of",
    "road work", "vehicle hiring", "housekeeping", "catering",
)


def _boundary(signal: str) -> re.Pattern[str]:
    escaped = re.escape(signal.lower())
    # Word-boundary at both ends, tolerant of the hyphen/space variants that
    # government listings use interchangeably ("wi-fi" / "wi fi").
    escaped = escaped.replace(r"\-", r"[\s\-]?").replace(r"\ ", r"\s+")
    return re.compile(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", re.IGNORECASE)


@dataclass
class _CompiledGroup:
    key: str
    label: str
    kind: str
    signals: list[tuple[str, re.Pattern[str]]]
    negatives: list[tuple[str, re.Pattern[str]]]


class ItClassifier:
    def __init__(self, settings: Settings) -> None:
        self.groups = [
            _CompiledGroup(
                key=key,
                label=group.get("label", key),
                kind=group.get("_kind", "product"),
                signals=[(s, _boundary(s)) for s in group.get("signals", []) or []],
                negatives=[(s, _boundary(s)) for s in group.get("negative_signals", []) or []],
            )
            for key, group in settings.all_groups.items()
        ]
        self.hard_vetoes = [(v, _boundary(v)) for v in HARD_VETOES]

    # ------------------------------------------------------------------
    def classify(self, tender: Tender, document_text: str = "") -> ClassificationResult:
        haystack = self._haystack(tender, document_text)
        result = ClassificationResult()

        for phrase, pattern in self.hard_vetoes:
            if pattern.search(haystack):
                result.vetoed_by.append(phrase)

        matched_labels: list[str] = []
        matched_signals: list[str] = []
        vetoed_groups: list[str] = []

        for group in self.groups:
            hits = [s for s, pattern in group.signals if pattern.search(haystack)]
            if not hits:
                continue
            negatives = [s for s, pattern in group.negatives if pattern.search(haystack)]
            # A negative only vetoes when it outweighs the positives — a bid
            # for "laptops and computer tables" is still a laptop bid.
            if negatives and len(negatives) >= len(hits):
                vetoed_groups.append(f"{group.label} ({', '.join(negatives)})")
                continue
            matched_labels.append(group.label)
            matched_signals.extend(hits)

        result.categories = _dedupe(matched_labels)
        result.matched_signals = _dedupe(matched_signals)
        result.vetoed_by.extend(vetoed_groups)

        if not result.categories:
            result.is_it_related = False
            result.confidence = (Confidence.CONFIRMED if result.vetoed_by
                                else Confidence.NOT_FOUND)
            result.reason = (
                f"No IT signal matched; vetoed by {', '.join(result.vetoed_by)}"
                if result.vetoed_by
                else "No IT product or service signal found in title, category or documents"
            )
            return result

        result.is_it_related = True
        result.kind = self._kind(tender, haystack, matched_labels)
        # Title/category hits are strong; a hit found only deep in a PDF is not.
        headline = normalize_key(f"{tender.title} {tender.category} {tender.item_description}")
        in_headline = any(_boundary(s).search(headline) for s in result.matched_signals)
        result.confidence = Confidence.CONFIRMED if in_headline else Confidence.INFERRED
        where = "title/category" if in_headline else "tender documents"
        result.reason = (
            f"Matched {', '.join(result.categories)} on {where} "
            f"via: {', '.join(result.matched_signals[:6])}"
        )
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _haystack(tender: Tender, document_text: str) -> str:
        parts = [
            tender.title, tender.category, tender.item_description,
            tender.buyer_organization, tender.department,
            tender.raw_fields.get("listing_text", ""),
            document_text[:400_000],
        ]
        return clean(" \n ".join(p for p in parts if p)).lower()

    def _kind(self, tender: Tender, haystack: str, labels: list[str]) -> TenderKind:
        """Supply, service, or both — decided from the title, not the body.

        The title is what the buyer chose to call the bid, so it settles the
        question on its own whenever it names a verb. Presence beats counting:
        a "Supply, Installation and Commissioning" bid is genuinely both, and
        tallying its three service words against one supply word would
        mislabel it as a pure service contract.
        """
        head = normalize_key(f"{tender.title} {tender.category} {tender.item_description}")
        head_supply = any(_boundary(v).search(head) for v in SUPPLY_VERBS)
        head_service = any(_boundary(v).search(head) for v in SERVICE_VERBS)

        if head_supply and head_service:
            return TenderKind.SUPPLY_AND_SERVICE
        if head_supply:
            return TenderKind.PRODUCT_SUPPLY
        if head_service:
            return TenderKind.SERVICE

        # Nothing in the title: fall back to the body, then to which taxonomy
        # groups matched at all.
        body_supply = sum(1 for v in SUPPLY_VERBS if _boundary(v).search(haystack))
        body_service = sum(1 for v in SERVICE_VERBS if _boundary(v).search(haystack))
        if body_service > body_supply:
            return TenderKind.SERVICE
        if body_supply > body_service:
            return TenderKind.PRODUCT_SUPPLY

        service_group_hit = any(g.label in labels and g.kind == "service"
                                for g in self.groups)
        product_group_hit = any(g.label in labels and g.kind == "product"
                                for g in self.groups)
        if service_group_hit and not product_group_hit:
            return TenderKind.SERVICE
        if product_group_hit and not service_group_hit:
            return TenderKind.PRODUCT_SUPPLY
        return TenderKind.UNKNOWN


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out
