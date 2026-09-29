# Tracker setup

The tracker is append-only: `publish`/`track` add rows; the **Status** column and row deletions belong to the
user. Columns come from config `tracker.columns` (fields: company, role, score, url, profile, location, posted,
source, date_added, status). Rows produced while the backend is unavailable are queued in
`data/pending_tracker_rows.jsonl` and pushed on the next publish or `./js tracker flush`.

## CSV (default)
Nothing to set up: `tracker.csv` in the data folder, created on the first publish (`./js tracker init` creates it
now). Open it in any spreadsheet app; if you edit it there, save as CSV and keep the header row.

## Google Sheets (service account; one-time, done by the user)
The Drive connector can create/read a sheet but cannot append rows, so a service account writes them.
Claude must not create accounts, handle passwords, or enter keys — the user performs these steps.

1. https://console.cloud.google.com → create a project (e.g. `job-search`).
2. APIs & Services → Library → enable **Google Sheets API**.
3. IAM & Admin → Service Accounts → Create (name `job-search-writer`, no roles needed) → Done.
4. Open it → Keys → Add key → JSON → download.
5. Move the file to `<data folder>/.secrets/service-account.json` and run `chmod 600` on it.
6. Create a sheet, open it → Share → add the service account's email (…@….iam.gserviceaccount.com) as **Editor**.
   (Claude can do this via the Drive connector's share tool if the user explicitly asks.)
7. In config.yaml: `tracker.backend: gsheets`, `tracker.gsheets.sheet_id` / `sheet_url` from the sheet's link.
8. Verify: `./js tracker init` then `./js tracker check`.

### Moving / replacing the sheet
- Moving it between Drive folders keeps its ID — nothing to change.
- A different sheet: update `sheet_id` / `sheet_url`, set `sheet_tab_gid: null`, share it with the service
  account, run `./js tracker init`.
