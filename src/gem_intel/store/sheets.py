"""Google Sheets mirror of the tender database.

Three tabs, matching the brief's column list:

* **Tenders**    — one row per tender, keyed on Bid Number so a re-run updates
                   the existing row instead of appending a duplicate.
* **Change Log** — append-only record of amendments.
* **Run Log**    — one row per daily run, including access issues, so a thin
                   day can be explained months later.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from gem_intel.models import DailyReport, RunManifest, Tender, TenderChange
from gem_intel.observability import get_logger

log = get_logger(__name__)

TENDER_HEADERS = [
    "Date Found", "Last Seen", "Tender Title", "Bid Number", "Tender ID", "Buyer",
    "Department", "Product Category", "Product / Service", "Location", "Districts",
    "Closing Date", "Days Remaining", "Urgency", "EMD Required", "EMD Amount",
    "ePBG Required", "ePBG %", "Estimated Value", "Opportunity Score", "Score Band",
    "Match Level", "Status", "Official GeM Link", "Google Doc Report Link",
    "Verification Flags",
]
CHANGELOG_HEADERS = [
    "Detected On", "Bid Number", "Field", "Old Value", "New Value", "Note",
]
RUN_HEADERS = [
    "Run ID", "Started", "Finished", "Queries", "Bids Seen", "Details Fetched",
    "Documents", "Accepted", "Watchlist", "New", "Updated", "Access Issues",
    "Degraded Modes", "Report Link",
]


class SheetsMirror:
    def __init__(self, sheets: Any, spreadsheet_id: str,
                 tab_tenders: str = "Tenders",
                 tab_changelog: str = "Change Log",
                 tab_runs: str = "Run Log") -> None:
        self.sheets = sheets
        self.spreadsheet_id = spreadsheet_id
        self.tab_tenders = tab_tenders
        self.tab_changelog = tab_changelog
        self.tab_runs = tab_runs

    # ------------------------------------------------------------------
    def sync(self, report: DailyReport, doc_url: str = "") -> bool:
        """Push today's tenders, changes and run record. Never raises."""
        try:
            self.ensure_tabs()
            self._sync_tenders(report.tenders + report.watchlist,
                               report.report_date, doc_url)
            self._append_changes(report.changes, report.tenders + report.watchlist)
            self._append_run(report.manifest, doc_url)
            log.info("Google Sheet updated", spreadsheet_id=self.spreadsheet_id)
            return True
        except Exception as exc:                    # noqa: BLE001
            log.error("failed to update the Google Sheet",
                      error=f"{type(exc).__name__}: {exc}")
            return False

    # ------------------------------------------------------------------
    def ensure_tabs(self) -> None:
        meta = self.sheets.spreadsheets().get(
            spreadsheetId=self.spreadsheet_id
        ).execute()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        wanted = {
            self.tab_tenders: TENDER_HEADERS,
            self.tab_changelog: CHANGELOG_HEADERS,
            self.tab_runs: RUN_HEADERS,
        }
        requests = [
            {"addSheet": {"properties": {"title": title}}}
            for title in wanted if title not in existing
        ]
        if requests:
            self.sheets.spreadsheets().batchUpdate(
                spreadsheetId=self.spreadsheet_id, body={"requests": requests}
            ).execute()
        for title, headers in wanted.items():
            self._ensure_header(title, headers)

    def _ensure_header(self, tab: str, headers: list[str]) -> None:
        current = self._read(f"{tab}!1:1")
        if current and current[0] == headers:
            return
        self.sheets.spreadsheets().values().update(
            spreadsheetId=self.spreadsheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            body={"values": [headers]},
        ).execute()

    def _read(self, cell_range: str) -> list[list[str]]:
        response = self.sheets.spreadsheets().values().get(
            spreadsheetId=self.spreadsheet_id, range=cell_range
        ).execute()
        return response.get("values", [])

    # ------------------------------------------------------------------
    def _sync_tenders(self, tenders: list[Tender], today: date, doc_url: str) -> None:
        """Update rows whose Bid Number already exists; append the rest."""
        existing = self._read(f"{self.tab_tenders}!A2:Z")
        bid_column = TENDER_HEADERS.index("Bid Number")
        index_of: dict[str, int] = {}
        for offset, row in enumerate(existing):
            if len(row) > bid_column and row[bid_column]:
                index_of[row[bid_column].strip().upper()] = offset + 2   # 1-based + header

        updates: list[dict[str, Any]] = []
        appends: list[list[Any]] = []
        for tender in tenders:
            row = self._row(tender, today, doc_url)
            key = (tender.bid_number or "").strip().upper()
            row_number = index_of.get(key)
            if row_number:
                updates.append({
                    "range": f"{self.tab_tenders}!A{row_number}",
                    "values": [row],
                })
            else:
                appends.append(row)

        if updates:
            self.sheets.spreadsheets().values().batchUpdate(
                spreadsheetId=self.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": updates},
            ).execute()
        if appends:
            self.sheets.spreadsheets().values().append(
                spreadsheetId=self.spreadsheet_id,
                range=f"{self.tab_tenders}!A1",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": appends},
            ).execute()

    @staticmethod
    def _row(tender: Tender, today: date, doc_url: str) -> list[Any]:
        first_seen = tender.first_seen_on or today
        return [
            first_seen.isoformat(),
            today.isoformat(),
            tender.title,
            tender.bid_number,
            tender.gem_tender_id,
            tender.buyer_organization,
            tender.department or tender.ministry,
            ", ".join(tender.classification.categories),
            tender.classification.kind.value,
            tender.delivery_location or tender.buyer_address,
            ", ".join(tender.geo.districts),
            tender.bid_end_at.strftime("%Y-%m-%d %H:%M") if tender.bid_end_at else "",
            tender.days_remaining if tender.days_remaining is not None else "",
            tender.urgency.value,
            tender.emd.status.value,
            tender.emd.amount.value if tender.emd.amount.known else "",
            tender.security.headline(),
            tender.security.percentage.value if tender.security.percentage.known else "",
            tender.estimated_value.value if tender.estimated_value.known else "",
            tender.score.total,
            tender.score.band_label,
            tender.eligibility.match_level.value,
            "open",
            tender.source_url,
            doc_url,
            " | ".join(tender.verification_flags)[:2000],
        ]

    def _append_changes(self, changes: list[TenderChange],
                        tenders: list[Tender]) -> None:
        if not changes:
            return
        lookup = {t.identity_key: t.bid_number for t in tenders}
        rows = [
            [
                change.detected_on.isoformat(),
                lookup.get(change.identity_key, change.identity_key),
                change.field_name,
                change.old_value,
                change.new_value,
                change.note,
            ]
            for change in changes
        ]
        self.sheets.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.tab_changelog}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": rows},
        ).execute()

    def _append_run(self, manifest: RunManifest, doc_url: str) -> None:
        row = [
            manifest.run_id,
            manifest.started_at.isoformat(),
            manifest.finished_at.isoformat() if manifest.finished_at else "",
            len(manifest.queries_executed),
            manifest.listings_seen,
            manifest.details_fetched,
            manifest.documents_downloaded,
            manifest.accepted,
            len(manifest.rejected),
            manifest.new_tenders,
            manifest.updated_tenders,
            " | ".join(i.render() for i in manifest.access_issues)[:2000],
            ", ".join(manifest.degraded_modes),
            doc_url,
        ]
        self.sheets.spreadsheets().values().append(
            spreadsheetId=self.spreadsheet_id,
            range=f"{self.tab_runs}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()


def create_spreadsheet(sheets: Any, title: str) -> str | None:
    """Create a new tracking spreadsheet and return its id."""
    try:
        created = sheets.spreadsheets().create(
            body={"properties": {"title": title}}, fields="spreadsheetId"
        ).execute()
        return created["spreadsheetId"]
    except Exception as exc:                        # noqa: BLE001
        log.error("failed to create the tracking spreadsheet", error=str(exc))
        return None
