"""Render the report into a Google Doc.

Docs API indices shift as content is inserted, so this builds the entire
plain-text body first, records the character range of every block, inserts the
text in one request, and only then applies paragraph styles and bullets by
range. That keeps index arithmetic in one place and makes the styling pass
order-independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from gem_intel.observability import get_logger
from gem_intel.report.document import Block, BlockType, ReportDocument
from gem_intel.report.drive import DriveOrganizer

log = get_logger(__name__)

# Docs API: body content starts at index 1.
BODY_START = 1

STYLE_FOR_BLOCK = {
    BlockType.TITLE: "TITLE",
    BlockType.HEADING1: "HEADING_1",
    BlockType.HEADING2: "HEADING_2",
    BlockType.HEADING3: "HEADING_3",
}
# Docs rejects a single request payload beyond this; batch conservatively.
MAX_REQUESTS_PER_BATCH = 400


@dataclass
class _Segment:
    start: int
    end: int
    block: Block
    bullet: bool = False
    bold_prefix_end: int = 0     # end index of a bold key/label run


class GoogleDocsWriter:
    def __init__(self, docs: Any, drive: Any, organizer: DriveOrganizer) -> None:
        self.docs = docs
        self.drive = drive
        self.organizer = organizer

    # ------------------------------------------------------------------
    def create_report(
        self,
        doc: ReportDocument,
        report_date: date,
        root_folder_id: str | None = None,
    ) -> str | None:
        """Create the day's Google Doc and return its URL, or ``None`` on failure."""
        try:
            folder_id = self.organizer.ensure_report_folder(report_date, root_folder_id)
            name = self.organizer.unique_name(folder_id, doc.title)
            created = self.docs.documents().create(body={"title": name}).execute()
            document_id = created["documentId"]
            self.organizer.move_to_folder(document_id, folder_id)
            self._write_body(document_id, doc)
            url = self.organizer.file_url(document_id)
            log.info("Google Doc created", title=name, url=url)
            return url
        except Exception as exc:                    # noqa: BLE001
            log.error("failed to create the Google Doc",
                      error=f"{type(exc).__name__}: {exc}")
            return None

    # ------------------------------------------------------------------
    def _write_body(self, document_id: str, doc: ReportDocument) -> None:
        text, segments = self._flatten(doc)
        if not text:
            return
        self._execute([{"insertText": {"location": {"index": BODY_START}, "text": text}}],
                      document_id)
        styling = self._style_requests(segments)
        for chunk in _chunks(styling, MAX_REQUESTS_PER_BATCH):
            self._execute(chunk, document_id)

    def _execute(self, requests: list[dict[str, Any]], document_id: str) -> None:
        if not requests:
            return
        self.docs.documents().batchUpdate(
            documentId=document_id, body={"requests": requests}
        ).execute()

    @staticmethod
    def _flatten(doc: ReportDocument) -> tuple[str, list[_Segment]]:
        """Build the document text and note where each block landed."""
        parts: list[str] = []
        segments: list[_Segment] = []
        cursor = BODY_START

        def emit(line: str, block: Block, bullet: bool = False,
                 bold_prefix: int = 0) -> None:
            nonlocal cursor
            payload = line + "\n"
            parts.append(payload)
            segments.append(_Segment(
                start=cursor, end=cursor + len(payload), block=block, bullet=bullet,
                bold_prefix_end=cursor + bold_prefix if bold_prefix else 0,
            ))
            cursor += len(payload)

        for block in doc:
            if block.type is BlockType.BULLET:
                for item in block.items:
                    emit(item, block, bullet=True)
            elif block.type is BlockType.KEYVALUE:
                label = f"{block.key}: "
                emit(f"{label}{block.text}", block, bold_prefix=len(label))
            elif block.type is BlockType.DIVIDER:
                emit("―" * 40, block)
            elif block.type is BlockType.SPACER:
                emit("", block)
            elif block.type is BlockType.CALLOUT:
                emit(block.text, block)
            else:
                emit(block.text, block)
        return "".join(parts), segments

    @staticmethod
    def _style_requests(segments: list[_Segment]) -> list[dict[str, Any]]:
        requests: list[dict[str, Any]] = []
        for segment in segments:
            block = segment.block
            span = {"startIndex": segment.start, "endIndex": segment.end}

            named_style = STYLE_FOR_BLOCK.get(block.type)
            if named_style:
                requests.append({"updateParagraphStyle": {
                    "range": span,
                    "paragraphStyle": {"namedStyleType": named_style},
                    "fields": "namedStyleType",
                }})

            if segment.bullet:
                requests.append({"createParagraphBullets": {
                    "range": span,
                    "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE",
                }})

            if block.type is BlockType.CALLOUT:
                requests.append({"updateParagraphStyle": {
                    "range": span,
                    "paragraphStyle": {
                        "indentStart": {"magnitude": 18, "unit": "PT"},
                        "borderLeft": {
                            "color": {"color": {"rgbColor": {"red": 0.8, "green": 0.5,
                                                             "blue": 0.1}}},
                            "width": {"magnitude": 3, "unit": "PT"},
                            "padding": {"magnitude": 6, "unit": "PT"},
                            "dashStyle": "SOLID",
                        },
                    },
                    "fields": "indentStart,borderLeft",
                }})
                requests.append({"updateTextStyle": {
                    "range": span,
                    "textStyle": {"italic": True},
                    "fields": "italic",
                }})

            if block.bold and block.type is BlockType.PARAGRAPH:
                requests.append({"updateTextStyle": {
                    "range": span,
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }})

            if segment.bold_prefix_end > segment.start:
                requests.append({"updateTextStyle": {
                    "range": {"startIndex": segment.start,
                              "endIndex": segment.bold_prefix_end},
                    "textStyle": {"bold": True},
                    "fields": "bold",
                }})
        return requests


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i:i + size] for i in range(0, len(items), size)]
