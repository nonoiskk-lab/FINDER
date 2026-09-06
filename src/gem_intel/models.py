"""Domain models.

Design rule that governs this whole file: **absence of information is a
first-class value.** A field that is ``None`` means "the verified source did
not tell us", never "no" and never "zero". Anywhere a downstream consumer
might otherwise assume a default, the model carries an explicit
``Confidence`` and an ``Evidence`` trail so the report can print
"⚠ Verification Required" instead of inventing an answer.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Confidence(str, Enum):
    """How well-supported a single extracted claim is."""

    CONFIRMED = "confirmed"      # stated explicitly in a verified GeM source
    INFERRED = "inferred"        # follows unambiguously from stated facts
    UNCLEAR = "unclear"          # source is ambiguous -> needs a human
    NOT_FOUND = "not_found"      # source is silent


class TenderKind(str, Enum):
    PRODUCT_SUPPLY = "product_supply"
    SERVICE = "service"
    SUPPLY_AND_SERVICE = "supply_and_service"
    UNKNOWN = "unknown"


class Urgency(str, Enum):
    URGENT = "URGENT"                    # 0-2 days
    HIGH_PRIORITY = "HIGH PRIORITY"      # 3-5 days
    ACTION_REQUIRED = "ACTION REQUIRED"  # 6-10 days
    WATCHLIST = "WATCHLIST"              # > 10 days, high value
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class MatchLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class TernaryFlag(str, Enum):
    """Required / not required / the source did not say.

    Deliberately three-valued. The system is forbidden from collapsing
    ``NOT_STATED`` into ``NOT_REQUIRED``.
    """

    REQUIRED = "required"
    NOT_REQUIRED = "not_required"
    NOT_STATED = "not_stated"


@dataclass(frozen=True)
class Evidence:
    """Where a claim came from, so a human can re-check it in seconds."""

    source_url: str
    locator: str = ""       # e.g. "bid document, page 4" or "listing card"
    quote: str = ""         # verbatim snippet, trimmed
    extractor: str = ""     # "rule:emd_regex" | "llm:claude" | "portal_field"

    def short(self) -> str:
        bits = [b for b in (self.locator, self.quote) if b]
        return " — ".join(bits) if bits else self.source_url


@dataclass
class Claim:
    """A single extracted value plus its provenance."""

    value: Any = None
    confidence: Confidence = Confidence.NOT_FOUND
    evidence: list[Evidence] = field(default_factory=list)
    note: str = ""

    @property
    def known(self) -> bool:
        return self.value is not None and self.confidence in (
            Confidence.CONFIRMED,
            Confidence.INFERRED,
        )

    @property
    def needs_verification(self) -> bool:
        return self.confidence in (Confidence.UNCLEAR, Confidence.NOT_FOUND)

    def render(self, unknown: str = "⚠ Verification Required") -> str:
        if not self.known:
            return unknown
        return str(self.value)


@dataclass
class MoneyClaim(Claim):
    """A Claim whose value is an amount in INR."""

    currency: str = "INR"

    def render(self, unknown: str = "⚠ Verification Required") -> str:
        if not self.known:
            return unknown
        try:
            return f"₹{float(self.value):,.0f}"
        except (TypeError, ValueError):
            return str(self.value)


@dataclass
class TenderDocument:
    """A file attached to the bid on the GeM portal."""

    name: str
    url: str
    kind: str = "unknown"          # bid_document | atc | technical_spec | corrigendum
    content_type: str = ""
    bytes_downloaded: int = 0
    sha256: str = ""
    local_path: str = ""
    text_chars: int = 0
    extraction_method: str = ""    # pdf_text | pdf_ocr | docx | xlsx | failed
    error: str = ""

    @property
    def extracted(self) -> bool:
        return self.text_chars > 0


@dataclass
class EMDAnalysis:
    status: TernaryFlag = TernaryFlag.NOT_STATED
    amount: MoneyClaim = field(default_factory=MoneyClaim)
    payment_mode: Claim = field(default_factory=Claim)
    last_date: Claim = field(default_factory=Claim)
    exemption_available: TernaryFlag = TernaryFlag.NOT_STATED
    msme_exemption: TernaryFlag = TernaryFlag.NOT_STATED
    startup_exemption: TernaryFlag = TernaryFlag.NOT_STATED
    exemption_proof_required: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def headline(self) -> str:
        if self.status is TernaryFlag.NOT_REQUIRED:
            return "✅ NO EMD REQUIRED"
        if self.status is TernaryFlag.REQUIRED:
            return "💰 EMD REQUIRED"
        return "⚠ EMD INFORMATION NOT CLEAR – MANUAL VERIFICATION REQUIRED"


@dataclass
class SecurityAnalysis:
    """Performance Security / ePBG / Security Deposit."""

    performance_security: TernaryFlag = TernaryFlag.NOT_STATED
    epbg: TernaryFlag = TernaryFlag.NOT_STATED
    security_deposit: TernaryFlag = TernaryFlag.NOT_STATED
    percentage: Claim = field(default_factory=Claim)
    amount: MoneyClaim = field(default_factory=MoneyClaim)
    validity: Claim = field(default_factory=Claim)          # e.g. "60 days beyond contract"
    submission_timeline: Claim = field(default_factory=Claim)
    exemptions: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)

    def headline(self) -> str:
        """Required / Not Required / ⚠ Verification Required.

        On GeM the ePBG percentage cell *is* the performance-security field,
        so an explicit 0% there is a real "not required" — treating it as
        unknown would bury a genuine answer under a warning. Only total
        silence across all three instruments yields the warning.
        """
        flags = (self.performance_security, self.epbg, self.security_deposit)
        if TernaryFlag.REQUIRED in flags:
            return "Required"
        if TernaryFlag.NOT_REQUIRED in flags:
            return "Not Required"
        return "⚠ Verification Required"

    @property
    def fully_stated(self) -> bool:
        """True only when every instrument was explicitly answered."""
        return all(
            f is not TernaryFlag.NOT_STATED
            for f in (self.performance_security, self.epbg, self.security_deposit)
        )


@dataclass
class RequiredDocument:
    name: str
    canonical: str                       # normalised key, used for de-duplication
    confidence: Confidence = Confidence.CONFIRMED
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def display(self) -> str:
        if self.confidence in (Confidence.UNCLEAR, Confidence.NOT_FOUND):
            return f"{self.name} ⚠ Verification Required"
        return self.name


@dataclass
class GeoAssessment:
    is_jharkhand: bool = False
    matched_terms: list[str] = field(default_factory=list)
    matched_fields: list[str] = field(default_factory=list)
    districts: list[str] = field(default_factory=list)
    pan_india: bool = False
    confidence: Confidence = Confidence.NOT_FOUND
    reason: str = ""


@dataclass
class ClassificationResult:
    is_it_related: bool = False
    kind: TenderKind = TenderKind.UNKNOWN
    categories: list[str] = field(default_factory=list)   # taxonomy labels
    matched_signals: list[str] = field(default_factory=list)
    vetoed_by: list[str] = field(default_factory=list)
    confidence: Confidence = Confidence.NOT_FOUND
    reason: str = ""


@dataclass
class ScoreComponent:
    name: str
    points: float
    max_points: float
    reason: str = ""

    @property
    def pct(self) -> float:
        return 0.0 if self.max_points == 0 else self.points / self.max_points


@dataclass
class OpportunityScore:
    components: list[ScoreComponent] = field(default_factory=list)
    total: float = 0.0
    band_label: str = ""
    band_emoji: str = ""
    rank_label: str = ""
    rank_emoji: str = ""

    def as_lines(self) -> list[str]:
        return [
            f"{c.name}: {c.points:.0f}/{c.max_points:.0f} — {c.reason}"
            for c in self.components
        ]


@dataclass
class EligibilityAssessment:
    match_level: MatchLevel = MatchLevel.UNKNOWN
    verdict: str = "Requires Verification"   # never "eligible"; see docs/REPORT_SPEC.md
    supporting_points: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    verification_points: list[str] = field(default_factory=list)

    EMOJI = {
        MatchLevel.HIGH: "🟢",
        MatchLevel.MEDIUM: "🟡",
        MatchLevel.LOW: "🔴",
        MatchLevel.UNKNOWN: "⚪",
    }

    @property
    def emoji(self) -> str:
        return self.EMOJI[self.match_level]


@dataclass
class Tender:
    """One GeM bid, from raw listing through full analysis."""

    # --- identity -------------------------------------------------------
    bid_number: str = ""
    gem_tender_id: str = ""
    source_url: str = ""
    source_host: str = ""

    # --- portal facts ---------------------------------------------------
    title: str = ""
    buyer_name: str = ""
    buyer_organization: str = ""
    ministry: str = ""
    department: str = ""
    office: str = ""
    category: str = ""
    item_description: str = ""
    quantity: float | None = None
    quantity_unit: str = ""
    delivery_location: str = ""
    service_location: str = ""
    installation_location: str = ""
    buyer_address: str = ""
    consignee_locations: list[str] = field(default_factory=list)

    published_at: datetime | None = None
    bid_start_at: datetime | None = None
    bid_end_at: datetime | None = None          # closing date+time
    bid_open_at: datetime | None = None

    estimated_value: MoneyClaim = field(default_factory=MoneyClaim)
    msme_exemption_portal_flag: TernaryFlag = TernaryFlag.NOT_STATED
    startup_exemption_portal_flag: TernaryFlag = TernaryFlag.NOT_STATED

    documents: list[TenderDocument] = field(default_factory=list)
    raw_fields: dict[str, Any] = field(default_factory=dict)

    # --- derived analysis ----------------------------------------------
    classification: ClassificationResult = field(default_factory=ClassificationResult)
    geo: GeoAssessment = field(default_factory=GeoAssessment)
    days_remaining: int | None = None
    urgency: Urgency = Urgency.UNKNOWN
    emd: EMDAnalysis = field(default_factory=EMDAnalysis)
    security: SecurityAnalysis = field(default_factory=SecurityAnalysis)
    required_documents: list[RequiredDocument] = field(default_factory=list)
    eligibility_requirements: list[str] = field(default_factory=list)
    technical_requirements: list[str] = field(default_factory=list)
    delivery_requirements: list[str] = field(default_factory=list)
    warranty_requirements: list[str] = field(default_factory=list)
    amc_requirements: list[str] = field(default_factory=list)
    payment_terms: Claim = field(default_factory=Claim)
    special_conditions: list[str] = field(default_factory=list)
    plain_summary: str = ""                    # "what does the buyer want?"
    why_it_matters: str = ""
    risks: list[str] = field(default_factory=list)
    next_action: str = ""

    eligibility: EligibilityAssessment = field(default_factory=EligibilityAssessment)
    score: OpportunityScore = field(default_factory=OpportunityScore)

    # --- bookkeeping ----------------------------------------------------
    first_seen_on: date | None = None
    last_seen_on: date | None = None
    analysis_mode: str = "rules+llm"          # or "rules-only" when LLM unavailable
    verification_flags: list[str] = field(default_factory=list)
    rejected_reason: str = ""

    # ------------------------------------------------------------------
    @property
    def identity_key(self) -> str:
        """Stable dedupe key.

        Bid number is authoritative when present; otherwise fall back to a
        hash of the canonical URL, then of title+buyer+closing time. Never
        falls back to title alone — different buyers reuse identical titles.
        """
        if self.bid_number:
            return f"bid:{self.bid_number.strip().upper()}"
        if self.gem_tender_id:
            return f"gem:{self.gem_tender_id.strip().upper()}"
        if self.source_url:
            return "url:" + hashlib.sha256(self.source_url.encode()).hexdigest()[:32]
        blob = f"{self.title}|{self.buyer_organization}|{self.bid_end_at}"
        return "fp:" + hashlib.sha256(blob.encode()).hexdigest()[:32]

    @property
    def has_document_text(self) -> bool:
        return any(d.extracted for d in self.documents)

    def flag(self, message: str) -> None:
        if message not in self.verification_flags:
            self.verification_flags.append(message)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(asdict(self))


@dataclass
class TenderChange:
    """One field-level difference against the previously stored version."""

    identity_key: str
    field_name: str
    old_value: str
    new_value: str
    detected_on: date
    note: str = ""

    def render(self) -> str:
        label = self.field_name.replace("_", " ")
        return f"⚠ {label} changed from {self.old_value} to {self.new_value}"


@dataclass
class AccessIssue:
    """Recorded whenever a verified source could not be reached.

    Its existence in a run is what allows the report to say "we could not
    check" instead of "there were no tenders".
    """

    occurred_at: datetime
    stage: str
    target: str
    error_type: str
    detail: str = ""
    attempts: int = 0

    def render(self) -> str:
        ts = self.occurred_at.strftime("%d %b %Y %H:%M %Z").strip()
        return f"⚠ GeM ACCESS ISSUE — {ts} — {self.error_type} at {self.stage} ({self.target})"


@dataclass
class RunManifest:
    """Everything needed to audit a single daily run."""

    run_id: str
    started_at: datetime
    finished_at: datetime | None = None
    timezone: str = "Asia/Kolkata"
    queries_executed: list[str] = field(default_factory=list)
    listings_seen: int = 0
    details_fetched: int = 0
    documents_downloaded: int = 0
    llm_calls: int = 0
    candidates: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)
    new_tenders: int = 0
    updated_tenders: int = 0
    access_issues: list[AccessIssue] = field(default_factory=list)
    degraded_modes: list[str] = field(default_factory=list)
    report_doc_url: str = ""
    report_local_path: str = ""

    @property
    def source_reachable(self) -> bool:
        return not any(i.stage == "listing" for i in self.access_issues)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1

    def degrade(self, mode: str) -> None:
        if mode not in self.degraded_modes:
            self.degraded_modes.append(mode)


@dataclass
class DailyReport:
    report_date: date
    manifest: RunManifest
    tenders: list[Tender] = field(default_factory=list)      # in-window, ranked
    watchlist: list[Tender] = field(default_factory=list)    # > 10 days, high value
    changes: list[TenderChange] = field(default_factory=list)

    # -- executive summary counters ------------------------------------
    @property
    def total_found(self) -> int:
        return len(self.tenders)

    @property
    def strong(self) -> int:
        return sum(1 for t in self.tenders if t.score.total >= 80)

    @property
    def good(self) -> int:
        return sum(1 for t in self.tenders if 60 <= t.score.total < 80)

    @property
    def urgent(self) -> int:
        return sum(1 for t in self.tenders if t.urgency is Urgency.URGENT)

    @property
    def with_emd(self) -> int:
        return sum(1 for t in self.tenders if t.emd.status is TernaryFlag.REQUIRED)

    @property
    def without_emd(self) -> int:
        return sum(1 for t in self.tenders if t.emd.status is TernaryFlag.NOT_REQUIRED)

    @property
    def emd_unclear(self) -> int:
        return sum(1 for t in self.tenders if t.emd.status is TernaryFlag.NOT_STATED)

    def by_urgency(self, urgency: Urgency) -> list[Tender]:
        return [t for t in self.tenders if t.urgency is urgency]


def _to_jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj
