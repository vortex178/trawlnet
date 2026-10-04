# Claude Desktop (free plan): how it works, limits, troubleshooting

Setup steps are in the README ("Free Claude plans"). This page is the detail behind them.

## How a run is split
| step | where | cost |
|---|---|---|
| fetch, filter, shortlist | `run_feeds`: a background process on your computer | no tokens |
| score the shortlist | the chat model, `scorer_batch_size` jobs (3 with the free preset) per `next_batch` | tokens (a few thousand per job) |
| tracker rows, digest | `submit_scores` publishes when everything is scored | no tokens |

`run_feeds` returns at once and `run_status` reports progress, so a fetch lasting minutes never runs into the tool-call
timeout. Every batch is saved when submitted: if the message limit stops you, send the `run_job_search` prompt (or
"run my job search") again later and it continues with the unscored jobs. A run with scores that are not in the
tracker yet is finished before a new search starts, even on a later day (`run_status` shows the newest run as
`unfinished`, and `run_feeds` names an older one); an earlier run nobody scored is left for the next search.

## Prompts and `get_instructions`
Some Claude Desktop versions do not show MCP prompts. Ask in plain words instead and Claude calls `get_instructions`:
"build my job-search profile from this resume" (`build_profile`), "run my job search" (`run_job_search`), "tailor my
resume for <job key>" (`tailor_for_job`), "review my week" (`weekly_review`).

## What the free preset changes
`init --preset free` writes smaller numbers to `config.yaml`: `shortlist_size: 6`, `scorer_batch_size: 3`,
`max_age_days: 10`. Edit them there (more jobs per run means more tokens); `next_batch` sends `scorer_batch_size`
jobs at a time, at most 5 (a folder made without the preset has 12, so 5). Indeed, Adzuna, undocumented ATS endpoints,
Alignerr and Firecrawl are off by default; We Work Remotely, Remote OK and Hacker News are on. Company ATS boards are
fetched only for companies in `data/companies.json`, which `init` does not create: build it once from a terminal with
`<data folder>/js discover run` (free, no tokens), otherwise a run uses the three
feeds. Google Sheets needs a service account: the CSV tracker is the default.

## Files
- `<data folder>/tracker.csv`: matches at or above `min_score_for_tracker`. The Status column is yours.
- `<data folder>/data/digests/<date>.md`: every scored job of the run (also via the `get_digest` tool).
- `<data folder>/data/runs/<date>/`: run files; `run.log` has the fetch output, `progress.json` the state.
- The server's log: `~/Library/Logs/Claude/mcp-server-trawlnet.log` (macOS).

## Troubleshooting
| symptom | fix |
|---|---|
| no `trawlnet` in Settings > Developer, or it shows "failed" | the JSON in `claude_desktop_config.json` must be valid and the paths absolute; rerun `setup.py desktop-config --home <folder>` and compare; check the server log |
| "is not registered for the MCP server" | `python3.12 <repo>/skills/job-search/scripts/setup.py link --home <folder>`, then restart Desktop |
| "no active profile" | build one first (`build_profile`, or ask Claude to build a profile from your resume) |
| `run_status` says `stopped` | the computer restarted, the process was killed or the runner failed mid-run (see `log_tail`; sleep only pauses a run); `run_feeds` again (all steps run again and `run.log` starts over; nothing is lost) |
| `run_status` says `failed` | the `log_tail` shows why (usually a network error or a broken profile); fix, then `run_feeds` |
| `run_feeds`: "already being scored" or "scores not in the tracker yet" | that run has scores; finish it with `run_job_search` (or publish it as is: `submit_scores` with its date, `scores=[]` and `finish=true`). After today's run is published, a new search starts tomorrow (unless no scores were saved) |
| "Python >= 3.10 needed" at `init` | nothing was created: install Python 3.10+ (python.org, Homebrew) or [`uv`](https://docs.astral.sh/uv/) and run the same command with it (`python3.12 .../setup.py init ...`). If `init` failed later, run `python3.12 .../setup.py env --home <folder>` and then `python3.12 .../setup.py link --home <folder>` (`init` links only after the venv is built) |
| after `git pull` nothing changed | restart Claude Desktop so it starts the new server |

## Limits
- macOS is the tested system. The `init` step builds the folder's `.venv` with POSIX paths, so Windows is not supported
  yet, and Claude Desktop has no official Linux build.
- Free plans cap messages per few hours. A first run (6 jobs) is about 2 batches; leave room for the profile build.
- Titles that fit a profile only loosely (`ambiguous` in `run_status`) are not scored: Claude Code's decide step has no
  Desktop counterpart. Keep `title_related` empty (`save_profile` defaults it to empty) and list clear titles in
  `title_include`.
- Job text is untrusted: Claude is told never to follow instructions inside a posting.
- trawlnet never applies to jobs, logs in to sites, or handles passwords.
