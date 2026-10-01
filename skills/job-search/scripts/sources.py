"""Zero-token fetchers: job-board APIs/RSS, public ATS APIs, custom career sites (free fetch, Firecrawl fallback).
Country specifics (place names, Adzuna country, currency) come from the config's country pack.

Each fetcher returns normalized raw records:
  source, source_id, title, company, location, remote (True/False/None), region_text,
  eligible_countries, posted (ISO), salary_text, url, description (plain text)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

from functools import lru_cache

from common import HOME, UA, age_days, html_to_text, load_config, parse_date


@lru_cache(maxsize=1)
def _cfg() -> dict:
    return load_config()


def _pack() -> dict:
    return _cfg()["pack"]


def _places_rx() -> str:
    return "|".join(re.escape(p) for p in _pack()["country_places"])


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _get_json(url: str):
    return json.loads(_get(url))


# ---------- We Work Remotely ----------

def fetch_wwr(feed: str) -> list:
    url = f"https://weworkremotely.com/{feed}.rss"
    root = ET.fromstring(_get(url))
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        company, _, title = g("title").partition(":")
        if not title:
            company, title = "", company
        out.append({
            "source": "wwr", "source_id": g("guid") or g("link"),
            "title": title.strip(), "company": company.strip(),
            "location": "Remote", "remote": True,
            "region_text": g("region"), "eligible_countries": g("country"),
            "posted": parse_date(g("pubDate")), "expires": parse_date(g("expires_at")), "salary_text": "",
            "url": g("link"), "description": html_to_text(g("description")),
        })
    return out


# ---------- Remote OK (no key; terms: credit Remote OK and link to the original listing) ----------

def fetch_remoteok(_=None) -> list:
    d = _get_json("https://remoteok.com/api")
    out = []
    for j in d if isinstance(d, list) else []:
        if not isinstance(j, dict) or not j.get("position"):
            continue  # first element is the legal notice
        lo, hi = j.get("salary_min") or 0, j.get("salary_max") or 0
        out.append({
            "source": "remoteok", "source_id": str(j.get("id", "")),
            "title": j.get("position", ""), "company": j.get("company", ""),
            "location": "Remote", "remote": True, "region_text": j.get("location") or "", "eligible_countries": "",
            "posted": parse_date(j.get("date")), "salary_text": f"USD {lo} - {hi} per year" if lo and hi else "",
            "url": j.get("url", ""), "apply_url": j.get("apply_url") or "",
            "description": html_to_text(j.get("description")),
        })
    return out


# ---------- Alignerr (AI-training contract roles; public /api/jobs used by alignerr.com/jobs) ----------

def fetch_alignerr(cfg: dict) -> list:
    """Search terms from config; each role is posted as many geo-targeted copies -> one record per title,
    preferring a copy whose teaser targets the user's country. Full description fetched lazily."""
    groups = {}
    for term in cfg.get("alignerr_searches") or []:
        off = 0
        while off < 600:
            d = _get_json(f"https://www.alignerr.com/api/jobs?search={urllib.parse.quote(term)}&limit=120&offset={off}")
            for j in d.get("jobs") or []:
                groups.setdefault(j["title"].strip(), []).append(j)
            off += 120
            if off >= (d.get("total") or 0):
                break
    out, name, places = [], cfg["pack"]["name"], _places_rx()
    for title, js in groups.items():
        local = next((j for j in js if re.search(rf"\b({places})\b", j.get("description") or "", re.I)), None)
        j = local or js[0]
        out.append({
            "source": "alignerr", "source_id": j["id"], "title": title, "company": "Alignerr",
            "location": "Remote" + (f" - {name}" if local else ""), "remote": True,
            "region_text": name if local else "", "eligible_countries": "", "posted": None,
            "salary_text": (j.get("pay") or "").replace("$", "USD ").replace("/hr", " per hour"),
            "url": f"https://www.alignerr.com/jobs/{j['id']}", "description": "",
            "detail_url": f"https://www.alignerr.com/jobs/{j['id']}",
        })
    return out


def alignerr_description(detail_url: str) -> str:
    h = _get(detail_url).decode("utf-8", "ignore")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    job = (json.loads(m.group(1))["props"]["pageProps"].get("job") or {}) if m else {}
    head = (f"Engagement: {job.get('jobType', '')} ({job.get('salaryType', '')}), AI-training work for Alignerr. "
            f"Listing location: {job.get('location', '')}. First posted: {str(job.get('firstPostDate', ''))[:10]}.\n\n")
    return (head + html_to_text(job.get("htmlLongDescription") or job.get("longDescription") or ""))[:9000]


# ---------- Adzuna (aggregator, country from the pack; key in .secrets/adzuna.json) ----------

def fetch_adzuna(cfg: dict) -> list:
    """Profile search terms x adzuna_locations (+ a remote query), capped at adzuna_max_requests calls.
    Results carry a 500-char snippet; the full posting (<details_domain>/details/<id>) is fetched only when shortlisted."""
    import time
    from common import load_profiles
    key_path = HOME / (cfg.get("adzuna_key_file") or ".secrets/adzuna.json")
    if not key_path.exists():
        raise RuntimeError(f"no key at {key_path.name}")
    k = json.loads(key_path.read_text())
    terms = [s.lower() for s in cfg.get("adzuna_searches") or []]
    for p in ([] if terms else load_profiles().values()):
        for q in p.get("search_queries") or p["target_titles"][:2]:
            if q.lower() not in terms:
                terms.append(q.lower())
    az = cfg["pack"].get("adzuna") or {}
    spell = {k.lower(): v for k, v in (az.get("city_spellings") or {}).items()}
    locs = [spell.get(x.lower(), x) for x in cfg.get("adzuna_locations") or []]
    searches = [(q, loc) for q in terms for loc in locs]
    if cfg.get("adzuna_remote_query", True):
        searches += [(f"{q} remote", None) for q in terms]
    out, calls = [], 0
    for what, where in searches[: int(cfg.get("adzuna_max_requests", 20))]:
        params = {"app_id": k["app_id"], "app_key": k["app_key"], "results_per_page": 50, "what": what,
                  "max_days_old": cfg["max_age_days"], "content-type": "application/json"}
        if where:
            params["where"] = where
        url = f"https://api.adzuna.com/v1/api/jobs/{az.get('country', cfg.get('country', 'IN').lower())}/search/1?" \
            + urllib.parse.urlencode(params)
        d = None
        for _ in (1, 2):  # the API returns occasional 5xx
            try:
                d = _get_json(url)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                time.sleep(3)
        calls += 1
        time.sleep(1)
        for j in (d or {}).get("results") or []:
            loc = (j.get("location") or {}).get("display_name", "")
            text = html_to_text(j.get("description"))
            title = html_to_text(j.get("title"))
            sal = ""
            if j.get("salary_min") and str(j.get("salary_is_predicted")) == "0":  # ignore Adzuna's estimates
                sal = f"{cfg['currency']} {int(j['salary_min'])} - {int(j.get('salary_max') or j['salary_min'])} per year"
            remote = bool(re.search(r"\bremote\b|work from home|\bwfh\b", f"{title} {text}", re.I))
            out.append({
                "source": "adzuna", "source_id": str(j["id"]), "title": title,
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": ("Remote; " if remote else "") + loc, "remote": remote or None,
                "region_text": cfg["pack"]["name"], "eligible_countries": "", "posted": parse_date(j.get("created")),
                "salary_text": sal, "url": j.get("redirect_url", ""), "description": text,
                "detail_url": f"https://{az.get('details_domain', 'www.adzuna.com')}/details/{j['id']}",
            })
    return out


# ---------- Hacker News "Ask HN: Who is hiring?" (monthly thread, Algolia API) ----------

def fetch_hn(cfg: dict) -> list:
    s = _get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    story = next((h for h in s.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story or (age_days(parse_date(story.get("created_at"))) or 0) > 35:
        return []
    item = _get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out, places = [], "|".join(re.escape(p) for p in cfg["pack"]["country_places"])
    for c in item.get("children") or []:
        text = html_to_text(c.get("text"))
        if not text:
            continue
        head = text.split("\n", 1)[0]
        parts = [p.strip() for p in head.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        company = parts[0][:80]
        loc_like = re.compile(r"^\s*(remote|onsite|on-site|hybrid|full[- ]time|part[- ]time|contract)\b", re.I)
        if loc_like.search(company):
            continue  # malformed header (company slot holds a location/type)
        if _ROLE.search(company) and not any(_ROLE.search(p) for p in parts[1:]):
            continue  # company slot holds the role and no company is given
        role_part = next((p for p in parts[1:] if _ROLE.search(p)), parts[1])
        if len(role_part.split()) > 12 or re.search(r"\b(we|we're|we are|looking|join)\b", role_part, re.I):
            continue  # a sentence, not a role list
        loc = "; ".join(p for p in parts[1:] if p is not role_part and re.search(
            rf"remote|onsite|on-site|hybrid|\b({places})\b|usa|us\b|uk|europe|"
            r"worldwide|global|anywhere|[A-Z][a-z]+, [A-Z]{2}\b", p, re.I))[:120]
        for title in [t.strip() for t in role_part.split(",") if t.strip()][:5]:
            out.append({
                "source": "hn", "source_id": f"{c['id']}:{title}", "title": title[:120], "company": company,
                "location": loc or "Unspecified", "remote": bool(re.search(r"remote", loc, re.I)),
                "region_text": "", "eligible_countries": "", "posted": parse_date(c.get("created_at")),
                "salary_text": "", "url": f"https://news.ycombinator.com/item?id={c['id']}",
                "description": text[:9000],
            })
    return out


# ---------- ATS ----------

def fetch_greenhouse(company: dict) -> list:
    d = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{company['token']}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        offices = ", ".join(o.get("name", "") for o in j.get("offices") or [])
        out.append({
            "source": "greenhouse", "source_id": str(j["id"]),
            "title": j.get("title", ""), "company": company["name"],
            "location": loc, "remote": None, "region_text": offices, "eligible_countries": "",
            "posted": parse_date(j.get("first_published") or j.get("updated_at")),
            "salary_text": "", "url": j.get("absolute_url", ""),
            "description": html_to_text(j.get("content")),
        })
    return out


def fetch_lever(company: dict) -> list:
    host = "api.eu.lever.co" if company.get("region") == "eu" else "api.lever.co"
    d = _get_json(f"https://{host}/v0/postings/{company['token']}?mode=json")
    out = []
    for j in d:
        cat = j.get("categories") or {}
        locs = cat.get("allLocations") or [cat.get("location", "")]
        sal = j.get("salaryRange") or {}
        sal_text = ""
        if sal.get("min"):
            sal_text = f"{sal.get('currency', '')} {sal['min']} - {sal.get('max', sal['min'])} per {sal.get('interval', 'year')}"
        desc = "\n\n".join(filter(None, [j.get("descriptionPlain"), *[
            f"{x.get('text', '')}\n{html_to_text(x.get('content'))}" for x in j.get("lists") or []
        ], j.get("additionalPlain")]))
        out.append({
            "source": "lever", "source_id": j["id"],
            "title": j.get("text", ""), "company": company["name"],
            "location": "; ".join(filter(None, locs)),
            "remote": True if j.get("workplaceType") == "remote" else (False if j.get("workplaceType") else None),
            "region_text": j.get("country") or "", "eligible_countries": "",
            "posted": parse_date(j.get("createdAt")), "salary_text": sal_text,
            "url": j.get("hostedUrl", ""), "description": desc[:9000],
        })
    return out


def fetch_ashby(company: dict) -> list:
    d = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company['token']}?includeCompensation=true")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations") or []]
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        comp = j.get("compensation") or {}
        out.append({
            "source": "ashby", "source_id": j["id"],
            "title": j.get("title", ""), "company": company["name"],
            "location": "; ".join(filter(None, locs)),
            "remote": bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            "region_text": addr.get("addressCountry", ""), "eligible_countries": "",
            "posted": parse_date(j.get("publishedAt")),
            "salary_text": comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary") or "",
            "url": j.get("jobUrl", ""), "description": (j.get("descriptionPlain") or "")[:9000],
        })
    return out


def fetch_workable(company: dict) -> list:
    d = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{company['token']}?details=true")
    out = []
    for j in d.get("jobs", []):
        locs = j.get("locations") or [{"city": j.get("city"), "country": j.get("country")}]
        loc = "; ".join(", ".join(filter(None, [l.get("city"), l.get("country")])) for l in locs)
        out.append({
            "source": "workable", "source_id": j.get("shortcode", ""),
            "title": j.get("title", ""), "company": company["name"],
            "location": ("Remote; " if j.get("telecommuting") else "") + loc,
            "remote": bool(j.get("telecommuting")), "region_text": "", "eligible_countries": "",
            "posted": parse_date(j.get("published_on") or j.get("created_at")), "salary_text": "",
            "url": j.get("url", ""), "description": html_to_text(j.get("description")),
        })
    return out


def fetch_smartrecruiters(company: dict) -> list:
    """List endpoint has no description; `shortlist` fetches it (smartrecruiters_description) for picked jobs."""
    out, offset = [], 0
    while offset < 1000:
        d = _get_json(f"https://api.smartrecruiters.com/v1/companies/{company['token']}/postings?limit=100&offset={offset}")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append({
                "source": "smartrecruiters", "source_id": j["id"],
                "title": j.get("name", ""), "company": company["name"],
                "location": ("Remote; " if loc.get("remote") else "") + (loc.get("fullLocation") or ""),
                "remote": bool(loc.get("remote")), "region_text": loc.get("country", ""), "eligible_countries": "",
                "posted": parse_date(j.get("releasedDate")), "salary_text": "",
                "url": f"https://jobs.smartrecruiters.com/{company['token']}/{j['id']}",
                "description": "", "detail_url": j.get("ref", ""),
            })
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


def smartrecruiters_description(detail_url: str) -> str:
    d = _get_json(detail_url)
    secs = (d.get("jobAd") or {}).get("sections") or {}
    parts = [f"{(secs.get(k) or {}).get('title', '')}\n{html_to_text((secs.get(k) or {}).get('text'))}"
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    return "\n\n".join(p for p in parts if p.strip())[:9000]


# ---------- Workday (undocumented public endpoint used by *.myworkdayjobs.com career sites) ----------

def _post_json(url: str, body: dict, headers: dict | None = None):
    h = {"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _workday_base(c: dict) -> str:
    return f"https://{c['host']}/wday/cxs/{c['token']}/{c['site']}"


def _workday_facets_flat(facets: list):
    for f in facets or []:
        yield f
        yield from _workday_facets_flat([v for v in f.get("values") or [] if "values" in v])


def _workday_country_facet(facets: list, country: str):
    """{facetParameter: [ids]} restricting to the country: a country facet if the tenant has one,
    else every value of a location facet whose name mentions the country (e.g. 'Gurgaon, India')."""
    flat = list(_workday_facets_flat(facets))
    for f in flat:
        if "country" in (f.get("facetParameter") or "").lower():
            ids = [v["id"] for v in f.get("values") or [] if (v.get("descriptor") or "").lower() == country]
            if ids:
                return {f["facetParameter"]: ids}
    for f in flat:
        if "location" in (f.get("facetParameter") or "").lower():
            places = [re.escape(p) for p in _pack()["country_places"]] or [country]
            ids = [v["id"] for v in f.get("values") or [] if "values" not in v and
                   any(re.search(rf"\b{p}\b", (v.get("descriptor") or "").lower()) for p in places)]
            if ids:
                return {f["facetParameter"]: ids}
    for f in flat:  # opaque facet names (e.g. 'a', 'b'): any facet offering the country itself as a value
        ids = [v["id"] for v in f.get("values") or [] if (v.get("descriptor") or "").lower() == country]
        if ids and f.get("facetParameter"):
            return {f["facetParameter"]: ids}
    return None


def _workday_posted(text: str):
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        days = int(m.group(1)) + (1 if "+" in t else 0) if m else None
    if days is None:
        return None
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch_workday(company: dict, country: str | None = None, max_pages: int = 10) -> list:
    """Server-side filtered to the pack's country (country facet, else location facet values naming its places)."""
    country = (country or _pack()["name"]).lower()
    base = _workday_base(company)
    first = _post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    facet = _workday_country_facet(first.get("facets"), country)
    applied = facet or {}
    out, offset, total = [], 0, None
    while offset < max_pages * 20:
        d = first if (offset == 0 and not facet) else _post_json(
            f"{base}/jobs", {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""})
        total = d.get("total", total) if total is None else total
        posts = d.get("jobPostings") or []
        for j in posts:
            loc = j.get("locationsText") or ""
            if facet and re.match(r"\d+ Locations", loc):
                loc = f"{_pack()['name']} ({loc})"  # facet guarantees at least one location in the country
            out.append({
                "source": "workday", "source_id": j.get("externalPath", ""),
                "title": j.get("title", ""), "company": company["name"],
                "location": loc, "remote": True if "remote" in loc.lower() else None,
                "region_text": _pack()["name"] if facet else "", "eligible_countries": "",
                "posted": _workday_posted(j.get("postedOn")), "salary_text": "",
                "url": f"https://{company['host']}/{company['site']}{j.get('externalPath', '')}",
                "description": "", "detail_url": f"{base}{j.get('externalPath', '')}",
            })
        offset += 20
        if not posts or offset >= (total or 0):
            break
    return out


def workday_description(detail_url: str) -> str:
    info = _get_json(detail_url).get("jobPostingInfo") or {}
    return html_to_text(info.get("jobDescription"))


# ---------- Darwinbox (public endpoint used by *.darwinbox.in career sites) ----------

def fetch_darwinbox(company: dict) -> list:
    host = company.get("host") or f"{company['token']}.darwinbox.in"
    cid = company.get("site") or "main"
    page = f"https://{host}/ms/candidatev2/{cid}/careers/allJobs"
    rows, pg = [], 1
    while pg <= 10:  # {"page", "limit"} paginate; response carries job_counts (total)
        d = _post_json(f"https://{host}/ms/candidateapi/job/alljobs?companyId={cid}", {"page": pg, "limit": 100},
                       {"Origin": f"https://{host}", "Referer": page})
        batch = d.get("data") or []
        rows += batch
        if not batch or len(rows) >= (d.get("job_counts") or 0):
            break
        pg += 1
    out = []
    for j in rows:
        loc = (j.get("locations") or j.get("officelocation_show_arr") or "").replace("\r", "")
        remote = bool(j.get("is_remote"))
        exp = j.get("experience") or ""
        desc = html_to_text(j.get("jd"))
        out.append({
            "source": "darwinbox", "source_id": j.get("id", ""),
            "title": j.get("title") or j.get("designation_display_name") or j.get("designation_name", ""),
            "company": company["name"], "location": ("Remote; " if remote else "") + loc,
            "remote": remote, "region_text": j.get("country", ""), "eligible_countries": "",
            "posted": parse_date(j.get("posted_on") or j.get("created_on")), "salary_text": "",
            "url": f"https://{host}/ms/candidatev2/{cid}/careers/jobDetails/{j.get('id', '')}",
            "description": (f"Experience: {exp}\n\n" if exp else "") + desc,
        })
    return out


# ---------- Custom career sites (free fetch first, Firecrawl markdown when JS-rendered) ----------

_JOB_PATH = re.compile(r"/(job|jobs|career|careers|position|positions|opening|openings|vacanc\w*|requisition\w*|"
                       r"role|roles|j|jobdetails|job-details|apply)/[^/?#\s]{2,}", re.I)
_GENERIC = {"apply", "apply now", "view", "view job", "view details", "learn more", "read more", "careers",
            "jobs", "see all jobs", "open positions", "view all", "join us", "see openings", "details", "know more"}


_ROLE = re.compile(r"\b(engineer|developer|sde|sre|devops|architect|analyst|scientist|manager|lead|head|director|"
                   r"designer|consultant|specialist|associate|executive|officer|administrator|admin|intern|"
                   r"programmer|tester|qa|researcher|recruiter|representative|pentester|trainee)\b", re.I)


_JOB_HOSTS = re.compile(r"(jobs\.gem\.com|turbohire\.co|keka\.com|freshteam\.com|zohorecruit\.|lever\.co|greenhouse\.io|"
                        r"ashbyhq\.com|workable\.com|darwinbox\.|smartrecruiters\.com|myworkdayjobs\.com|recruitee\.com|"
                        r"breezy\.hr|jobvite\.com|instahyre\.com|teamtailor\.com|hirist\.|cutshort\.io|wellfound\.com)", re.I)


def _clean(text: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>|[*_#`]|!\[[^\]]*\]\([^)]*\)", " ", text))
    return re.sub(r"\s+", " ", text).strip()


def _job_links(pairs, page_url: str) -> list:
    """(link text, url) pairs -> [(title, url, location)] for links that look like individual jobs."""
    out, seen = [], set()
    for text, url in pairs:
        url = urllib.parse.urljoin(page_url, url.strip())
        parts = [_clean(x) for x in re.split(r"\\+\s*\n|\n", text)]
        parts = [x for x in parts if x and x.lower() not in _GENERIC]
        if not parts:
            continue
        title = re.sub(r"\s+(apply( now)?|view( job)?|details)$", "", parts[0], flags=re.I)
        loc = ", ".join(x for x in parts[1:] if len(x) <= 60)
        path = urllib.parse.urlparse(url).path
        if (not url.startswith("http") or url.rstrip("/") == page_url.rstrip("/") or url in seen
                or not (_JOB_PATH.search(path) or (_JOB_HOSTS.search(url) and len(path.strip("/").split("/")) >= 2))
                or not (4 <= len(title) <= 120) or not _ROLE.search(title)):
            continue
        seen.add(url)
        out.append((title, url, loc))
    return out


def _text_titles(md: str, page_url: str) -> list:
    """Fallback for pages listing titles without per-job links: short standalone role-title lines."""
    out, seen = [], set()
    for line in md.splitlines():
        s = _clean(re.sub(r"^\s*[-*#>\d.]+\s*", "", line))
        if (4 <= len(s) <= 70 and len(s.split()) <= 9 and _ROLE.search(s) and not re.search(r"[.!?:;]$|\(|http", s)
                and s[0].isupper() and s.lower() not in seen and not re.search(r"\b(we|our|you|your|join|team of)\b", s, re.I)):
            seen.add(s.lower())
            out.append((s, page_url, ""))
    return out


def _custom_records(company: dict, links: list) -> list:
    return [{"source": "custom", "source_id": f"{url}#{title}" if url == company.get("careers_url") else url,
             "title": title, "company": company["name"], "location": loc, "remote": None, "region_text": "",
             "eligible_countries": "", "posted": None, "salary_text": "", "url": url, "description": "",
             "detail_url": url}
            for title, url, loc in links]


def fetch_custom_free(company: dict) -> list:
    url = company["careers_url"]
    html = _get(url, timeout=20).decode("utf-8", "ignore")
    pairs = re.findall(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html, re.S | re.I)
    # job cards often wrap a heading (title) plus a teaser paragraph; prefer the heading when present
    pairs = [(u, (re.search(r"<h[1-5][^>]*>(.*?)</h[1-5]>", t, re.S | re.I) or [None, t])[1]) for u, t in pairs]
    return _custom_records(company, _job_links([(t, u) for u, t in pairs], url))


_OPENINGS = re.compile(r"(open (positions|roles)|current openings|view (all )?(openings|jobs|positions|roles)|"
                       r"see (all )?(openings|jobs|positions|roles)|all jobs|browse jobs|job openings|explore (jobs|roles|openings)|"
                       r"join (our )?team|we.re hiring|search jobs|find (a )?job)", re.I)
_MD_LINK = re.compile(r"\[([^\]]{2,160})\]\((https?://[^)\s]+|/[^)\s]*)\)")


def fetch_custom_firecrawl(company: dict, budget) -> list:
    """Scrape the careers page; if it's a landing page, follow one 'open positions'-style link (1 more credit)
    and remember that URL as the company's careers_url for future runs."""
    url = company["careers_url"]
    md = budget.scrape_markdown(url)
    if not md:
        return []
    pairs = _MD_LINK.findall(md)
    jobs = _job_links(pairs, url)
    if not jobs:
        nxt = next((urllib.parse.urljoin(url, u) for t, u in pairs if _OPENINGS.search(t)
                    and urllib.parse.urljoin(url, u).rstrip("/") != url.rstrip("/")), None)
        if nxt and budget.left() > 0:
            md2 = budget.scrape_markdown(nxt)
            jobs = _job_links(_MD_LINK.findall(md2 or ""), nxt)
            if jobs:
                company["careers_url"] = nxt
            elif md2:
                md = md2  # use the openings page for the text fallback
                url = nxt
    if not jobs:
        jobs = _text_titles(md, url)
    return _custom_records(company, jobs)


def _looks_like_jd(text: str) -> bool:
    return len(text) >= 600 and len(re.findall(
        r"responsibilit|requirement|qualification|years of experience|what you.ll|you will|we.re looking|must have", text, re.I)) >= 2


_ATLASSIAN_JOB = re.compile(r"https?://(?:www\.)?atlassian\.com/company/careers/details/(\d+)")
_atlassian_cache: dict = {}


def atlassian_description(url: str) -> str:
    """Atlassian's career pages are JS-rendered; its listings endpoint (undocumented, so opt-in) carries the full text.
    Returns "" for other URLs, when the opt-in is off, or when the job is not listed."""
    m = _ATLASSIAN_JOB.match(url)
    if not m or not _cfg()["sources"].get("undocumented_ats"):
        return ""
    if "jobs" not in _atlassian_cache:  # one request per run covers every Atlassian job
        _atlassian_cache["jobs"] = _get_json("https://www.atlassian.com/endpoint/careers/listings")
    job = next((j for j in _atlassian_cache["jobs"] if str(j.get("id")) == m.group(1)), None)
    return html_to_text("\n".join(job.get(k) or "" for k in ("overview", "responsibilities", "qualifications"))) if job else ""


def custom_description(detail_url: str, budget=None) -> str:
    """Full posting text, or "" when none could be obtained (nav/CSS-only pages are never returned as a JD)."""
    text = ""
    try:
        text = atlassian_description(detail_url)
        if _looks_like_jd(text):
            return text
    except Exception:
        pass
    try:
        text = html_to_text(_get(detail_url, timeout=20).decode("utf-8", "ignore"))
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
                    ("lever", r"jobs\.lever\.co/([\w.-]+)"), ("workable", r"apply\.workable\.com/([\w-]+)")):
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
    if cfg["sources"].get("alignerr"):
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
    tasks += [(f"custom:{c['name']}", fetch_custom_free, c) for c in customs]
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
