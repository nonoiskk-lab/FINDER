"""Shared test fixtures."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from gem_intel.config import load_settings  # noqa: E402
from gem_intel.models import Tender  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
# A fixed "now" so deadline maths in tests never depends on the wall clock.
NOW = datetime(2026, 9, 6, 8, 30, tzinfo=IST)


@pytest.fixture
def settings():
    return load_settings(REPO_ROOT / "config", environ={})


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def now() -> datetime:
    return NOW


def make_tender(**overrides) -> Tender:
    """A minimally valid tender, so each test only states what it cares about."""
    defaults = {
        "bid_number": "GEM/2026/B/9000001",
        "source_url": "https://bidplus.gem.gov.in/showbidDocument/9000001",
        "title": "Supply of Laptop for office use",
        "item_description": "Supply of Laptop (Notebook Computer)",
        "category": "Laptop - Business",
        "buyer_organization": "Department of Higher Education, Government of Jharkhand",
        "buyer_address": "Nepal House, Doranda, Ranchi, Jharkhand 834002",
        "delivery_location": "Ranchi, Jharkhand",
        "quantity": 45.0,
        "quantity_unit": "Nos",
        "bid_end_at": datetime(2026, 9, 10, 15, 0, tzinfo=IST),
    }
    defaults.update(overrides)
    return Tender(**defaults)
