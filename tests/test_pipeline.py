"""End-to-end run over offline fixtures.

This is the test that proves the pieces compose: seven bids in, two in the
report, one on the watchlist, four filtered out for stated reasons — with no
network, no Google, and no model call.
"""

from __future__ import annotations

from datetime import date

import pytest

from gem_intel.analyze.llm import LlmAnalyzer
from gem_intel.config import load_settings
from gem_intel.http_client import GemHttpClient
from gem_intel.models import TernaryFlag, Urgency
from gem_intel.pipeline import TenderPipeline
from gem_intel.report.builder import build_report_document
from gem_intel.report.markdown import render_markdown
from gem_intel.sources.gem_bidplus import FixtureSource
from gem_intel.store.database import TenderDatabase
from gem_intel.validate.rules import Rejection

TODAY = date(2026, 9, 6)


@pytest.fixture
def offline_settings(tmp_path):
    settings = load_settings(environ={})
    settings.raw["storage"]["database_url"] = f"sqlite:///{tmp_path}/tenders.db"
    settings.raw["storage"]["reports_dir"] = str(tmp_path / "reports")
    settings.raw["storage"]["artifacts_dir"] = str(tmp_path / "artifacts")
    settings.raw["google"]["enabled"] = False
    settings.raw["llm"]["enabled"] = False
    settings.raw["documents"]["download"] = False
    return settings


@pytest.fixture
def pipeline(offline_settings, fixtures_dir, tmp_path):
    source = FixtureSource(offline_settings, fixtures_dir, today=TODAY)
    client = GemHttpClient(offline_settings)
    database = TenderDatabase(tmp_path / "tenders.db")
    built = TenderPipeline(
        offline_settings, source=source, client=client, database=database,
        llm=LlmAnalyzer(offline_settings),
    )
    yield built
    built.close()


def test_end_to_end_run(pipeline):
    result = pipeline.run(report_date=TODAY)
    report = result.report

    bid_numbers = {t.bid_number for t in report.tenders}
    assert bid_numbers == {"GEM/2026/B/7100011", "GEM/2026/B/7100012"}

    watchlisted = {t.bid_number for t in report.watchlist}
    assert watchlisted == {"GEM/2026/B/7100015"}

    rejected = result.manifest.rejected
    assert rejected[Rejection.NOT_IT_RELATED.value] == 2   # furniture + lift AMC
    assert rejected[Rejection.NOT_JHARKHAND.value] == 1    # Kerala
    assert rejected[Rejection.EXPIRED.value] == 1          # closed three days ago


def test_urgency_and_scores_are_assigned(pipeline):
    report = pipeline.run(report_date=TODAY).report
    by_bid = {t.bid_number: t for t in report.tenders}
    assert by_bid["GEM/2026/B/7100012"].urgency is Urgency.URGENT       # closes tomorrow
    assert by_bid["GEM/2026/B/7100011"].urgency is Urgency.HIGH_PRIORITY
    assert all(0 < t.score.total <= 100 for t in report.tenders)
    # Ranked best-first.
    assert report.tenders == sorted(report.tenders, key=lambda t: -t.score.total)


def test_portal_emd_and_epbg_survive_the_pipeline(pipeline):
    report = pipeline.run(report_date=TODAY).report
    by_bid = {t.bid_number: t for t in report.tenders}
    laptops = by_bid["GEM/2026/B/7100011"]
    assert laptops.emd.status is TernaryFlag.REQUIRED
    assert laptops.emd.amount.value == 75000.0
    assert laptops.security.percentage.value == 3.0

    amc = by_bid["GEM/2026/B/7100012"]
    assert amc.emd.status is TernaryFlag.NOT_REQUIRED


def test_report_file_is_written_and_complete(pipeline, tmp_path):
    result = pipeline.run(report_date=TODAY)
    assert result.document_path.exists()
    text = result.document_path.read_text(encoding="utf-8")
    assert "GEM/2026/B/7100011" in text
    assert "Executive Summary" in text
    assert not result.doc_url          # Google disabled


def test_rerun_deduplicates_and_logs_no_spurious_changes(pipeline):
    first = pipeline.run(report_date=TODAY)
    assert first.manifest.new_tenders == 3

    second = pipeline.run(report_date=TODAY)
    assert second.manifest.new_tenders == 0
    assert second.manifest.updated_tenders == 3
    assert second.report.changes == []


def test_degraded_modes_are_reported(pipeline):
    result = pipeline.run(report_date=TODAY)
    assert any("AI document analysis unavailable" in mode
               for mode in result.manifest.degraded_modes)


def test_every_reported_tender_has_an_official_source(pipeline):
    report = pipeline.run(report_date=TODAY).report
    for tender in report.tenders + report.watchlist:
        assert tender.source_host.endswith("gem.gov.in")


def test_no_hallucinated_values_without_evidence(pipeline):
    """Anything the fixtures do not state must come through as unknown."""
    report = pipeline.run(report_date=TODAY).report
    amc = next(t for t in report.tenders if t.bid_number == "GEM/2026/B/7100012")
    assert not amc.estimated_value.known        # not published in the fixture
    text = render_markdown(build_report_document(report, "Report"))
    assert "Not published on GeM" in text
