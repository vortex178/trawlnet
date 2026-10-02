"""Generic handler for company career pages with no known board API: free link scraping first, Firecrawl markdown for
JS-rendered pages, plus the JD-text check used when a description has to be scraped."""
from __future__ import annotations

import html
import re
import urllib.parse

from . import net
from .text import ROLE


_JOB_PATH = re.compile(r"/(job|jobs|career|careers|position|positions|opening|openings|vacanc\w*|requisition\w*|"
                       r"role|roles|j|jobdetails|job-details|apply)/[^/?#\s]{2,}", re.I)
_GENERIC = {"apply", "apply now", "view", "view job", "view details", "learn more", "read more", "careers",
            "jobs", "see all jobs", "open positions", "view all", "join us", "see openings", "details", "know more"}
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
                or not (4 <= len(title) <= 120) or not ROLE.search(title)):
            continue
        seen.add(url)
        out.append((title, url, loc))
    return out


def _text_titles(md: str, page_url: str) -> list:
    """Fallback for pages listing titles without per-job links: short standalone role-title lines."""
    out, seen = [], set()
    for line in md.splitlines():
        s = _clean(re.sub(r"^\s*[-*#>\d.]+\s*", "", line))
        if (4 <= len(s) <= 70 and len(s.split()) <= 9 and ROLE.search(s) and not re.search(r"[.!?:;]$|\(|http", s)
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
    html = net.get(url, timeout=20).decode("utf-8", "ignore")
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
