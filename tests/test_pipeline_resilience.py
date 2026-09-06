"""The pipeline must never crash on a mid-run access challenge.

Before this hardening, ``AccessBlocked`` raised from a detail-page fetch
propagated straight out of ``TenderPipeline._analyze`` and crashed the whole
run — losing every tender already gathered that day, not just the one whose
detail page triggered the challenge. This file pins the fix: a CAPTCHA seen
partway through detail-fetching degrades the run and still produces a report.
"""

from __future__ import annotations

from datetime import date

import pytest

from gem_intel.analyze.llm import LlmAnalyzer
from gem_intel.config import load_settings
from gem_intel.http_client import AccessBlocked, GemHttpClient
from gem_intel.pipeline import TenderPipeline
from gem_intel.sources.base import SourceAdapter, SourceResult
from gem_intel.sources.gem_bidplus import FixtureSource
from gem_intel.store.database import TenderDatabase

TODAY = date(2026, 9, 6)


class CaptchaOnSecondDetailFetch(SourceAdapter):
    """Wraps a real FixtureSource but blocks partway through detail fetching."""

    name = "captcha-after-first-detail-fetch (test double)"

    def __init__(self, inner: FixtureSource) -> None:
        self.inner = inner
        self.detail_calls = 0

    def search(self, queries) -> SourceResult:
        return self.inner.search(queries)

    def fetch_detail(self, tender):
        self.detail_calls += 1
        if self.detail_calls >= 2:
            raise AccessBlocked("human-verification challenge", tender.source_url)
        return self.inner.fetch_detail(tender)


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


def test_captcha_mid_run_degrades_instead_of_crashing(offline_settings, fixtures_dir, tmp_path):
    inner = FixtureSource(offline_settings, fixtures_dir, today=TODAY)
    source = CaptchaOnSecondDetailFetch(inner)
    client = GemHttpClient(offline_settings)
    database = TenderDatabase(tmp_path / "tenders.db")
    pipeline = TenderPipeline(
        offline_settings, source=source, client=client, database=database,
        llm=LlmAnalyzer(offline_settings),
    )
    try:
        result = pipeline.run(report_date=TODAY)      # must not raise
    finally:
        pipeline.close()

    assert source.detail_calls >= 2
    assert any("human-verification challenge" in mode
              for mode in result.manifest.degraded_modes)
    # The report still exists — the run did not lose everything it had.
    assert result.document_path.exists()


def test_tenders_after_the_challenge_are_flagged_not_dropped(offline_settings, fixtures_dir,
                                                              tmp_path):
    inner = FixtureSource(offline_settings, fixtures_dir, today=TODAY)
    source = CaptchaOnSecondDetailFetch(inner)
    client = GemHttpClient(offline_settings)
    database = TenderDatabase(tmp_path / "tenders.db")
    pipeline = TenderPipeline(
        offline_settings, source=source, client=client, database=database,
        llm=LlmAnalyzer(offline_settings),
    )
    try:
        result = pipeline.run(report_date=TODAY)
    finally:
        pipeline.close()

    everything = result.report.tenders + result.report.watchlist
    # At least one tender never got its detail page and must say so, rather
    # than silently reporting listing-only data as if it were complete.
    flagged = [t for t in everything
              if any("verification challenge" in f for f in t.verification_flags)]
    assert flagged, "expected at least one tender flagged as detail-fetch-blocked"
