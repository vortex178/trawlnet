"""Ashby public job-board API."""
from __future__ import annotations

from common import parse_date

from . import net
from .board import Board


def fetch_ashby(company: dict) -> list:
    d = net.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{company['token']}?includeCompensation=true")
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
            "job_type": j.get("employmentType") or "",
            "url": j.get("jobUrl", ""), "description": (j.get("descriptionPlain") or "")[:9000],
        })
    return out


BOARD = Board("ashby", fetch_ashby, link=r"jobs\.ashbyhq\.com/([\w.-]+)")
