"""Normalisation helpers shared by every parser.

Government listings are full of non-breaking spaces, doubled whitespace,
Devanagari digits, and rupee amounts written six different ways. Everything
that downstream code compares or regexes goes through here first.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")

# Indian numbering: 1,00,000 / 1,00,000.00 / 10 lakh / 2.5 crore
_AMOUNT = re.compile(
    r"(?:(?:rs\.?|inr|₹)\s*)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})*(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<scale>lakhs?|lacs?|crores?|cr|k|thousand)?",
    re.IGNORECASE,
)
_SCALES = {
    "k": 1_000, "thousand": 1_000,
    "lakh": 100_000, "lakhs": 100_000, "lac": 100_000, "lacs": 100_000,
    "crore": 10_000_000, "crores": 10_000_000, "cr": 10_000_000,
}

DATE_FORMATS = (
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%b-%Y %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y",
    "%d %b %Y %H:%M:%S", "%d %b %Y %H:%M", "%d %b %Y",
    "%d %B %Y %H:%M", "%d %B %Y",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%d-%m-%Y %I:%M %p", "%d/%m/%Y %I:%M %p", "%d-%b-%Y %I:%M %p",
)


def clean(value: str | None) -> str:
    """Collapse whitespace and normalise unicode. Never returns ``None``."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    text = text.replace(" ", " ").replace("​", "")
    return _WS.sub(" ", text).strip()


def normalize_key(value: str | None) -> str:
    """Lower-case, punctuation-free form used for label matching."""
    return _WS.sub(" ", _PUNCT.sub(" ", clean(value).lower())).strip()


def strip_label(value: str, label: str) -> str:
    """Remove a leading ``Label:`` prefix from a scraped cell."""
    cleaned = clean(value)
    pattern = re.compile(rf"^\s*{re.escape(label)}\s*[:\-–]\s*", re.IGNORECASE)
    return pattern.sub("", cleaned).strip()


def parse_datetime(
    value: str | None, tz: str = "Asia/Kolkata"
) -> datetime | None:
    """Parse a GeM-style date/time into a timezone-aware datetime.

    Returns ``None`` rather than guessing when the string is unparseable —
    an unknown deadline must never be silently treated as "today".
    """
    text = clean(value)
    if not text:
        return None
    text = re.sub(r"\b(ist|hrs|hours)\b", "", text, flags=re.IGNORECASE).strip()
    text = text.replace(",", " ")
    text = _WS.sub(" ", text)
    zone = ZoneInfo(tz)
    for fmt in DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=zone)
    # Last resort: pull a date-looking substring out of a longer sentence.
    match = re.search(
        r"\d{1,2}[-/ ][A-Za-z]{3,9}[-/ ]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?"
        r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?",
        text,
    )
    if match and match.group(0) != text:
        return parse_datetime(match.group(0), tz)
    return None


def parse_amount(value: str | None) -> float | None:
    """Extract an INR amount. Returns ``None`` when nothing parseable is found.

    ``0`` is a meaningful value (explicit "EMD: 0") and is returned as ``0.0``,
    which is why the return type is Optional rather than defaulting to zero.
    """
    text = clean(value)
    if not text:
        return None
    lowered = text.lower()
    no_digits = not re.search(r"\d", text)
    if no_digits and any(token in lowered for token in
                         ("nil", "not applicable", "n/a", "na ", "exempt")):
        return 0.0 if "nil" in lowered else None
    match = _AMOUNT.search(text)
    if not match:
        return None
    number = match.group("num").replace(",", "")
    try:
        amount = float(number)
    except ValueError:
        return None
    scale = (match.group("scale") or "").lower()
    if scale:
        amount *= _SCALES.get(scale, 1)
    return amount


def parse_percentage(value: str | None) -> float | None:
    text = clean(value)
    if not text:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if match:
        return float(match.group(1))
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:percent|per cent|pc)\b", text, re.IGNORECASE)
    return float(match.group(1)) if match else None


def parse_quantity(value: str | None) -> tuple[float | None, str]:
    """Split '50 Nos' into (50.0, 'Nos')."""
    text = clean(value)
    if not text:
        return None, ""
    match = re.search(r"(\d[\d,]*(?:\.\d+)?)\s*([A-Za-z./]+)?", text)
    if not match:
        return None, ""
    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None, ""
    return number, clean(match.group(2) or "")


def snippet(text: str, around: int, width: int = 240) -> str:
    """A trimmed verbatim quote centred on ``around`` — used as evidence."""
    if not text:
        return ""
    start = max(0, around - width // 2)
    end = min(len(text), around + width // 2)
    fragment = clean(text[start:end])
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{fragment}{suffix}"


def truncate(text: str, limit: int) -> str:
    text = clean(text)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
