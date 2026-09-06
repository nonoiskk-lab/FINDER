"""Duplicate detection and the change log."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_tender

from gem_intel.models import MoneyClaim, TernaryFlag
from gem_intel.store.database import TenderDatabase

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 9, 6)
TOMORROW = date(2026, 9, 7)


@pytest.fixture
def database(tmp_path):
    db = TenderDatabase(tmp_path / "test.db")
    yield db
    db.close()


def test_first_sighting_is_new(database):
    is_new, changes = database.upsert(make_tender(), TODAY)
    assert is_new and not changes


def test_second_sighting_is_an_update_not_a_duplicate(database):
    database.upsert(make_tender(), TODAY)
    is_new, _ = database.upsert(make_tender(), TOMORROW)
    assert not is_new
    assert len(database.open_tenders()) == 1


def test_first_seen_date_is_preserved_across_runs(database):
    database.upsert(make_tender(), TODAY)
    tender = make_tender()
    database.upsert(tender, TOMORROW)
    assert tender.first_seen_on == TODAY
    assert tender.last_seen_on == TOMORROW


def test_identity_falls_back_when_the_bid_number_is_missing():
    url = "https://bidplus.gem.gov.in/showbidDocument/999"
    a = make_tender(bid_number="", source_url=url)
    b = make_tender(bid_number="", source_url=url)
    different = make_tender(bid_number="", source_url=url + "9")
    assert a.identity_key == b.identity_key
    assert a.identity_key != different.identity_key


def test_identical_titles_from_different_buyers_are_distinct():
    """Never dedupe on title — buyers reuse the same wording constantly."""
    a = make_tender(bid_number="", source_url="",
                    buyer_organization="Ranchi Municipal Corporation")
    b = make_tender(bid_number="", source_url="",
                    buyer_organization="Dhanbad Municipal Corporation")
    assert a.identity_key != b.identity_key


def test_closing_date_extension_is_logged(database):
    database.upsert(make_tender(), TODAY)
    extended = make_tender(bid_end_at=datetime(2026, 9, 14, 15, 0, tzinfo=IST))
    _, changes = database.upsert(extended, TOMORROW)
    assert len(changes) == 1
    assert changes[0].field_name == "Closing date"
    rendered = changes[0].render()
    assert "10 September 2026" in rendered and "14 September 2026" in rendered


def test_emd_change_is_logged(database):
    original = make_tender()
    original.emd.status = TernaryFlag.NOT_REQUIRED
    database.upsert(original, TODAY)

    amended = make_tender()
    amended.emd.status = TernaryFlag.REQUIRED
    amended.emd.amount = MoneyClaim(value=50000.0)
    _, changes = database.upsert(amended, TOMORROW)
    fields = {c.field_name for c in changes}
    assert "EMD requirement" in fields and "EMD amount" in fields


def test_first_disclosure_is_annotated_not_reported_as_an_amendment(database):
    database.upsert(make_tender(), TODAY)
    amended = make_tender()
    amended.emd.status = TernaryFlag.REQUIRED
    _, changes = database.upsert(amended, TOMORROW)
    emd_change = next(c for c in changes if c.field_name == "EMD requirement")
    assert "first time" in emd_change.note


def test_no_change_means_no_log_entry(database):
    database.upsert(make_tender(), TODAY)
    _, changes = database.upsert(make_tender(), TOMORROW)
    assert changes == []


def test_changes_are_queryable_by_day(database):
    database.upsert(make_tender(), TODAY)
    extended = make_tender(bid_end_at=datetime(2026, 9, 20, 15, 0, tzinfo=IST))
    database.upsert(extended, TOMORROW)
    database.conn.commit()
    assert database.changes_for(extended.identity_key, TOMORROW)
    assert not database.changes_for(extended.identity_key, TODAY)


def test_expired_tenders_are_closed_out(database):
    past = make_tender(bid_end_at=datetime(2026, 9, 1, 15, 0, tzinfo=IST))
    database.upsert(past, TODAY)
    database.conn.commit()
    assert database.mark_expired(TODAY) == 1
    assert database.open_tenders() == []


def test_report_url_is_stamped_onto_tenders(database):
    tender = make_tender()
    database.upsert(tender, TODAY)
    database.conn.commit()
    database.set_report_url([tender.identity_key], "https://docs.google.com/document/d/x")
    assert database.get(tender.identity_key)["report_doc_url"].endswith("/x")


def test_upsert_many_reports_new_and_changed(database):
    first = [make_tender(), make_tender(bid_number="GEM/2026/B/9000002")]
    new, changes = database.upsert_many(first, TODAY)
    assert len(new) == 2 and not changes

    second = [make_tender(bid_end_at=datetime(2026, 9, 12, 15, 0, tzinfo=IST)),
              make_tender(bid_number="GEM/2026/B/9000003")]
    new, changes = database.upsert_many(second, TOMORROW)
    assert len(new) == 1
    assert any(c.field_name == "Closing date" for c in changes)
