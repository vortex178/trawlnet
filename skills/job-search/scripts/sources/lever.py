"""Lever public postings API (US and EU hosts)."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net
from .board import Board


def fetch_lever(company: dict) -> list:
    host = "api.eu.lever.co" if company.get("region") == "eu" else "api.lever.co"
    d = net.get_json(f"https://{host}/v0/postings/{company['token']}?mode=json")
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
            "posted": parse_date(j.get("createdAt")), "salary_text": sal_text, "job_type": cat.get("commitment") or "",
            "url": j.get("hostedUrl", ""), "description": desc[:9000],
        })
    return out


BOARD = Board("lever", fetch_lever, link=r"jobs\.lever\.co/([\w.-]+)")
