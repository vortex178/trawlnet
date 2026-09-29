# Sources, fetch priority, compliance

## Fetch priority (tokens first, credits second)
1. Script-fetched structured data (0 LLM tokens, 0 credits): Greenhouse / Lever / Ashby / Workable /
   SmartRecruiters public board APIs (companies in `data/companies.json`), We Work Remotely RSS (`wwr_feeds`),
   Remote OK API, HN "Who is hiring?" (Algolia), Adzuna API (key), Alignerr (opt-in).
   - Opt-in (`sources.undocumented_ats`): Workday `POST https://<host>/wday/cxs/<tenant>/<site>/jobs` and Darwinbox
     `POST https://<tenant>.darwinbox.in/ms/candidateapi/job/alljobs?companyId=<site>` (`{"page": n, "limit": 100}`,
     the page's own Origin/Referer) — the endpoints the public career sites use. Workday is filtered server-side to
     the pack's country (country facet, else location-facet values naming the pack's `country_places`); ≤10 pages
     of 20; descriptions fetched only for shortlisted jobs. Undocumented; may change without notice.
2. Indeed connector (optional; claude.ai connector) via the `job-fetcher` (search) and `job-scorer` (details).
   Max 10 results per call, no pagination or date filter (freshness enforced by script on "Posted on").
   Job IDs are session-scoped and the short URLs change per call → never use either as a persistent id;
   dedupe is by company + title + location. Its details tool rate-limits parallel callers → score sequentially.
3. Default WebSearch / WebFetch (0 credits) for other pages. WebFetch returns a condensed answer; always
   prompt it to return requirements/responsibilities verbatim.
4. Firecrawl (opt-in, paid) — REST API from scripts (key in `.secrets/firecrawl.key`, never via an MCP tool, so pages
   never enter Claude's context). Markdown only (1 credit/page), scraped as a visitor from the config country.
   Used for: `custom` careers pages with no job links on a free fetch (rotated, least-recently scraped first;
   landing pages → one "open positions" link followed), shortlisted `custom`/Adzuna descriptions that are
   JS-rendered or throttled, and `./js fetch-url` (tailor) fallback. Budget per run = max(`base_credits_per_run`,
   (remaining − `reserve_credits`) ÷ days left in billing period); `shortlist_reserve` credits kept for
   descriptions; ~10 requests/min on the free plan (paced); 429s aren't billed; spend reconciled from the balance
   API and logged in `data/runs/<run>/firecrawl.json`.

## companies.json entry
```json
{"name": "Stripe", "ats": "greenhouse", "token": "stripe", "tags": ["remote-friendly"], "active": true}
```
`ats`: greenhouse | lever | ashby | workable | smartrecruiters | workday | darwinbox | custom (`careers_url` =
job-listing page; created by `./js discover custom --seed <seed>`). Workday/Darwinbox entries also need `host` and
`site` (e.g. `{"ats":"workday","token":"acme","host":"acme.wd5.myworkdayjobs.com","site":"AcmeCareers"}`).
`token`: board slug (Greenhouse `boards-api.greenhouse.io/v1/boards/<token>`, Lever `jobs.lever.co/<token>` — add
`"region": "eu"` for jobs.eu.lever.co, Ashby `jobs.ashbyhq.com/<token>`, Workable `apply.workable.com/<token>`,
SmartRecruiters `jobs.smartrecruiters.com/<token>` — case-sensitive). Only `active: true` entries are fetched. Set
`"manual": true` to stop discovery from changing `active`.

## Discovery (occasional, e.g. monthly)
- Seeds: plugin `seeds/in.yaml` (curated Indian product/security companies), `seeds/remoteintech.json` (from
  github.com/remoteintech/remote-jobs, regions worldwide/asia-pacific/other); your own lists in `data/seeds/<name>.yaml`
  (`{tag: [[name, domain], ...]}`) take precedence. `./js discover run --seed <name>` finds boards (careers-page
  links first, then name probes that must match the company name / have open jobs), counts jobs eligible under your
  location rules, sets `active`, and writes leftovers to `data/seeds/unresolved-<seed>.json` with `hints`.
- Staleness: `status`/`doctor` warn when a seed has many dead boards or is over 180 days old. `./js discover refresh
  [--seed X]` re-checks known boards for free (counts, dead boards go inactive, unresolved retried). To add companies to a
  YAML seed, `./js discover topup --seed X` prints its groups and every known name; propose 40-60 NEW, real companies
  that hire engineers in that region (verified domains, no repeats) as JSONL `{"name","domain","tag"}` to the path it
  names (about 2-4k tokens), then `./js discover topup --seed X --import <file>` drops duplicates and dead domains and
  writes the user's copy of the seed (it overrides the plugin's from then on); finish with `discover run --seed X`.
- False matches → add to `data/seeds/blocklist.json` ({ats, token, why}; merged with the plugin's `seeds/blocklist.json`)
  and remove from companies.json.
- `company-researcher` (Haiku) can research unresolved companies; its output is merged only via
  `./js discover verify <jsonl>`, which re-checks every board. Yield was low (~1 in 10) — use sparingly.
- Unsupported ATSs seen in hints: keka, freshteam, zoho-recruit, turbohire, icims, successfactors, etc.

## Adzuna
Key: `.secrets/adzuna.json` {app_id, app_key} (free developer account, created by the user). Country and details
domain from the pack (`adzuna.country`, `adzuna.details_domain`); `adzuna_locations` are mapped through the pack's
`city_spellings` (e.g. Gurugram → Gurgaon). `what` requires every word to match, so keep terms broad. Only
non-estimated salaries are used. Results carry a 500-char snippet; the full posting is fetched for shortlisted jobs
only, `adzuna_detail_delay_seconds` apart (the site returns 429 on quick fetches), Firecrawl fallback; kept only if it
contains the title. Aggregator reposts (config `aggregator_companies`, e.g. Foundit) are flagged at publish: their
salary/employer are often only on the source listing.

## Alignerr (opt-in; AI-training contract work)
`GET https://www.alignerr.com/api/jobs?search=<term>&limit=120&offset=N`, one search per `alignerr_searches` term.
Roles are posted as many city-targeted copies → one record per title, preferring a copy targeting the pack's
country. Full description fetched only when shortlisted. Pay is hourly USD (annualized ×2080 for the floor).
`shortlist_source_caps.alignerr` limits slots per run; the digest marks them as contract work.

## WWR free-apply verification
WWR applications can be paywalled for job seekers, so with `wwr_require_free_apply: true` every WWR job that passes
the filters must also exist on a free source (`scripts/wwr_verify.py`, no LLM tokens): the company's ATS board
(found by name, strict match) containing a similar title, or the company site (the "URL:" line in the WWR
description) whose careers pages contain the title. Found → the free URL replaces the WWR link (flag
`wwr-verified-free`); not found → rejected `wwr-only-paywalled`. Per-company lookups cached 7 days (jobs DB).
Limitation: careers sites rendered only by JavaScript can't be checked for free.

## Remote OK and Hacker News
- Remote OK: `https://remoteok.com/api`, no key, once per run. Terms: credit Remote OK and link to the original
  listing (the digest says "via Remote OK").
- HN: latest "Ask HN: Who is hiring?" thread by `whoishiring` (Algolia API), skipped if >35 days old. Each
  top-level post's first line is parsed as `Company | Role(s) | Location | …`; malformed headers are skipped.
  HN/Remote OK texts are summaries → for shortlisted jobs the linked posting is fetched and used only if it contains
  the title. Freshness uses `max_age_days_by_source.hn` (31).

## ZipRecruiter
Connector covers US/Canada only; `sources.ziprecruiter` is auto-disabled for other countries. Not wired into the
agents yet.

## Excluded / manual-only
- LinkedIn: ToS prohibits automated access. Never fetch linkedin.com. For tailoring, ask the user to paste the JD text.
- Reddit: dropped (low signal; API requires a registered app).
- Naukri, Foundit, Instahyre, Cutshort, Hirist: no known public APIs; don't scrape.
- Evaluated and skipped: Himalayas, Jobicy, Remotive (notice discourages third-party use), Arbeitnow (Europe),
  Jooble (partner application).
- Never log in, submit applications, or fill forms on job sites.

## Politeness
Script requests identify themselves in the User-Agent, run once per day, hit each board once per run, and never use
challenge bypasses or browser impersonation.
