"""Job-search pipeline CLI. All steps are deterministic; LLM steps live in the agents.

  plan                 create today's run dir + Indeed queries (queries.json)
  feeds                fetch WWR + ATS boards -> feeds.jsonl
  filter               normalize, dedupe, seen/freshness/location/salary/title rules
  decide               apply Haiku title decisions (decisions.jsonl)
  shortlist            rank, cap, write JD files + scorer batches
  publish              validate scores, queue/push sheet rows, update seen, write digest
  fetch-url URL        (tailor) fetch a single ATS job to data/tailoring/<slug>/jd.txt
  track URL ...        add one job to the tracker and mark it seen
  status               config/profile/seen/queue summary
  context              write data/scoring-context.md (country, salary floors, work rules) for the scorer
Options: --date YYYY-MM-DD to operate on another run dir; feeds --dry skips paid (Firecrawl) scrapes.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict

import db
from common import (COMPANIES_PATH, DATA, HOME, RUNS_DIR, UA, age_days, has_phrase, html_to_text, job_key,
                    load_config, load_preferences, load_profiles, norm, norm_company, parse_date, parse_salary,
                    read_jsonl, rel, run_dir, salary_floor, today, write_jsonl)

SOURCE_RANK = {"greenhouse": 3, "lever": 3, "ashby": 3, "workable": 3, "smartrecruiters": 3, "workday": 3, "darwinbox": 3, "wwr": 2, "remoteok": 2, "hn": 2, "alignerr": 2, "adzuna": 1, "indeed": 1, "custom": 2}

# ---------- location / remote eligibility ----------

ANYWHERE = ["anywhere", "worldwide", "global", "anywhere in the world"]
RESTRICT_RE = re.compile(
    r"(must|should|need to)\s+(be\s+)?(based|located|resid\w*|liv\w*)\s+in"
    r"|(position|role|job)\s+(is\s+)?based\s+in|remote\s+(position|role)\s+based\s+in"
    r"|authori[sz]ed\s+to\s+work\s+in|work\s+authori[sz]ation\s+in"
    r"|\b(us|u\.s\.|usa|uk|eu|europe|canada|latam)[- ](based|only)\b"
    r"|within\s+the\s+(us|u\.s\.|united states|uk|eu)\b", re.I)


def _mentions(text_norm: str, terms) -> bool:
    return any(has_phrase(text_norm, t) for t in terms)


def location_check(rec: dict, cfg: dict) -> tuple:
    """-> (verdict 'ok'|'reject'|'verify', bucket, reason). Country specifics come from the pack (packs/<cc>.yaml)."""
    pack = cfg["pack"]
    country_words = [pack["name"].lower()] + list(pack.get("country_aliases") or [])
    positive, negative = pack["remote_positive"], pack["remote_reject"]
    if rec["source"] == "custom" and not rec["location"].strip():
        return "verify", "unknown", "location-unverified"
    loc_n = norm(f"{rec['location']} {rec.get('region_text', '')}")
    remote = rec.get("remote") is True or _mentions(norm(f"{rec['location']} {rec['title']}"),
                                                    ["remote", "work from home", "wfh"])
    cities = cfg["accept_cities"]
    city = next((c for c, aliases in cities.items() if _mentions(loc_n, [c] + list(aliases or []))), None)
    if city:
        return "ok", city, ""
    if not remote:
        other_city = _mentions(loc_n, [c for c in pack["country_places"] if c not in country_words])
        any_loc = cfg.get("accept_any_country_location", cfg.get("accept_any_india_location"))
        if any_loc and _mentions(loc_n, country_words) and not other_city:
            return "ok", country_words[0], ""  # country named with no city
        if rec["source"] == "custom" and not _mentions(loc_n, negative) and not other_city:
            return "verify", "unknown", "location-unverified"  # scraped link text may be a department, not a place
        return "reject", "", "location"
    if cfg.get("remote_scope") == "global_ok" or rec["source"] == "indeed":
        return "ok", "remote", ""  # Indeed results come from the country-scoped index
    paren = " ".join(re.findall(r"\(([^)]*)\)", rec["title"]))  # e.g. "(US Remote)", "(India)"
    loc_n = norm(f"{rec['location']} {rec.get('region_text', '')} {paren}")
    countries = norm(rec.get("eligible_countries"))
    if countries.strip():
        return ("ok", "remote", "") if _mentions(countries, country_words) else ("reject", "", "remote-country-list")
    if _mentions(loc_n, positive):
        return "ok", "remote", ""
    if _mentions(loc_n, negative):
        return "reject", "", "remote-region"
    m = RESTRICT_RE.search(rec.get("description") or "")
    if m:  # the match itself can name the region ("US-based"), so the window starts at the match
        window = norm(rec["description"][m.start(): m.end() + 60])
        return ("ok", "remote", "") if _mentions(window, positive + ANYWHERE) else ("reject", "", "remote-restricted-jd")
    if _mentions(loc_n, ANYWHERE):
        return "ok", "remote", ""
    return "verify", "remote", "remote-unverified"


# ---------- contract roles (config `exclude_contract`) ----------

CONTRACT_TYPE = re.compile(r"contract|freelance|temporary|\btemp\b|fixed[- ]?term|\bc2h\b", re.I)  # structured job-type fields
CONTRACT_TITLE = re.compile(r"(?<!smart )(?<!smart-)\b(contract(or)?|freelance|temporary|fixed[- ]term|c2h|"
                            r"contract[- ]to[- ]hire)\b", re.I)  # "Smart Contract Engineer" is a permanent-role title


def is_contract(rec: dict) -> bool:
    return bool(CONTRACT_TYPE.search(rec.get("job_type") or "") or CONTRACT_TITLE.search(rec["title"]))


# ---------- title routing ----------

SENIORITY = [  # highest priority first
    ("manager", ["manager", "head", "director", "vp", "vice president", "chief"]),
    ("principal", ["principal", "distinguished"]),
    ("staff", ["staff", "iv"]),
    ("lead", ["lead", "tech lead"]),
    ("senior", ["senior", "sr", "iii"]),
    ("mid", ["ii", "mid"]),
    ("junior", ["junior", "jr", "associate", "graduate", "fresher", "entry level", "sde 1", "sde i",
                "engineer i", "developer i", "engineer 1", "developer 1", "l1"]),
    ("intern", ["intern", "internship", "trainee", "apprentice"]),
]


def seniority(title: str) -> str:
    t = norm(title).replace(" member of technical staff ", " mts ")  # MTS is an IC title, not "staff" level
    return next((lvl for lvl, words in SENIORITY if _mentions(t, words)), "mid")


def route(title: str, profiles: dict) -> tuple:
    """-> (routes [(profile_id, strength 2|3)], ambiguous_profile_ids, reject_reason)"""
    t = norm(title)
    lvl = seniority(title)
    routes, ambiguous, reasons = [], [], set()
    for pid, p in profiles.items():
        if _mentions(t, p.get("title_exclude", [])):
            reasons.add("title-exclude")
            continue
        if lvl not in p.get("seniority_allowed", ["mid", "senior"]):
            reasons.add(f"seniority-{lvl}")
            continue
        if _mentions(t, p.get("target_titles", [])):
            routes.append((pid, 3))
        elif _mentions(t, p.get("title_include", [])):
            routes.append((pid, 2))
        elif _mentions(t, p.get("title_related", ["engineer", "developer", "sde", "programmer"])):
            ambiguous.append(pid)
        else:
            reasons.add("title")
    routes.sort(key=lambda r: -r[1])
    return routes, ambiguous, (sorted(reasons)[0] if reasons else "title")


# ---------- commands ----------

def write_context(cfg) -> str:
    """Compact, resolved scoring context (pack + config + preferences) so the scorer never reads config.yaml."""
    pack, prefs, cur = cfg["pack"], load_preferences(), cfg["currency"]
    work = prefs.get("work") or {}
    fmt = lambda v: f"{v / 1e5:g} LPA" if cur == "INR" else f"{v:,.0f}"  # noqa: E731
    floors = []
    for pid in ["default"] + list(load_profiles()):
        f = salary_floor(prefs, pid, cur)
        if f and (pid == "default" or f != salary_floor(prefs, "default", cur)):
            floors.append(f"{pid} {fmt(f)}")
    cities = "; ".join(f"{c} ({', '.join(al or [])})" if al else c for c, al in cfg["accept_cities"].items())
    shift = work.get("forbidden_shift") or work.get("forbidden_shift_ist")
    tz = work.get("timezone") or ("Asia/Kolkata" if work.get("forbidden_shift_ist") else pack.get("timezone_default"))
    lines = [
        "# Scoring context (generated by `./js context`; change config.yaml / preferences.yaml instead)",
        f"- Country: {pack['name']} ({cfg.get('country')}). Currency {cur}; FX to {cur}: "
        + ", ".join(f"{k} {v}" for k, v in cfg["fx_to_local"].items()),
        f"- On-site/hybrid accepted only in: {cities or 'nowhere (remote only)'}"
        + (f"; a location naming only {pack['name']} is accepted" if cfg.get("accept_any_country_location",
                                                                         cfg.get("accept_any_india_location")) else ""),
        f"- Remote: " + ("any location is fine" if cfg.get("remote_scope") == "global_ok"
                         else f"must be open to candidates in {pack['name']}"),
        f"- Salary floor (annual, {cur}): " + (", ".join(floors) or "none set"),
        f"- Max required experience (the JD's minimum years): {work.get('max_required_yoe', 'no limit')}",
        f"- Remote bias: {work.get('remote_bias', 'some')}; reject strict work-from-office: "
        + ("yes" if work.get("reject_strict_wfo") else "no"),
        f"- Forbidden working hours: {shift + ' in ' + tz if shift else 'none'} (user time zone {tz})",
    ]
    if cfg.get("exclude_contract"):
        lines.append("- Contract, freelance, temporary or fixed-term engagements: excluded (a deal_breaker fail)")
    path = DATA / "scoring-context.md"
    path.write_text("\n".join(lines) + "\n")
    return rel(path)


def cmd_context(a, cfg):
    print(write_context(cfg))


def cmd_plan(a, cfg):
    profiles = load_profiles()
    if not profiles:
        sys.exit("No active profiles in data/profiles/. Run the profile command first.")
    seen, queries = set(), []
    for pid, p in profiles.items():
        for q in p.get("search_queries") or p["target_titles"][:3]:
            for loc in cfg["search_locations"]:
                k = (q.lower(), loc.lower())
                if k not in seen:
                    seen.add(k)
                    queries.append({"search": q, "location": loc, "country_code": cfg["indeed_country_code"],
                                    "profiles": [pid]})
                else:
                    next(x for x in queries if (x["search"].lower(), x["location"].lower()) == k)["profiles"].append(pid)
    queries = queries[: cfg["max_indeed_queries"]]
    d = run_dir(a.date)
    (d / "queries.json").write_text(json.dumps({"indeed": queries if cfg["sources"].get("indeed") else []}, indent=1))
    write_context(cfg)
    zr = "" if cfg["sources"].get("ziprecruiter") else " (ZipRecruiter off: connector covers US/CA only)"
    print(f"run dir: {rel(d)} | profiles: {', '.join(profiles)} | indeed queries: {len(queries)}{zr}")


def cmd_feeds(a, cfg):
    from firecrawl import Budget
    from sources import fetch_all
    companies = json.loads(COMPANIES_PATH.read_text()) if COMPANIES_PATH.exists() else []
    budget = Budget(cfg, a.date)
    if a.dry:
        budget.enabled, budget.note = False, "--dry"
    records, counts, errors = fetch_all(cfg, companies, budget)
    budget.reconcile()
    COMPANIES_PATH.write_text(json.dumps(companies, indent=1, ensure_ascii=False) + "\n")  # last_firecrawl
    d = run_dir(a.date)
    write_jsonl(d / "feeds.jsonl", records)
    print(f"feeds: {len(records)} jobs from {len(counts)} sources" + (f"; {len(errors)} errors" if errors else "")
          + f" | {budget.summary()}")
    cust = {k: v for k, v in counts.items() if k.startswith("custom:")}
    if cust:
        print(f"  custom sites: {sum(1 for v in cust.values() if v)}/{len(cust)} returned jobs "
              f"({sum(cust.values())} listings); free-fetch empties go to Firecrawl in rotation")
    for e in errors:
        print("  ERR", e)


def _load_indeed(d) -> list:
    out = []
    for r in read_jsonl(d / "indeed_raw.jsonl"):
        if not (r.get("title") and r.get("company")):
            continue
        out.append({
            "source": "indeed", "source_id": r.get("id", ""), "title": r["title"], "company": r["company"],
            "location": r.get("location", ""), "remote": None, "region_text": "", "eligible_countries": "",
            "posted": parse_date(r.get("posted")), "salary_text": r.get("compensation", ""),
            "job_type": r.get("job_type", ""), "url": r.get("url", ""), "description": "",
        })
    return out


def cmd_filter(a, cfg):
    d = run_dir(a.date)
    profiles, prefs, seen = load_profiles(), load_preferences(), db.seen_keys()
    ccy, fx = cfg["currency"], cfg["fx_to_local"]
    raw = read_jsonl(d / "feeds.jsonl") + _load_indeed(d)
    stats = Counter()
    best = {}
    for rec in raw:
        verdict, bucket, reason = location_check(rec, cfg)
        rec["loc_bucket"] = bucket or norm(rec["location"]).strip()[:40]
        rec["key"] = job_key(rec["company"], rec["title"], rec["loc_bucket"])
        rec["_loc"] = (verdict, reason)
        cur = best.get(rec["key"])
        if cur is None or SOURCE_RANK[rec["source"]] > SOURCE_RANK[cur["source"]]:
            if cur:
                rec["also_on"] = sorted(set(cur.get("also_on", []) + [cur["source"]]))
                rec["posted"] = rec["posted"] or cur["posted"]
            best[rec["key"]] = rec
        else:
            cur.setdefault("also_on", [])
            cur["also_on"] = sorted(set(cur["also_on"] + [rec["source"]]))
    stats["duplicates"] = len(raw) - len(best)

    accepted, ambiguous, rejected = [], [], []
    for key, rec in best.items():
        if key in seen:
            stats["already_seen"] += 1
            continue
        if rec.get("expires") and rec["expires"] < today():
            rejected.append({"key": key, "reason": "expired"})
            continue
        age = age_days(rec["posted"])
        max_age = (cfg.get("max_age_days_by_source") or {}).get(rec["source"], cfg["max_age_days"])
        if age is not None and age > max_age:
            rejected.append({"key": key, "reason": "stale"})
            continue
        if age is None and not cfg["keep_undated"]:
            rejected.append({"key": key, "reason": "undated"})
            continue
        verdict, reason = rec.pop("_loc")
        if verdict == "reject":  # (details for the seen DB are added below)
            rejected.append({"key": key, "reason": reason})
            continue
        if cfg.get("exclude_contract") and is_contract(rec):
            rejected.append({"key": key, "reason": "contract"})
            continue
        flags = [reason] if verdict == "verify" else []
        if age is None:
            flags.append("undated")
        routes, amb, why = route(rec["title"], profiles)
        sal = parse_salary(rec["salary_text"], fx, ccy)
        if sal:
            rec["salary"] = sal
            floor = {p: salary_floor(prefs, p, ccy) for p in {x for x, _ in routes} | set(amb)}
            routes = [(p, s) for p, s in routes if not floor[p] or sal[1] >= floor[p]]
            amb = [p for p in amb if not floor[p] or sal[1] >= floor[p]]
            if not routes and not amb:
                rejected.append({"key": key, "reason": "salary<floor"})
                continue
        elif rec["salary_text"] and rec["salary_text"].lower() not in ("n/a", "none"):
            flags.append("salary-unparsed")
        rec["flags"] = flags
        if routes:
            rec["routes"] = routes
            accepted.append(rec)
        elif amb:
            rec["ambiguous_profiles"] = amb
            ambiguous.append(rec)
        else:
            rejected.append({"key": key, "reason": why})
    wwr_report = []
    if cfg.get("wwr_require_free_apply", True) and any(r["source"] == "wwr" for r in accepted + ambiguous):
        from wwr_verify import run as verify_wwr  # WWR-only listings (paywalled apply) are rejected
        accepted, rejected, rep1 = verify_wwr(accepted, rejected)
        ambiguous, rejected, rep2 = verify_wwr(ambiguous, rejected)
        wwr_report = rep1 + rep2
    for r in rejected:
        stats["rej:" + r["reason"]] += 1
        src = best.get(r["key"]) or {}
        r.update({k: src.get(k) for k in ("source", "company", "title", "location", "posted", "url")})
    write_jsonl(d / "accepted.jsonl", accepted)
    write_jsonl(d / "ambiguous.jsonl", ambiguous)
    write_jsonl(d / "rejected.jsonl", rejected)
    print(f"in: {len(raw)} | accepted: {len(accepted)} | ambiguous: {len(ambiguous)} | rejected: {len(rejected)} | "
          + ", ".join(f"{k}={v}" for k, v in sorted(stats.items())))
    for line in wwr_report:
        print("  WWR", line)
    if ambiguous and not a.quiet:
        print("PROFILES (id: label | target titles | seniority allowed):")
        for pid in sorted({p for r in ambiguous for p in r["ambiguous_profiles"]}):
            p = profiles[pid]
            print(f"{pid}: {p.get('label', pid)}{' — ' + p['family'] if p.get('family') else ''} | "
                  f"{', '.join(p['target_titles'])} | {', '.join(p.get('seniority_allowed', []))}")
        print("AMBIGUOUS (key | title | company | candidate profiles):")
        for r in ambiguous:
            print(f"{r['key']} | {r['title']} | {r['company']} | {','.join(r['ambiguous_profiles'])}")


def cmd_decide(a, cfg):
    d = run_dir(a.date)
    amb = {r["key"]: r for r in read_jsonl(d / "ambiguous.jsonl")}
    decisions = {x["key"]: x.get("profile") for x in read_jsonl(d / "decisions.jsonl") if x.get("key") in amb}
    accepted, rejected = read_jsonl(d / "accepted.jsonl"), read_jsonl(d / "rejected.jsonl")
    acc_keys = {r["key"] for r in accepted}
    moved = dropped = 0
    for key, pid in decisions.items():
        rec = amb[key]
        if pid and pid in rec["ambiguous_profiles"]:
            if key not in acc_keys:
                rec["routes"] = [(pid, 1)]
                accepted.append(rec)
                moved += 1
        else:
            rejected.append({"key": key, "reason": "title-llm",
                             **{k: rec.get(k) for k in ("source", "company", "title", "location", "posted", "url")}})
            dropped += 1
    write_jsonl(d / "accepted.jsonl", accepted)
    write_jsonl(d / "rejected.jsonl", rejected)
    undecided = len(amb) - len(decisions)
    print(f"decisions applied: +{moved} accepted, {dropped} rejected, {undecided} undecided (retried next run)")


REMOTE_BONUS = {"strong": 6, "some": 3, "none": 0}


def _rank(rec: dict, prefs: dict) -> float:
    s = rec["routes"][0][1] * 10
    bias = str(((prefs.get("work") or {}).get("remote_bias")) or "some").lower()
    s += REMOTE_BONUS.get(bias, 3) if rec.get("loc_bucket") == "remote" else 0
    age = age_days(rec.get("posted"))
    s += 5 if age is not None and age <= 3 else 3 if age is not None and age <= 7 else 0
    s -= min(4, (age - 7) // 3) if age is not None and age > 7 else 0  # stale postings sink, not dropped
    s += 2 if rec.get("salary") or rec.get("salary_inr") else 0
    s += 2 if rec.get("description") else 0
    s -= 3 if "remote-unverified" in rec["flags"] else 0
    s -= 2 if "undated" in rec["flags"] else 0
    return s


def cmd_shortlist(a, cfg):
    from firecrawl import Budget
    budget = Budget(cfg, a.date)
    d = run_dir(a.date)
    prefs = load_preferences()
    accepted = read_jsonl(d / "accepted.jsonl")
    by_profile = defaultdict(list)
    for r in accepted:
        by_profile[r["routes"][0][0]].append(r)
    for pid in by_profile:
        by_profile[pid].sort(key=lambda r: -_rank(r, prefs))
    caps, used = cfg.get("shortlist_source_caps") or {}, Counter()
    for pid in by_profile:  # per-source caps (e.g. contract gig platforms) so they can't crowd out salaried roles
        keep = []
        for r in by_profile[pid]:
            cap = caps.get(r["source"])
            if cap is None or used[r["source"]] < cap:
                keep.append(r)
                used[r["source"]] += cap is not None
        by_profile[pid] = keep
    picked, i = [], 0
    while len(picked) < cfg["shortlist_size"] and any(i < len(v) for v in by_profile.values()):
        for pid in sorted(by_profile):
            if i < len(by_profile[pid]) and len(picked) < cfg["shortlist_size"]:
                picked.append(by_profile[pid][i])
        i += 1
    rows = []
    for r in picked:
        if r["source"] in ("hn", "remoteok"):
            from sources import enrich_short_description
            try:
                r["description"] = enrich_short_description(r, budget)
            except Exception as e:
                print(f"  WARN enrich failed for {r['key']}: {type(e).__name__}")
        if r["source"] == "adzuna" and r.get("detail_url"):
            import time
            from sources import lazy_description
            time.sleep(cfg.get("adzuna_detail_delay_seconds", 3))  # the details site throttles quick fetches (429)
            try:
                full = lazy_description(r, budget)
                if full:
                    r["description"] = full
            except Exception as e:
                print(f"  WARN adzuna detail failed for {r['key']}: {type(e).__name__}")
        if not r.get("description") and r.get("detail_url"):
            from sources import lazy_description
            try:
                r["description"] = lazy_description(r, budget)
            except Exception as e:  # leave undescribed; scorer will mark vague-jd and it retries next run
                print(f"  WARN {r['source']} detail failed for {r['key']}: {type(e).__name__}")
        if r.get("description"):
            (d / "jd" / f"{r['key']}.txt").write_text(
                f"{r['title']} — {r['company']} — {r['location']}\n\n{r['description']}")
            jd = rel(d / "jd" / f"{r['key']}.txt")
        elif r["source"] == "indeed":
            jd = f"indeed:{r['source_id']}"  # the scorer fetches it through the connector
        else:
            jd = ""  # no description could be fetched: publish lists it as a lead, no scorer tokens spent
        rows.append({"key": r["key"], "profiles": [p for p, _ in r["routes"]], "title": r["title"],
                     "company": r["company"], "location": r["location"], "posted": r.get("posted"),
                     "salary_text": r.get("salary_text", ""), "url": r["url"], "source": r["source"],
                     "jd": jd, "flags": r["flags"], "expires": r.get("expires")})
    write_jsonl(d / "shortlist.jsonl", rows)
    for old in d.glob("batch-*.jsonl"):
        old.unlink()
    n = cfg["scorer_batch_size"]
    scorable = [r for r in rows if r["jd"]]
    batches = [scorable[i:i + n] for i in range(0, len(scorable), n)]
    for bi, b in enumerate(batches, 1):
        write_jsonl(d / f"batch-{bi}.jsonl", b)
    left = len(accepted) - len(picked)
    per = Counter(r["routes"][0][0] for r in picked)
    print(f"shortlist: {len(rows)} ({', '.join(f'{p}={per[p]}' for p in sorted(by_profile))}); "
          f"{left} accepted not shortlisted (retried next run)")
    for bi in range(1, len(batches) + 1):
        print(f"BATCH {rel(d / f'batch-{bi}.jsonl')} -> {rel(d / f'scores-{bi}.jsonl')}")


def _valid_score(s: dict, short: dict, profiles: dict) -> str:
    if s.get("key") not in short:
        return "unknown key"
    if s.get("profile") not in profiles:
        return "unknown profile"
    if not isinstance(s.get("score"), int) or not 0 <= s["score"] <= 100:
        return "bad score"
    if s.get("verdict") not in ("apply", "consider", "skip"):
        return "bad verdict"
    return ""


def _adjust_score(s: dict, job: dict, cfg: dict) -> None:
    """Deterministic post-scoring tweaks: stale-posting penalty, aggregator-repost flag."""
    flags = s.setdefault("flags", [])
    age, after = age_days(job.get("posted")), cfg.get("stale_after_days", 7)
    if age is not None and age > after:
        pen = min(cfg.get("stale_penalty_max", 10), (age - after) * cfg.get("stale_penalty_per_day", 1))
        s["raw_score"], s["score"] = s["score"], max(0, s["score"] - pen)
        flags.append(f"posted {age}d ago: -{pen}")
    if norm_company(job["company"]) in {norm_company(c) for c in cfg.get("aggregator_companies", [])}:
        flags.append("aggregator repost: check salary/employer on the source listing before applying")


def cmd_publish(a, cfg):
    from tracker import flush, queue_rows
    d = run_dir(a.date)
    run = a.date or today()
    profiles = load_profiles()
    short = {r["key"]: r for r in read_jsonl(d / "shortlist.jsonl")}
    scores, bad, leads = {}, [], []
    for f in sorted(d.glob("scores-*.jsonl")):
        for s in read_jsonl(f):
            err = _valid_score(s, short, profiles)
            if err:
                bad.append(f"{s.get('key')}: {err}")
            elif "vague-jd" in (s.get("flags") or []):
                if short[s["key"]]["source"] != "indeed":  # no usable description: surface as a lead, don't retry
                    leads.append(short[s["key"]])
                continue  # Indeed (rate-limited details): leave unseen so it's retried next run
            elif s["key"] not in scores or s["score"] > scores[s["key"]]["score"]:
                scores[s["key"]] = s
    for k, s in scores.items():
        _adjust_score(s, short[k], cfg)
    leads += [r for r in short.values() if not r["jd"]]  # shortlisted without any description (never sent to a scorer)
    lead_keys = {j["key"] for j in leads}
    missing = [k for k in short if k not in scores and k not in lead_keys]
    already = db.seen_keys()
    min_score = cfg.get("min_score_for_tracker", cfg.get("min_score_for_sheet", 60))
    sheet_rows, digest = [], []
    now = today()
    for k, s in sorted(scores.items(), key=lambda kv: -kv[1]["score"]):
        j = short[k]
        gate_fail = any(v == "fail" for v in (s.get("gates") or {}).values())
        to_sheet = s["score"] >= min_score and s["verdict"] != "skip" and not gate_fail
        if to_sheet and k not in already:
            sheet_rows.append({"company": j["company"], "role": j["title"], "score": s["score"],
                               "url": s.get("apply_url") or j["url"], "profile": s["profile"],
                               "location": j.get("location", ""), "posted": j.get("posted") or "",
                               "source": j["source"], "date_added": now, "status": ""})
        digest.append((s, j, to_sheet))
    job = lambda j: {k: j.get(k) for k in ("source", "company", "title", "location", "posted", "url")}  # noqa: E731
    seen_rows = [{**r, "status": "rejected"} for r in read_jsonl(d / "rejected.jsonl")]
    seen_rows += [{"key": k, "status": "scored", "score": s["score"], "profile": s["profile"], **job(short[k])}
                  for k, s in scores.items()]
    seen_rows += [{"key": j["key"], "status": "lead-no-jd", **job(j)} for j in leads]
    seen_rows = [r for r in seen_rows if r["key"] not in already]
    added = db.mark_seen(seen_rows, run)
    db.save_scores(run, list(scores.values()))
    if sheet_rows:
        queue_rows(sheet_rows)
    pushed, pending, msg = flush(cfg)

    lines = [f"# Job search digest — {run}", "",
             f"Scored {len(scores)} of {len(short)} shortlisted; {len(sheet_rows)} added to tracker "
             f"(min score {min_score}).", ""]
    for s, j, to_sheet in digest:
        mark = "✅" if to_sheet else "·"
        lines.append(f"- {mark} **{s['score']}** [{j['title']} — {j['company']}]({s.get('apply_url') or j['url']}) "
                     f"· {s['profile']} · {s['verdict']}")
        if s.get("strengths"):
            lines.append(f"  - strengths: {'; '.join(s['strengths'])}")
        if s.get("gaps"):
            lines.append(f"  - gaps: {'; '.join(s['gaps'])}")
        if s.get("flags"):
            lines.append(f"  - flags: {'; '.join(s['flags'])}")
        if j["source"] == "alignerr":
            lines.append("  - Alignerr: hourly CONTRACT AI-training work (not a salaried role); apply on alignerr.com")
        board = {"wwr": "We Work Remotely", "remoteok": "Remote OK", "hn": "Hacker News 'Who is hiring?'"}.get(j["source"])
        if board:
            lines.append(f"  - via {board}: apply through the listing (may not be on the company's careers page)"
                         + (f"; listing expires {j['expires']}" if j.get("expires") else ""))
    if leads:
        lines += ["", "## Leads to check manually (no usable job description to score)"]
        lines += [f"- [{j['title']} — {j['company']}]({j['url']}) · {', '.join(j['profiles'])}" for j in leads]
    (DATA / "digests").mkdir(exist_ok=True)
    dig = DATA / "digests" / f"{a.date or now}.md"  # one digest per run folder (same-day re-runs don't overwrite)
    dig.write_text("\n".join(lines) + "\n")

    fc = json.loads((d / "firecrawl.json").read_text()) if (d / "firecrawl.json").exists() else {}
    top = next((f"{s['score']} {j['company']} — {j['title']}" for s, j, ok in digest if ok), "none")
    db.save_run(run, {"shortlisted": len(short), "scored": len(scores), "tracker": len(sheet_rows),
                      "leads": len(leads), "unscored": len(missing), "invalid": len(bad),
                      "firecrawl_spent": fc.get("spent", 0), "firecrawl_allowance": fc.get("allowance")})
    update_claude_md(run,
                     f"| scored {len(scores)}/{len(short)} | {sum(ok for _, _, ok in digest)} tracker-worthy (top: {top[:60]}) | "
                     f"{len(leads)} leads | {len(missing)} unscored | firecrawl {fc.get('spent', 0)}/{fc.get('allowance', '-')}"
                     + (f" | {len(bad)} invalid" if bad else ""))
    retention = apply_retention(cfg, d)
    print(f"scored {len(scores)}/{len(short)} | tracker: +{len(sheet_rows)} queued, {pushed} pushed, {pending} pending "
          f"({msg}) | seen +{added} | digest {rel(dig)} | {retention}")
    for b in bad:
        print("  INVALID", b)
    for j in leads:
        print(f"  LEAD (no JD): {j['company']} — {j['title']} {j['url']}")
    if missing:
        print(f"  NOT SCORED ({len(missing)}): {', '.join(missing)} (retried next run)")
    for s, j, to_sheet in digest[: a.top]:
        print(f"  {s['score']:>3} {'*' if to_sheet else ' '} {j['company']} — {j['title']} [{s['profile']}]"
              + (f" | gaps: {'; '.join(s.get('gaps', [])[:2])}" if s.get("gaps") else ""))


def apply_retention(cfg: dict, d) -> str:
    """Compress this run's raw fetch file; delete raw data (feeds.jsonl.gz, Firecrawl page cache) of runs older than
    `retention_days_raw`. Kept forever: shortlist, scores, jd/, digests, decisions, rejected/accepted lists."""
    import gzip
    import shutil
    import time
    freed = 0
    for name in ("feeds.jsonl", "indeed_raw.jsonl"):
        f = d / name
        if f.exists() and f.stat().st_size:
            gz = d / (name + ".gz")
            gz.write_bytes(gzip.compress(f.read_bytes(), compresslevel=6))
            freed += f.stat().st_size - gz.stat().st_size
            f.unlink()
    cutoff = time.time() - int(cfg.get("retention_days_raw", 7)) * 86400
    for old in RUNS_DIR.iterdir():
        if not old.is_dir() or old == d:
            continue
        for target in [old / "feeds.jsonl.gz", old / "indeed_raw.jsonl.gz", old / "fc"]:
            if target.exists() and target.stat().st_mtime < cutoff:
                freed += sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) if target.is_dir() else target.stat().st_size
                shutil.rmtree(target) if target.is_dir() else target.unlink()
    return f"retention: freed {freed / 1e6:.0f} MB"


def update_claude_md(run: str, row: str, keep: int = 7) -> None:
    """Rewrite the auto run-log block in CLAUDE.md (one line per run folder, newest first). Zero LLM tokens."""
    path = HOME / "CLAUDE.md"
    if not path.exists():
        return
    text = path.read_text()
    start, end = "<!-- AUTO:RUNLOG:START -->", "<!-- AUTO:RUNLOG:END -->"
    if start not in text or end not in text:
        return
    head, rest = text.split(start, 1)
    block, tail = rest.split(end, 1)
    lines = [l for l in block.strip().splitlines() if l.startswith("- ") and not l.startswith(f"- {run} ")]
    lines = ([f"- {run} {row}"] + lines)[:keep]
    path.write_text(f"{head}{start}\n" + "\n".join(lines) + f"\n{end}{tail}")


def cmd_fetch_url(a, cfg):
    """Fetch a single ATS job via its public API; prints the jd path or FALLBACK."""
    import urllib.request
    url = a.url.split("?")[0].rstrip("/")
    get = lambda u: json.loads(urllib.request.urlopen(  # noqa: E731
        urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30).read())
    job = None
    m = re.search(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)", a.url)
    if m:
        j = get(f"https://boards-api.greenhouse.io/v1/boards/{m[1]}/jobs/{m[2]}")
        job = (j["title"], j.get("company_name") or m[1], j["location"]["name"], html_to_text(j.get("content"), 20000), j["absolute_url"])
    ml = re.search(r"jobs\.(eu\.)?lever\.co/([\w.-]+)/([0-9a-f-]{36})", url)
    if ml:
        host = "api.eu.lever.co" if ml[1] else "api.lever.co"
        j = get(f"https://{host}/v0/postings/{ml[2]}/{ml[3]}?mode=json")
        body = "\n\n".join(filter(None, [j.get("descriptionPlain")] + [
            f"{x['text']}\n{html_to_text(x['content'])}" for x in j.get("lists", [])] + [j.get("additionalPlain")]))
        job = (j["text"], ml[2], j["categories"].get("location", ""), body, j["hostedUrl"])
    ma = re.search(r"jobs\.ashbyhq\.com/([\w.-]+)/([0-9a-f-]{36})", url)
    if ma:
        board = get(f"https://api.ashbyhq.com/posting-api/job-board/{ma[1]}")
        j = next((x for x in board["jobs"] if x["id"] == ma[2]), None)
        if j:
            job = (j["title"], ma[1], j.get("location", ""), j.get("descriptionPlain", ""), j["jobUrl"])
    if not job and "linkedin.com" not in a.url:
        from firecrawl import Budget
        from sources import custom_description
        body = custom_description(a.url, Budget(cfg, a.date))  # free fetch, Firecrawl only if JS-rendered
        if len(body) >= 600:
            job = ("(see description)", re.sub(r"^www\.", "", url.split("/")[2]), "", body, a.url)
    if not job:
        print(f"FALLBACK: could not fetch; use Indeed get_job_details or WebFetch for {a.url}")
        return
    title, company, loc, body, canon = (x.strip() if isinstance(x, str) else x for x in job)
    write_context(cfg)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:60]
    out = DATA / "tailoring" / slug
    out.mkdir(parents=True, exist_ok=True)
    (out / "jd.txt").write_text(f"{title} — {company} — {loc}\nURL: {canon}\n\n{body}")
    print(f"JD {rel(out / 'jd.txt')} | {title} — {company} — {loc}")


def cmd_track(a, cfg):
    """Add one job (e.g. from `tailor`) to the tracker and mark it seen."""
    from tracker import flush, queue_rows
    if not all([a.company, a.role, a.url, a.profile]) or a.score is None:
        sys.exit("track needs --company --role --score --profile --location and the URL")
    loc = a.location or ""
    key = job_key(a.company, a.role, "remote" if "remote" in loc.lower() else norm(loc).strip()[:40])
    if key in db.seen_keys():
        print(f"already tracked/seen: {key}")
        return
    queue_rows([{"company": a.company, "role": a.role, "score": a.score, "url": a.url, "profile": a.profile,
                 "location": loc, "posted": "", "source": "manual", "date_added": today(), "status": ""}])
    db.mark_seen([{"key": key, "status": "tracked", "score": a.score, "profile": a.profile, "source": "manual",
                   "company": a.company, "title": a.role, "location": loc, "url": a.url}])
    pushed, pending, msg = flush(cfg)
    print(f"tracked {a.company} — {a.role}: {pushed} pushed, {pending} pending ({msg})")


def status_data(cfg) -> dict:
    """Config / profile / seen / tracker summary; `status` prints it, the MCP server returns it."""
    from setup import seed_warnings
    from tracker import describe
    companies = json.loads(COMPANIES_PATH.read_text()) if COMPANIES_PATH.exists() else []
    return {"data_folder": str(HOME), "country": cfg.get("country"), "currency": cfg["currency"],
            "max_age_days": cfg["max_age_days"], "remote_scope": cfg.get("remote_scope"),
            "sources": [k for k, v in cfg["sources"].items() if v], "profiles": list(load_profiles()),
            "companies": len(companies), "companies_active": sum(bool(c.get("active", True)) for c in companies),
            "seen": db.seen_count(), "tracker": describe(cfg), "warnings": seed_warnings(HOME)}


def cmd_status(a, cfg):
    d = status_data(cfg)
    print(f"data folder {d['data_folder']} | country {d['country']} ({d['currency']}) | max_age {d['max_age_days']}d | "
          f"remote_scope {d['remote_scope']} | sources {d['sources']}")
    print(f"profiles: {', '.join(d['profiles']) or 'none'} | companies: {d['companies']} "
          f"({d['companies_active']} active) | seen: {d['seen']}")
    print(d["tracker"])
    for w in d["warnings"]:
        print(f"warning: {w}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "feeds", "filter", "decide", "shortlist", "publish", "fetch-url",
                                    "track", "status", "context"])
    ap.add_argument("url", nargs="?")
    ap.add_argument("--date")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--top", type=int, default=10)
    for opt in ("company", "role", "profile", "location"):
        ap.add_argument(f"--{opt}")
    ap.add_argument("--score", type=int)
    a = ap.parse_args()
    cfg = load_config()
    {"plan": cmd_plan, "feeds": cmd_feeds, "filter": cmd_filter, "decide": cmd_decide,
     "shortlist": cmd_shortlist, "publish": cmd_publish, "fetch-url": cmd_fetch_url,
     "track": cmd_track, "status": cmd_status, "context": cmd_context}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
