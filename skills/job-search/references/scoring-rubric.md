# Scoring rubric (job-scorer)

Score each job against ONE profile (the best of the job's `profiles`). Use only the JD text and the
profile/resume. Never assume facts not in either. Unknown ≠ fail. User rules come from `data/scoring-context.md`
("context") and the profile.

## 1. Gates (pass | fail | unknown)
| gate | fail when |
|---|---|
| `location` | JD requires on-site/hybrid outside the context's accepted cities, or remote restricted to countries/time zones that exclude the user's country (e.g. "must reside in the US" for a user in India). Remote "anywhere"/the user's country/region = pass. |
| `must_have` | JD hard requirement the resume clearly lacks (a required core language/stack, a mandatory license/degree, clearance, a language fluency). "Nice to have"/"preferred" never fails. |
| `seniority` | JD's MINIMUM required years exceeds the context's max required experience (e.g. with 5: "6+ years" fails, "5–8 years" passes), or the role is clearly a people-manager. |
| `salary` | JD states a maximum below the context's salary floor (convert currency and period with the context's FX; hourly × 2080 for annual). No salary stated = unknown. |
| `deal_breaker` | Any of: (a) if the context says reject strict work-from-office: fully on-site / 5 days in office with no hybrid or remote option (hybrid = pass); (b) required working hours/shift overlapping the context's forbidden hours — convert the JD's time zone into the user's (e.g. for 00:00–06:00 IST, "US Eastern business hours" = 18:30–02:30 IST fails, "4-hour overlap with EST mornings" ≈ 18:30–22:30 IST passes); "rotational/night shifts" without times = fail; (c) if the context says contract roles are excluded: the JD describes a contract, freelance, temporary or fixed-term engagement (a permanent role that merely mentions contractors or smart contracts passes); (d) anything in the profile's `deal_breakers`. |

Any `fail` → `score` ≤ 40 and `verdict: skip`.

## 2. Score (0–100), only if no gate failed
| weight | criterion |
|---|---|
| 35 | Required skills/stack covered by resume evidence (proportion of stated requirements met) |
| 15 | Work arrangement, by the context's remote bias — strong: fully remote 15 · remote-first/flexible 12 · hybrid 6 · on-site 3 · unclear 5; some: remote 15 · flexible 13 · hybrid 10 · on-site 6 · unclear 8; none: 12 for any arrangement that passes the gates |
| 15 | Domain / problem-space overlap (industry, product type, scale) |
| 15 | Seniority & scope fit (years, ownership, leadership signals) |
| 10 | Preferred / nice-to-have coverage |
| 10 | Role-title & responsibility alignment with profile `target_titles` |

Calibration: 85+ = would pass a recruiter screen as-is; 70–84 = strong with minor gaps; 60–69 = worth
applying with tailoring; 40–59 = stretch; <40 = poor. Use the full range; don't cluster at 70–80.
`verdict`: apply (≥70), consider (55–69), skip (<55 or any gate fail).
Don't adjust the score for posting age or aggregator reposts — `publish` applies those deterministically.

## 3. Evidence discipline
- `strengths`: ≤3 items, each "JD requirement ← resume evidence" (short, e.g. "Kafka pipelines ← built Kafka ingestion at Acme").
- `gaps`: ≤3 items, the most material missing requirements. Say "not evidenced" rather than guessing.
- `flags`: from: `remote-unverified`, `possible-repost` (JD body shows a posting date much older than listed),
  `agency-posting`, `salary-below-target`, `vague-jd`, `location-unclear`, `scam-signals` (fees, chat-app-only contact, unrealistic pay).

## 4. Output — one JSON object per line (JSONL), exactly these keys
{"key":"<from batch>","profile":"<profile id>","score":72,"verdict":"apply",
 "gates":{"location":"pass","must_have":"pass","seniority":"pass","salary":"unknown","deal_breaker":"pass"},
 "strengths":["..."],"gaps":["..."],"flags":[],"apply_url":"<canonical apply/job URL if the JD gives one, else the batch url>"}
