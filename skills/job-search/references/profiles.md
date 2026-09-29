# Profiles: schema and add/update procedure

## Files
- `data/resumes/<id>.<ext>` original resume; `data/resumes/<id>.md` plain-text copy (source of truth for scoring/tailoring)
- `data/profiles/<id>.yaml` one role profile per resume/target role
- `data/profiles/master.yaml` merged fact store across all resumes (with provenance)
- `data/profiles/preferences.yaml` salary floors and work rules (user-supplied; never inferred)

## Role profile schema (`data/profiles/<id>.yaml`)
```yaml
id: backend-eng                 # kebab-case, stable (used in sheet "Profile" column)
label: Backend Engineer
family: backend / general software engineering   # one line; guides the Haiku title check
active: true
resume: data/resumes/backend-eng.md
years_experience: 6             # total relevant years, from resume dates
target_titles: [backend engineer, software engineer backend]   # strongest title match (+3)
title_include: [backend, back end, software engineer, sde, software development engineer, platform engineer]  # (+2)
title_related: [engineer, developer, programmer]   # ambiguous -> Haiku decides
title_exclude: [frontend, front end, android, ios, qa, test, sales, support]
seniority_allowed: [mid, senior, lead]   # from: intern junior mid senior lead staff principal manager
search_queries: [backend engineer, python developer]   # Indeed search terms (defaults to target_titles[:3])
core_skills: [python, go, postgresql, kafka, aws]
secondary_skills: [kubernetes, terraform]
must_have: []                   # things the user insists the job must include (optional)
deal_breakers: [night shift, bond period]   # JD phrases that disqualify (optional, user-supplied)
summary: >-                     # 2-3 lines, factual, from resume
  ...
```
Title phrases are matched case-insensitively as whole words after punctuation is stripped
("back-end" == "back end"). Keep lists short and specific; over-broad `title_include` floods the scorer.

## Master facts (`data/profiles/master.yaml`)
```yaml
facts:
  - id: F001
    resumes: [backend-eng]          # which resume(s) contain it
    kind: experience                # experience | project | education | skill | cert | award
    org: Acme Corp
    role: Senior Software Engineer
    dates: 2021-03 – 2024-06
    text: Built Kafka-based ingestion processing 2B events/day; cut p99 latency 40%.
    skills: [kafka, go, aws]
```
One fact per bullet, text copied faithfully (light cleanup only). Same bullet in two resumes → one fact,
both ids in `resumes`. Keep ids stable across updates; new facts get the next free id.

## preferences.yaml
```yaml
salary:                       # min_annual in the pack currency, or min_lpa (INR lakhs per annum)
  default: {min_annual: null}
  backend-eng: {min_lpa: 30}
work:
  max_required_yoe: 6         # JD minimum years above this fails the seniority gate (null = no limit)
  remote_bias: strong         # strong | some | none
  reject_strict_wfo: true
  timezone: Asia/Kolkata
  forbidden_shift: "00:00-06:00"
```

## Add procedure (main agent)
1. Save the resume under `data/resumes/<id>.<ext>`; if PDF/DOCX, extract text to `data/resumes/<id>.md`.
2. Read it once. Write `data/profiles/<id>.yaml` and merge facts into `master.yaml`.
3. Show the user a compact review: titles/include/exclude, family, seniority, years, skills. Ask for corrections,
   salary floor (→ preferences.yaml) and any deal-breakers. Don't guess salary or deal-breakers.
   PDF → text: `.venv/bin/python -c "import pypdf,sys; print('\\n'.join(p.extract_text() for p in pypdf.PdfReader(sys.argv[1]).pages))" <pdf>`.
4. If the role family isn't covered by `wwr_feeds` in config.yaml, propose the matching WWR category feed.
5. Run `./js status` to confirm the profile loads, then `./js context`.

## Update procedure
Diff the new resume against `data/resumes/<id>.md`; report added/removed/changed bullets; update facts
(keep ids, mark removed facts `retired: true` rather than deleting), refresh skills/years/summary; show the diff summary.
