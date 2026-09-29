# AppSec Engineer — Fabrikam Security
Match: 81/100 (apply) · profile: appsec · resume: data/resumes/appsec.md
Gates: experience pass (3+ yrs asked, 4 yrs) · location pass (India) · salary unknown · shift pass

## Top edits
1. **Summary** — Replace: "threat modelling, secure code review, SAST/DAST in CI for payment APIs" →
   "threat modelling (STRIDE) and secure code review for payment APIs; SAST/DAST (Semgrep, OWASP ZAP) in CI" —
   why: JD asks for "threat modelling" and "SAST/DAST in CI" by name — source: [F004], [F005]
2. **Experience › Northwind Traders, bullet 2** — Move "Added Semgrep and OWASP ZAP stages…blocked 25 high-severity
   issues" to the first bullet — why: the JD lists CI security tooling first — source: [F005]
3. **Experience › Tailwind Traders** — Replace: "implemented OAuth 2.0 scopes for partner access" →
   "designed OAuth 2.0 scopes for partner API access control" only if accurate; otherwise keep as is —
   why: JD mentions "API authorization" — source: [F008] (don't claim design if you only implemented)

## Reorder / emphasis
- Put Certifications above Skills (the JD lists certifications as a plus).
- Drop the Kafka pipeline bullet to last under Northwind Traders — it's backend work, keep it as context [F001].

## Keywords already evidenced (name them explicitly)
- "OWASP ASVS" [F006], "STRIDE" [F004], "SAST", "DAST" [F005]

## Gaps (don't fake)
- Cloud security (AWS IAM, CSPM): not in your facts. Mitigation: mention the EKS migration context [F003] in the
  cover letter only as exposure, not as security ownership.

## Summary line suggestion (optional, sourced)
"Security engineer with 2 years of AppSec on payment APIs — threat modelling (STRIDE), secure code review against
OWASP ASVS, and Semgrep/ZAP gates in CI that blocked 25 high-severity issues." [F004][F005][F006]
