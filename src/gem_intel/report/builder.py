"""Builds the daily report document from analysed tenders.

Section order follows the brief:

    1. Executive Summary          6. Required Documents Summary
    2. Priority Opportunities     7. Eligibility Risks
    3. Urgent Deadlines           8. Recommended Actions
    4. Detailed Tender Analysis   9. Official GeM References
    5. EMD Summary               (+ Watchlist, Change Log, Run & Data Quality)

Two additions the brief implies but does not name: a **Change Log** (section
18's amendment tracking has to surface somewhere) and a **Run & Data Quality**
section, which is what turns "0 tenders found" from an ambiguous result into
an auditable one.
"""

from __future__ import annotations

from datetime import date, datetime

from gem_intel.analyze.security import render_security_line
from gem_intel.models import (
    Confidence,
    DailyReport,
    Tender,
    TenderKind,
    TernaryFlag,
    Urgency,
)
from gem_intel.report.document import ReportDocument

URGENCY_EMOJI = {
    Urgency.URGENT: "🔴",
    Urgency.HIGH_PRIORITY: "🟠",
    Urgency.ACTION_REQUIRED: "🟡",
    Urgency.WATCHLIST: "🔭",
    Urgency.EXPIRED: "⛔",
    Urgency.UNKNOWN: "⚪",
}
KIND_LABEL = {
    TenderKind.PRODUCT_SUPPLY: "Product supply",
    TenderKind.SERVICE: "Service contract",
    TenderKind.SUPPLY_AND_SERVICE: "Supply + service",
    TenderKind.UNKNOWN: "Type not clear",
}


def build_report_document(report: DailyReport, title: str) -> ReportDocument:
    doc = ReportDocument(title)
    _executive_summary(doc, report)
    _data_quality(doc, report)
    _priority_opportunities(doc, report)
    _urgent_deadlines(doc, report)
    _detailed_analysis(doc, report)
    _emd_summary(doc, report)
    _documents_summary(doc, report)
    _eligibility_risks(doc, report)
    _recommended_actions(doc, report)
    _watchlist(doc, report)
    _change_log(doc, report)
    _references(doc, report)
    _footer(doc, report)
    return doc


# ----------------------------------------------------------------------
def _executive_summary(doc: ReportDocument, report: DailyReport) -> None:
    doc.heading("📊 Executive Summary", 1)
    doc.paragraph(
        f"Report date: {report.report_date.strftime('%d %B %Y')} "
        f"({report.manifest.timezone}). "
        f"Source: official Government e-Marketplace portal only."
    )
    doc.keyvalue("Tenders in today's report", str(report.total_found))
    doc.keyvalue("Strong opportunities (80–100)", str(report.strong))
    doc.keyvalue("Good opportunities (60–79)", str(report.good))
    doc.keyvalue("Urgent deadlines (closing in 0–2 days)", str(report.urgent))
    doc.keyvalue("Tenders requiring EMD", str(report.with_emd))
    doc.keyvalue("Tenders with no EMD", str(report.without_emd))
    if report.emd_unclear:
        doc.keyvalue("Tenders where EMD is unclear", f"{report.emd_unclear} ⚠")
    if report.watchlist:
        doc.keyvalue("On the future watchlist", str(len(report.watchlist)))
    if report.changes:
        doc.keyvalue("Amendments detected since the last run", str(len(report.changes)))

    if report.total_found == 0:
        doc.spacer()
        if report.manifest.source_reachable:
            doc.callout(
                "No GeM tender met all six checks today (official source, active, "
                "IT category, Jharkhand location, valid closing date, closing within "
                f"{report.manifest.rejected and '10' or '10'} days). "
                "See Run & Data Quality below for exactly what was filtered out — "
                "this is a normal outcome on quiet days."
            )
        else:
            doc.callout(
                "⚠ The GeM portal could not be searched successfully in this run, so "
                "an empty report does NOT mean there were no tenders. See Run & Data "
                "Quality below and re-run, or search the portal manually."
            )
    doc.divider()


def _data_quality(doc: ReportDocument, report: DailyReport) -> None:
    manifest = report.manifest
    doc.heading("🔍 Run & Data Quality", 1)
    doc.paragraph(
        f"Run {manifest.run_id} · {len(manifest.queries_executed)} search terms · "
        f"{manifest.listings_seen} bids seen · {manifest.details_fetched} detail pages "
        f"read · {manifest.documents_downloaded} documents analysed."
    )
    if manifest.rejected:
        from gem_intel.validate.rules import HUMAN_REASONS, Rejection

        lines = []
        for reason, count in sorted(manifest.rejected.items(), key=lambda kv: -kv[1]):
            try:
                label = HUMAN_REASONS[Rejection(reason)]
            except ValueError:
                label = reason.replace("_", " ")
            lines.append(f"{count} — {label}")
        doc.paragraph("Candidates filtered out:")
        doc.bullets(lines)

    if manifest.access_issues:
        doc.paragraph("⚠ GeM ACCESS ISSUES", bold=True)
        doc.bullets([issue.render() for issue in manifest.access_issues])
        doc.callout(
            "Where the portal could not be reached, nothing has been substituted or "
            "estimated. Those tenders are simply absent from this report."
        )
    if manifest.degraded_modes:
        doc.paragraph("Degraded modes in this run:")
        doc.bullets(manifest.degraded_modes)
    doc.divider()


def _priority_opportunities(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders:
        return
    doc.heading("🏆 Priority Opportunities", 1)
    ranks = [
        ("🥇 TOP PRIORITY", lambda t: t.score.total >= 80),
        ("🥈 GOOD OPPORTUNITY", lambda t: 60 <= t.score.total < 80),
        ("🥉 REVIEW REQUIRED", lambda t: 40 <= t.score.total < 60),
        ("❌ LOW PRIORITY", lambda t: t.score.total < 40),
    ]
    for label, predicate in ranks:
        group = [t for t in report.tenders if predicate(t)]
        if not group:
            continue
        doc.heading(f"{label} ({len(group)})", 2)
        doc.bullets([_one_liner(t) for t in group])
    doc.divider()


def _urgent_deadlines(doc: ReportDocument, report: DailyReport) -> None:
    urgent = report.by_urgency(Urgency.URGENT)
    high = report.by_urgency(Urgency.HIGH_PRIORITY)
    if not urgent and not high:
        return
    doc.heading("⏰ Urgent Deadlines", 1)
    if urgent:
        doc.heading("🔴 Closing within 2 days", 2)
        doc.bullets([_deadline_line(t) for t in urgent])
    if high:
        doc.heading("🟠 Closing within 3–5 days", 2)
        doc.bullets([_deadline_line(t) for t in high])
    doc.divider()


def _detailed_analysis(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders:
        return
    doc.heading("📋 Detailed Tender Analysis", 1)
    for index, tender in enumerate(report.tenders, start=1):
        _tender_section(doc, tender, index)
    doc.divider()


def _tender_section(doc: ReportDocument, tender: Tender, index: int) -> None:
    emoji = URGENCY_EMOJI.get(tender.urgency, "")
    doc.heading(f"TENDER #{index} — {tender.title or tender.bid_number}", 2)

    doc.keyvalue("Bid Number", tender.bid_number or "⚠ Not available")
    if tender.gem_tender_id:
        doc.keyvalue("GeM Tender ID", tender.gem_tender_id)
    doc.keyvalue("Buyer", _buyer_line(tender))
    doc.keyvalue("Location", _location_line(tender))
    doc.keyvalue("Product / Service Required",
                 tender.plain_summary or "⚠ Summary unavailable")
    doc.keyvalue("Type", KIND_LABEL[tender.classification.kind])
    doc.keyvalue("Quantity / Scope", _quantity_line(tender))
    doc.keyvalue("Closing Date", _closing_line(tender))
    doc.keyvalue("Days Remaining",
                 f"{tender.days_remaining} days" if tender.days_remaining is not None
                 else "⚠ Verification Required")
    doc.keyvalue("Priority", f"{emoji} {tender.urgency.value}")
    doc.keyvalue("Estimated Value",
                 tender.estimated_value.render("Not published on GeM"))

    # -- EMD ------------------------------------------------------------
    doc.keyvalue("EMD", tender.emd.headline())
    if tender.emd.status is TernaryFlag.REQUIRED:
        details = [f"Amount: {tender.emd.amount.render()}"]
        if tender.emd.payment_mode.known:
            details.append(f"Payment: {tender.emd.payment_mode.value}")
        if tender.emd.last_date.known:
            details.append(f"Last date: {tender.emd.last_date.value}")
        if tender.emd.msme_exemption is TernaryFlag.REQUIRED:
            details.append("MSME exemption appears available")
        if tender.emd.startup_exemption is TernaryFlag.REQUIRED:
            details.append("Startup exemption appears available")
        if tender.emd.exemption_proof_required:
            details.append("Exemption proof: "
                           + ", ".join(tender.emd.exemption_proof_required))
        doc.bullets(details)

    doc.keyvalue("Performance Security / ePBG", render_security_line(tender.security))

    # -- requirements ---------------------------------------------------
    if tender.required_documents:
        doc.paragraph("Required Documents", bold=True)
        doc.bullets([d.display for d in tender.required_documents])
    else:
        doc.paragraph("Required Documents", bold=True)
        doc.bullets(["⚠ Not stated in the source we could read — check the bid on GeM"])

    if tender.eligibility_requirements:
        doc.paragraph("Important Eligibility Requirements", bold=True)
        doc.bullets(tender.eligibility_requirements[:8])
    if tender.technical_requirements:
        doc.paragraph("Key Technical Requirements", bold=True)
        doc.bullets(tender.technical_requirements[:8])
    delivery = tender.delivery_requirements + tender.warranty_requirements + \
        tender.amc_requirements
    if delivery:
        doc.paragraph("Delivery / Warranty / AMC Requirements", bold=True)
        doc.bullets(delivery[:8])
    if tender.payment_terms.known:
        doc.keyvalue("Payment Terms", str(tender.payment_terms.value))
    if tender.special_conditions:
        doc.paragraph("Special Conditions", bold=True)
        doc.bullets(tender.special_conditions[:6])

    # -- assessment -----------------------------------------------------
    doc.keyvalue("AI Opportunity Score",
                 f"{tender.score.total:.0f} / 100  {tender.score.band_emoji} "
                 f"{tender.score.band_label}")
    doc.bullets(tender.score.as_lines())
    doc.keyvalue("Match Assessment",
                 f"{tender.eligibility.emoji} {tender.eligibility.match_level.value} — "
                 f"{tender.eligibility.verdict}")
    if tender.eligibility.supporting_points:
        doc.paragraph("Why this could suit us", bold=True)
        doc.bullets(tender.eligibility.supporting_points)
    if tender.eligibility.gaps:
        doc.paragraph("Requirement gaps", bold=True)
        doc.bullets(tender.eligibility.gaps)
    if tender.eligibility.verification_points:
        doc.paragraph("To verify before bidding", bold=True)
        doc.bullets([f"⚠ {p}" for p in tender.eligibility.verification_points])

    if tender.why_it_matters:
        doc.keyvalue("Why This Tender Matters", tender.why_it_matters)
    if tender.risks:
        doc.paragraph("Important Risks", bold=True)
        doc.bullets([f"⚠ {r}" for r in tender.risks])
    if tender.verification_flags:
        doc.paragraph("Verification Points", bold=True)
        doc.bullets([f"⚠ {f}" for f in tender.verification_flags])

    doc.keyvalue("Recommended Next Action",
                 tender.next_action or _default_action(tender))
    doc.keyvalue("Official GeM Source", tender.source_url or "⚠ Not available")
    if tender.analysis_mode != "rules+llm":
        doc.callout(
            "This tender was analysed without AI document reading "
            f"({tender.analysis_mode}); treat the summary as indicative and open the "
            "bid on GeM before acting."
        )
    doc.spacer()


def _emd_summary(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders:
        return
    doc.heading("💰 EMD Summary", 1)
    required = [t for t in report.tenders if t.emd.status is TernaryFlag.REQUIRED]
    free = [t for t in report.tenders if t.emd.status is TernaryFlag.NOT_REQUIRED]
    unclear = [t for t in report.tenders if t.emd.status is TernaryFlag.NOT_STATED]

    if free:
        doc.paragraph(f"✅ No EMD required ({len(free)})", bold=True)
        doc.bullets([f"{t.bid_number} — {t.title[:90]}" for t in free])
    if required:
        total = sum(float(t.emd.amount.value) for t in required if t.emd.amount.known)
        doc.paragraph(f"💰 EMD required ({len(required)})", bold=True)
        doc.bullets([
            f"{t.bid_number} — {t.emd.amount.render()} — {t.title[:80]}"
            for t in required
        ])
        if total:
            doc.paragraph(
                f"Total EMD across these tenders: ₹{total:,.0f} "
                "(before any MSME/Startup exemption)."
            )
    if unclear:
        doc.paragraph(f"⚠ EMD not clearly stated ({len(unclear)}) — verify manually",
                      bold=True)
        doc.bullets([f"{t.bid_number} — {t.title[:90]}" for t in unclear])
    doc.divider()


def _documents_summary(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders:
        return
    doc.heading("📎 Required Documents Summary", 1)
    counts: dict[str, int] = {}
    for tender in report.tenders:
        for document in tender.required_documents:
            counts[document.name] = counts.get(document.name, 0) + 1
    if not counts:
        doc.paragraph("No document requirements could be read from today's tenders.")
        doc.divider()
        return
    doc.paragraph("Across today's tenders, keep these ready:")
    doc.bullets([
        f"{name} — needed by {count} of {len(report.tenders)} tenders"
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ])
    doc.divider()


def _eligibility_risks(doc: ReportDocument, report: DailyReport) -> None:
    risky = [t for t in report.tenders if t.eligibility.gaps]
    if not risky:
        return
    doc.heading("⚠ Eligibility Risks", 1)
    for tender in risky:
        doc.paragraph(f"{tender.bid_number} — {tender.title[:90]}", bold=True)
        doc.bullets(tender.eligibility.gaps)
    doc.divider()


def _recommended_actions(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders:
        return
    doc.heading("✅ Recommended Actions", 1)
    ordered = sorted(
        report.tenders,
        key=lambda t: (t.days_remaining if t.days_remaining is not None else 99,
                       -t.score.total),
    )
    doc.bullets([
        f"{URGENCY_EMOJI.get(t.urgency, '')} {t.bid_number} "
        f"({t.days_remaining if t.days_remaining is not None else '?'} days) — "
        f"{t.next_action or _default_action(t)}"
        for t in ordered
    ])
    doc.callout(
        "This system does not submit bids, pay EMD, or sign anything. Every action "
        "above needs a human decision on the GeM portal."
    )
    doc.divider()


def _watchlist(doc: ReportDocument, report: DailyReport) -> None:
    if not report.watchlist:
        return
    doc.heading("🔭 Watchlist — Future High-Value Tenders", 1)
    doc.paragraph(
        "These close later than the 10-day window but scored highly enough to "
        "prepare for now."
    )
    doc.bullets([
        f"{t.score.total:.0f}/100 · {t.bid_number} · closes in {t.days_remaining} days "
        f"· {t.title[:80]}"
        for t in report.watchlist
    ])
    doc.divider()


def _change_log(doc: ReportDocument, report: DailyReport) -> None:
    if not report.changes:
        return
    doc.heading("📝 Change Log", 1)
    doc.paragraph("Amendments detected against tenders already in the database:")
    by_tender: dict[str, list[str]] = {}
    for change in report.changes:
        by_tender.setdefault(change.identity_key, []).append(change.render())
    lookup = {t.identity_key: t for t in report.tenders + report.watchlist}
    for key, lines in by_tender.items():
        tender = lookup.get(key)
        label = f"{tender.bid_number} — {tender.title[:70]}" if tender else key
        doc.paragraph(label, bold=True)
        doc.bullets(lines)
    doc.divider()


def _references(doc: ReportDocument, report: DailyReport) -> None:
    if not report.tenders and not report.watchlist:
        return
    doc.heading("🔗 Official GeM References", 1)
    doc.paragraph(
        "Every tender above is traceable to the official GeM portal. Nothing in this "
        "report comes from a tender aggregator or third-party listing site."
    )
    doc.bullets([
        f"{t.bid_number} — {t.source_url}"
        for t in report.tenders + report.watchlist if t.source_url
    ])
    doc.divider()


def _footer(doc: ReportDocument, report: DailyReport) -> None:
    manifest = report.manifest
    finished = manifest.finished_at or datetime.now()
    doc.paragraph(
        f"Generated automatically at {finished.strftime('%d %B %Y %H:%M')} "
        f"{manifest.timezone} · run {manifest.run_id} · "
        f"discovery and analysis only, no bids submitted."
    )


# ----------------------------------------------------------------------
def _one_liner(tender: Tender) -> str:
    days = tender.days_remaining
    return (
        f"{tender.score.total:.0f}/100 · {URGENCY_EMOJI.get(tender.urgency, '')} "
        f"{days if days is not None else '?'} days · {tender.bid_number} · "
        f"{tender.title[:80]} · {tender.emd.headline().split(' ', 1)[-1]}"
    )


def _deadline_line(tender: Tender) -> str:
    closing = tender.bid_end_at.strftime("%d %b %Y %H:%M") if tender.bid_end_at else "?"
    return (
        f"{tender.bid_number} — closes {closing} "
        f"({tender.days_remaining} days) — {tender.title[:70]} — "
        f"score {tender.score.total:.0f}/100"
    )


def _buyer_line(tender: Tender) -> str:
    parts = [tender.buyer_organization, tender.department, tender.ministry]
    seen: list[str] = []
    for part in parts:
        cleaned = (part or "").strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return " · ".join(seen) or "⚠ Not available"


def _location_line(tender: Tender) -> str:
    location = (tender.delivery_location or tender.service_location
                or tender.buyer_address or "")
    districts = ", ".join(tender.geo.districts)
    if districts and districts.lower() not in location.lower():
        location = f"{location} ({districts})" if location else districts
    suffix = ""
    if tender.geo.confidence is not Confidence.CONFIRMED:
        suffix = f"  ⚠ {tender.geo.reason}"
    return (location or "⚠ Not stated") + suffix


def _quantity_line(tender: Tender) -> str:
    if tender.quantity:
        return f"{tender.quantity:g} {tender.quantity_unit or 'units'}"
    if tender.classification.kind is TenderKind.SERVICE:
        return "Service contract — see scope of work"
    return "⚠ Not stated"


def _closing_line(tender: Tender) -> str:
    if not tender.bid_end_at:
        return "⚠ Verification Required"
    return tender.bid_end_at.strftime("%d %B %Y at %H:%M (IST)")


def _default_action(tender: Tender) -> str:
    if tender.score.total >= 80:
        return ("Open the bid on GeM today, confirm the technical specification and "
                "start assembling documents.")
    if tender.score.total >= 60:
        return "Review the bid document on GeM and decide whether to bid."
    if tender.eligibility.gaps:
        return ("Check the eligibility gaps listed above before spending any time "
                "on this bid.")
    return "Skim the bid on GeM; low priority unless the buyer relationship matters."


def report_title(report_date: date, template: str) -> str:
    return template.format(date=report_date)
