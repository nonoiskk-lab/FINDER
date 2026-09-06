"""Markdown rendering of the report document."""

from __future__ import annotations

from pathlib import Path

from gem_intel.report.document import Block, BlockType, ReportDocument


def render_markdown(doc: ReportDocument) -> str:
    lines: list[str] = []
    for block in doc:
        lines.extend(_render_block(block))
    text = "\n".join(lines)
    # Collapse runs of blank lines that the block model naturally produces.
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")
    return text.strip() + "\n"


def _render_block(block: Block) -> list[str]:
    if block.type is BlockType.TITLE:
        return [f"# {block.text}", ""]
    if block.type is BlockType.HEADING1:
        return ["", f"## {block.text}", ""]
    if block.type is BlockType.HEADING2:
        return ["", f"### {block.text}", ""]
    if block.type is BlockType.HEADING3:
        return ["", f"#### {block.text}", ""]
    if block.type is BlockType.PARAGRAPH:
        return [f"**{block.text}**" if block.bold else block.text, ""]
    if block.type is BlockType.KEYVALUE:
        return [f"**{block.key}:** {block.text}", ""]
    if block.type is BlockType.BULLET:
        return [f"- {item}" for item in block.items] + [""]
    if block.type is BlockType.CALLOUT:
        return [f"> {block.text}", ""]
    if block.type is BlockType.DIVIDER:
        return ["", "---", ""]
    if block.type is BlockType.SPACER:
        return [""]
    return []


def write_markdown(doc: ReportDocument, path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_markdown(doc), encoding="utf-8")
    return target


def render_terminal(doc: ReportDocument, max_lines: int = 120) -> str:
    """A trimmed view for the console, so an operator sees the outcome."""
    lines = render_markdown(doc).splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    head = lines[:max_lines]
    return "\n".join(head + [f"… ({len(lines) - max_lines} more lines in the report file)"])
