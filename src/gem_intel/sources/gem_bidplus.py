"""Official GeM bid-listing source adapter.

Scope: the publicly accessible bid search on ``bidplus.gem.gov.in``. This is
the only tender source the system is permitted to use (docs/COMPLIANCE.md).

Access model
------------
The adapter performs the same requests a person would make while browsing the
public bid list: it loads the search page, reads the CSRF token the page hands
out, and submits the search form. It does not authenticate, does not touch
non-public endpoints, and stops immediately if a human-verification challenge
appears.

Portal drift
------------
GeM changes its markup and its search payload periodically. Two guards keep a
drift from masquerading as "no tenders today":

* :class:`~gem_intel.extract.gem_html.ParserDrift` is raised when a page holds
  bid-shaped content that no configured selector matches.
* A search that returns zero rows *for every query* is reported as a
  ``ZERO_RESULTS_ALL_QUERIES`` access issue, not as an empty result set.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from gem_intel.config import Settings
from gem_intel.extract.gem_html import (
    GemDetailParser,
    GemListingParser,
    ParserDrift,
    Selectors,
)
from gem_intel.http_client import AccessBlocked, GemHttpClient
from gem_intel.models import Tender
from gem_intel.observability import get_logger
from gem_intel.sources.base import SourceAdapter, SourceResult

log = get_logger(__name__)

CSRF_INPUT_NAMES = ("csrf_bd_gem_nk", "csrf_token", "_csrf", "csrf_bd_gem_hd")


class GemBidPlusSource(SourceAdapter):
    name = "GeM Bid Plus (official public bid list)"

    def __init__(self, settings: Settings, client: GemHttpClient) -> None:
        self.settings = settings
        self.client = client
        self.base_url = settings.get("source.base_url", "https://bidplus.gem.gov.in")
        self.all_bids_url = urljoin(
            self.base_url + "/", settings.get("source.all_bids_path", "/all-bids").lstrip("/")
        )
        self.search_data_url = urljoin(self.base_url + "/", "all-bids-data")
        self.max_pages = int(settings.get("run.max_listing_pages", 40))
        self.max_details = int(settings.get("run.max_detail_fetches", 250))

        selectors_path = settings.config_dir / "selectors.yaml"
        self.selectors = Selectors.load(selectors_path)
        self.listing_parser = GemListingParser(self.selectors, self.base_url, settings.timezone)
        self.detail_parser = GemDetailParser(self.selectors, self.base_url, settings.timezone)

        self.hosts = tuple(settings.allowed_hosts)
        self._csrf: tuple[str, str] | None = None   # (field name, token)
        self._details_fetched = 0

    # ------------------------------------------------------------------
    def search(self, queries: Iterable[str]) -> SourceResult:
        result = SourceResult()
        query_list = [q for q in queries if q.strip()]

        try:
            self._prime_session(result)
        except AccessBlocked as exc:
            result.aborted = True
            result.abort_reason = f"Human-verification challenge at {exc.url}"
            result.access_issues.extend(self.client.access_issues)
            return result

        seen: dict[str, Tender] = {}
        for query in query_list:
            try:
                found = self._search_one(query, result)
            except AccessBlocked as exc:
                result.aborted = True
                result.abort_reason = f"Human-verification challenge at {exc.url}"
                break
            except ParserDrift as exc:
                self.client.record_issue("listing", self.all_bids_url,
                                         "PARSER_DRIFT", str(exc))
                continue
            result.queries_executed.append(query)
            for tender in found:
                # Same bid surfaces under many keywords; keep the first, but
                # remember every query that found it (useful signal later).
                key = tender.identity_key
                if key in seen:
                    seen[key].raw_fields.setdefault("matched_queries", []).append(query)
                    continue
                tender.raw_fields["matched_queries"] = [query]
                seen[key] = tender

        result.tenders = list(seen.values())
        result.listings_seen = len(result.tenders)

        if query_list and not result.aborted and not result.tenders:
            self.client.record_issue(
                "listing", self.all_bids_url, "ZERO_RESULTS_ALL_QUERIES",
                f"{len(query_list)} search terms returned no rows. This is unusual "
                "for GeM and more likely a portal or parser change than a genuinely "
                "empty day — verify manually before trusting an empty report.",
            )
        result.access_issues.extend(self.client.access_issues)
        return result

    # ------------------------------------------------------------------
    def _prime_session(self, result: SourceResult) -> None:
        """Load the public search page and capture its CSRF token."""
        response = self.client.fetch(self.all_bids_url, stage="listing")
        if response is None:
            return
        self._csrf = self._extract_csrf(response.text)
        if self._csrf:
            log.info("csrf token captured", field=self._csrf[0])
        else:
            log.info("no csrf token on search page; will use GET search")

    @staticmethod
    def _extract_csrf(html: str) -> tuple[str, str] | None:
        soup = BeautifulSoup(html, "html.parser")
        for name in CSRF_INPUT_NAMES:
            node = soup.find("input", attrs={"name": name})
            if node and node.get("value"):
                return name, str(node["value"])
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta and meta.get("content"):
            return "csrf_bd_gem_nk", str(meta["content"])
        match = re.search(r"""['"]csrf[_a-z]*['"]\s*:\s*['"]([A-Za-z0-9_\-]{16,})['"]""",
                          html, re.IGNORECASE)
        return ("csrf_bd_gem_nk", match.group(1)) if match else None

    def _search_one(self, query: str, result: SourceResult) -> list[Tender]:
        """Run one keyword across as many result pages as the budget allows."""
        tenders: list[Tender] = []
        page = 1
        while page <= self.max_pages:
            html = self._request_page(query, page)
            if html is None:
                break
            page_tenders = self.listing_parser.parse(html, self.all_bids_url)
            if not page_tenders:
                break
            tenders.extend(page_tenders)
            log.info("listing page parsed", query=query, page=page,
                     found=len(page_tenders), running_total=len(tenders))
            if len(page_tenders) < 10:      # short page == last page
                break
            page += 1
        return tenders

    def _request_page(self, query: str, page: int) -> str | None:
        """POST the search form when we have a CSRF token, else GET with params."""
        if self._csrf:
            field, token = self._csrf
            payload = {
                field: token,
                "payload": json.dumps({
                    "page": page,
                    "param": {"searchBid": query, "searchType": "fullText"},
                    "filter": {
                        "bidStatusType": "ongoing_bids",
                        "byType": "all",
                        "sort": "Bid-End-Date-Oldest",
                    },
                }),
            }
            response = self.client.fetch(
                self.search_data_url, method="POST", data=payload, stage="listing"
            )
            if response is not None and response.ok:
                return self._unwrap(response.text)
            log.info("POST search unavailable; falling back to GET", query=query)

        response = self.client.fetch(
            self.all_bids_url,
            params={"searchBid": query, "page": page, "bidStatusType": "ongoing_bids"},
            stage="listing",
        )
        return response.text if response is not None and response.ok else None

    @staticmethod
    def _unwrap(body: str) -> str:
        """The data endpoint answers with JSON whose payload is an HTML blob."""
        stripped = body.lstrip()
        if not stripped.startswith(("{", "[")):
            return body
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body
        for key in ("response", "html", "data", "bidList", "docs"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and "<" in value:
                return value
        return json.dumps(data)

    # ------------------------------------------------------------------
    def fetch_detail(self, tender: Tender) -> Tender:
        if self._details_fetched >= self.max_details:
            tender.flag("Detail page not fetched (per-run budget reached) — "
                        "figures below come from the search listing only.")
            return tender
        if not tender.source_url:
            tender.flag("No official GeM detail URL was available for this bid.")
            return tender

        response = self.client.fetch(tender.source_url, stage="detail")
        self._details_fetched += 1
        if response is None:
            tender.flag("The official GeM detail page could not be loaded — "
                        "figures below come from the search listing only.")
            return tender

        content_type = (response.content_type or "").lower()
        if "pdf" in content_type:
            # Some bids expose only a PDF at the detail URL. Record it as the
            # tender's primary document; the document processor reads it next.
            from gem_intel.models import TenderDocument

            if not any(d.url == response.url for d in tender.documents):
                tender.documents.insert(0, TenderDocument(
                    name=f"{tender.bid_number or 'bid'}.pdf",
                    url=response.url, kind="bid_document",
                ))
            return tender

        try:
            self.detail_parser.parse(tender, response.text, response.url)
        except Exception as exc:                    # noqa: BLE001
            self.client.record_issue("detail", tender.source_url,
                                     "DETAIL_PARSE_FAILED", str(exc))
            tender.flag("The GeM detail page could not be parsed — verify this bid manually.")
        return tender

    @property
    def details_fetched(self) -> int:
        return self._details_fetched


def load_fixture_source(settings: Settings, fixture_dir: Path) -> FixtureSource:
    return FixtureSource(settings, fixture_dir)


RELATIVE_DATE = re.compile(r"\{\{\s*([+-]?\d+)d(?:\s+(\d{2}:\d{2}))?\s*\}\}")


def render_relative_dates(html: str, today: date | None = None) -> str:
    """Expand ``{{+4d 15:00}}`` tokens into real dates.

    Offline fixtures need deadlines that stay meaningful as time passes;
    hard-coded dates would silently turn every fixture tender into an expired
    one and make a dry run look like a bug.
    """
    reference = today or date.today()

    def replace(match: re.Match[str]) -> str:
        offset = int(match.group(1))
        clock = match.group(2) or "15:00"
        return f"{(reference + timedelta(days=offset)).strftime('%d-%m-%Y')} {clock}:00"

    return RELATIVE_DATE.sub(replace, html)


class FixtureSource(SourceAdapter):
    """Replays saved GeM HTML from disk.

    Used by tests and by ``--dry-run`` so the pipeline can be exercised end to
    end without touching the portal. Fixtures must be captured from an official
    GeM host; the pipeline still applies the same host verification to them, so
    a fixture with a fabricated URL is rejected exactly as it should be.
    """

    name = "GeM fixture replay (offline)"

    def __init__(self, settings: Settings, fixture_dir: Path,
                 today: date | None = None) -> None:
        self.settings = settings
        self.fixture_dir = Path(fixture_dir)
        self.today = today
        self.selectors = Selectors.load(settings.config_dir / "selectors.yaml")
        base = settings.get("source.base_url", "https://bidplus.gem.gov.in")
        self.listing_parser = GemListingParser(self.selectors, base, settings.timezone)
        self.detail_parser = GemDetailParser(self.selectors, base, settings.timezone)

    def _load(self, path: Path) -> str:
        return render_relative_dates(path.read_text(encoding="utf-8"), self.today)

    def search(self, queries: Iterable[str]) -> SourceResult:
        result = SourceResult(queries_executed=list(queries))
        for path in sorted(self.fixture_dir.glob("listing*.html")):
            try:
                found = self.listing_parser.parse(self._load(path), str(path))
            except ParserDrift as exc:
                log.warning("fixture drift", fixture=str(path), error=str(exc))
                continue
            result.tenders.extend(found)
        result.listings_seen = len(result.tenders)
        return result

    def fetch_detail(self, tender: Tender) -> Tender:
        safe = tender.bid_number.replace("/", "_")
        candidate = self.fixture_dir / f"detail_{safe}.html"
        if candidate.exists():
            self.detail_parser.parse(tender, self._load(candidate),
                                     tender.source_url or str(candidate))
        return tender
