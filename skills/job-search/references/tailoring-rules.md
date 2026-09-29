# Resume tailoring rules

Goal: make true experience easier to recognize for this JD. Never create new experience.

## Hard rules
1. Every suggested line must cite its source: a fact id from `data/profiles/master.yaml` (e.g. `[F012]`)
   or a quoted fragment of the resume. No source → don't suggest it. Ignore facts with `retired: true`.
2. Allowed: reorder bullets/sections, reword using the JD's terminology for the SAME thing
   (e.g. "message queue" → "Kafka" only if the fact says Kafka), surface a fact from the other resume
   (master facts have provenance), tighten wording, quantify only with numbers already in the facts.
3. Not allowed: new skills, tools, titles, employers, dates, metrics, certifications, or scope inflation
   ("contributed to" → "led" is inflation unless the fact says led).
4. Gaps stay gaps. List them under "Gaps (don't fake)" with an honest mitigation idea
   (e.g. mention adjacent experience, a side project if one exists in facts, or address in cover letter).
5. Suggest only edits that change the match meaningfully; 3–8 edits max. Skip cosmetic churn.
6. Keyword alignment: list JD keywords the resume already evidences but doesn't name explicitly.
   Don't recommend keyword stuffing.

## Output file: data/tailoring/<slug>/tailoring.md
# <Role> — <Company>
Match: <score>/100 (<verdict>) · profile: <id> · resume: <path>
Gates: <one line>
## Top edits
1. **Section › bullet** — Replace: "<current>" → "<suggested>" — why: <JD requirement> — source: [F0xx]
## Reorder / emphasis
## Keywords already evidenced (name them explicitly)
## Gaps (don't fake)
## Summary line suggestion (optional, sourced)
