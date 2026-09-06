"""Bid-detail fetching, shared by every source adapter.

Every adapter's ``search()`` differs in how it discovers *candidate URLs* —
the portal's own search form, or a Google Search result pointing at the same
portal. Once a candidate URL is found, fetching and parsing its detail page
is identical work and must apply the identical safety rules (host allow-list
already enforced by ``GemHttpClient``, CAPTCHA abort, parse-failure
recording). Keeping it in one place means a portal markup fix, or a new
safety rule, only has to happen once.
"""

from __future__ import annotations

from gem_intel.extract.gem_html import GemDetailParser
from gem_intel.http_client import GemHttpClient
from gem_intel.models import Tender, TenderDocument
from gem_intel.observability import get_logger

log = get_logger(__name__)


def fetch_and_parse_detail(
    client: GemHttpClient, detail_parser: GemDetailParser, tender: Tender
) -> Tender:
    """Fetch ``tender.source_url`` and merge its fields into ``tender``.

    Never raises for an ordinary failure (network error, parse error) — those
    are recorded as an access issue and/or a verification flag on the tender
    itself. A CAPTCHA/bot-challenge (``AccessBlocked``) is the one exception
    this deliberately lets propagate, exactly like ``GemHttpClient.fetch``
    does: the caller decides whether that ends the whole run or just this
    tender (see ``TenderPipeline._analyze``).
    """
    if not tender.source_url:
        tender.flag("No official GeM detail URL was available for this bid.")
        return tender

    response = client.fetch(tender.source_url, stage="detail")
    if response is None:
        tender.flag("The official GeM detail page could not be loaded — "
                    "figures below come from the search listing only.")
        return tender

    content_type = (response.content_type or "").lower()
    if "pdf" in content_type:
        # Some bids expose only a PDF at the detail URL. Record it as the
        # tender's primary document; the document processor reads it next.
        if not any(d.url == response.url for d in tender.documents):
            tender.documents.insert(0, TenderDocument(
                name=f"{tender.bid_number or 'bid'}.pdf",
                url=response.url, kind="bid_document",
            ))
        return tender

    try:
        detail_parser.parse(tender, response.text, response.url)
    except Exception as exc:                        # noqa: BLE001
        client.record_issue("detail", tender.source_url,
                            "DETAIL_PARSE_FAILED", str(exc))
        tender.flag("The GeM detail page could not be parsed — verify this bid manually.")
    return tender


class BudgetedDetailFetcher:
    """Adds a per-run fetch budget on top of :func:`fetch_and_parse_detail`.

    Each adapter owns one of these so its own budget (``run.max_detail_fetches``
    split however the pipeline configures it) is independent of any other
    adapter's.
    """

    def __init__(self, client: GemHttpClient, detail_parser: GemDetailParser,
                max_details: int) -> None:
        self.client = client
        self.detail_parser = detail_parser
        self.max_details = max_details
        self.details_fetched = 0

    def fetch(self, tender: Tender) -> Tender:
        if self.details_fetched >= self.max_details:
            tender.flag("Detail page not fetched (per-run budget reached) — "
                        "figures below come from the search listing only.")
            return tender
        self.details_fetched += 1
        return fetch_and_parse_detail(self.client, self.detail_parser, tender)
