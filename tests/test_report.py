"""Report assembly — the contract between analysis and what a human reads."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import make_tender

from gem_intel.models import (
    AccessIssue,
    DailyReport,
    OpportunityScore,
    RunManifest,
    TenderChange,
    TernaryFlag,
    Urgency,
)
from gem_intel.report.builder import build_report_document, report_title
from gem_intel.report.document import BlockType
from gem_intel.report.gdocs import GoogleDocsWriter
from gem_intel.report.markdown import render_markdown

IST = ZoneInfo("Asia/Kolkata")
TODAY = date(2026, 9, 6)


def manifest(**overrides) -> RunManifest:
    base = RunManifest(run_id="run-test", started_at=datetime(2026, 9, 6, 8, 30, tzinfo=IST))
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def scored(total: float, **overrides):
    tender = make_tender(**overrides)
    tender.score = OpportunityScore(total=total, band_label="STRONG OPPORTUNITY",
                                    band_emoji="🟢", rank_label="TOP PRIORITY",
                                    rank_emoji="🥇")
    tender.days_remaining = 4
    tender.urgency = Urgency.HIGH_PRIORITY
    tender.plain_summary = "The buyer wants 45 business laptops delivered to Ranchi."
    return tender


@pytest.fixture
def report():
    return DailyReport(report_date=TODAY, manifest=manifest(accepted=1),
                       tenders=[scored(88.0)])


def test_title_uses_the_required_format():
    title = report_title(TODAY, "GeM IT Tender Intelligence Report – {date:%d %B %Y}")
    assert title == "GeM IT Tender Intelligence Report – 06 September 2026"


def test_all_required_sections_are_present(report):
    text = render_markdown(build_report_document(report, "Report"))
    for heading in ["Executive Summary", "Priority Opportunities",
                    "Detailed Tender Analysis", "EMD Summary",
                    "Required Documents Summary", "Recommended Actions",
                    "Official GeM References", "Run & Data Quality"]:
        assert heading in text, heading


def test_tender_block_carries_every_required_field(report):
    text = render_markdown(build_report_document(report, "Report"))
    for label in ["Bid Number", "Buyer", "Location", "Product / Service Required",
                  "Closing Date", "Days Remaining", "Priority", "EMD",
                  "Performance Security / ePBG", "Required Documents",
                  "AI Opportunity Score", "Match Assessment",
                  "Recommended Next Action", "Official GeM Source"]:
        assert label in text, label


def test_unknown_emd_renders_as_a_warning_not_a_no(report):
    report.tenders[0].emd.status = TernaryFlag.NOT_STATED
    text = render_markdown(build_report_document(report, "Report"))
    assert "MANUAL VERIFICATION REQUIRED" in text
    assert "NO EMD REQUIRED" not in text


def test_empty_day_with_a_reachable_portal_reads_as_normal():
    report = DailyReport(report_date=TODAY, manifest=manifest())
    text = render_markdown(build_report_document(report, "Report"))
    assert "normal outcome on quiet days" in text


def test_empty_day_with_an_unreachable_portal_says_so_loudly():
    """The difference between 'nothing found' and 'we could not look'."""
    issue = AccessIssue(occurred_at=datetime(2026, 9, 6, 8, 31, tzinfo=IST),
                        stage="listing", target="https://bidplus.gem.gov.in/all-bids",
                        error_type="NETWORK_FAILURE")
    report = DailyReport(report_date=TODAY, manifest=manifest(access_issues=[issue]))
    text = render_markdown(build_report_document(report, "Report"))
    assert "does NOT mean there were no tenders" in text
    assert "GeM ACCESS ISSUE" in text


def test_filtered_counts_are_explained(report):
    report.manifest.rejected = {"not_jharkhand": 178, "not_it_related": 45}
    text = render_markdown(build_report_document(report, "Report"))
    assert "178 — no Jharkhand delivery / service location" in text


def test_change_log_is_rendered(report):
    report.changes = [TenderChange(
        identity_key=report.tenders[0].identity_key, field_name="Closing date",
        old_value="08 September 2026", new_value="12 September 2026",
        detected_on=TODAY)]
    text = render_markdown(build_report_document(report, "Report"))
    assert "Change Log" in text
    assert "08 September 2026 to 12 September 2026" in text


def test_watchlist_is_separate_from_the_main_report(report):
    future = scored(90.0, bid_number="GEM/2026/B/9000009")
    future.days_remaining = 25
    future.urgency = Urgency.WATCHLIST
    report.watchlist = [future]
    text = render_markdown(build_report_document(report, "Report"))
    assert "Watchlist — Future High-Value Tenders" in text
    assert report.total_found == 1          # watchlist is not counted in the headline


def test_rules_only_analysis_is_disclosed(report):
    report.tenders[0].analysis_mode = "rules-only"
    text = render_markdown(build_report_document(report, "Report"))
    assert "without AI document reading" in text


def test_executive_counters(report):
    report.tenders[0].emd.status = TernaryFlag.REQUIRED
    urgent = scored(70.0, bid_number="GEM/2026/B/9000002")
    urgent.urgency = Urgency.URGENT
    urgent.emd.status = TernaryFlag.NOT_REQUIRED
    report.tenders.append(urgent)
    assert report.total_found == 2
    assert report.strong == 1
    assert report.good == 1
    assert report.urgent == 1
    assert report.with_emd == 1
    assert report.without_emd == 1


def test_no_bid_submission_disclaimer_is_present(report):
    text = render_markdown(build_report_document(report, "Report"))
    assert "does not submit bids" in text


# -- Google Docs index arithmetic --------------------------------------
def test_gdocs_segments_line_up_with_the_inserted_text(report):
    doc = build_report_document(report, "Report")
    text, segments = GoogleDocsWriter._flatten(doc)
    assert segments[0].start == 1
    for segment in segments:
        # Docs indices are 1-based over the body; slice back to verify.
        assert text[segment.start - 1:segment.end - 1].endswith("\n")
    assert segments[-1].end - 1 == len(text)


def test_gdocs_headings_and_bullets_produce_requests(report):
    doc = build_report_document(report, "Report")
    _, segments = GoogleDocsWriter._flatten(doc)
    requests = GoogleDocsWriter._style_requests(segments)
    kinds = {next(iter(r)) for r in requests}
    assert "updateParagraphStyle" in kinds
    assert "createParagraphBullets" in kinds
    assert "updateTextStyle" in kinds
    for request in requests:
        span = next(iter(request.values())).get("range")
        if span:
            assert span["startIndex"] < span["endIndex"]


def test_markdown_renders_every_block_type():
    from gem_intel.report.document import ReportDocument

    doc = ReportDocument("T")
    doc.heading("H1", 1).heading("H2", 2).heading("H3", 3)
    doc.paragraph("plain").paragraph("bold", bold=True)
    doc.keyvalue("Key", "Value").bullets(["a", "b"]).callout("note").divider().spacer()
    text = render_markdown(doc)
    for expected in ["# T", "## H1", "### H2", "#### H3", "plain", "**bold**",
                     "**Key:** Value", "- a", "> note", "---"]:
        assert expected in text
    assert {b.type for b in doc} >= {BlockType.TITLE, BlockType.BULLET,
                                     BlockType.CALLOUT, BlockType.DIVIDER}
