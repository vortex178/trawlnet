# Example data folder (fictional)

What a job-search data folder looks like after setup, two profiles and one run. **Everything here is made up**:
"Alex Sample", the employers (Northwind, Contoso, Fabrikam, Tailspin, Wingtip, Litware, Proseware, Adventure Works,
Woodgrove) and every job posting. URLs point at `example.com` / `.example` domains.

| Path | What it is | Written by |
|---|---|---|
| `config.yaml` | sources, cities, shortlist size, tracker backend | `setup init`, then you |
| `CLAUDE.md` | rules + status for Claude; run log block auto-updated | `setup init`, `publish` |
| `data/profiles/*.yaml` | one role profile per resume, `master.yaml` fact store, `preferences.yaml` gates | `profile add` + you |
| `data/resumes/*.md` | resumes as text | `profile add` |
| `data/companies.json` | company boards to poll (ATS + token) | `discover`, you |
| `data/seeds/blocklist.json` | boards discovery must never re-add | you |
| `data/runs/<date>/` | one run: accepted / ambiguous / rejected, shortlist, batches, scores, `jd/` | the pipeline |
| `data/digests/<date>.md` | human-readable run summary | `publish` |
| `data/tailoring/<slug>/tailoring.md` | sourced resume edits for one job | `tailor` |
| `tracker.csv` | tracker rows (append-only; the Status column is yours) | `publish` |

Not shown (created at runtime, git-ignored by the `.gitignore` setup writes): `data/jobs.db` (seen list, scores, runs, caches), `.secrets/`, `.venv/`,
`.claude/agents/` (rendered by `setup link`), `./js`, raw `feeds.jsonl.gz`.

The run in `data/runs/2026-09-01/` is produced by `tests/test_pipeline.py` from `tests/fixtures/` — regenerate with
`JS_WRITE_EXAMPLE=1 python -m unittest discover -s tests -p test_pipeline.py`. Posting dates are relative to the day
it was generated.
