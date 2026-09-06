"""A renderer-independent report document.

The daily report has to exist in three places — a Markdown file on disk, a
Google Doc, and the terminal — and they must not drift apart. So the report is
built once as a list of blocks, and each renderer walks the same blocks.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import Enum


class BlockType(str, Enum):
    TITLE = "title"
    HEADING1 = "heading1"
    HEADING2 = "heading2"
    HEADING3 = "heading3"
    PARAGRAPH = "paragraph"
    BULLET = "bullet"
    KEYVALUE = "keyvalue"
    CALLOUT = "callout"        # rendered as a blockquote / shaded paragraph
    DIVIDER = "divider"
    SPACER = "spacer"


@dataclass
class Block:
    type: BlockType
    text: str = ""
    key: str = ""
    bold: bool = False
    items: list[str] = field(default_factory=list)


class ReportDocument:
    """Ordered blocks plus a couple of convenience builders."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.blocks: list[Block] = [Block(BlockType.TITLE, title)]

    def __iter__(self) -> Iterator[Block]:
        return iter(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)

    # -- builders -------------------------------------------------------
    def heading(self, text: str, level: int = 1) -> ReportDocument:
        mapping = {1: BlockType.HEADING1, 2: BlockType.HEADING2, 3: BlockType.HEADING3}
        self.blocks.append(Block(mapping.get(level, BlockType.HEADING3), text))
        return self

    def paragraph(self, text: str, bold: bool = False) -> ReportDocument:
        if text:
            self.blocks.append(Block(BlockType.PARAGRAPH, text, bold=bold))
        return self

    def bullets(self, items: list[str]) -> ReportDocument:
        cleaned = [i for i in items if i]
        if cleaned:
            self.blocks.append(Block(BlockType.BULLET, items=cleaned))
        return self

    def keyvalue(self, key: str, value: str) -> ReportDocument:
        self.blocks.append(Block(BlockType.KEYVALUE, text=value, key=key))
        return self

    def callout(self, text: str) -> ReportDocument:
        if text:
            self.blocks.append(Block(BlockType.CALLOUT, text))
        return self

    def divider(self) -> ReportDocument:
        self.blocks.append(Block(BlockType.DIVIDER))
        return self

    def spacer(self) -> ReportDocument:
        self.blocks.append(Block(BlockType.SPACER))
        return self
