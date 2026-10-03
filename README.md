# trawlnet — a job-search plugin for Claude Code

A local, token-frugal job-search pipeline. Point it at your resume(s) and it fetches postings from public sources,
filters them with deterministic rules (location, remote eligibility, seniority, salary floor, freshness), has Claude
score a capped shortlist against your profiles with cited evidence, and appends good matches to a tracker
(CSV by default, or Google Sheets). It also suggests resume tailoring for a job without inventing experience.

It casts a wide net: seed lists of ~1,600 companies across ten lists resolve to hundreds of public ATS boards across eight platforms, alongside
job feeds and aggregators.

It never applies to jobs, logs in to job sites, or handles your passwords.

## Status: beta

trawlnet is a **beta** (v0.1.x). It is built and used daily by one person, and the testing behind it is narrow:

- **Only the India pack (`in`) has been run end to end** against live sources, on one machine (macOS), with one user's
  profiles and a Google Sheets tracker. The other packs (`us`, `gb`, `ca`, `au`, `sg`, `ae`, `br`, `mx`, `de`, `nl`,
  `ie`, `fr`, `es`, `pl`) and region seed lists are checked only by offline tests (structure, routing basics) and by
  measuring how many seed companies resolve to a job board. Their city lists, remote-eligibility terms and FX rates
  are best-effort starting points; expect gaps and tune them via `pack_overrides:`.
- **Seed lists are model-generated and unverified beyond board resolution.** Resolution rates vary by region (about 75%
  for `us`, down to about 12% for `uae`); some companies are missing, moved or share a name with an unrelated
  board (known cases are in `seeds/blocklist.json`). Use `discover refresh` and `discover topup` to keep lists fresh.
- **Scoring is LLM-based and not validated against human judgement.** Scores and gate checks are evidence-cited but can be
  wrong or inconsistent between runs; read the evidence before acting on a match.
- **Automated tests are offline** (mocked network, Python 3.9 and 3.12 in CI). Live sources (ATS boards, Adzuna, Indeed
  connector, Firecrawl, We Work Remotely, Remote OK) change without notice and can break a fetcher. The Indeed
  connector depends on your Claude account and region; Firecrawl and Adzuna need your own keys.
- Linux, Windows and the CSV tracker have had little or no real-world use.

Please report problems at the repository's issue tracker, with the failing command and `./js setup doctor` output
(remove personal details first).

## How a run works

| Step | Who | Tokens |
|---|---|---|
| fetch: company ATS boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters), We Work Remotely, Remote OK, HN "Who is hiring?", optional Adzuna / Indeed connector / Firecrawl | scripts | 0 |
| filter + dedupe: location & remote eligibility, seniority, title routing, salary floor, age | script | 0 |
| ambiguous titles ("Integration Engineer" — which profile?) | Haiku agent | low |
| shortlist + full job descriptions | script | 0 |
| score shortlist against profiles, with hard gates and evidence | Sonnet agents, sequential batches | most |
| publish: digest, tracker rows, seen list, run log | script | 0 |

Country specifics (cities, remote-eligibility words, currency, FX, Adzuna domain) live in **country packs**
(`packs/<cc>.yaml`: `in`, `us`, `gb`, `ca`, `au`, `sg`, `ae`, `br`, `mx`, `de`, `nl`, `ie`, `fr`, `es`, `pl`). Adding a country is a YAML file, not code.

## Install

Requirements: Claude Code, Python ≥ 3.9 (or [`uv`](https://docs.astral.sh/uv/)).

```
/plugin marketplace add vortex178/trawlnet
/plugin install trawlnet@trawlnet
```
(From a local clone: `/plugin marketplace add /path/to/trawlnet`.)

## Set up

1. `/trawlnet:setup` — Claude asks for a data folder (default `~/job-search`), country pack, cities, remote
   preference, time zone and forbidden working hours, max required experience, salary floor, and tracker backend,
   then runs `setup.py init` (creates the folder, a `.venv`, the `./js` CLI, and the agents).
2. **Open a new Claude Code session in the data folder** (its `CLAUDE.md` holds your rules; the agents live there).
3. `/trawlnet:profile add ~/resume.pdf --role backend-sde` for each target role. Review the generated profile.
4. Optional keys, all off until you add them to `.secrets/` and enable the flag in `config.yaml`:
   Adzuna (`adzuna.json`), Firecrawl (`firecrawl.key`, for JS-rendered career pages), Google Sheets service account
   (see `skills/job-search/references/tracker-setup.md`). The optional Indeed connector is the claude.ai connector.
5. `/trawlnet:discover` — find which ATS boards companies from a seed list use (`seeds/`), or add your own.
   Seeds: `in`, `us`, `ca`, `uk` (UK & Ireland), `eu`, `sea` (Singapore & SEA), `anz`, `uae`, `latam`, and the global
   `remoteintech`; pick any mix with `seeds: [...]` in `config.yaml` (default: the country pack's `default_seed`).
6. `/trawlnet:run --dry` (fetch + filter only; no tokens beyond the command, no paid credits), then `/trawlnet:run`.

Other commands: `/trawlnet:tailor <url or pasted JD>`, `/trawlnet:track <url>`, `/trawlnet:status`.
Power users: `./js status`, `./js setup doctor`, `./js tracker check`, `./js filter --date …`.

See [`examples/data-folder`](examples/data-folder) for a complete (fictional) data folder after one run.

## MCP server

The plugin also starts a local MCP server (`trawlnet`, stdio, no extra install) for the data folder it is started in.
It runs on the folder's Python env once setup has registered the folder (`./js setup link`, part of every run;
restart Claude Code after the first registration).
Read tools: `status`, `search_jobs`, `get_job`, `list_runs`, `get_digest`, `query_tracker` (the tracker, read-only).
Action tools: `fetch_job_description` (fetches one public job URL) and `track_job` (adds one tracker row); Claude Code
asks before each call. Resources expose your profiles, `master` facts, scoring context and digests; prompts
`tailor_for_job` and `weekly_review`. Job text is returned as untrusted data. The full `run` stays a skill command.

## Costs

Defaults: shortlist 15 jobs, scorer batches of 12. Rough per-run usage:

| Shortlist | Scoring (Sonnet) | Title decisions (Haiku) |
|---|---|---|
| 15 (default) | ~100k tokens | ~20k |
| 30 | ~200–250k | ~40k |
| 50 (batches of 13) | ~320k | ~60k |

Most of it is prompt-cached re-reads of job descriptions. On a Pro plan a 50-job run is roughly a third of a
5-hour window. Firecrawl (off by default) uses a per-run credit budget spread over your billing period and caches
pages per day; `run --dry` never spends credits.

## Privacy

The plugin has no server and no telemetry. It stores resumes, profiles, the tracker queue, run history and keys in your
data folder (`data/jobs.db` is SQLite; configs are YAML/JSON; keys live in `.secrets/`). Data leaves your machine only
as follows:

| To | What is sent | When |
| --- | --- | --- |
| Claude (your Claude Code session and its subagents) | job-description text, your profile and resume facts, job titles | scoring, title checks, tailoring |
| Job boards: Greenhouse, Lever, Ashby, Workable, SmartRecruiters, We Work Remotely, Remote OK, Hacker News (Algolia) | public listing requests (company board name, feed URL); no personal data | each run |
| Company career pages and other job pages | plain GET requests for career/jobs pages of tracked companies, of companies behind We Work Remotely listings and of seed-list companies (`discover`); for shortlisted jobs, links found in Hacker News / Remote OK listings and Adzuna / Workable detail pages; for `tailor`/`track` and the MCP `fetch_job_description` tool, the job URL you paste or Claude passes (plus a Greenhouse/Lever/Ashby API call for it); no personal data | each run (shortlisted jobs), `discover`, `tailor`/`track` |
| Workday, Darwinbox, Atlassian, Alignerr | the same kind of public listing request, and for Alignerr your configured `alignerr_searches` terms (`discover` also probes Workday/Darwinbox hosts when validating seed companies) | fetching only if you opt in (`sources.undocumented_ats`, `sources.alignerr`) |
| Adzuna | your search terms, locations and country, with your own API key | only if you enable it |
| Firecrawl | URLs that need rendering: career pages of companies you track, shortlisted-job detail pages (including Adzuna), links found in Hacker News / Remote OK listings and job URLs you paste; sent with your own API key and country | only if you enable it |
| Indeed (Claude connector) | search terms and location from your queries | only if the connector is enabled in your Claude account |
| Google (Sheets API and OAuth) | tracker rows (default columns: company, role, score, apply URL, profile, status, plus any you add in `tracker.columns`) to your own sheet, authenticated with your own service account; the MCP `query_tracker` tool reads them back | only if you choose the Sheets tracker |

Direct requests to job boards and career pages carry the `trawlnet` User-Agent and your IP address; Firecrawl and Google receive your own credentials, and Firecrawl fetches the pages it renders from its own servers. Nothing else is uploaded or shared, and your data is never sent to the plugin's author. Keys are read only from
files in `.secrets/` and are never written to run files. Keep your data folder out of public repos (setup
writes a `.gitignore`; `.secrets/` and `data/` are ignored). Resumes and tracker rows contain personal data: share
neither.

## Compliance

- Public, documented job APIs/feeds by default. Workday/Darwinbox career endpoints, the Atlassian board and Alignerr are undocumented:
  **opt-in** (`sources.undocumented_ats`, `sources.alignerr`), paced, and may break.
- Requests identify themselves (`trawlnet` User-Agent). No CAPTCHA or bot-detection bypass,
  no spoofed browser identity.
- LinkedIn and sites whose terms forbid scraping are never fetched — paste the job text instead.
- Remote OK listings are credited and linked; We Work Remotely jobs must be applyable on a free source.
- Content from job pages is treated as untrusted data (instructions inside postings are ignored).

## Development

```
python -m unittest discover -s tests -v      # offline; needs PyYAML
python tests/pii_scan.py [--deny ~/my-terms.txt]
```
To work on the plugin against your own data folder without installing it:
`python skills/job-search/scripts/setup.py link --home ~/job-search --dev` (symlinks the skill into
`~/job-search/.claude/skills/`). Design notes: [`docs/PLUGIN_DESIGN.md`](docs/PLUGIN_DESIGN.md).
Contributor guide: [`CLAUDE.md`](CLAUDE.md).

License: [MIT](LICENSE). Third-party data and dependencies: [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
