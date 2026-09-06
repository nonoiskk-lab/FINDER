"""The six-point verification gate.

Nothing reaches the daily report without passing every check:

    1. Official GeM source  — canonical URL on an allow-listed GeM host
    2. Active tender        — not withdrawn/cancelled, closing date in the future
    3. Relevant IT category — classified as an IT product or service
    4. Jharkhand relevance  — delivery/service/installation/buyer location
    5. Valid closing date   — a real, parseable date and time
    6. Within the window    — closing in ``deadline.max_days_remaining`` or fewer

Failures are counted by reason so the run manifest can explain exactly why a
day looks thin — "we found 180 bids and 178 were outside Jharkhand" is useful;
"0 tenders" is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

from gem_intel.config import Settings
from gem_intel.models import Tender, Urgency


class Rejection(str, Enum):
    NOT_OFFICIAL_SOURCE = "not_official_gem_source"
    NOT_ACTIVE = "not_active"
    NOT_IT_RELATED = "not_it_related"
    NOT_JHARKHAND = "not_jharkhand"
    NO_CLOSING_DATE = "no_closing_date"
    EXPIRED = "expired"
    OUTSIDE_WINDOW = "outside_10_day_window"


HUMAN_REASONS = {
    Rejection.NOT_OFFICIAL_SOURCE: "no verifiable official GeM link",
    Rejection.NOT_ACTIVE: "tender is cancelled, withdrawn or not open",
    Rejection.NOT_IT_RELATED: "not an IT product or IT service",
    Rejection.NOT_JHARKHAND: "no Jharkhand delivery / service location",
    Rejection.NO_CLOSING_DATE: "closing date could not be verified",
    Rejection.EXPIRED: "bid submission has already closed",
    Rejection.OUTSIDE_WINDOW: "closes later than the 10-day window",
}

CANCELLED_MARKERS = ("cancelled", "canceled", "withdrawn", "bid cancelled",
                     "tender cancelled", "closed", "retendered")


@dataclass
class VerificationResult:
    accepted: list[Tender] = field(default_factory=list)
    watchlist: list[Tender] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    rejected_tenders: list[Tender] = field(default_factory=list)

    def reject(self, tender: Tender, reason: Rejection) -> None:
        tender.rejected_reason = reason.value
        self.rejected[reason.value] = self.rejected.get(reason.value, 0) + 1
        self.rejected_tenders.append(tender)

    def summary_line(self) -> str:
        if not self.rejected:
            return "No candidates were rejected."
        parts = [
            f"{count} {HUMAN_REASONS.get(Rejection(reason), reason)}"
            for reason, count in sorted(self.rejected.items(),
                                        key=lambda kv: -kv[1])
        ]
        return "Filtered out: " + "; ".join(parts) + "."


class VerificationGate:
    def __init__(self, settings: Settings) -> None:
        self.allowed_hosts = settings.allowed_hosts
        self.max_days = settings.max_days_remaining
        self.watchlist_max = int(settings.get("deadline.watchlist_max_days_remaining", 60))
        self.watchlist_min_score = float(settings.get("deadline.watchlist_min_score", 70))

    # ------------------------------------------------------------------
    def verify(self, tenders: list[Tender]) -> VerificationResult:
        result = VerificationResult()
        for tender in tenders:
            reason = self._first_failure(tender)
            if reason is not None:
                if (reason is Rejection.OUTSIDE_WINDOW
                        and self._qualifies_for_watchlist(tender)):
                    tender.urgency = Urgency.WATCHLIST
                    result.watchlist.append(tender)
                    continue
                result.reject(tender, reason)
                continue
            result.accepted.append(tender)
        return result

    def _first_failure(self, tender: Tender) -> Rejection | None:
        if not self.is_official_source(tender):
            return Rejection.NOT_OFFICIAL_SOURCE
        if not self.is_active(tender):
            return Rejection.NOT_ACTIVE
        if not tender.classification.is_it_related:
            return Rejection.NOT_IT_RELATED
        if not tender.geo.is_jharkhand:
            return Rejection.NOT_JHARKHAND
        if tender.bid_end_at is None or tender.days_remaining is None:
            return Rejection.NO_CLOSING_DATE
        if tender.days_remaining < 0:
            return Rejection.EXPIRED
        if tender.days_remaining > self.max_days:
            return Rejection.OUTSIDE_WINDOW
        return None

    # ------------------------------------------------------------------
    def is_official_source(self, tender: Tender) -> bool:
        """Rule 1. A tender with no verifiable GeM URL never enters the report."""
        if not tender.source_url:
            return False
        host = (urlparse(tender.source_url).hostname or "").lower()
        if not host:
            return False
        tender.source_host = host
        return host in self.allowed_hosts or any(
            host.endswith("." + allowed) for allowed in self.allowed_hosts
        )

    @staticmethod
    def is_active(tender: Tender) -> bool:
        status = " ".join([
            str(tender.raw_fields.get("detail.status", "")),
            str(tender.raw_fields.get("listing_text", ""))[:400],
        ]).lower()
        return not any(marker in status for marker in CANCELLED_MARKERS)

    def _qualifies_for_watchlist(self, tender: Tender) -> bool:
        days = tender.days_remaining
        return (
            days is not None
            and self.max_days < days <= self.watchlist_max
            and tender.score.total >= self.watchlist_min_score
        )
