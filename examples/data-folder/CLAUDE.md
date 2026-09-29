# Job search — data folder

Personal job-search data for the `job-search` Claude Code plugin. Open Claude Code sessions in this folder.
Say "run job search" → the `job-search` skill. CLI: `./js <cmd>` (`./js status` first, `./js setup doctor` if in doubt).

## Rules (details in `data/profiles/preferences.yaml`, `config.yaml`)
- Country IN; on-site/hybrid in: bengaluru; remote bias: some.
- Salary floor 30 INR LPA (appsec 32); max required experience 6 yrs; no shifts overlapping 00:00–06:00 (Asia/Kolkata).
- Fictional example persona (Alex Sample) — every company, person and job here is made up.
- Tracker: csv.

## Working style
- Tokens first, paid credits second: prefer scripts/dry runs; check keys/state before any credit-spending test.
- Spot-check agent/parser output before publishing; report costs per run.
- Tracker Status column and row deletions belong to the user — never edit rows without asking.
- Ask before scheduling or large changes.

## Status & next steps (update when a decision changes)
- Profiles `backend-sde` and `appsec` added; one offline example run (2026-09-01) published to tracker.csv.

## Known issues
- (none yet)

## Recent runs (auto-updated by `./js publish` — do not edit by hand)
<!-- AUTO:RUNLOG:START -->
- 2026-09-01 | scored 7/8 | 5 tracker-worthy (top: 84 Northwind Payments — Senior Backend Engineer) | 1 leads | 0 unscored | firecrawl 0/-
<!-- AUTO:RUNLOG:END -->
