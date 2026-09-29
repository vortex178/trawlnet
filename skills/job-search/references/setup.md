# Setup (once per user)

Collect answers in one short exchange (offer defaults), then run one command. Never ask for or handle passwords/keys.

1. **Data folder** — default `~/job-search` (must be outside the plugin dir; it holds personal data). Sessions
   should later be opened there so its `CLAUDE.md` loads.
2. **Country pack** — list `<plugin>/packs/*.yaml` (`country` + `name`). Then **cities** for on-site/hybrid work
   from that pack's `cities` keys (may be none = remote only).
3. **Work rules** — remote bias (strong/some/none), time zone (pack default), forbidden working hours (e.g.
   `00:00-06:00`, or none), max required years of experience (or none), salary floor (annual in the pack currency,
   or LPA for India; may be skipped).
4. **Tracker** — `csv` (default, zero setup) or `gsheets` (user creates a service account: `tracker-setup.md`).
5. **Indeed connector** (optional) — if this session has a tool named `mcp__<id>__search_jobs` from the claude.ai
   Indeed connector, pass the prefix `mcp__<id>`; otherwise skip (Indeed off).

Run (requires Python ≥ 3.9 or `uv`; creates `.venv` and installs requirements):
```
python3 "<skill>/scripts/setup.py" init --home <folder> --country <CC> --cities "<a,b>" --remote-bias <b> \
  --timezone <tz> --forbidden-shift "<hh:mm-hh:mm>" --max-yoe <n> (--salary-floor <n> | --salary-floor-lpa <n>) \
  --tracker csv [--indeed mcp__<id>]
```
Then tell the user to: open a Claude Code session in the data folder; add each resume (`profile add <file>`);
optionally add keys to `.secrets/` (Adzuna `adzuna.json`, Firecrawl `firecrawl.key`, Google service account) and
enable the matching `sources`/`firecrawl` flags; run `./js discover run` (seeds: config `seeds:` list, else the pack's default_seed; `--seed a,b` overrides); do a dry run
(`run --dry`), then a real run. `./js setup doctor` checks everything.

Updating the plugin: the next `run` re-links automatically (`setup.py link`); restart the session if agents changed.
