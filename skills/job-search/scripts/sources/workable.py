"""Workable public widget API."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net


def fetch_workable(company: dict) -> list:
    d = net.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{company['token']}?details=true")
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
            "job_type": j.get("employment_type") or "",
            "url": j.get("url", ""), "description": html_to_text(j.get("description")),
        })
    return out
