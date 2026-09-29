"""Company discovery: find each company's public ATS board and count jobs eligible for the user's country/remote
rules. Zero LLM tokens. Seeds: the data folder's data/seeds/<name>.(yaml|json) (user lists) else the plugin's seeds/.

  discover.py import-remoteintech <repo_dir>   build data/seeds/remoteintech.json from the repo's src/companies/*.md
  discover.py run --seed <name> [--limit N]    e.g. `in` (curated Indian product companies), `remoteintech`
                                              detect ATS (careers-page links, then verified slug probes),
                                              count relevant jobs, merge into data/companies.json,
                                              write unresolved to data/seeds/unresolved-<seed>.json
  discover.py custom --seed <name>            add unresolved companies as `custom` careers-page entries
  discover.py verify <jsonl>                  verify agent findings {"name","ats","token"} and merge
  discover.py summary                         counts by seed/ATS/active
"""
from __future__ import annotations

import glob
import json
import re
import sys
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import yaml

from common import COMPANIES_PATH, DATA, SEEDS_DIR, UA, age_days, load_config, norm_company, rel, today
from jobsearch import location_check
from sources import ATS_FETCHERS, _cfg, _post_json


def _undocumented_ok() -> bool:
    """Workday/Darwinbox career-site endpoints are undocumented: only probed when the user opts in."""
    try:
        return bool(_cfg()["sources"].get("undocumented_ats"))
    except SystemExit:
        return False

SEEDS = DATA / "seeds"  # user seeds, unresolved lists, user blocklist
SEED_ALIASES = {"india": "in"}
KEEP_REGIONS = {"worldwide", "asia-pacific", "other", None}
ATS_PATTERNS = [
    ("greenhouse", r"greenhouse\.io/embed/job_board(?:/js)?\?for=([A-Za-z0-9_-]+)"),
    ("greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"https?://([A-Za-z0-9-]+)\.workable\.com"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
]
OTHER_ATS = {  # recognized but not (yet) fetchable -> recorded as hints, skipped by research
    "workday": r"myworkdayjobs\.com|myworkdaysite\.com", "darwinbox": r"darwinbox\.(in|com)", "keka": r"keka\.com",
    "zoho-recruit": r"zohorecruit\.", "freshteam": r"freshteam\.com", "icims": r"icims\.com",
    "successfactors": r"successfactors\.|sapsf\.", "oracle": r"oraclecloud\.com/hcmUI", "eightfold": r"eightfold\.ai",
    "phenom": r"phenompeople\.com", "jobvite": r"jobvite\.com", "teamtailor": r"teamtailor\.com",
    "recruitee": r"recruitee\.com", "instahyre": r"instahyre\.com", "mynexthire": r"mynexthire\.com",
    "breezy": r"breezy\.hr", "bamboohr": r"bamboohr\.com", "personio": r"jobs\.personio\.",
}
# Hosted ATSs: URL carries tenant + host + site
HOSTED_PATTERNS = [
    ("workday", r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)",
     lambda m: {"token": m[0], "host": f"{m[0]}.{m[1]}.myworkdayjobs.com", "site": m[2]}),
    ("workday", r"https?://(wd\d+)\.myworkdaysite\.com/(?:[a-z]{2}-[A-Z]{2}/)?recruiting/([\w-]+)/([\w-]+)",
     lambda m: {"token": m[1], "host": f"{m[0]}.myworkdaysite.com", "site": m[2]}),
    ("darwinbox", r"https?://([\w-]+)\.darwinbox\.(in|com)(?:/ms/candidate(?:v2)?/(?!careers)(\w+)/careers|/ms/candidate/careers|/jobs)?",
     lambda m: {"token": m[0], "host": f"{m[0]}.darwinbox.{m[1]}", "site": m[2] or "main"}),
]
BAD_TOKENS = {"embed", "api", "j", "js", "v1", "static", "www", "careers", "apply", "jobs", "job_board", "widget", "css"}


def _get(url: str, timeout: int = 15, limit: int = 1_500_000):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(limit)


def _json(url: str):
    try:
        status, body = _get(url, timeout=20, limit=20_000_000)
        return json.loads(body) if status == 200 else None
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None


def name_match(a: str, b: str) -> bool:
    x, y = norm_company(a).replace(" ", ""), norm_company(b).replace(" ", "")
    return bool(x and y) and (x in y or y in x)


def _blocked(seed=None):
    """Known false matches: the plugin's shared blocklist plus the user's data/seeds/blocklist.json.
    An entry with a "seeds" list applies only when that seed is being run; without one it is global."""
    out = set()
    for p in (SEEDS_DIR / "blocklist.json", SEEDS / "blocklist.json"):
        if p.exists():
            out |= {(b["ats"], b["token"].lower()) for b in json.loads(p.read_text())
                    if not b.get("seeds") or seed in b["seeds"]}
    return out


BLOCKED = _blocked()  # global entries; cmd_run/cmd_verify widen it to the seed being processed


def _post(url: str, body: dict, headers=None):
    try:
        return _post_json(url, body, headers)
    except Exception:
        return None


def validate(ats: str, token: str, name: str, strict: bool, extra=None) -> bool:
    """Confirm the board exists (and, when strict, belongs to this company). Blocklisted matches never pass."""
    if (ats, token.lower()) in BLOCKED:
        return False
    extra = extra or {}
    if ats == "workday":
        d = _post(f"https://{extra['host']}/wday/cxs/{token}/{extra['site']}/jobs",
                  {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
        return bool(d) and "total" in d and (not strict or d["total"] > 0)
    if ats == "darwinbox":
        h = extra["host"]
        d = _post(f"https://{h}/ms/candidateapi/job/alljobs?companyId={extra['site']}", {},
                  {"Origin": f"https://{h}", "Referer": f"https://{h}/ms/candidatev2/{extra['site']}/careers/allJobs"})
        return bool(d) and d.get("status") == "success" and (not strict or len(d.get("data") or []) > 0)
    if ats == "greenhouse":
        d = _json(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        return bool(d) and (not strict or name_match(d.get("name", ""), name))
    if ats == "lever":
        d = _json(f"https://api.lever.co/v0/postings/{token}?mode=json&limit=1")
        return isinstance(d, list) and (not strict or len(d) > 0)
    if ats == "ashby":
        d = _json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
        return bool(d) and (not strict or len(d.get("jobs", [])) > 0)
    if ats == "workable":
        d = _json(f"https://apply.workable.com/api/v1/widget/accounts/{token}")
        # dormant namesake accounts exist (e.g. 'flipkart' with 0 jobs), so a probe also needs open jobs
        return bool(d) and (not strict or (name_match(d.get("name", ""), name) and len(d.get("jobs", [])) > 0))
    if ats == "smartrecruiters":
        d = _json(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1")
        c = (d or {}).get("content") or []
        return bool(c) and (not strict or name_match((c[0].get("company") or {}).get("name", ""), name))
    return False


def from_pages(c: dict):
    c.setdefault("hints", [])
    pages = [c.get("careers_url")] if c.get("careers_url") else []
    if c.get("domain"):
        pages += [f"https://{c['domain']}/careers", f"https://www.{c['domain']}/careers", f"https://{c['domain']}/jobs"]
    for url in pages:
        if re.search(r"(greenhouse|lever|ashbyhq|workable|smartrecruiters|myworkdayjobs|myworkdaysite|darwinbox)\.", url or ""):
            html = url  # the careers URL itself is the ATS link
        else:
            try:
                _, body = _get(url)
                html = body.decode("utf-8", "ignore")
            except Exception:
                continue
        c["page_ok"] = True
        c["hints"] = sorted(set(c["hints"]) | {k for k, rx in OTHER_ATS.items() if re.search(rx, html)})
        hits = Counter()
        for ats, rx in ATS_PATTERNS:
            for tok in re.findall(rx, html):
                tok = tok.strip("/.").split("?")[0]
                if tok.lower() not in BAD_TOKENS:
                    hits[(ats, tok)] += 1
        for (ats, tok), _ in hits.most_common(3):
            if validate(ats, tok, c["name"], strict=False):
                return ats, tok, "careers-page", {}
        hosted = Counter()
        for ats, rx, parse in HOSTED_PATTERNS if _undocumented_ok() else []:
            for m in re.findall(rx, html):
                x = parse(m)
                if x["token"].lower() not in BAD_TOKENS | {"blog", "academy", "explore", "help"}:
                    hosted[(ats, json.dumps(x, sort_keys=True))] += 1
        for (ats, xs), _ in hosted.most_common(3):
            x = json.loads(xs)
            if validate(ats, x["token"], c["name"], strict=False, extra=x):
                return ats, x["token"], "careers-page", {k: v for k, v in x.items() if k != "token"}
    return None


def from_probes(c: dict):
    base = re.sub(r"[^a-z0-9]+", "", c["name"].lower())
    hyph = re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-")
    root = (c.get("domain") or "").split(".")[0].lower()
    guesses = list(dict.fromkeys(g for g in [base, hyph, root] if g))
    sr_guesses = list(dict.fromkeys([c["name"].replace(" ", ""), base]))
    for ats in ("greenhouse", "lever", "ashby", "workable"):
        for g in guesses:
            if validate(ats, g, c["name"], strict=True):
                return ats, g, "probe", {}
    for g in sr_guesses:
        if validate("smartrecruiters", g, c["name"], strict=True):
            return "smartrecruiters", g, "probe", {}
    for g in guesses if _undocumented_ok() else []:  # Darwinbox tenants: <name>.darwinbox.in; needs open jobs
        x = {"host": f"{g}.darwinbox.in", "site": "main"}
        if validate("darwinbox", g, c["name"], strict=True, extra=x):
            return "darwinbox", g, "probe", x
    return None


def count_jobs(entry: dict, cfg: dict) -> dict:
    try:
        jobs = ATS_FETCHERS[entry["ats"]](entry)
    except Exception as e:
        return {"total_jobs": None, "relevant_jobs": 0, "error": type(e).__name__}
    rel = [j for j in jobs if location_check(j, cfg)[0] != "reject"]
    fresh = [j for j in rel if (age_days(j["posted"]) or 0) <= cfg["max_age_days"]]
    return {"total_jobs": len(jobs), "relevant_jobs": len(rel), "relevant_fresh": len(fresh)}


def resolve(c: dict, cfg: dict):
    hit = from_pages(c) or from_probes(c)
    if not hit:
        return None
    ats, token, how, extra = hit
    e = {"name": c["name"], "ats": ats, "token": token, **extra, "tags": c.get("tags", []), "seed": c["seed"],
         "domain": c.get("domain", ""), "detected_by": how, "checked": today()}
    e.update(count_jobs(e, cfg))
    e["active"] = e["relevant_jobs"] > 0
    return e


def load_companies() -> list:
    return json.loads(COMPANIES_PATH.read_text()) if COMPANIES_PATH.exists() else []


def merge(new: list) -> tuple:
    cur = load_companies()
    idx = {(c["ats"], c["token"].lower()): c for c in cur}
    added = updated = 0
    for e in new:
        k = (e["ats"], e["token"].lower())
        if k in idx:
            old = idx[k]
            keep_active = old.get("manual") and "active" in old
            old.update({x: e[x] for x in ("total_jobs", "relevant_jobs", "relevant_fresh", "checked") if x in e})
            old["tags"] = sorted(set(old.get("tags", [])) | set(e.get("tags", [])))
            if not keep_active:
                old["active"] = e["active"]
            updated += 1
        else:
            cur.append(e)
            idx[k] = e
            added += 1
    cur.sort(key=lambda c: (not c.get("active"), c["name"].lower()))
    COMPANIES_PATH.write_text(json.dumps(cur, indent=1, ensure_ascii=False) + "\n")
    return added, updated


def load_seed(seed: str) -> list:
    """YAML seeds: {tag: [[name, domain], ...]}; JSON seeds: [{name, domain, careers_url?, tags, seed}]."""
    for d in (SEEDS, SEEDS_DIR):
        y, j = d / f"{seed}.yaml", d / f"{seed}.json"
        if y.exists():
            rows = yaml.safe_load(y.read_text()) or {}
            return [{"name": n, "domain": dom, "tags": [tag], "seed": seed} for tag, xs in rows.items() for n, dom in xs]
        if j.exists():
            return json.loads(j.read_text())
    sys.exit(f"seed '{seed}' not found in {rel(SEEDS)} or {SEEDS_DIR}")


def cmd_import_remoteintech(repo: str):
    out = []
    for f in sorted(glob.glob(f"{repo}/src/companies/*.md")):
        m = re.match(r"---\n(.*?)\n---", open(f).read(), re.S)
        try:
            fm = yaml.safe_load(m.group(1)) if m else None
        except yaml.YAMLError:
            fm = None
        if not fm or fm.get("region") not in KEEP_REGIONS:
            continue
        dom = re.sub(r"^https?://(www\.)?", "", str(fm.get("website") or "")).split("/")[0]
        out.append({"name": str(fm.get("title") or fm.get("slug")), "domain": dom,
                    "careers_url": fm.get("careers_url") or "", "seed": "remoteintech",
                    "tags": ["remoteintech", f"region:{fm.get('region')}", f"policy:{fm.get('remote_policy')}"]})
    SEEDS.mkdir(parents=True, exist_ok=True)
    (SEEDS / "remoteintech.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")
    print(f"remoteintech seed: {len(out)} companies (regions worldwide/asia-pacific/other)")


def cmd_run(seed: str, limit):
    global BLOCKED
    seed = SEED_ALIASES.get(seed, seed)
    BLOCKED = _blocked(seed)
    SEEDS.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    cands = load_seed(seed)[: limit or None]
    known = {norm_company(c["name"]) for c in load_companies()}
    todo = [c for c in cands if norm_company(c["name"]) not in known]
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(lambda c: (c, resolve(c, cfg)), todo))
    found = [e for _, e in results if e]
    unresolved = [c for c, e in results if not e]
    added, updated = merge(found)
    (SEEDS / f"unresolved-{seed}.json").write_text(json.dumps(unresolved, indent=1, ensure_ascii=False) + "\n")
    by = Counter(e["ats"] for e in found)
    act = [e for e in found if e["active"]]
    print(f"{seed}: {len(cands)} candidates, {len(cands) - len(todo)} already known | resolved {len(found)} "
          f"({', '.join(f'{k}={v}' for k, v in by.most_common())}) | active (eligible jobs) {len(act)} | "
          f"unresolved {len(unresolved)} -> {rel(SEEDS / f'unresolved-{seed}.json')} | companies.json +{added}")


def cmd_verify(path: str):
    cfg = load_config()
    rows = [json.loads(l) for l in open(path) if l.strip()]
    ok, bad = [], []
    global BLOCKED
    for r in rows:
        BLOCKED = _blocked(r.get("seed"))
        if r.get("ats") in ATS_FETCHERS and r.get("token") and validate(r["ats"], r["token"], r["name"], strict=False):
            e = {"name": r["name"], "ats": r["ats"], "token": r["token"], "tags": r.get("tags", []),
                 "seed": r.get("seed", "research"), "domain": r.get("domain", ""), "detected_by": "research",
                 "checked": today()}
            e.update(count_jobs(e, cfg))
            e["active"] = e["relevant_jobs"] > 0
            ok.append(e)
        else:
            bad.append(r.get("name"))
    added, _ = merge(ok)
    print(f"verified {len(ok)}/{len(rows)} (+{added} new, {sum(e['active'] for e in ok)} active)"
          + (f" | rejected: {', '.join(map(str, bad))}" if bad else ""))


def _listing_url(c: dict):
    """Best job-listing URL for an unresolved company: an unsupported-ATS link on its careers page, else the page."""
    pages = [c.get("careers_url")] if c.get("careers_url") else []
    if c.get("domain"):
        pages += [f"https://{c['domain']}/careers", f"https://www.{c['domain']}/careers", f"https://{c['domain']}/jobs"]
    for url in filter(None, pages):
        try:
            status, body = _get(url)
        except Exception:
            continue
        html = body.decode("utf-8", "ignore")
        for rx in OTHER_ATS.values():
            m = re.search(rf"https?://[\w.-]*(?:{rx})[^\"'\s<>)]*", html)
            if m:
                return m.group(0).rstrip("\\/.,;")
        return url
    return None


def cmd_custom(seed: str):
    """Add unresolved companies of a seed as `custom` entries (free fetch, Firecrawl fallback in rotation)."""
    from sources import fetch_custom_free
    seed = SEED_ALIASES.get(seed, seed)
    un = json.loads((SEEDS / f"unresolved-{seed}.json").read_text())
    known = {norm_company(c["name"]) for c in load_companies()}
    todo = [c for c in un if norm_company(c["name"]) not in known]
    with ThreadPoolExecutor(max_workers=12) as ex:
        urls = list(ex.map(_listing_url, todo))
    new, free_ok = [], 0
    for c, url in zip(todo, urls):
        if not url:
            continue
        e = {"name": c["name"], "ats": "custom", "token": re.sub(r"[^a-z0-9]+", "-", c["name"].lower()).strip("-"),
             "careers_url": url, "tags": c.get("tags", []), "seed": seed, "domain": c.get("domain", ""),
             "detected_by": "custom", "checked": today(), "active": True, "hints": c.get("hints", [])}
        try:
            e["free_links"] = len(fetch_custom_free(e))
        except Exception:
            e["free_links"] = 0
        free_ok += e["free_links"] > 0
        new.append(e)
    added, _ = merge(new)
    print(f"custom ({seed}): {len(new)} of {len(todo)} unresolved have a reachable careers page (+{added}); "
          f"{free_ok} list jobs in plain HTML (free), {len(new) - free_ok} need Firecrawl (rotated within budget)")


def cmd_summary():
    cur = load_companies()
    print(f"companies: {len(cur)} | active: {sum(bool(c.get('active')) for c in cur)} | "
          f"by ats: {dict(Counter(c['ats'] for c in cur))} | by seed: {dict(Counter(c.get('seed') for c in cur))}")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        sys.exit(__doc__)
    if a[0] == "import-remoteintech":
        cmd_import_remoteintech(a[1])
    elif a[0] == "run":
        seed = a[a.index("--seed") + 1]
        lim = int(a[a.index("--limit") + 1]) if "--limit" in a else None
        cmd_run(seed, lim)
    elif a[0] == "custom":
        cmd_custom(a[a.index("--seed") + 1])
    elif a[0] == "verify":
        cmd_verify(a[1])
    elif a[0] == "summary":
        cmd_summary()
    else:
        sys.exit(__doc__)
