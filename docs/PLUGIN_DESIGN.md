# Job Search Plugin — Design & Deployment Plan

Status: **roadmap steps 1–2 implemented in v0.1.0** (written 2026-09-28; updated 2026-09-29 after the refactor —
see the checklist in §4 and the roadmap in §16). This document describes how to turn the personal
job-search pipeline in this folder into a public Claude Code plugin that anyone can run against their own resumes.
It contains no personal data and is intended to live in the public repo.

---

## 1. Goals and non-goals

**Goals**
- Anyone can install the plugin, point it at their resume(s), and get a daily shortlist of scored, evidence-backed
  job matches plus a tracker — for their country, cities and work preferences.
- Token-efficient by construction: deterministic scripts do fetching/filtering/deduping; LLMs only judge ambiguous
  titles (Haiku) and score a capped shortlist (Sonnet).
- Accurate: hard gates (location, must-haves, experience, salary, deal-breakers), evidence-cited scores, no
  fabricated resume content, leads instead of guesses when a job description can't be read.
- Safe to distribute: no personal data in the repo, conservative defaults, source terms respected.

**Non-goals**
- Auto-applying, logging in to job sites, or handling user credentials on their behalf.
- Scraping sites whose terms prohibit it (LinkedIn, Naukri, etc.).
- A hosted/multi-tenant service. Everything runs locally on the user's machine.

---

## 2. Distribution

- **Format:** Claude Code plugin in a public GitHub repo.
  ```
  .claude-plugin/plugin.json        manifest
  .claude-plugin/marketplace.json   so the repo doubles as a marketplace
  skills/job-search/                SKILL.md + references/ + scripts/
  templates/agents/                 job-fetcher (Haiku), job-scorer (Sonnet), company-researcher (Haiku) —
                                    rendered into <data folder>/.claude/agents/ by `setup.py link` (the Indeed
                                    connector's tool prefix is per account, so agents can't ship pre-built)
  commands/                         setup, run, profile, tailor, track, discover, status
  templates/                        config, preferences, CLAUDE.md, tracker header
  packs/                            country packs (in.yaml, us.yaml, …)
  seeds/                            per-country company seed lists + shared blocklist
  examples/                         fictional generated data (see §10)
  tests/                            parser/pipeline fixtures (shared with examples/)
  CLAUDE.md                         contributor guide (NOT a user's personal CLAUDE.md)
  ```
- **Install (end user):** `/plugin marketplace add <owner>/<repo>` then `/plugin install job-search@<marketplace>`.
- **Why not a claude.ai skill:** the pipeline needs local scripts, a Python env, network access, subagents and
  persistent files.
- **Data lives outside the plugin** (plugin dirs are replaced on update): a user-chosen data folder, e.g.
  `~/job-search`, located via a setting/env var (`JOB_SEARCH_HOME`). Sessions should be opened in the data folder
  so its `CLAUDE.md` loads.

---

## 3. Current architecture (reference)

Pipeline per run (`run` command):

| Step | Who | Tokens | Output |
|---|---|---|---|
| `plan` | script | 0 | queries for connector-based sources |
| `feeds` | script | 0 | raw jobs from all script sources (+ Firecrawl within budget) |
| Indeed search | Haiku agent (connector) | low | raw Indeed jobs |
| `filter` | script | 0 | normalize, dedupe, seen, freshness/expiry, location & remote eligibility, salary floor, title routing & seniority, WWR free-apply check |
| ambiguous titles | Haiku agent | low | profile or reject per title |
| `shortlist` | script | 0 | ranked, capped (per-source caps), full JDs fetched lazily |
| scoring | Sonnet agent(s), sequential | main cost | gates + score + evidence per job |
| `publish` | script | 0 | tracker rows, seen update, digest, leads, CLAUDE.md run log, retention |

Sources: Indeed (connector), Adzuna (API key), company ATS boards (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters — public APIs; Workday, Darwinbox — undocumented public career-site endpoints), custom career
sites (free fetch → Firecrawl fallback, rotated within a credit budget), We Work Remotely RSS (free-apply
verified), Remote OK API, HN "Who is hiring?" (Algolia), Alignerr (AI-training contract roles, capped).

---

## 4. What stays vs what becomes configurable vs what is personal

**Engine (ships as-is):** pipeline scripts, fetchers, dedupe/seen logic, Firecrawl budget + page cache, WWR
verification, discovery tooling + blocklist mechanism, agent instructions, scoring rubric, tailoring rules,
digest/leads, retention.

**Configurable (templates + packs):**

| Area | Today (hard-coded) | Target |
|---|---|---|
| Country/region | INR/LPA parsing, Indian city list, India-eligible remote rules, reject-region list, Workday India facet places, Adzuna `in`/`adzuna.in`, Indeed `IN`, Alignerr India teaser match | Country pack (§5) |
| Work rules | shift window in IST, max required YOE, remote bias, strict-WFO rule | preferences with user time zone; rubric reads them as variables |
| Profiles | fetcher agent instructions cite example profile ids | derived at run time from profile labels/titles |
| Connectors | agent frontmatter contains one account's Indeed connector UUID | optional; detect at runtime or run Indeed searches from the main session |
| Tracker | Google Sheets (service account), fixed 6 columns | backends: local CSV/XLSX (default) or Google Sheets; configurable columns |
| Sources/budgets | enabled sources, search terms, shortlist size, Firecrawl reserve, Adzuna calls | config with conservative defaults; undocumented endpoints opt-in |
| Paths/CLI | ROOT from folder depth; `./js` wrapper; hand-built `.venv` | data-folder setting; plugin commands; setup creates env (prefer `uv`), pinned deps, Python ≥ 3.10 |
| Models | set in agent frontmatter | documented; optional override |

**Personal (never committed; gitignored; generated per user):** resumes, `profiles/*.yaml`, `master.yaml`,
`preferences.yaml`, `config.yaml`, `companies.json` edits, `seen`/DB, runs, digests, tailoring, `.secrets/`,
the user's `CLAUDE.md`, tracker IDs.

### Refactor checklist (from the current code)
- [x] `common.py`: `ROOT = parents[4]` → data folder from setting; separate plugin root vs data root.
- [x] `jobsearch.py`: `POSITIVE`/`NEGATIVE`/`ANYWHERE` lists, `RESTRICT_RE` window terms → country pack.
- [x] `sources.py`: `COUNTRY_PLACES`, `_IN_PLACES`, Adzuna domain/country, `fetch_workday(country="india")` → pack.
- [x] Agents: remove connector UUID from `tools:`; fetcher's job-family examples → generated from profiles.
- [x] Rubric: "India", "IST", "00:00–06:00" → preference variables.
- [x] `sheets.py`: tracker backend interface (`append`, `check`, `init`); CSV backend default. (`read_status` for
      the Status→score feedback loop: not yet.)
- [x] `save_config_value` regex editing → kept for the one remaining scalar (`tracker.gsheets.sheet_tab_gid`);
      all other state moved out of config (SQLite).
- [x] Replace `./js` with plugin commands; keep a thin CLI for power users (`./js` is generated by setup).
- [x] Move machine state to SQLite (§7).

---

## 5. Country pack schema (draft)

```yaml
# packs/in.yaml
country: IN
currency: INR
salary_units: {lpa: 100000, lakh: 100000, crore: 10000000}
fx_to_local: {USD: 83.0, EUR: 90.0, GBP: 105.0}
cities:                       # key + aliases; users pick a subset in setup
  delhi ncr: [ncr, delhi, new delhi, gurugram, gurgaon, noida, greater noida, ghaziabad, faridabad]
  bengaluru: [bangalore]
  # …
country_places: [india, bengaluru, bangalore, hyderabad, pune, …]   # Workday facet matching, "other city" checks
remote_positive: [india, apac, asia, asia pacific, south asia]
remote_reject: [us, usa, united states, europe, emea, uk, …, san francisco, new york, …]
indeed_country: IN
adzuna: {country: in, details_domain: www.adzuna.in, city_spellings: {gurugram: Gurgaon}}
default_seed: seeds/in.yaml
timezone_default: Asia/Kolkata
```
Validate the pack format with a second pack (e.g. `us.yaml`) before release.

---

## 6. Tracker backends

- Interface: `append_rows(rows)`, `read_status() -> {key: status}` (for feedback loop), `check()`.
- **CSV/XLSX (default):** zero setup; file in the data folder.
- **Google Sheets:** service account (user creates it; steps in setup doc), append-only, never writes the Status
  column; Drive connector optional for creating the sheet.
- Rows queue locally when a backend is unavailable and flush on the next publish.

---

## 7. Storage

- **Keep as editable files:** config, profiles, preferences, `companies.json`, blocklist, `CLAUDE.md`, digests.
- **SQLite `data/jobs.db`** (stdlib `sqlite3`) for machine state:
  - `jobs(key PK, source, company, title, location, posted, url, first_seen, last_seen, status, reason)`
  - `scores(key, run, profile, score, verdict, gates_json, strengths_json, gaps_json, flags_json)`
  - `runs(id PK, started, stats_json, firecrawl_spent, firecrawl_allowance)`
  - `cache(kind, key, value_json, updated)` — WWR verification, Firecrawl page index, etc.
- Store full JD text only for shortlisted jobs.
- **Retention (implemented):** raw `feeds.jsonl` gzipped after publish; raw files + Firecrawl page cache deleted
  after `retention_days_raw` (7). Typical run ≈ 10 MB for a week, ≈ 1 MB after.
- Migrations: schema version table; setup/upgrade command applies migrations.
- Enables: history queries via scripts (cheap), repost detection, Status→score feedback loop, crash/concurrency
  safety (cron + manual runs).

---

## 8. Scheduling

- Desktop scheduled tasks work but require the app to be open and a permission allowlist; deferred.
- **Preferred:** cron/launchd.
  - Script-only stages (`plan`, `feeds`, `filter` minus LLM decisions, retention) can run from cron with no Claude.
  - LLM stages via headless Claude Code (`claude -p`) — **open question:** whether claude.ai connectors (e.g.
    Indeed) load headlessly; if not, make connector sources optional in cron mode.
  - Needs a project permission allowlist and a lock file to prevent overlapping runs.
- Otherwise users run on demand.

---

## 9. Session memory: `CLAUDE.md`

- Setup generates the user's `CLAUDE.md` from `templates/CLAUDE.md.tmpl`: what the project is, how to run, the
  user's rules (from preferences), working style, status/next steps, known issues.
- `publish` rewrites an auto block (`<!-- AUTO:RUNLOG:START/END -->`) with the last 7 runs: scored/shortlisted,
  tracker-worthy count + top match, leads, unscored, Firecrawl spend. Zero tokens.
- Hand-written sections are updated by Claude only when a decision/source/setting changes. Keep it short (it loads
  every session).
- The repo-root `CLAUDE.md` is a contributor guide and never contains user data.

---

## 10. Examples directory

Fictional persona only (no real names, contacts or employers). Generate — don't hand-write — by running the real
`profile add` on a fictional resume and a recorded offline run; reuse the same files as test fixtures; add a CI
check scanning `examples/` for emails, phone numbers and known real names.

```
examples/
  README.md                     what each file is, which step creates it, which are hand-edited
  data-folder/
    CLAUDE.md                   filled template + sample run log
    config.yaml
    tracker.csv
    data/profiles/{backend-sde,appsec,master,preferences}.yaml
    data/resumes/{backend-sde,appsec}.md
    data/companies.json         ~10 entries: one per ATS type + custom + manual/inactive
    data/seeds/blocklist.json
    data/digests/<date>.md
    data/tailoring/<company-role>/tailoring.md
    data/runs/<date>/{accepted,ambiguous,rejected,decisions,shortlist,batch-1,scores-1}.jsonl + jd/
```

---

## 11. End-user setup (target flow)

1. Prereqs: Claude Code, Python ≥ 3.9 (or `uv`); optional claude.ai Indeed connector.
2. Install the plugin (§2).
3. `/job-search:setup` — choose data folder; create env; pick country pack, cities, remote preference, time zone +
   forbidden shift window, salary floor, max required experience; choose tracker backend.
4. `/job-search:profile add <resume> --role <id>` per target role; review generated titles/skills/facts.
5. Optional keys (all off until provided): Firecrawl (custom sites), Adzuna, Google Sheets service account.
6. `/job-search:discover --seed <country>` (+ own target-company list).
7. `/job-search:run --dry` — fetch + filter only; no tokens/credits; sanity-check volumes.
8. `/job-search:run` — first real run; review digest + tracker.

---

## 12. Compliance and safety

- Undocumented endpoints (Workday, Darwinbox, Alignerr) are **opt-in**, paced, identified by a descriptive
  User-Agent with a project URL, and documented as fragile. No challenge-bypass, no spoofed browser identity.
- Respect source terms: Remote OK/Jobicy-style attribution (credit + original link), WWR free-apply rule, LinkedIn
  never fetched (users paste JD text), HN via official Algolia API.
- Discovery runs occasionally (monthly), not daily; per-company lookups cached.
- Users create their own accounts/keys; the plugin never enters credentials or submits applications.
- Privacy statement: resumes and data stay local; JD + resume text is sent to Claude only for scoring/tailoring.
- Content from job pages is untrusted data (e.g. "mention this secret word" instructions in postings are ignored).

---

## 13. Costs and defaults

| Setting | Personal setup | Public default |
|---|---|---|
| Shortlist | 50 in batches of 13 (≈ 320k Sonnet + 60k Haiku tokens/run) | 15 in batches of 12 (≈ 100k) |
| Firecrawl | on, dynamic budget ≥ 30/run | off |
| Undocumented endpoints | on | off (opt-in) |
| Adzuna calls/run | 24 | 12 |
| Retention | 7 days raw | 7 days raw |

Document per-run token and credit estimates in the README.

---

## 14. Testing

- Fixtures from `examples/` + saved raw responses per source (Greenhouse/Lever/Ashby/Workable/SmartRecruiters/
  Workday/Darwinbox JSON, WWR RSS, Remote OK, HN thread, Adzuna, custom-site HTML/markdown).
- Unit tests: salary parsing (INR/LPA/USD hourly), dates (relative Workday dates, epoch ms), location & remote
  eligibility (incl. "other city" rule), seniority/title routing, HN header parsing, custom link extraction
  (headings in cards, job-board hosts, text-title fallback), WWR verification, Firecrawl budget math.
- Pipeline test: offline run over fixtures → deterministic shortlist.
- CI: tests + PII scan on `examples/`.

Implemented (v0.1.0): `tests/test_parsing.py`, `test_location_routing.py` (IN + US packs), `test_state.py`
(SQLite + legacy import, CSV tracker + queue, Firecrawl budget, stale/aggregator adjustments),
`test_pipeline.py` (offline filter → decide → shortlist → publish → re-filter; also regenerates the example run),
`tests/pii_scan.py`. Not yet: saved raw responses per source, WWR verification.

---

## 15. Lessons learned (carry into the refactor)

- Indeed connector job IDs are session-scoped; short URLs change per call → dedupe by company+title+location.
  Its details tool rate-limits parallel callers → score sequentially, cap retries.
- Firecrawl free plan ≈ 10 req/min → pace; 429s aren't billed; reconcile spend from the balance API.
- Career pages are geo-targeted → scrape as a visitor from the user's country; many are landing pages → follow one
  "open positions" link; job cards put titles in headings; some list titles without links.
- Workable/SmartRecruiters have dormant namesake accounts → name probes need open jobs; keep a blocklist.
- `str.splitlines()` splits on U+2028 found in JDs → split on `\n` only.
- Haiku title decisions need explicit job-family rules and "when unsure → null".
- Location rules must reject other named cities before accepting a bare country.
- HN/Remote OK texts are summaries → fetch linked posting, accept only if it contains the title; otherwise lead.
- Unscoreable jobs become leads (never retried forever); Indeed rate-limit misses are the only retries.
- Digest/run files must be keyed by run folder, not calendar date (same-day re-runs).
- Never re-run pipeline steps on a published run folder for testing; use a scratch folder.

---

## 16. Roadmap

1. ✅ **Portable (India):** templates + data-folder split, no personal data, CSV tracker default, connector UUID
   removed, setup command, CLAUDE.md template, fictional examples, tests.
2. ✅ **Generalize:** country packs + `us.yaml` (unit-tested; not yet validated on a live US run),
   preferences-driven rubric, SQLite state + migrations.
3. **Automation:** cron mode (script stages) + headless LLM stages if feasible; lock file.
4. **Release:** README (install, setup, costs, privacy, compliance), license, marketplace listing, versioning.

Open questions: headless connector availability; Adzuna free-tier limits; license choice; whether to include
Alignerr-style gig platforms by default.
