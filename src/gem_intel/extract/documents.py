"""Download tender attachments and turn them into analysable text.

Extraction ladder, cheapest first:
  PDF   -> pdfplumber text (+ table flattening) -> PyPDF2 -> OCR (opt-in)
  DOCX  -> python-docx paragraphs + tables
  XLSX  -> openpyxl cell sweep
  TXT   -> as-is

A document that yields no text is recorded with ``extraction_method="failed"``
and an error string. It is never treated as an empty document — the tender
gets a ⚠ flag so the report tells a human to open it themselves.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from gem_intel.config import Settings
from gem_intel.extract.text import clean
from gem_intel.http_client import GemHttpClient
from gem_intel.models import Tender, TenderDocument
from gem_intel.observability import get_logger

log = get_logger(__name__)

MAX_TABLE_CELL = 200


class DocumentProcessor:
    def __init__(self, settings: Settings, client: GemHttpClient) -> None:
        self.settings = settings
        self.client = client
        self.enabled = bool(settings.get("documents.download", True))
        self.max_bytes = int(settings.get("documents.max_bytes_per_document", 25 * 1024 * 1024))
        self.allowed_types = set(settings.get("documents.allowed_content_types", []))
        self.ocr_enabled = bool(settings.get("documents.ocr_fallback", True))
        self.ocr_max_pages = int(settings.get("documents.ocr_max_pages", 30))
        self.artifacts_dir = Path(settings.get("storage.artifacts_dir", "data/artifacts"))
        self.downloaded = 0

    # ------------------------------------------------------------------
    def process_tender(self, tender: Tender, budget: int) -> int:
        """Download and extract up to ``budget`` documents. Returns count used."""
        if not self.enabled or budget <= 0:
            return 0
        used = 0
        for document in tender.documents:
            if used >= budget:
                tender.flag(
                    "Not all tender documents were read (per-run download budget reached) "
                    "— review the remaining attachments manually."
                )
                break
            if document.text_chars:
                continue
            self.fetch_and_extract(document, tender)
            used += 1
        if tender.documents and not tender.has_document_text:
            tender.flag(
                "No tender document text could be extracted — every requirement below "
                "comes from the portal listing only."
            )
        return used

    def fetch_and_extract(self, document: TenderDocument, tender: Tender) -> TenderDocument:
        result = self.client.fetch(
            document.url, stage="document", binary=True, max_bytes=self.max_bytes
        )
        if result is None:
            document.error = "download failed (see access issues)"
            document.extraction_method = "failed"
            return document

        document.content_type = result.content_type
        document.bytes_downloaded = len(result.content)
        document.sha256 = hashlib.sha256(result.content).hexdigest()

        if self.allowed_types and result.content_type not in self.allowed_types:
            document.error = f"unsupported content type {result.content_type!r}"
            document.extraction_method = "skipped"
            log.info("skipping document", url=document.url, content_type=result.content_type)
            return document

        document.local_path = self._persist(tender, document, result.content)
        text, method, error = extract_text(result.content, result.content_type,
                                           document.name,
                                           ocr=self.ocr_enabled,
                                           ocr_max_pages=self.ocr_max_pages)
        document.extraction_method = method
        document.error = error
        document.text_chars = len(text)
        if text:
            self._persist_text(tender, document, text)
        self.downloaded += 1
        log.info("document processed", name=document.name, method=method,
                 chars=document.text_chars, error=error or None)
        return document

    # ------------------------------------------------------------------
    def _tender_dir(self, tender: Tender) -> Path:
        safe = tender.identity_key.replace(":", "_").replace("/", "_")
        path = self.artifacts_dir / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persist(self, tender: Tender, document: TenderDocument, payload: bytes) -> str:
        directory = self._tender_dir(tender)
        name = f"{document.sha256[:12]}_{_safe_name(document.name)}"
        path = directory / name
        path.write_bytes(payload)
        return str(path)

    def _persist_text(self, tender: Tender, document: TenderDocument, text: str) -> None:
        directory = self._tender_dir(tender)
        path = directory / f"{document.sha256[:12]}.txt"
        path.write_text(text, encoding="utf-8")

    def load_text(self, document: TenderDocument) -> str:
        if not document.local_path or not document.sha256:
            return ""
        path = Path(document.local_path).parent / f"{document.sha256[:12]}.txt"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _safe_name(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in "._-" else "_" for c in clean(name))
    return (keep or "document")[:80]


# ----------------------------------------------------------------------
def extract_text(
    payload: bytes,
    content_type: str,
    filename: str = "",
    ocr: bool = False,
    ocr_max_pages: int = 30,
) -> tuple[str, str, str]:
    """Return ``(text, method, error)``. Never raises."""
    suffix = Path(filename).suffix.lower()
    kind = _kind_of(content_type, suffix)

    try:
        if kind == "pdf":
            return _extract_pdf(payload, ocr, ocr_max_pages)
        if kind == "docx":
            return _extract_docx(payload)
        if kind == "xlsx":
            return _extract_xlsx(payload)
        if kind == "text":
            return payload.decode("utf-8", errors="replace"), "text", ""
    except Exception as exc:                       # noqa: BLE001 - never fail a run
        log.warning("extraction error", filename=filename, error=str(exc))
        return "", "failed", f"{type(exc).__name__}: {exc}"
    return "", "unsupported", f"no extractor for {content_type or suffix or 'unknown type'}"


def _kind_of(content_type: str, suffix: str) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct or suffix == ".pdf":
        return "pdf"
    if "wordprocessingml" in ct or suffix == ".docx":
        return "docx"
    if "spreadsheetml" in ct or suffix in (".xlsx", ".xlsm"):
        return "xlsx"
    if ct.startswith("text/") or suffix in (".txt", ".csv"):
        return "text"
    return "unknown"


def _extract_pdf(payload: bytes, ocr: bool, ocr_max_pages: int) -> tuple[str, str, str]:
    text, error = _pdf_via_pdfplumber(payload)
    if text.strip():
        return text, "pdf_text", ""

    fallback, fallback_error = _pdf_via_pypdf(payload)
    if fallback.strip():
        return fallback, "pdf_text_fallback", ""

    if ocr:
        ocr_text, ocr_error = _pdf_via_ocr(payload, ocr_max_pages)
        if ocr_text.strip():
            return ocr_text, "pdf_ocr", ""
        return "", "failed", ocr_error or error or fallback_error or "no extractable text"
    return "", "failed", error or fallback_error or "no extractable text (scanned PDF?)"


def _pdf_via_pdfplumber(payload: bytes) -> tuple[str, str]:
    try:
        import pdfplumber
    except ImportError:
        return "", "pdfplumber not installed"
    parts: list[str] = []
    try:
        with pdfplumber.open(io.BytesIO(payload)) as pdf:
            for index, page in enumerate(pdf.pages, start=1):
                parts.append(f"\n[page {index}]\n")
                page_text = page.extract_text() or ""
                if page_text:
                    parts.append(page_text)
                for table in page.extract_tables() or []:
                    parts.append(_flatten_table(table))
    except Exception as exc:                       # noqa: BLE001
        return "", f"pdfplumber: {exc}"
    return "\n".join(parts), ""


def _pdf_via_pypdf(payload: bytes) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore[no-redef]
        except ImportError:
            return "", "pypdf not installed"
    try:
        reader = PdfReader(io.BytesIO(payload))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            pages.append(f"\n[page {index}]\n{page.extract_text() or ''}")
        return "\n".join(pages), ""
    except Exception as exc:                       # noqa: BLE001
        return "", f"pypdf: {exc}"


def _pdf_via_ocr(payload: bytes, max_pages: int) -> tuple[str, str]:
    """OCR is opt-in and only ever runs on PDFs with no text layer."""
    try:
        import pytesseract
        from pdf2image import convert_from_bytes
    except ImportError:
        return "", "OCR requested but pytesseract/pdf2image not installed"
    try:
        images = convert_from_bytes(payload, dpi=200, fmt="png",
                                    first_page=1, last_page=max_pages)
    except Exception as exc:                       # noqa: BLE001
        return "", f"pdf2image: {exc}"
    parts = []
    for index, image in enumerate(images, start=1):
        try:
            parts.append(f"\n[page {index} (OCR)]\n{pytesseract.image_to_string(image)}")
        except Exception as exc:                   # noqa: BLE001
            return "\n".join(parts), f"tesseract: {exc}"
    return "\n".join(parts), ""


def _extract_docx(payload: bytes) -> tuple[str, str, str]:
    try:
        import docx
    except ImportError:
        return "", "failed", "python-docx not installed"
    document = docx.Document(io.BytesIO(payload))
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        rows = [[cell.text for cell in row.cells] for row in table.rows]
        parts.append(_flatten_table(rows))
    return "\n".join(parts), "docx", ""


def _extract_xlsx(payload: bytes) -> tuple[str, str, str]:
    try:
        import openpyxl
    except ImportError:
        return "", "failed", "openpyxl not installed"
    workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    parts = []
    for sheet in workbook.worksheets:
        parts.append(f"\n[sheet {sheet.title}]")
        for row in sheet.iter_rows(values_only=True):
            cells = [clean(str(c))[:MAX_TABLE_CELL] for c in row if c is not None]
            if cells:
                parts.append(" | ".join(cells))
    workbook.close()
    return "\n".join(parts), "xlsx", ""


def _flatten_table(table: list[list[str | None]]) -> str:
    lines = []
    for row in table or []:
        cells = [clean(str(cell or ""))[:MAX_TABLE_CELL] for cell in row]
        if any(cells):
            lines.append(" | ".join(cells))
    return "\n".join(lines)
