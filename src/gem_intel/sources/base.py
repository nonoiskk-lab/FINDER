"""Source adapter contract.

A source adapter's only job is to return *verified-origin* raw tenders.
It must not analyse, score or filter on business grounds — that belongs to
the analyze/ package. It must guarantee, however, that every tender it emits
carries a canonical URL on an allow-listed official GeM host.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass, field

from gem_intel.models import AccessIssue, Tender


@dataclass
class SourceResult:
    tenders: list[Tender] = field(default_factory=list)
    queries_executed: list[str] = field(default_factory=list)
    listings_seen: int = 0
    details_fetched: int = 0
    access_issues: list[AccessIssue] = field(default_factory=list)
    aborted: bool = False
    abort_reason: str = ""

    def extend(self, other: SourceResult) -> None:
        self.tenders.extend(other.tenders)
        self.queries_executed.extend(other.queries_executed)
        self.listings_seen += other.listings_seen
        self.details_fetched += other.details_fetched
        self.access_issues.extend(other.access_issues)
        if other.aborted:
            self.aborted = True
            self.abort_reason = self.abort_reason or other.abort_reason


class SourceAdapter(ABC):
    """Base class for every tender source."""

    #: Human-readable name used in logs and the report's source section.
    name: str = "unnamed"

    #: Hosts this adapter is allowed to touch. Checked against settings.
    hosts: tuple[str, ...] = ()

    @abstractmethod
    def search(self, queries: Iterable[str]) -> SourceResult:
        """Run the given search terms and return candidate tenders."""

    @abstractmethod
    def fetch_detail(self, tender: Tender) -> Tender:
        """Enrich a tender with its full detail page and document list."""
