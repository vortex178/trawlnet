"""Atlassian careers: the whole board and every description from its listings endpoint (undocumented; opt-in via
sources.undocumented_ats). The career pages are JS-rendered."""
from __future__ import annotations

import re

from common import html_to_text

from . import net
from .board import Board


_ATLASSIAN_JOB = re.compile(r"https?://(?:www\.)?atlassian\.com/company/careers/details/(\d+)")
_ATLASSIAN_CAREERS = re.compile(r"https?://(?:www\.)?atlassian\.com/company/careers", re.I)
_atlassian_cache: dict = {}


def _atlassian_listings() -> list:
    if "jobs" not in _atlassian_cache:  # one request per run covers the job list and every description
        _atlassian_cache["jobs"] = net.get_json("https://www.atlassian.com/endpoint/careers/listings")
    return _atlassian_cache["jobs"]


def _atlassian_text(job: dict) -> str:
    return html_to_text("\n".join(job.get(k) or "" for k in ("overview", "responsibilities", "qualifications")))


def atlassian_description(url: str) -> str:
    """Atlassian's career pages are JS-rendered; its listings endpoint (undocumented, so opt-in) carries the full text.
    Returns "" for other URLs, when the opt-in is off, or when the job is not listed."""
    m = _ATLASSIAN_JOB.match(url)
    if not m or not net.cfg()["sources"].get("undocumented_ats"):
        return ""
    job = next((j for j in _atlassian_listings() if str(j.get("id")) == m.group(1)), None)
    return _atlassian_text(job) if job else ""


def _atlassian_location(entry: str) -> str:
    """'Bengaluru - India -   Bengaluru,  560071 India' -> 'Bengaluru (India)'; 'Remote - ...' entries stay as listed."""
    parts = [p.strip() for p in entry.split(" - ")]
    return entry.strip() if parts[0] == "Remote" or len(parts) < 2 else f"{parts[0]} ({parts[1]})"


def fetch_atlassian(company: dict) -> list:
    """The whole board, with full text, from Atlassian's listings endpoint (undocumented: opt-in). The careers page
    itself is JS-rendered, so without this it costs a Firecrawl credit per scrape and yields no locations."""
    out = []
    for j in _atlassian_listings():
        url = f"https://www.atlassian.com/company/careers/details/{j['id']}"
        out.append({
            "source": "custom", "source_id": url, "title": j.get("title", ""), "company": company["name"],
            "location": "; ".join(_atlassian_location(x) for x in j.get("locations") or []), "remote": None,
            "region_text": "", "eligible_countries": "", "posted": None, "salary_text": "",
            "job_type": j.get("type") or "", "url": url, "description": _atlassian_text(j)[:9000], "detail_url": url,
        })
    return out


BOARD = Board("atlassian", fetch_atlassian, resolve=atlassian_description, careers=_ATLASSIAN_CAREERS,
              undocumented=True)
