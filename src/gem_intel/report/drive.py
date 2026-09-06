"""Google Drive folder management.

Creates and reuses the ``GeM Tender Intelligence / <Year> / <Month>`` tree.
Historical reports are never overwritten: each day gets its own document, and
a same-day re-run creates a suffixed revision rather than replacing yesterday.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from gem_intel.observability import get_logger

log = get_logger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"


class DriveOrganizer:
    def __init__(self, drive: Any, root_name: str,
                 shared_drive_id: str | None = None) -> None:
        self.drive = drive
        self.root_name = root_name
        self.shared_drive_id = shared_drive_id
        self._cache: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------
    def _list_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "spaces": "drive",
            "fields": "files(id, name)",
            "supportsAllDrives": True,
            "includeItemsFromAllDrives": True,
        }
        if self.shared_drive_id:
            kwargs.update(corpora="drive", driveId=self.shared_drive_id)
        return kwargs

    def find_folder(self, name: str, parent_id: str | None) -> str | None:
        escaped = name.replace("'", "\\'")
        query = [f"name = '{escaped}'", f"mimeType = '{FOLDER_MIME}'", "trashed = false"]
        query.append(f"'{parent_id}' in parents" if parent_id else "'root' in parents")
        response = self.drive.files().list(q=" and ".join(query),
                                           **self._list_kwargs()).execute()
        files = response.get("files", [])
        return files[0]["id"] if files else None

    def create_folder(self, name: str, parent_id: str | None) -> str:
        metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
        if parent_id:
            metadata["parents"] = [parent_id]
        elif self.shared_drive_id:
            metadata["parents"] = [self.shared_drive_id]
        created = self.drive.files().create(
            body=metadata, fields="id", supportsAllDrives=True
        ).execute()
        log.info("created Drive folder", name=name, id=created["id"])
        return created["id"]

    def ensure_folder(self, name: str, parent_id: str | None) -> str:
        cache_key = (parent_id or "root", name)
        if cache_key in self._cache:
            return self._cache[cache_key]
        folder_id = self.find_folder(name, parent_id) or self.create_folder(name, parent_id)
        self._cache[cache_key] = folder_id
        return folder_id

    # ------------------------------------------------------------------
    def ensure_report_folder(self, report_date: date,
                             root_folder_id: str | None = None) -> str:
        """Return the folder id for ``root/<YYYY>/<Month>``, creating as needed."""
        root = root_folder_id or self.ensure_folder(self.root_name, None)
        year = self.ensure_folder(str(report_date.year), root)
        month = self.ensure_folder(report_date.strftime("%B"), year)
        return month

    def unique_name(self, folder_id: str, desired: str) -> str:
        """Never overwrite: if today's name exists, add a revision suffix."""
        escaped = desired.replace("'", "\\'")
        query = (f"name contains '{escaped}' and '{folder_id}' in parents "
                 "and trashed = false")
        response = self.drive.files().list(q=query, **self._list_kwargs()).execute()
        existing = {f["name"] for f in response.get("files", [])}
        if desired not in existing:
            return desired
        revision = 2
        while f"{desired} (rev {revision})" in existing:
            revision += 1
        name = f"{desired} (rev {revision})"
        log.info("report already exists for today; creating a revision", name=name)
        return name

    def move_to_folder(self, file_id: str, folder_id: str) -> None:
        file = self.drive.files().get(
            fileId=file_id, fields="parents", supportsAllDrives=True
        ).execute()
        previous = ",".join(file.get("parents", []))
        self.drive.files().update(
            fileId=file_id, addParents=folder_id, removeParents=previous,
            fields="id, parents", supportsAllDrives=True,
        ).execute()

    def file_url(self, file_id: str) -> str:
        return f"https://docs.google.com/document/d/{file_id}/edit"
