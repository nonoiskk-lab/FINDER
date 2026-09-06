"""SQLite tender database with duplicate detection and a change log.

Identity
--------
``Tender.identity_key`` is the dedupe key: bid number first, then GeM tender
id, then a hash of the canonical URL. Seeing a key again is an *update*, never
a new row — and the fields that changed are written to ``changes`` so the
report can print "⚠ Closing date extended from 08 September to 12 September".

The database is also the memory that makes the daily report incremental: it
knows which tenders are new today and which have been carried forward.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from gem_intel.models import Tender, TenderChange, TernaryFlag
from gem_intel.observability import get_logger

log = get_logger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenders (
    identity_key      TEXT PRIMARY KEY,
    bid_number        TEXT,
    gem_tender_id     TEXT,
    title             TEXT,
    buyer             TEXT,
    department        TEXT,
    categories        TEXT,
    kind              TEXT,
    location          TEXT,
    districts         TEXT,
    published_at      TEXT,
    bid_end_at        TEXT,
    days_remaining    INTEGER,
    urgency           TEXT,
    emd_status        TEXT,
    emd_amount        REAL,
    epbg_status       TEXT,
    epbg_percentage   REAL,
    estimated_value   REAL,
    score             REAL,
    score_band        TEXT,
    match_level       TEXT,
    status            TEXT DEFAULT 'open',
    source_url        TEXT,
    report_doc_url    TEXT,
    first_seen_on     TEXT,
    last_seen_on      TEXT,
    payload           TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key  TEXT NOT NULL,
    field_name    TEXT NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    detected_on   TEXT NOT NULL,
    note          TEXT,
    FOREIGN KEY (identity_key) REFERENCES tenders(identity_key)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT,
    finished_at   TEXT,
    manifest      TEXT
);

CREATE INDEX IF NOT EXISTS idx_tenders_end     ON tenders(bid_end_at);
CREATE INDEX IF NOT EXISTS idx_tenders_seen    ON tenders(last_seen_on);
CREATE INDEX IF NOT EXISTS idx_tenders_bid     ON tenders(bid_number);
CREATE INDEX IF NOT EXISTS idx_changes_key     ON changes(identity_key, detected_on);
"""

# Fields watched for amendments. The label is what the change log prints.
TRACKED_FIELDS: tuple[tuple[str, str], ...] = (
    ("bid_end_at", "Closing date"),
    ("emd_status", "EMD requirement"),
    ("emd_amount", "EMD amount"),
    ("epbg_status", "Performance security requirement"),
    ("epbg_percentage", "ePBG percentage"),
    ("estimated_value", "Estimated value"),
    ("title", "Tender title"),
    ("status", "Tender status"),
    ("days_remaining", None),      # derived; tracked but never announced
)


class TenderDatabase:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(str(path).replace("sqlite:///", ""))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------------
    def upsert(self, tender: Tender, today: date) -> tuple[bool, list[TenderChange]]:
        """Insert or update. Returns ``(is_new, changes)``."""
        row = self._row_from(tender, today)
        existing = self.get(tender.identity_key)

        if existing is None:
            row["first_seen_on"] = today.isoformat()
            tender.first_seen_on = today
            tender.last_seen_on = today
            self._insert(row)
            return True, []

        tender.first_seen_on = _parse_date(existing["first_seen_on"]) or today
        tender.last_seen_on = today
        changes = self._diff(existing, row, today)
        row["first_seen_on"] = existing["first_seen_on"]
        # A tender we have reported before keeps its Google Doc link unless a
        # newer run produced one.
        row["report_doc_url"] = row["report_doc_url"] or existing["report_doc_url"]
        self._update(row)
        for change in changes:
            self._record_change(change)
        return False, changes

    def upsert_many(self, tenders: Iterable[Tender],
                    today: date) -> tuple[list[Tender], list[TenderChange]]:
        new_tenders: list[Tender] = []
        all_changes: list[TenderChange] = []
        for tender in tenders:
            is_new, changes = self.upsert(tender, today)
            if is_new:
                new_tenders.append(tender)
            all_changes.extend(changes)
        self.conn.commit()
        return new_tenders, all_changes

    # ------------------------------------------------------------------
    def get(self, identity_key: str) -> sqlite3.Row | None:
        cursor = self.conn.execute(
            "SELECT * FROM tenders WHERE identity_key = ?", (identity_key,)
        )
        return cursor.fetchone()

    def exists(self, tender: Tender) -> bool:
        return self.get(tender.identity_key) is not None

    def changes_for(self, identity_key: str, on: date) -> list[TenderChange]:
        rows = self.conn.execute(
            "SELECT * FROM changes WHERE identity_key = ? AND detected_on = ?",
            (identity_key, on.isoformat()),
        ).fetchall()
        return [
            TenderChange(
                identity_key=r["identity_key"], field_name=r["field_name"],
                old_value=r["old_value"] or "", new_value=r["new_value"] or "",
                detected_on=_parse_date(r["detected_on"]) or on, note=r["note"] or "",
            )
            for r in rows
        ]

    def mark_expired(self, today: date) -> int:
        """Close out tenders whose deadline has passed. Returns rows touched."""
        cursor = self.conn.execute(
            "UPDATE tenders SET status = 'closed' "
            "WHERE status = 'open' AND bid_end_at IS NOT NULL AND bid_end_at < ?",
            (today.isoformat(),),
        )
        self.conn.commit()
        return cursor.rowcount

    def set_report_url(self, identity_keys: Iterable[str], url: str) -> None:
        self.conn.executemany(
            "UPDATE tenders SET report_doc_url = ? WHERE identity_key = ?",
            [(url, key) for key in identity_keys],
        )
        self.conn.commit()

    def record_run(self, manifest: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, finished_at, manifest) "
            "VALUES (?, ?, ?, ?)",
            (manifest.get("run_id"), manifest.get("started_at"),
             manifest.get("finished_at"),
             json.dumps(manifest, ensure_ascii=False, default=str)),
        )
        self.conn.commit()

    def open_tenders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tenders WHERE status = 'open' ORDER BY bid_end_at"
        ).fetchall()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> TenderDatabase:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    @staticmethod
    def _row_from(tender: Tender, today: date) -> dict[str, Any]:
        return {
            "identity_key": tender.identity_key,
            "bid_number": tender.bid_number,
            "gem_tender_id": tender.gem_tender_id,
            "title": tender.title,
            "buyer": tender.buyer_organization,
            "department": tender.department or tender.ministry,
            "categories": ", ".join(tender.classification.categories),
            "kind": tender.classification.kind.value,
            "location": tender.delivery_location or tender.buyer_address,
            "districts": ", ".join(tender.geo.districts),
            "published_at": _iso(tender.published_at),
            "bid_end_at": _iso(tender.bid_end_at),
            "days_remaining": tender.days_remaining,
            "urgency": tender.urgency.value,
            "emd_status": tender.emd.status.value,
            "emd_amount": _float(tender.emd.amount.value),
            "epbg_status": tender.security.headline(),
            "epbg_percentage": _float(tender.security.percentage.value),
            "estimated_value": _float(tender.estimated_value.value),
            "score": tender.score.total,
            "score_band": tender.score.band_label,
            "match_level": tender.eligibility.match_level.value,
            "status": "open",
            "source_url": tender.source_url,
            "report_doc_url": "",
            "first_seen_on": today.isoformat(),
            "last_seen_on": today.isoformat(),
            "payload": json.dumps(tender.to_dict(), ensure_ascii=False, default=str),
        }

    def _insert(self, row: dict[str, Any]) -> None:
        columns = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        self.conn.execute(
            f"INSERT INTO tenders ({columns}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    def _update(self, row: dict[str, Any]) -> None:
        assignments = ", ".join(f"{k} = ?" for k in row if k != "identity_key")
        values = [v for k, v in row.items() if k != "identity_key"]
        values.append(row["identity_key"])
        self.conn.execute(
            f"UPDATE tenders SET {assignments} WHERE identity_key = ?", tuple(values)
        )

    @staticmethod
    def _diff(existing: sqlite3.Row, row: dict[str, Any],
              today: date) -> list[TenderChange]:
        changes: list[TenderChange] = []
        for field_name, label in TRACKED_FIELDS:
            if label is None:
                continue
            old = existing[field_name]
            new = row.get(field_name)
            if _same(old, new):
                continue
            # "not_stated" -> a real value is new information, not an amendment;
            # both directions are still worth logging, but the note differs.
            note = ""
            if old in (None, "", "not_stated"):
                note = "first time this was stated on the portal"
            changes.append(TenderChange(
                identity_key=row["identity_key"],
                field_name=label,
                old_value=_display(old),
                new_value=_display(new),
                detected_on=today,
                note=note,
            ))
        return changes

    def _record_change(self, change: TenderChange) -> None:
        self.conn.execute(
            "INSERT INTO changes (identity_key, field_name, old_value, new_value, "
            "detected_on, note) VALUES (?, ?, ?, ?, ?, ?)",
            (change.identity_key, change.field_name, change.old_value,
             change.new_value, change.detected_on.isoformat(), change.note),
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _same(old: Any, new: Any) -> bool:
    if old is None and new is None:
        return True
    if isinstance(old, float) or isinstance(new, float):
        try:
            return abs(float(old or 0) - float(new or 0)) < 0.01
        except (TypeError, ValueError):
            return str(old) == str(new)
    return str(old or "") == str(new or "")


def _display(value: Any) -> str:
    if value is None or value == "":
        return "not stated"
    if value == TernaryFlag.NOT_STATED.value:
        return "not stated"
    if isinstance(value, str) and len(value) >= 10 and value[4] == "-":
        parsed = _parse_date(value)
        if parsed:
            return parsed.strftime("%d %B %Y")
    return str(value)
