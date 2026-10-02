"""Zero-token fetchers: job-board APIs/RSS, public ATS APIs, custom career sites (free fetch, Firecrawl fallback).
Country specifics (place names, Adzuna country, currency) come from the config's country pack.

Each fetcher returns normalized raw records:
  source, source_id, title, company, location, remote (True/False/None), region_text,
  eligible_countries, posted (ISO), salary_text, url, description (plain text)
"""
from __future__ import annotations

import datetime as dt
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from common import UA, html_to_text

from . import net
from .generic import _custom_records, _job_links, _looks_like_jd, _text_titles, fetch_custom_firecrawl, fetch_custom_free  # noqa: F401
from .atlassian import _ATLASSIAN_CAREERS, _atlassian_cache, atlassian_description, fetch_atlassian  # noqa: F401
from .workday import _workday_country_facet, _workday_posted, fetch_workday, workday_description  # noqa: F401
from .darwinbox import fetch_darwinbox
from .greenhouse import fetch_greenhouse
from .lever import fetch_lever
from .ashby import fetch_ashby
from .workable import fetch_workable
from .smartrecruiters import fetch_smartrecruiters, smartrecruiters_description
from .adzuna import fetch_adzuna
from .alignerr import fetch_alignerr, alignerr_description
from .wwr import fetch_wwr
from .remoteok import fetch_remoteok
from .hn import fetch_hn


_WORKABLE_JOB = re.compile(r"https?://apply\.workable\.com/(?:([\w-]+)/)?j/(\w+)")


def workable_description(url: str) -> str:
    """Description of a Workable job linked as apply.workable.com/[<account>/]j/<shortcode> (careers pages that embed the
    board). The bare shortlink redirects to /<account>/j/<shortcode>; the account's public widget API has the text."""
    m = _WORKABLE_JOB.match(url)
    if not m:
        return ""
    account, code = m.groups()
    if not account:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
                final = r.geturl()
        except urllib.error.HTTPError as e:  # a bot-check page still carries the redirected URL
            final = e.geturl()
        m = _WORKABLE_JOB.match(final)
        account = m.group(1) if m else None
    if not account:
        return ""
    return next((j["description"] for j in fetch_workable({"name": "", "token": account}) if j["source_id"] == code), "")


def custom_description(detail_url: str, budget=None) -> str:
    """Full posting text, or "" when none could be obtained (nav/CSS-only pages are never returned as a JD)."""
    text = ""
    for resolver in (atlassian_description, workable_description):  # known JS-rendered/embedded career sites
        try:
            text = resolver(detail_url)
        except Exception:
            text = ""
        if text:  # structured sources: trusted as is, no keyword heuristics
            return text
    try:
        text = html_to_text(net.get(detail_url, timeout=20).decode("utf-8", "ignore"))
    except Exception:
        pass
    if not _looks_like_jd(text) and budget is not None and budget.left() > 0:  # JS-rendered/nav-only page -> Firecrawl (1 credit)
        text = (budget.scrape_markdown(detail_url) or text)[:9000]
    return text if _looks_like_jd(text) else ""


def _title_in(title: str, text: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9+#]+", re.sub(r"\([^)]*\)", " ", title.lower())) if len(w) > 2]
    low = text.lower()
    return bool(words) and sum(w in low for w in words) >= max(1, -(-len(words) * 4 // 5))  # ceil(80%)


def _ats_job(url: str, title: str, company: str):
    """If url points at a supported ATS board, return the matching job's description via its API."""
    for ats, rx in (("ashby", r"jobs\.ashbyhq\.com/([\w.-]+)"), ("greenhouse", r"greenhouse\.io/([\w-]+)"),
                    ("lever", r"jobs\.lever\.co/([\w.-]+)"), ("workable", r"apply\.workable\.com/(?!j/)([\w-]+)")):
        m = re.search(rx, url)
        if m:
            try:
                jobs = ATS_FETCHERS[ats]({"name": company, "token": m.group(1)})
            except Exception:
                return None
            best = [j for j in jobs if _title_in(title, j["title"]) or _title_in(j["title"], title)]
            return (best[0]["url"], best[0]["description"]) if best else None
    return None


def enrich_short_description(rec: dict, budget=None) -> str:
    """HN posts / Remote OK API text are often summaries: fetch the linked full posting (ATS API when the link is a
    job board, else free fetch with Firecrawl fallback). Only text that contains the job title is used."""
    desc = rec.get("description") or ""
    if len(desc) >= 1500:
        return desc
    urls = [rec.get("apply_url")] + [u.rstrip(".,;") for u in re.findall(r"https?://[^\s)\]>\"']+", desc)]
    urls = [u for u in urls if u and not re.search(r"news\.ycombinator|linkedin\.com", u, re.I)
            and urllib.parse.urlparse(u).path.strip("/")]  # skip bare homepages
    for url in urls[:3]:
        hit = _ats_job(url, rec["title"], rec["company"])
        if hit:
            return desc + f"\n\nFull posting ({hit[0]}):\n{hit[1]}"
        full = custom_description(url, budget)
        if len(full) >= 600 and _title_in(rec["title"], full):
            return desc + f"\n\nFull posting ({url}):\n{full}"
    return desc


def lazy_description(rec: dict, budget=None) -> str:
    """Descriptions not included in list endpoints; fetched only for shortlisted jobs."""
    if rec["source"] == "smartrecruiters":
        return smartrecruiters_description(rec["detail_url"])
    if rec["source"] == "workday":
        return workday_description(rec["detail_url"])
    if rec["source"] == "custom":
        return custom_description(rec["detail_url"], budget)
    if rec["source"] == "alignerr":
        return alignerr_description(rec["detail_url"])
    if rec["source"] == "adzuna":  # full posting on the Adzuna details page; keep only if it contains the title
        full = custom_description(rec["detail_url"], budget)
        return full if _title_in(rec["title"], full) else ""
    return ""


UNDOCUMENTED_ATS = {"workday", "darwinbox"}
ATS_FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
                "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters,
                "workday": fetch_workday, "darwinbox": fetch_darwinbox}


GONE_CODES = (404, 410)  # "board does not exist"; timeouts, 429 and 5xx are transient and never count
DEAD_AFTER = 3           # consecutive days with a gone response before a board is considered dead


def record_outcome(c: dict, exc=None) -> None:
    """Track ATS board health on the company entry: `last_ok`, and `gone_days` = consecutive days of 404/410."""
    today = dt.date.today().isoformat()
    if exc is None:
        c["last_ok"], c["gone_days"] = today, 0
        c.pop("last_gone", None)
    elif getattr(exc, "code", None) in GONE_CODES and c.get("last_gone") != today:
        c["gone_days"], c["last_gone"] = c.get("gone_days", 0) + 1, today


def is_dead(c: dict) -> bool:
    return c.get("gone_days", 0) >= DEAD_AFTER


def fetch_all(cfg: dict, companies: list, budget=None) -> tuple:
    """Fetch WWR feeds + ATS boards in parallel. Returns (records, per-source counts, errors)."""
    tasks = []
    if cfg["sources"].get("wwr"):
        tasks += [(f"wwr:{f}", fetch_wwr, f) for f in cfg.get("wwr_feeds", [])]
    if cfg["sources"].get("remoteok"):
        tasks.append(("remoteok", fetch_remoteok, None))
    if cfg["sources"].get("hn"):
        tasks.append(("hn", fetch_hn, cfg))
    if cfg["sources"].get("alignerr") and not cfg.get("exclude_contract"):  # every Alignerr role is contract work
        tasks.append(("alignerr", fetch_alignerr, cfg))
    if cfg["sources"].get("adzuna"):
        tasks.append(("adzuna", fetch_adzuna, cfg))
    undocumented = cfg["sources"].get("undocumented_ats", False)  # Workday/Darwinbox career-site endpoints: opt-in
    if cfg["sources"].get("ats"):
        for c in companies:
            fn = ATS_FETCHERS.get(c.get("ats"))
            if c.get("ats") in UNDOCUMENTED_ATS and not undocumented:
                continue
            if fn and c.get("token") and c.get("active", True):
                tasks.append((f"{c['ats']}:{c['name']}", fn, c))
    customs = [c for c in companies if c.get("ats") == "custom" and c.get("careers_url") and c.get("active", True)] \
        if cfg["sources"].get("ats") else []
    tasks += [(f"custom:{c['name']}", fetch_atlassian if undocumented and _ATLASSIAN_CAREERS.match(c["careers_url"])
               else fetch_custom_free, c) for c in customs]
    records, counts, errors = [], {}, []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fn, arg): (label, arg) for label, fn, arg in tasks}
        for fut, (label, arg) in futs.items():
            board = arg if label.split(":")[0] in ATS_FETCHERS else None
            try:
                rows = fut.result()
                records += rows
                counts[label] = len(rows)
                if board is not None:
                    record_outcome(board)
            except Exception as e:  # network / schema errors are reported, not fatal
                errors.append(f"{label}: {type(e).__name__}: {str(e)[:120]}")
                if board is not None:
                    record_outcome(board, e)
    # custom sites that yielded nothing for free -> Firecrawl, least recently scraped first, keeping a reserve
    if budget is not None and budget.enabled:
        keep = int((cfg.get("firecrawl") or {}).get("shortlist_reserve", 5))
        empty = [c for c in customs if not counts.get(f"custom:{c['name']}")]
        for c in sorted(empty, key=lambda c: c.get("last_firecrawl") or ""):
            if budget.left() <= keep:
                break
            rows = fetch_custom_firecrawl(c, budget)
            c["last_firecrawl"] = dt.date.today().isoformat()
            records += rows
            counts[f"custom:{c['name']}"] = len(rows)
    return records, counts, errors
