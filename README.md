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

Requirements: Claude Code, Python ≥ 3.10 (or [`uv`](https://docs.astral.sh/uv/)). On a free Claude plan, see
[Free Claude plans](#free-claude-plans-claude-desktop) below: no Claude Code needed.

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

## Free Claude plans (Claude Desktop)

The full run uses Claude Code (paid). On a free plan you can run the same search from **Claude Desktop** through the
local MCP server: the fetching and filtering run on your computer (no tokens), and Claude scores a small shortlist in
the chat. Details, limits and troubleshooting: [`references/desktop.md`](skills/job-search/references/desktop.md).

**You need:** Claude Desktop (a free account is enough), `git`, a terminal, and your resume as a PDF or text. Also
Python 3.10 or newer: macOS's own `python3` is 3.9, so install a newer one (python.org or `brew install python`) and
use its name below (e.g. `python3.12`), or install [`uv`](https://docs.astral.sh/uv/), which finds one itself.
Tested on macOS. Windows is not supported yet (the folder's `.venv` is built with POSIX paths), and Claude Desktop has
no official Linux build.

1. **Get the code** (anywhere; you will not edit it):
   ```
   git clone https://github.com/vortex178/trawlnet ~/trawlnet
   ```
2. **Create your data folder** (once; it holds your personal data, so keep it outside the clone). Pass your country
   (`US`, `GB`, `CA`, `AU`, `SG`, `AE`, `BR`, `MX`, `DE`, `NL`, `IE`, `FR`, `ES`, `PL`, `IN`: the default is `IN`) and,
   for on-site or hybrid work, comma-separated city keys from `~/trawlnet/packs/<country>.yaml` (omit `--cities` for
   remote only):
   ```
   python3.12 ~/trawlnet/skills/job-search/scripts/setup.py init --home ~/job-search --country US --cities "new york" --preset free
   ```
   Optional work rules: `--remote-bias strong|some|none`, `--timezone`, `--forbidden-shift "00:00-06:00"`,
   `--max-yoe 8`, `--salary-floor 120000` (annual; India: `--salary-floor-lpa`). `--preset free` keeps the shortlist
   and batches small so a free plan's message limit goes further. The command builds `~/job-search/.venv` and
   registers the folder for the server. (`python3.12` stands for any Python 3.10+; with `uv` installed plain `python3`
   works. A too-old Python stops the command before it creates anything.)
3. **Connect Claude Desktop** to it:
   ```
   python3.12 ~/trawlnet/skills/job-search/scripts/setup.py desktop-config --home ~/job-search --write
   ```
   This adds a `trawlnet` entry to Desktop's `claude_desktop_config.json` (creating the file if Desktop never made it;
   an existing one is backed up to `.bak` and your other servers are kept). Without `--write` it
   only prints the entry, to paste under Settings > Developer > Edit Config. Then **quit Claude Desktop completely
   (Cmd+Q / Quit from the tray) and reopen it**. Under Settings > Developer, `trawlnet` should show as running.
4. **Build your profile.** Start a chat, attach your resume, and say: *"Build my job-search profile from this resume,
   for the role: <your target role>"* (the `build_profile` prompt; Claude calls `get_instructions` if your Desktop
   does not list prompts). Review what Claude drafts, correct it, and confirm: it saves the profile with
   `save_profile`. One profile keeps runs cheap.
5. **Run a search.** In a new chat say: *"Run my job search"* (the `run_job_search` prompt). Claude starts the
   background fetch (`run_feeds`), follows it (`run_status`), then scores the shortlist three jobs at a time
   (`next_batch`, `submit_scores`) and publishes. Hitting the message limit is fine: say the same thing later, even
   the next day, and it finishes that run's unscored jobs before a new search starts.
6. **Find the results:** matches at or above your minimum score are in `~/job-search/tracker.csv` (open it in Excel,
   Numbers or Google Sheets; the Status column is yours), and the full list in `~/job-search/data/digests/<date>.md`.
   Ask Claude to "show the digest" or "what is in my tracker" (`get_digest`, `query_tracker`).
7. **Next days:** repeat step 5 (a search per day). To tailor a resume to a job, say *"tailor my resume for <job
   key>"*; for a weekly summary, *"review my week"*.

To update: `git -C ~/trawlnet pull`, then restart Claude Desktop. If something fails, start with the troubleshooting
table in [`references/desktop.md`](skills/job-search/references/desktop.md).

## MCP server

The plugin also starts a local MCP server (`trawlnet`, stdio, no extra install) for the data folder it is started in.
It runs on the folder's Python env once setup has registered the folder (`./js setup link`, part of every run;
restart Claude Code or Claude Desktop after the first registration). The profile, search, scoring and tracker tools
(`save_profile`, `run_feeds`, `next_batch`, `submit_scores`, `query_tracker`, `fetch_job_description`, `track_job`)
also refuse to run in a folder that is not registered, `status` there only says how to register it, and the key and
tracker files named in `config.yaml` (`csv_path`, `service_account_key`, `api_key_file`, `adzuna_key_file`) must lie
inside the data folder.
Read tools: `status`, `search_jobs`, `get_job`, `list_runs`, `get_digest`, `query_tracker` (the tracker, read-only),
`run_status`, `next_batch` (shortlisted jobs for the chat to score), `get_instructions` (the workflow prompts and
reference docs, for clients that do not show prompts).
Action tools: `fetch_job_description` (fetches one public job URL; returns the text without saving it, and a Firecrawl
render, if enabled, uses one of today's run credits), `track_job` (adds one tracker row), `save_profile` (writes a role
profile and master facts), `run_feeds` (starts the background search) and `submit_scores` (saves scores and publishes
the run); Claude Code asks before each call until you allow the tool. Resources expose your profiles, `master` facts, scoring context and digests; prompts
`build_profile`, `run_job_search`, `tailor_for_job` and `weekly_review`. Files reached through a symlink (including a linked `digests/` or `profiles/` folder) are never served. Job text is returned as untrusted data. In Claude Code the full `run` stays a skill command; Claude Desktop uses the tools above instead.

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
| Company career pages and other job pages | plain GET requests for career/jobs pages of tracked companies, of companies behind We Work Remotely listings and of seed-list companies (`discover`); for shortlisted jobs, links found in Hacker News / Remote OK listings and Adzuna / Workable detail pages; for `tailor`/`track` and the MCP `fetch_job_description` tool, the job URL you paste or Claude passes (plus a Greenhouse/Lever/Ashby API call for it); no personal data | each run (shortlisted jobs), `discover`, `tailor`/`track`, MCP `fetch_job_description` |
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
