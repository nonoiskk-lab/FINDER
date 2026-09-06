"""Deadline arithmetic and urgency banding."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_tender

from gem_intel.analyze.deadline import DeadlineAnalyzer
from gem_intel.models import Urgency

IST = ZoneInfo("Asia/Kolkata")
NOW = datetime(2026, 9, 6, 8, 30, tzinfo=IST)


@pytest.fixture
def analyzer(settings):
    return DeadlineAnalyzer(settings)


@pytest.mark.parametrize("closing,expected_days", [
    (datetime(2026, 9, 6, 15, 0, tzinfo=IST), 0),     # later today
    (datetime(2026, 9, 7, 9, 0, tzinfo=IST), 1),
    (datetime(2026, 9, 11, 15, 0, tzinfo=IST), 5),
    (datetime(2026, 9, 16, 15, 0, tzinfo=IST), 10),
    (datetime(2026, 9, 17, 15, 0, tzinfo=IST), 11),
])
def test_days_remaining(analyzer, closing, expected_days):
    assert analyzer.days_remaining(closing, NOW) == expected_days


def test_deadline_earlier_today_counts_as_passed(analyzer):
    """08:30 now, closed at 08:00 — the calendar day matches but it is gone."""
    closed = datetime(2026, 9, 6, 8, 0, tzinfo=IST)
    assert analyzer.days_remaining(closed, NOW) == -1
    assert analyzer.urgency_for(-1) is Urgency.EXPIRED


def test_unknown_closing_date_stays_unknown(analyzer):
    assert analyzer.days_remaining(None, NOW) is None
    assert analyzer.urgency_for(None) is Urgency.UNKNOWN


@pytest.mark.parametrize("days,expected", [
    (0, Urgency.URGENT), (2, Urgency.URGENT),
    (3, Urgency.HIGH_PRIORITY), (5, Urgency.HIGH_PRIORITY),
    (6, Urgency.ACTION_REQUIRED), (10, Urgency.ACTION_REQUIRED),
    (11, Urgency.WATCHLIST), (60, Urgency.WATCHLIST),
])
def test_urgency_bands(analyzer, days, expected):
    assert analyzer.urgency_for(days) is expected


def test_window_membership(analyzer):
    inside = make_tender(bid_end_at=NOW + timedelta(days=4))
    outside = make_tender(bid_end_at=NOW + timedelta(days=20))
    analyzer.apply(inside, NOW)
    analyzer.apply(outside, NOW)
    assert analyzer.in_main_window(inside)
    assert not analyzer.in_main_window(outside)
    assert analyzer.in_watchlist_window(outside)


def test_missing_deadline_raises_a_verification_flag(analyzer):
    tender = make_tender(bid_end_at=None)
    analyzer.apply(tender, NOW)
    assert tender.days_remaining is None
    assert any("closing date" in flag.lower() for flag in tender.verification_flags)


def test_naive_datetime_is_treated_as_ist(analyzer):
    naive = datetime(2026, 9, 10, 15, 0)
    assert analyzer.days_remaining(naive, NOW) == 4
