"""Google Search as a supplementary tender-discovery adapter.

Why this exists
----------------
``GemBidPlusSource`` searches GeM's own search box, which is the most
complete source when it works — but its markup and its internal search API
can change without notice (see :class:`~gem_intel.extract.gem_html.ParserDrift`).
This adapter asks Google's Custom Search API for pages already indexed under
``site:bidplus.gem.gov.in`` / ``site:gem.gov.in`` instead, as a second,
independent way to find the same official pages. It is meant to run
*alongside* the direct adapter (see :class:`~gem_intel.sources.base.CompositeSource`),
not to replace it.

What this is NOT
-----------------
This is a *discovery* mechanism only — it only ever proposes candidate URLs.
It never treats Google's search-result title/snippet as a tender fact:

* Every candidate URL is checked against ``source.allowed_hosts`` before it
  becomes a :class:`Tender` at all (via ``GemHttpClient.assert_allowed_host``),
  exactly like every other source. A result pointing at a tender-aggregator
  site, however well it ranks, is silently dropped.
* Every fact that ends up in the report (buyer, dates, EMD, documents, ...)
  still comes from fetching and parsing the official GeM page itself, via
  the same :func:`~gem_intel.sources.detail_fetch.fetch_and_parse_detail`
  used by the direct adapter. The title/snippet only ever seed a
  placeholder ``title`` and give the first-pass IT/geography screen
  something to look at before the detail page is fetched.

Credentials
-----------
Needs a Google Cloud **Custom Search JSON API** key and a **Programmable
Search Engine** ID (``cx``) — see docs/GOOGLE_SETUP.md. This is a *different*
credential from the Drive/Docs/Sheets service account or OAuth token used
for report publishing; having one configured does not imply the other is.
Free tier is 100 queries/day, which is why ``daily_query_budget`` defaults
well under that.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

import requests

from gem_intel.config import Settings
from gem_intel.extract.gem_html import GemDetailParser, Selectors
from gem_intel.extract.text import clean
from gem_intel.http_client import DisallowedHost, GemHttpClient
from gem_intel.models import Tender
from gem_intel.observability import get_logger
from gem_intel.sources.base import SourceAdapter, SourceResult
from gem_intel.sources.detail_fetch import BudgetedDetailFetcher

log = get_logger(__name__)

BID_NUMBER_RE = re.compile(r"\bGEM/\d{4}/[A-Z]/\d+\b", re.IGNORECASE)
REQUEST_TIMEOUT_SECONDS = 20


class GoogleSearchGemSource(SourceAdapter):
    """Discovers candidate GeM bid pages via Google's Custom Search API."""

    name = "Google Search discovery (site:gem.gov.in)"

    def __init__(self, settings: Settings, client: GemHttpClient) -> None:
        self.settings = settings
        self.client = client

        cfg = settings.get("source.google_search", {}) or {}
        self.enabled = bool(cfg.get("enabled", False))
        self.api_key = os.getenv(cfg.get("api_key_env", "GOOGLE_SEARCH_API_KEY"), "").strip()
        self.cse_id = os.getenv(cfg.get("cse_id_env", "GOOGLE_SEARCH_CSE_ID"), "").strip()
        self.endpoint = cfg.get("endpoint", "https://www.googleapis.com/customsearch/v1")
        self.results_per_query = min(int(cfg.get("results_per_query", 10)), 10)
        self.daily_query_budget = int(cfg.get("daily_query_budget", 90))
        self.site_filters = list(cfg.get("site_filters", ["site:bidplus.gem.gov.in"]) or [])
        self.location_hints = list(cfg.get("location_hints", ["Jharkhand"]) or [""])

        base = settings.get("source.base_url", "https://bidplus.gem.gov.in")
        selectors = Selectors.load(Path(settings.config_dir) / "selectors.yaml")
        self.detail_parser = GemDetailParser(selectors, base, settings.timezone)
        self._detail_fetcher = BudgetedDetailFetcher(
            self.client, self.detail_parser,
            int(settings.get("run.max_detail_fetches", 250)),
        )
        self._queries_used = 0

    # ------------------------------------------------------------------
    def search(self, queries: Iterable[str]) -> SourceResult:
        result = SourceResult()
        if not self.enabled:
            return result

        if not self.api_key or not self.cse_id:
            self.client.record_issue(
                "listing", self.endpoint, "GOOGLE_SEARCH_NOT_CONFIGURED",
                "source.google_search.enabled is true but GOOGLE_SEARCH_API_KEY / "
                "GOOGLE_SEARCH_CSE_ID are not set — see docs/GOOGLE_SETUP.md",
            )
            result.access_issues.extend(self.client.access_issues)
            return result

        seen: dict[str, Tender] = {}
        budget_hit = False

        for keyword in queries:
            if self._queries_used >= self.daily_query_budget:
                budget_hit = True
                break
            for site_filter in self.site_filters:
                if self._queries_used >= self.daily_query_budget:
                    budget_hit = True
                    break
                for location in self.location_hints or [""]:
                    if self._queries_used >= self.daily_query_budget:
                        budget_hit = True
                        break
                    query_text = " ".join(p for p in (site_filter, keyword, location) if p)
                    result.queries_executed.append(query_text)
                    self._queries_used += 1

                    for item in self._call_google(query_text):
                        tender = self._tender_from_result(item, query_text)
                        if tender is None:
                            continue
                        key = tender.identity_key
                        if key in seen:
                            seen[key].raw_fields.setdefault(
                                "matched_queries", []).append(query_text)
                            continue
                        seen[key] = tender

        if budget_hit:
            self.client.record_issue(
                "listing", self.endpoint, "GOOGLE_SEARCH_BUDGET_EXHAUSTED",
                f"stopped after {self._queries_used} of a "
                f"{self.daily_query_budget}-query daily budget; some taxonomy "
                "keywords were not searched via Google this run",
            )

        result.tenders = list(seen.values())
        result.listings_seen = len(result.tenders)
        result.access_issues.extend(self.client.access_issues)
        return result

    def _call_google(self, query: str) -> list[dict]:
        try:
            response = requests.get(
                self.endpoint,
                params={"key": self.api_key, "cx": self.cse_id,
                       "q": query, "num": self.results_per_query},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            self.client.record_issue("listing", self.endpoint,
                                     "GOOGLE_SEARCH_NETWORK_FAILURE", str(exc))
            return []

        if response.status_code == 429:
            self.client.record_issue(
                "listing", self.endpoint, "GOOGLE_SEARCH_QUOTA_EXCEEDED",
                "Google Custom Search API quota exceeded for today",
            )
            return []
        if not response.ok:
            self.client.record_issue("listing", self.endpoint,
                                     f"GOOGLE_SEARCH_HTTP_{response.status_code}",
                                     response.text[:300])
            return []

        try:
            data = response.json()
        except ValueError:
            self.client.record_issue("listing", self.endpoint,
                                     "GOOGLE_SEARCH_BAD_RESPONSE",
                                     "response was not valid JSON")
            return []

        error = data.get("error")
        if error:
            self.client.record_issue("listing", self.endpoint,
                                     "GOOGLE_SEARCH_API_ERROR",
                                     str(error.get("message", error))[:300])
            return []

        return data.get("items", []) or []

    def _tender_from_result(self, item: dict, query: str) -> Tender | None:
        link = item.get("link", "")
        if not link:
            return None
        try:
            # The single most important line in this file: a result Google
            # ranked highly is still rejected if it is not an official GeM
            # host. Search ranking is never treated as a trust signal.
            self.client.assert_allowed_host(link)
        except DisallowedHost:
            log.debug("dropped a non-official search result", url=link)
            return None

        title = clean(item.get("title", ""))
        snippet = clean(item.get("snippet", ""))
        bid_match = BID_NUMBER_RE.search(f"{title} {snippet} {link}")

        tender = Tender(
            bid_number=bid_match.group(0).upper() if bid_match else "",
            source_url=link,
            title=title or snippet[:120] or link,
            item_description=snippet,
        )
        tender.raw_fields["discovered_via"] = self.name
        tender.raw_fields["matched_queries"] = [query]
        # Feeds the first-pass IT/geography screen before the detail page is
        # fetched — the same role ``listing_text`` plays for the direct adapter.
        tender.raw_fields["listing_text"] = f"{title} {snippet}"
        return tender

    # ------------------------------------------------------------------
    def fetch_detail(self, tender: Tender) -> Tender:
        return self._detail_fetcher.fetch(tender)

    @property
    def details_fetched(self) -> int:
        return self._detail_fetcher.details_fetched
