"""Normalisation and parsing helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from gem_intel.extract.text import (
    clean,
    normalize_key,
    parse_amount,
    parse_datetime,
    parse_percentage,
    parse_quantity,
    strip_label,
    truncate,
)

IST = ZoneInfo("Asia/Kolkata")


@pytest.mark.parametrize("raw,expected", [
    ("15-09-2026 15:00:00", datetime(2026, 9, 15, 15, 0, tzinfo=IST)),
    ("15/09/2026 15:00", datetime(2026, 9, 15, 15, 0, tzinfo=IST)),
    ("15-Sep-2026", datetime(2026, 9, 15, 0, 0, tzinfo=IST)),
    ("06 September 2026", datetime(2026, 9, 6, 0, 0, tzinfo=IST)),
    ("2026-09-15 15:00:00", datetime(2026, 9, 15, 15, 0, tzinfo=IST)),
    ("Bid closes on 15-09-2026 15:00 IST", datetime(2026, 9, 15, 15, 0, tzinfo=IST)),
])
def test_parse_datetime_formats(raw, expected):
    assert parse_datetime(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "not a date", "TBD", "as per schedule"])
def test_unparseable_dates_return_none(raw):
    """An unknown deadline must stay unknown — never silently become today."""
    assert parse_datetime(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("Rs. 1,20,000", 120000.0),
    ("₹75,000", 75000.0),
    ("INR 2,50,000.50", 250000.50),
    ("2.5 Crore", 25_000_000.0),
    ("10 Lakh", 1_000_000.0),
    ("0", 0.0),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == pytest.approx(expected)


def test_parse_amount_returns_none_when_absent():
    assert parse_amount("to be intimated later") is None
    assert parse_amount("") is None


def test_nil_is_zero_not_none():
    """'NIL' is a real answer (no EMD); it must not read as 'unknown'."""
    assert parse_amount("NIL") == 0.0


def test_parse_percentage():
    assert parse_percentage("3% of contract value") == 3.0
    assert parse_percentage("5 percent") == 5.0
    assert parse_percentage("3.00") is None       # bare number needs a caller hint


def test_parse_quantity():
    assert parse_quantity("45 Nos") == (45.0, "Nos")
    assert parse_quantity("1,200 Units") == (1200.0, "Units")
    assert parse_quantity("") == (None, "")


def test_normalize_key_ignores_punctuation_and_case():
    assert normalize_key("Bid End Date/Time") == "bid end date time"
    assert normalize_key("ePBG Percentage(%)") == "epbg percentage"


def test_strip_label():
    assert strip_label("Quantity: 45 Nos", "Quantity") == "45 Nos"
    assert strip_label("Quantity - 45", "Quantity") == "45"


def test_clean_collapses_nbsp_and_whitespace():
    assert clean("Ranchi,   Jharkhand \n") == "Ranchi, Jharkhand"


def test_truncate_adds_ellipsis():
    assert truncate("abcdefghij", 5) == "abcd…"
    assert truncate("abc", 10) == "abc"
