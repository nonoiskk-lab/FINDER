"""HTML parsing for the public GeM bid listing and bid detail pages.

Two rules shape this module:

1. **Selectors live in config**, not in code (``config/selectors.yaml``), so a
   portal redesign is a config edit.
2. **Silence is loud.** If a page parses to zero cards but clearly contains
   bid-shaped content, the parser raises :class:`ParserDrift` so the run
   records a GeM access issue. "No tenders today" must never be an artefact
   of a broken selector.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup, Tag

from gem_intel.extract.text import (
    clean,
    normalize_key,
    parse_amount,
    parse_datetime,
    parse_percentage,
    parse_quantity,
    strip_label,
)
from gem_intel.models import (
    Confidence,
    Evidence,
    MoneyClaim,
    Tender,
    TenderDocument,
    TernaryFlag,
)
from gem_intel.observability import get_logger

log = get_logger(__name__)

# A page that contains these tokens is a bid page even if our selectors miss.
DRIFT_CANARIES = ("bid number", "bid no.", "gem/20", "bid end date", "start date")

BID_NUMBER_RE = re.compile(r"\bGEM/\d{4}/[A-Z]/\d+\b", re.IGNORECASE)


class ParserDrift(RuntimeError):
    """The page looks like a bid page but none of our selectors matched."""


@dataclass
class Selectors:
    listing: dict[str, list[str]]
    detail: dict[str, list[str]]
    field_labels: dict[str, list[str]]

    @classmethod
    def load(cls, path: Path) -> Selectors:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            listing=data.get("listing", {}) or {},
            detail=data.get("detail", {}) or {},
            field_labels=data.get("field_labels", {}) or {},
        )

    def label_lookup(self) -> dict[str, str]:
        """Flattened ``normalised label -> canonical field`` map."""
        table: dict[str, str] = {}
        for field_name, labels in self.field_labels.items():
            for label in labels:
                table[normalize_key(label)] = field_name
        return table


def _select_first(node: Tag, patterns: Iterable[str]) -> Tag | None:
    for pattern in patterns:
        try:
            found = node.select_one(pattern)
        except Exception:            # invalid selector after a config edit
            log.warning("invalid selector skipped", selector=pattern)
            continue
        if found is not None:
            return found
    return None


def _select_all(node: Tag, patterns: Iterable[str]) -> list[Tag]:
    for pattern in patterns:
        try:
            found = node.select(pattern)
        except Exception:
            log.warning("invalid selector skipped", selector=pattern)
            continue
        if found:
            return found
    return []


def _select_union(node: Tag, patterns: Iterable[str]) -> list[Tag]:
    """Every match from every selector, de-duplicated, in document order.

    Unlike :func:`_select_all` this does not stop at the first productive
    selector. Attachment links need the union: a GeM bid page carries the bid
    document under one URL shape and the ATC under another, and first-match
    would quietly return only the first kind.
    """
    seen: set[int] = set()
    found: list[Tag] = []
    for pattern in patterns:
        try:
            matches = node.select(pattern)
        except Exception:
            log.warning("invalid selector skipped", selector=pattern)
            continue
        for match in matches:
            if id(match) not in seen:
                seen.add(id(match))
                found.append(match)
    return found


def _text_of(node: Tag | None) -> str:
    return clean(node.get_text(" ", strip=True)) if node is not None else ""


class GemListingParser:
    """Turns a search-results page into candidate :class:`Tender` objects."""

    def __init__(self, selectors: Selectors, base_url: str, tz: str = "Asia/Kolkata") -> None:
        self.selectors = selectors
        self.base_url = base_url
        self.tz = tz

    def parse(self, html: str, page_url: str) -> list[Tender]:
        soup = BeautifulSoup(html, "html.parser")
        cards = _select_all(soup, self.selectors.listing.get("card", []))
        if not cards:
            if self._looks_like_bid_page(html):
                raise ParserDrift(
                    "Listing page contains bid-shaped content but no card matched "
                    "config/selectors.yaml → listing.card"
                )
            return []
        tenders = []
        for card in cards:
            tender = self._parse_card(card, page_url)
            if tender is not None:
                tenders.append(tender)
        if cards and not tenders:
            raise ParserDrift(
                f"{len(cards)} listing cards found but none yielded a bid number"
            )
        return tenders

    @staticmethod
    def _looks_like_bid_page(html: str) -> bool:
        lowered = html.lower()
        return sum(1 for canary in DRIFT_CANARIES if canary in lowered) >= 2

    def _parse_card(self, card: Tag, page_url: str) -> Tender | None:
        link = _select_first(card, self.selectors.listing.get("bid_link", []))
        card_text = _text_of(card)

        bid_number = _text_of(link)
        if not BID_NUMBER_RE.search(bid_number):
            match = BID_NUMBER_RE.search(card_text)
            bid_number = match.group(0) if match else clean(bid_number)
        if not bid_number:
            return None

        href = link.get("href") if isinstance(link, Tag) else None
        source_url = urljoin(self.base_url, href) if href else page_url

        tender = Tender(
            bid_number=bid_number.upper(),
            source_url=source_url,
            title=self._first_nonempty(card, "items") or bid_number,
            item_description=self._first_nonempty(card, "items"),
            category=self._first_nonempty(card, "category"),
            buyer_address=self._first_nonempty(card, "department"),
            buyer_organization=self._first_nonempty(card, "department"),
        )

        quantity_text = self._first_nonempty(card, "quantity")
        if quantity_text:
            tender.quantity, tender.quantity_unit = parse_quantity(quantity_text)

        tender.bid_start_at = parse_datetime(self._first_nonempty(card, "start_date"), self.tz)
        tender.bid_end_at = parse_datetime(self._first_nonempty(card, "end_date"), self.tz)

        tender.raw_fields["listing_text"] = card_text
        tender.raw_fields["listing_page_url"] = page_url
        return tender

    def _first_nonempty(self, card: Tag, key: str) -> str:
        node = _select_first(card, self.selectors.listing.get(key, []))
        if node is None:
            return ""
        text = _text_of(node)
        # Cells often read "Quantity: 50" — drop the label half.
        for label in self.selectors.field_labels.get(key, []) + [key.replace("_", " ")]:
            text = strip_label(text, label)
        return clean(text)

    def find_last_page(self, html: str) -> int:
        soup = BeautifulSoup(html, "html.parser")
        node = _select_first(soup, self.selectors.listing.get("pagination_last", []))
        text = _text_of(node)
        match = re.search(r"\d+", text)
        return int(match.group(0)) if match else 1


class GemDetailParser:
    """Reads a bid detail page into the tender's portal-fact fields."""

    def __init__(self, selectors: Selectors, base_url: str, tz: str = "Asia/Kolkata") -> None:
        self.selectors = selectors
        self.base_url = base_url
        self.tz = tz
        self.labels = selectors.label_lookup()

    def parse(self, tender: Tender, html: str, page_url: str) -> Tender:
        soup = BeautifulSoup(html, "html.parser")
        fields = self._extract_fields(soup)
        tender.raw_fields.update({f"detail.{k}": v for k, v in fields.items()})
        self._apply(tender, fields, page_url)
        tender.documents.extend(self._extract_documents(soup, page_url, tender))
        return tender

    def _extract_fields(self, soup: BeautifulSoup) -> dict[str, str]:
        found: dict[str, str] = {}
        rows = _select_all(soup, self.selectors.detail.get("field_row", []))
        for row in rows:
            key_node = _select_first(row, self.selectors.detail.get("key_cell", []))
            value_node = _select_first(row, self.selectors.detail.get("value_cell", []))
            if key_node is None or value_node is None:
                continue
            canonical = self.labels.get(normalize_key(_text_of(key_node)))
            if not canonical:
                continue
            value = _text_of(value_node)
            # First occurrence wins: detail pages repeat labels in summaries.
            if value and canonical not in found:
                found[canonical] = value
        return found

    def _apply(self, tender: Tender, fields: dict[str, str], page_url: str) -> None:
        evidence = Evidence(source_url=page_url, locator="bid detail page",
                            extractor="portal_field")

        def text_field(name: str, current: str) -> str:
            value = fields.get(name, "")
            return value or current

        tender.bid_number = (fields.get("bid_number") or tender.bid_number).upper()
        tender.gem_tender_id = text_field("gem_tender_id", tender.gem_tender_id)
        tender.ministry = text_field("ministry", tender.ministry)
        tender.department = text_field("department", tender.department)
        tender.buyer_organization = text_field("buyer_organization", tender.buyer_organization)
        tender.office = text_field("office", tender.office)
        tender.buyer_address = text_field("buyer_address", tender.buyer_address)
        tender.category = text_field("category", tender.category)
        tender.delivery_location = text_field("delivery_location", tender.delivery_location)

        for attr, key in (
            ("published_at", "published_at"),
            ("bid_start_at", "bid_start_at"),
            ("bid_end_at", "bid_end_at"),
            ("bid_open_at", "bid_open_at"),
        ):
            parsed = parse_datetime(fields.get(key), self.tz)
            if parsed is not None:
                setattr(tender, attr, parsed)

        if "quantity" in fields:
            quantity, unit = parse_quantity(fields["quantity"])
            if quantity is not None:
                tender.quantity = quantity
                # The detail page often gives a bare number; keep the unit the
                # listing supplied rather than replacing it with "".
                tender.quantity_unit = unit or tender.quantity_unit

        if "estimated_value" in fields:
            amount = parse_amount(fields["estimated_value"])
            if amount is not None:
                tender.estimated_value = MoneyClaim(
                    value=amount, confidence=Confidence.CONFIRMED,
                    evidence=[Evidence(page_url, "estimated bid value",
                                       fields["estimated_value"], "portal_field")],
                )

        # The portal's structured EMD / ePBG cells are the strongest evidence
        # available; the document-text analysers only fill gaps they leave.
        if "emd_amount" in fields:
            amount = parse_amount(fields["emd_amount"])
            tender.raw_fields["portal.emd_amount"] = fields["emd_amount"]
            if amount is not None:
                tender.emd.amount = MoneyClaim(
                    value=amount, confidence=Confidence.CONFIRMED,
                    evidence=[Evidence(page_url, "EMD amount field",
                                       fields["emd_amount"], "portal_field")],
                )
                tender.emd.status = (
                    TernaryFlag.REQUIRED if amount > 0 else TernaryFlag.NOT_REQUIRED
                )
                tender.emd.evidence.append(evidence)

        if "epbg_percentage" in fields:
            # GeM prints this cell as a bare number ("3.00"), so fall back to
            # reading it as a plain figure when there is no % sign.
            percentage = parse_percentage(fields["epbg_percentage"])
            if percentage is None:
                percentage = parse_percentage(f"{clean(fields['epbg_percentage'])}%")
            tender.raw_fields["portal.epbg_percentage"] = fields["epbg_percentage"]
            if percentage is not None:
                tender.security.epbg = (
                    TernaryFlag.REQUIRED if percentage > 0 else TernaryFlag.NOT_REQUIRED
                )
                tender.security.percentage.value = percentage
                tender.security.percentage.confidence = Confidence.CONFIRMED
                tender.security.percentage.evidence = [
                    Evidence(page_url, "ePBG percentage field",
                             fields["epbg_percentage"], "portal_field")
                ]
                tender.security.evidence.append(evidence)

        for key, attr in (("msme_exemption", "msme_exemption_portal_flag"),
                          ("startup_exemption", "startup_exemption_portal_flag")):
            raw = clean(fields.get(key, "")).lower()
            if raw in ("yes", "true"):
                setattr(tender, attr, TernaryFlag.REQUIRED)
            elif raw in ("no", "false"):
                setattr(tender, attr, TernaryFlag.NOT_REQUIRED)

    def _extract_documents(
        self, soup: BeautifulSoup, page_url: str, tender: Tender
    ) -> list[TenderDocument]:
        seen: set[str] = {d.url for d in tender.documents}
        documents: list[TenderDocument] = []
        for anchor in _select_union(soup, self.selectors.detail.get("document_link", [])):
            href = anchor.get("href")
            if not href:
                continue
            url = urljoin(page_url, str(href))
            if url in seen:
                continue
            seen.add(url)
            name = _text_of(anchor) or Path(str(href)).name or "document"
            documents.append(TenderDocument(name=name, url=url, kind=classify_document(name)))
        return documents


def classify_document(name: str) -> str:
    lowered = normalize_key(name)
    if "atc" in lowered or "additional terms" in lowered:
        return "atc"
    if "corrigend" in lowered or "amend" in lowered:
        return "corrigendum"
    if "spec" in lowered or "technical" in lowered:
        return "technical_spec"
    if "scope" in lowered or "sow" in lowered:
        return "scope_of_work"
    if "bid" in lowered or "tender" in lowered or "document" in lowered:
        return "bid_document"
    return "unknown"
