"""Required documents, eligibility conditions and technical requirements.

The brief is explicit: *do not assume documents are required*. So this module
only ever emits a document when a sentence in a verified GeM source names it,
and it keeps the sentence as evidence. Anything hedged in the source
("if applicable", "wherever required") is emitted with UNCLEAR confidence,
which the report renders as "⚠ Verification Required".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from gem_intel.extract.text import clean
from gem_intel.models import Confidence, Evidence, RequiredDocument, Tender

# Hedging words that downgrade a hit from CONFIRMED to UNCLEAR.
HEDGES = re.compile(
    r"\b(if\s+(?:any|applicable|required)|wherever\s+applicable|as\s+applicable|"
    r"may\s+be\s+required|optional|preferabl[ey]|desirable)\b",
    re.IGNORECASE,
)
# Sentences that *ask for* a document. Without one of these nearby, a mere
# mention of "PAN" is not a requirement.
DEMAND = re.compile(
    r"\b(shall\s+(?:be\s+)?(?:submit|upload|furnish|provide|attach|enclose)|"
    r"must\s+(?:submit|upload|furnish|provide|attach|be\s+uploaded|be\s+submitted)|"
    r"to\s+be\s+(?:submitted|uploaded|furnished|enclosed|attached)|"
    r"required\s+to\s+(?:submit|upload|furnish)|"
    r"documents?\s+required|required\s+documents?|"
    r"bidder\s+(?:shall|must|should|has\s+to)|"
    r"upload(?:ed)?|submit(?:ted)?|furnish(?:ed)?|enclose(?:d)?|attach(?:ed)?|"
    r"copy\s+of|certificate\s+of|proof\s+of|scanned\s+cop(?:y|ies))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DocumentPattern:
    canonical: str
    display: str
    pattern: re.Pattern[str]


def _p(canonical: str, display: str, regex: str) -> DocumentPattern:
    return DocumentPattern(canonical, display, re.compile(regex, re.IGNORECASE))


DOCUMENT_PATTERNS: tuple[DocumentPattern, ...] = (
    _p("pan", "PAN Card", r"\bpan\s*(?:card|number|no\.?)?\b(?!\s*india)"),
    _p("gst", "GST Registration Certificate",
       r"\bgst(?:in)?\b|goods\s+and\s+services?\s+tax\s+registration"),
    _p("msme", "MSME / Udyam Registration Certificate",
       r"\b(?:msme|mse)\s*(?:certificate|registration)?\b|\budyam\b|\bnsic\b"),
    _p("itr", "Income Tax Returns (ITR)",
       r"\bitr\b|income\s+tax\s+returns?"),
    _p("balance_sheet", "Audited Balance Sheet / Financial Statements",
       r"audited\s+(?:balance\s+sheet|financial\s+statement)|balance\s+sheet"),
    _p("turnover", "Turnover Certificate (CA certified)",
       r"turnover\s+certificate|certificate\s+of\s+turnover|annual\s+turnover"),
    _p("experience", "Experience Certificate",
       r"experience\s+certificate|proof\s+of\s+experience|past\s+experience"),
    _p("work_order", "Work Order / Purchase Order copies",
       r"\bwork\s+orders?\b|\bpurchase\s+orders?\b|\bsupply\s+orders?\b"),
    _p("completion_certificate", "Completion / Satisfactory Performance Certificate",
       r"completion\s+certificate|satisfactory\s+(?:performance|completion)"),
    _p("oem_authorization", "OEM Authorization Letter / MAF",
       r"\boem\s+(?:authori[sz]ation|authori[sz]ed)|manufacturer'?s?\s+authori[sz]ation|"
       r"\bmaf\b|authori[sz]ation\s+certificate"),
    _p("iso", "ISO Certificate",
       r"\biso\s*[-:]?\s*\d{4,5}\b|\biso\s+certificat"),
    _p("datasheet", "Technical Datasheet / Compliance Sheet",
       r"technical\s+(?:datasheet|data\s+sheet|literature|compliance)"),
    _p("catalogue", "Product Catalogue / Brochure",
       r"\bcatalogue\b|\bcatalog\b|\bbrochure\b|product\s+literature"),
    _p("company_registration", "Company / Firm Registration Certificate",
       r"(?:company|firm|shop\s+and\s+establishment|partnership|incorporation)\s+"
       r"registration|certificate\s+of\s+incorporation|\bgumasta\b"),
    _p("bank_details", "Bank Account Details / Cancelled Cheque",
       r"cancelled\s+che(?:que|ck)|bank\s+(?:account\s+)?details|bank\s+mandate"),
    _p("dsc", "Digital Signature Certificate (DSC)",
       r"digital\s+signature|(?<![a-z])dsc(?![a-z])"),
    _p("affidavit", "Affidavit", r"\baffidavit\b"),
    _p("undertaking", "Undertaking", r"\bundertaking\b"),
    _p("declaration", "Declaration",
       r"\bdeclaration\b|self[-\s]?declaration"),
    _p("compliance_certificate", "Compliance Certificate",
       r"compliance\s+certificate|certificate\s+of\s+compliance"),
    _p("blacklisting", "Non-blacklisting / Non-debarment Certificate",
       r"black\s?list|debarr?ed|non[-\s]?blacklisting"),
    _p("land_border", "Land Border Sharing Declaration (Order (Public Procurement) 2017)",
       r"land\s+border|order\s*\(public\s+procurement.{0,20}2017"),
    _p("make_in_india", "Make in India / Local Content Certificate",
       r"make\s+in\s+india|local\s+content|class[-\s]?i\s+local\s+supplier"),
    _p("epf_esic", "EPF / ESIC Registration",
       r"\bepf\b|\besic\b|provident\s+fund\s+registration"),
    _p("labour_licence", "Labour Licence",
       r"labour\s+licen[cs]e|contract\s+labour\s+registration"),
    _p("power_of_attorney", "Power of Attorney / Authorised Signatory Letter",
       r"power\s+of\s+attorney|authori[sz]ed\s+signator"),
    _p("tender_fee", "Tender Fee / Processing Fee Receipt",
       r"tender\s+(?:fee|document\s+fee)|processing\s+fee"),
)

ELIGIBILITY_CUES = (
    re.compile(r"eligibilit(?:y|ies)\s+(?:criteria|conditions?|requirements?)", re.IGNORECASE),
    re.compile(r"pre[-\s]?qualification", re.IGNORECASE),
    re.compile(r"qualification\s+(?:criteria|requirements?)", re.IGNORECASE),
    re.compile(r"minimum\s+(?:average\s+)?annual\s+turnover", re.IGNORECASE),
    re.compile(r"years?\s+of\s+(?:past\s+)?experience", re.IGNORECASE),
    re.compile(r"similar\s+(?:work|supply|nature)", re.IGNORECASE),
    re.compile(r"bidder\s+(?:should|shall|must)\s+(?:have|be)", re.IGNORECASE),
)
TECHNICAL_CUES = (
    re.compile(r"technical\s+specification", re.IGNORECASE),
    re.compile(r"\b(?:processor|ram|ssd|hdd|display|resolution|warranty|"
               r"operating\s+system|form\s+factor|ports?|throughput|"
               r"megapixel|lumens|ppm|dpi|kva|va\b)", re.IGNORECASE),
    re.compile(r"\bmake\s+and\s+model\b|\bbrand\b", re.IGNORECASE),
)
DELIVERY_CUES = (
    re.compile(r"delivery\s+(?:period|schedule|time|days|within)", re.IGNORECASE),
    re.compile(r"installation\s+(?:and\s+commissioning|shall|within)", re.IGNORECASE),
    re.compile(r"\bfor\s+delivery\s+at\b|\bdelivery\s+location\b", re.IGNORECASE),
)
WARRANTY_CUES = (
    re.compile(r"\bwarrant(?:y|ee)\b[^.\n]{0,120}", re.IGNORECASE),
    re.compile(r"\bguarantee\s+period\b", re.IGNORECASE),
)
AMC_CUES = (
    re.compile(r"\b(?:c?amc|annual\s+maintenance)\b[^.\n]{0,140}", re.IGNORECASE),
    re.compile(r"post[-\s]?warranty\s+(?:support|maintenance)", re.IGNORECASE),
)
PAYMENT_CUES = (
    re.compile(r"payment\s+(?:terms?|shall\s+be|will\s+be)[^.\n]{0,200}", re.IGNORECASE),
    re.compile(r"\b\d{1,3}\s*%\s+(?:on|after)\s+(?:delivery|installation|"
               r"successful)[^.\n]{0,120}", re.IGNORECASE),
)
SPECIAL_CUES = (
    re.compile(r"buyer\s+added\s+(?:bid\s+specific\s+)?terms?[^.\n]{0,200}", re.IGNORECASE),
    re.compile(r"penalt(?:y|ies)[^.\n]{0,160}", re.IGNORECASE),
    re.compile(r"liquidated\s+damages[^.\n]{0,160}", re.IGNORECASE),
    re.compile(r"past\s+performance[^.\n]{0,120}", re.IGNORECASE),
    re.compile(r"reverse\s+auction[^.\n]{0,120}", re.IGNORECASE),
)

SENTENCE_SPLIT = re.compile(r"(?<=[.;:\n])\s+")
MAX_ITEMS = 12


class RequirementsExtractor:
    """Pulls requirement lists out of tender text, always with evidence."""

    def extract(self, tender: Tender, document_text: str, source_url: str) -> Tender:
        text = clean(document_text)
        if not text:
            if not tender.required_documents:
                tender.flag(
                    "Required-documents list is unavailable: no tender document text "
                    "could be read. Open the bid on GeM to confirm what to upload."
                )
            return tender

        sentences = [s for s in SENTENCE_SPLIT.split(text) if 15 <= len(s) <= 600]

        tender.required_documents = self._documents(sentences, source_url)
        tender.eligibility_requirements = self._collect(sentences, ELIGIBILITY_CUES)
        tender.technical_requirements = self._collect(sentences, TECHNICAL_CUES)
        tender.delivery_requirements = self._collect(sentences, DELIVERY_CUES)
        tender.warranty_requirements = self._collect(sentences, WARRANTY_CUES)
        tender.amc_requirements = self._collect(sentences, AMC_CUES)
        tender.special_conditions = self._collect(sentences, SPECIAL_CUES)

        payment = self._collect(sentences, PAYMENT_CUES, limit=2)
        if payment:
            tender.payment_terms.value = payment[0]
            tender.payment_terms.confidence = Confidence.CONFIRMED
            tender.payment_terms.evidence = [
                Evidence(source_url, "tender documents", payment[0][:240], "rule:payment")
            ]

        if not tender.required_documents:
            tender.flag(
                "No specific document requirements were stated in the text we could "
                "read — check the bid's 'Documents Required' section on GeM."
            )
        return tender

    # ------------------------------------------------------------------
    def _documents(self, sentences: list[str], source_url: str) -> list[RequiredDocument]:
        found: dict[str, RequiredDocument] = {}
        for sentence in sentences:
            if not DEMAND.search(sentence):
                continue
            hedged = bool(HEDGES.search(sentence))
            for spec in DOCUMENT_PATTERNS:
                match = spec.pattern.search(sentence)
                if not match:
                    continue
                confidence = Confidence.UNCLEAR if hedged else Confidence.CONFIRMED
                evidence = Evidence(
                    source_url=source_url, locator="tender documents",
                    quote=clean(sentence)[:300], extractor="rule:required_documents",
                )
                existing = found.get(spec.canonical)
                if existing is None:
                    found[spec.canonical] = RequiredDocument(
                        name=spec.display, canonical=spec.canonical,
                        confidence=confidence, evidence=[evidence],
                    )
                elif (existing.confidence is Confidence.UNCLEAR
                      and confidence is Confidence.CONFIRMED):
                    # A firm statement anywhere beats a hedged one.
                    existing.confidence = Confidence.CONFIRMED
                    existing.evidence.insert(0, evidence)
        return sorted(found.values(),
                      key=lambda d: (d.confidence is Confidence.UNCLEAR, d.name))

    @staticmethod
    def _collect(sentences: list[str], cues: tuple[re.Pattern[str], ...],
                 limit: int = MAX_ITEMS) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for sentence in sentences:
            if not any(cue.search(sentence) for cue in cues):
                continue
            cleaned = clean(sentence)
            key = cleaned.lower()[:120]
            if key in seen:
                continue
            seen.add(key)
            out.append(cleaned[:400])
            if len(out) >= limit:
                break
        return out
