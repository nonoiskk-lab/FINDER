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


class CompositeSource(SourceAdapter):
    """Fans a search out across several discovery adapters and merges them.

    Each adapter only needs to discover candidate tenders in its own way
    (the portal's own search form, a Google Search query, ...); the same
    underlying GeM detail page still gets fetched and parsed identically
    afterwards. Merging happens here, deduplicated by
    :attr:`Tender.identity_key`, so the same bid found by two adapters is
    reported once — with both adapters' search terms recorded against it.

    ``fetch_detail`` is routed back to whichever adapter actually discovered
    the tender, remembered internally by identity key (never on the tender
    itself — a live adapter object is not JSON-serialisable and must never
    end up in ``Tender.raw_fields``, which gets persisted to the database).
    """

    name = "Composite (multiple discovery adapters)"

    def __init__(self, adapters: list[SourceAdapter]) -> None:
        if not adapters:
            raise ValueError("CompositeSource needs at least one adapter")
        self.adapters = adapters
        self._owner: dict[str, SourceAdapter] = {}

    def search(self, queries: Iterable[str]) -> SourceResult:
        query_list = list(queries)
        combined = SourceResult()
        seen: dict[str, Tender] = {}

        for adapter in self.adapters:
            result = adapter.search(query_list)
            combined.queries_executed.extend(result.queries_executed)
            combined.listings_seen += result.listings_seen
            combined.access_issues.extend(result.access_issues)
            if result.aborted:
                combined.aborted = True
                combined.abort_reason = combined.abort_reason or (
                    f"{adapter.name}: {result.abort_reason}"
                )
            for tender in result.tenders:
                key = tender.identity_key
                self._owner.setdefault(key, adapter)
                existing = seen.get(key)
                if existing is None:
                    seen[key] = tender
                    continue
                # Same bid found twice: keep the first copy, but remember
                # every query/adapter that surfaced it.
                existing.raw_fields.setdefault("matched_queries", []).extend(
                    tender.raw_fields.get("matched_queries", [])
                )
                sources = existing.raw_fields.setdefault("discovered_via", [])
                new_source = tender.raw_fields.get("discovered_via", adapter.name)
                if isinstance(sources, str):
                    sources = [sources]
                    existing.raw_fields["discovered_via"] = sources
                if new_source not in sources:
                    sources.append(new_source)

        combined.tenders = list(seen.values())
        return combined

    def fetch_detail(self, tender: Tender) -> Tender:
        adapter = self._owner.get(tender.identity_key, self.adapters[0])
        return adapter.fetch_detail(tender)
