# Google Workspace setup

The pipeline writes a Google Doc report and (optionally) mirrors the tender
database into a Google Sheet. Both are optional in the sense that the run
completes without them — it always writes `data/reports/<date>.md` first —
but you need this setup for the "automatically create a Google Doc and save
it into Drive" part of the brief to actually happen.

## Option A — Service account (recommended for the daily job)

Best for an unattended run (GitHub Actions, a cron server) because it never
expires and needs no human to re-authenticate it.

1. In [Google Cloud Console](https://console.cloud.google.com/), create (or
   reuse) a project, then enable the **Google Drive API**, **Google Docs
   API**, and **Google Sheets API**.
2. Create a **Service Account** (IAM & Admin → Service Accounts), then create
   a JSON key for it and download it.
3. **Share your Drive folder with the service account's email**
   (`...@...iam.gserviceaccount.com`) as an Editor — service accounts have no
   storage quota of their own, so they must be given access to a human's
   folder (or a Shared Drive) rather than creating files in a personal quota.
   - If you use a **Shared Drive** instead, add the service account as a
     member of the Shared Drive and set `GOOGLE_SHARED_DRIVE_ID` to its ID
     (the `id` in the Shared Drive's URL).
4. Set the environment variable, either:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` — the entire JSON key file content, or
   - `GOOGLE_APPLICATION_CREDENTIALS` — a filesystem path to the key file.
5. Run `gem-intel doctor` — it authenticates and prints the service account
   email so you can confirm you shared the right folder with it.

## Option B — OAuth user credentials

Use this only if you want the report to land directly in a personal Drive
that you cannot (or do not want to) share with a service account.

1. Create an OAuth 2.0 Client ID (Desktop app type) in Cloud Console.
2. Run through the standard `google-auth-oauthlib` installed-app flow once,
   locally, to produce a token with the scopes in
   `gem_intel.store.google_auth.SCOPES`
   (`drive`, `documents`, `spreadsheets`).
3. Set `GOOGLE_OAUTH_TOKEN_JSON` to the resulting authorized-user JSON. Note
   this token can expire; a service account avoids that maintenance burden
   for a daily unattended job.

## Folder layout

The pipeline creates, and reuses, this structure automatically under the
folder named by `google.drive_root_folder_name` (default
`GeM Tender Intelligence`):

```
GeM Tender Intelligence/
  2026/
    September/
      GeM IT Tender Intelligence Report – 06 September 2026
      GeM IT Tender Intelligence Report – 07 September 2026
```

Set `GOOGLE_DRIVE_FOLDER_ID` to pin the root folder to a specific existing
folder (its ID is the segment after `/folders/` in its Drive URL); otherwise
the tool searches for a folder with the configured name at the Drive root (or
Shared Drive root) and creates it if missing. Year and month subfolders are
always created automatically. Existing reports are never overwritten — a
same-day re-run creates `... (rev 2)`, `... (rev 3)`, etc.

## The tracking spreadsheet

Set `GOOGLE_SHEET_ID` to an existing spreadsheet's ID (from its URL) to have
the pipeline maintain three tabs in it: **Tenders**, **Change Log**, and
**Run Log** (see `gem_intel.store.sheets` for the exact columns, matching the
brief's recommended schema). If you don't already have one, create a blank
spreadsheet, share it with the same principal as the Drive folder, and copy
its ID from the URL. Leaving `GOOGLE_SHEET_ID` unset skips the spreadsheet
mirror entirely — the SQLite database at `storage.database_url` remains the
authoritative record either way.

## What happens if this isn't set up

Nothing breaks. `gem_intel.store.google_auth.try_build_clients` catches every
credential/API error, logs it, and the pipeline records
`"Google Workspace unavailable"` as a degraded mode in the report rather than
failing the run. You still get `data/reports/<date>-gem-it-tender-report.md`
every day.
