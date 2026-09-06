"""Daily orchestration: FIND → VERIFY → FILTER → ANALYZE → SCORE → SUMMARIZE → SAVE.

Failure policy for the whole pipeline: **a stage that cannot do its job
degrades and says so.** No stage is allowed to substitute invented data, and
no stage may turn "we could not check" into "there was nothing".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

from gem_intel.analyze.classifier import ItClassifier
from gem_intel.analyze.deadline import DeadlineAnalyzer
from gem_intel.analyze.eligibility import EligibilityAnalyzer
from gem_intel.analyze.emd import EmdAnalyzer
from gem_intel.analyze.geo import JharkhandGeoFilter
from gem_intel.analyze.llm import LlmAnalyzer, fallback_summary
from gem_intel.analyze.profile import CompanyProfile, load_profile
from gem_intel.analyze.requirements import RequirementsExtractor
from gem_intel.analyze.scoring import OpportunityScorer, rank_tenders
from gem_intel.analyze.security import SecurityAnalyzer
from gem_intel.config import Settings
from gem_intel.extract.documents import DocumentProcessor
from gem_intel.http_client import AccessBlocked, GemHttpClient
from gem_intel.models import DailyReport, RunManifest, Tender
from gem_intel.observability import get_logger, new_run_id, now_ist
from gem_intel.report.builder import build_report_document, report_title
from gem_intel.report.markdown import write_markdown
from gem_intel.sources.base import SourceAdapter
from gem_intel.sources.gem_bidplus import GemBidPlusSource
from gem_intel.store.database import TenderDatabase
from gem_intel.validate.rules import Rejection, VerificationGate

log = get_logger(__name__)


@dataclass
class PipelineResult:
    report: DailyReport
    document_path: Path | None = None
    doc_url: str = ""
    sheet_synced: bool = False

    @property
    def manifest(self) -> RunManifest:
        return self.report.manifest


class TenderPipeline:
    def __init__(
        self,
        settings: Settings,
        source: SourceAdapter | None = None,
        client: GemHttpClient | None = None,
        database: TenderDatabase | None = None,
        profile: CompanyProfile | None = None,
        llm: LlmAnalyzer | None = None,
    ) -> None:
        self.settings = settings
        self.client = client or GemHttpClient(settings)
        self.source = source or GemBidPlusSource(settings, self.client)
        self.database = database or TenderDatabase(
            settings.get("storage.database_url", "sqlite:///data/tenders.db")
        )
        self.profile = profile if profile is not None else load_profile(
            settings.config_dir / "company_profile.yaml"
        )

        self.classifier = ItClassifier(settings)
        self.geo = JharkhandGeoFilter(settings)
        self.deadlines = DeadlineAnalyzer(settings)
        self.emd = EmdAnalyzer()
        self.security = SecurityAnalyzer()
        self.requirements = RequirementsExtractor()
        self.eligibility = EligibilityAnalyzer(self.profile)
        self.scorer = OpportunityScorer(settings, self.profile)
        self.gate = VerificationGate(settings)
        self.documents = DocumentProcessor(settings, self.client)
        self.llm = llm if llm is not None else LlmAnalyzer(settings)

        self._document_text: dict[str, str] = {}

    # ------------------------------------------------------------------
    def run(self, report_date: date | None = None,
            queries: list[str] | None = None) -> PipelineResult:
        started = now_ist(self.settings.timezone)
        today = report_date or started.date()
        manifest = RunManifest(
            run_id=new_run_id(started, self.settings.timezone),
            started_at=started,
            timezone=self.settings.timezone,
        )
        log.info("run starting", run_id=manifest.run_id, date=today.isoformat())

        if not self.profile.configured:
            manifest.degrade(
                "company profile not configured — eligibility and capability scores "
                "are neutral placeholders (edit config/company_profile.yaml)"
            )
        if not self.llm.budget.available:
            manifest.degrade(
                f"AI document analysis unavailable: {self.llm.budget.disabled_reason}"
            )

        candidates = self._find(queries or self.settings.search_queries(), manifest)
        analysed = self._analyze(candidates, manifest)
        report = self._verify_and_rank(analysed, today, manifest)
        self._persist(report, today, manifest)

        manifest.finished_at = now_ist(self.settings.timezone)
        result = self._emit(report, manifest, today)
        self._record_run(report, result)
        log.info("run finished", run_id=manifest.run_id, accepted=manifest.accepted,
                 rejected=sum(manifest.rejected.values()),
                 access_issues=len(manifest.access_issues))
        return result

    # -- FIND -----------------------------------------------------------
    def _find(self, queries: list[str], manifest: RunManifest) -> list[Tender]:
        log.info("searching the official GeM portal", queries=len(queries))
        try:
            found = self.source.search(queries)
        except AccessBlocked as exc:
            manifest.access_issues.extend(self.client.access_issues)
            manifest.degrade(
                "GeM search aborted at a human-verification challenge; the portal "
                "was not searched in this run"
            )
            log.error("search aborted at an access challenge", url=exc.url)
            return []

        manifest.queries_executed = found.queries_executed
        manifest.listings_seen = found.listings_seen
        manifest.access_issues.extend(found.access_issues)
        manifest.candidates = len(found.tenders)
        if found.aborted:
            manifest.degrade(f"search stopped early: {found.abort_reason}")
        log.info("search complete", candidates=len(found.tenders))
        return found.tenders

    # -- ANALYZE --------------------------------------------------------
    def _analyze(self, candidates: list[Tender], manifest: RunManifest) -> list[Tender]:
        """Cheap filters first, expensive work only on survivors.

        Detail pages and PDFs cost the portal real bandwidth, so a bid is only
        fetched in full after it passes a first-pass IT + Jharkhand + deadline
        screen on the listing data we already have.
        """
        document_budget = int(self.settings.get("run.max_document_downloads", 400))
        shortlist: list[Tender] = []

        for tender in candidates:
            self.deadlines.apply(tender)
            tender.classification = self.classifier.classify(tender)
            tender.geo = self.geo.assess(tender)
            reason = self._first_pass_reason(tender)
            if reason is None:
                shortlist.append(tender)
            else:
                # Counted here so the report can explain a thin day: a bid
                # screened out on the listing is still a bid we looked at.
                tender.rejected_reason = reason.value
                manifest.reject(reason.value)

        log.info("first-pass screen complete",
                 candidates=len(candidates), shortlisted=len(shortlist))

        analysed: list[Tender] = []
        for tender in shortlist:
            self.source.fetch_detail(tender)
            manifest.details_fetched += 1

            used = self.documents.process_tender(tender, document_budget)
            document_budget -= used
            manifest.documents_downloaded += used

            text = self._collect_document_text(tender)
            self._document_text[tender.identity_key] = text

            # Re-run the cheap analysers now that we have the full text.
            self.deadlines.apply(tender)
            tender.classification = self.classifier.classify(tender, text)
            tender.geo = self.geo.assess(tender, text)
            self.emd.analyze(tender, text, tender.source_url)
            self.security.analyze(tender, text, tender.source_url)
            self.requirements.extract(tender, text, tender.source_url)

            self.llm.analyze(tender, text)
            if not tender.plain_summary:
                tender.plain_summary = fallback_summary(tender)

            tender.eligibility = self.eligibility.assess(tender)
            tender.score = self.scorer.score(tender)
            analysed.append(tender)

        manifest.llm_calls = self.llm.budget.calls_made
        if self.llm.budget.refusals:
            manifest.degrade(
                f"{self.llm.budget.refusals} AI analysis request(s) were declined; "
                "those tenders show rule-based findings only"
            )
        if self.llm.budget.exhausted:
            manifest.degrade(
                f"AI analysis budget ({self.llm.budget.max_calls} calls) was exhausted"
            )
        return analysed

    def _first_pass_reason(self, tender: Tender) -> Rejection | None:
        """First-pass screen on listing data. Returns why a bid was dropped.

        Deliberately generous: the listing is thin, so anything plausibly
        IT-and-Jharkhand-and-open earns a full look. An unreadable closing
        date is NOT a rejection here — the detail page usually carries a
        cleaner one, and dropping it now would hide a live tender.
        """
        if tender.days_remaining is not None:
            if tender.days_remaining < 0:
                return Rejection.EXPIRED
            if tender.days_remaining > int(
                self.settings.get("deadline.watchlist_max_days_remaining", 60)
            ):
                return Rejection.OUTSIDE_WINDOW
        if not tender.classification.is_it_related:
            return Rejection.NOT_IT_RELATED
        if not tender.geo.is_jharkhand:
            return Rejection.NOT_JHARKHAND
        return None

    def _collect_document_text(self, tender: Tender) -> str:
        parts = []
        for document in tender.documents:
            if not document.extracted:
                continue
            text = self.documents.load_text(document)
            if text:
                parts.append(f"\n===== {document.name} ({document.kind}) =====\n{text}")
        return "\n".join(parts)

    # -- VERIFY / RANK --------------------------------------------------
    def _verify_and_rank(self, tenders: list[Tender], today: date,
                         manifest: RunManifest) -> DailyReport:
        verification = self.gate.verify(tenders)
        manifest.accepted = len(verification.accepted)
        for reason, count in verification.rejected.items():
            manifest.rejected[reason] = manifest.rejected.get(reason, 0) + count

        report = DailyReport(
            report_date=today,
            manifest=manifest,
            tenders=rank_tenders(verification.accepted),
            watchlist=rank_tenders(verification.watchlist),
        )
        log.info("verification complete", accepted=len(report.tenders),
                 watchlist=len(report.watchlist),
                 rejected=sum(verification.rejected.values()))
        return report

    # -- SAVE -----------------------------------------------------------
    def _persist(self, report: DailyReport, today: date,
                 manifest: RunManifest) -> None:
        everything = report.tenders + report.watchlist
        new_tenders, changes = self.database.upsert_many(everything, today)
        manifest.new_tenders = len(new_tenders)
        manifest.updated_tenders = len(everything) - len(new_tenders)
        report.changes = changes
        closed = self.database.mark_expired(today)
        if closed:
            log.info("closed expired tenders in the database", count=closed)

    def _emit(self, report: DailyReport, manifest: RunManifest,
              today: date) -> PipelineResult:
        title = report_title(
            today,
            self.settings.get("google.doc_title_template",
                              "GeM IT Tender Intelligence Report – {date:%d %B %Y}"),
        )
        document = build_report_document(report, title)

        reports_dir = Path(self.settings.get("storage.reports_dir", "data/reports"))
        filename = f"{today.isoformat()}-gem-it-tender-report.md"
        path = write_markdown(document, reports_dir / filename)
        manifest.report_local_path = str(path)
        log.info("report written", path=str(path))

        result = PipelineResult(report=report, document_path=path)
        self._publish_to_google(report, document, today, result)
        return result

    def _publish_to_google(self, report: DailyReport, document, today: date,
                           result: PipelineResult) -> None:
        if not self.settings.get("google.enabled", True):
            return
        from gem_intel.store.google_auth import try_build_clients

        clients = try_build_clients()
        if clients is None:
            report.manifest.degrade(
                "Google Workspace unavailable — the report was saved locally only "
                "(see docs/GOOGLE_SETUP.md)"
            )
            return

        import os

        from gem_intel.report.drive import DriveOrganizer
        from gem_intel.report.gdocs import GoogleDocsWriter

        organizer = DriveOrganizer(
            clients.drive,
            self.settings.get("google.drive_root_folder_name", "GeM Tender Intelligence"),
            shared_drive_id=os.getenv("GOOGLE_SHARED_DRIVE_ID") or None,
        )
        writer = GoogleDocsWriter(clients.docs, clients.drive, organizer)
        url = writer.create_report(document, today,
                                  root_folder_id=os.getenv("GOOGLE_DRIVE_FOLDER_ID") or None)
        if url:
            result.doc_url = url
            report.manifest.report_doc_url = url
            self.database.set_report_url(
                [t.identity_key for t in report.tenders + report.watchlist], url
            )
        else:
            report.manifest.degrade("Google Doc creation failed — report saved locally")

        spreadsheet_id = os.getenv("GOOGLE_SHEET_ID", "").strip()
        if self.settings.get("google.sheets_enabled", True) and spreadsheet_id:
            from gem_intel.store.sheets import SheetsMirror

            mirror = SheetsMirror(
                clients.sheets, spreadsheet_id,
                tab_tenders=self.settings.get("google.sheet_tab_tenders", "Tenders"),
                tab_changelog=self.settings.get("google.sheet_tab_changelog", "Change Log"),
                tab_runs=self.settings.get("google.sheet_tab_runs", "Run Log"),
            )
            result.sheet_synced = mirror.sync(report, url or "")
            if not result.sheet_synced:
                report.manifest.degrade("Google Sheet sync failed")

    def _record_run(self, report: DailyReport, result: PipelineResult) -> None:
        manifest = report.manifest
        self.database.record_run({
            "run_id": manifest.run_id,
            "started_at": manifest.started_at.isoformat(),
            "finished_at": manifest.finished_at.isoformat()
            if manifest.finished_at else None,
            "listings_seen": manifest.listings_seen,
            "details_fetched": manifest.details_fetched,
            "documents_downloaded": manifest.documents_downloaded,
            "llm_calls": manifest.llm_calls,
            "accepted": manifest.accepted,
            "rejected": manifest.rejected,
            "new_tenders": manifest.new_tenders,
            "updated_tenders": manifest.updated_tenders,
            "access_issues": [i.render() for i in manifest.access_issues],
            "degraded_modes": manifest.degraded_modes,
            "report_doc_url": result.doc_url,
            "report_local_path": manifest.report_local_path,
        })

    def close(self) -> None:
        self.client.close()
        self.database.close()
