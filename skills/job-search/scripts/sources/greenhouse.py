"""Greenhouse public job-board API."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net
from .board import Board


def fetch_greenhouse(company: dict) -> list:
    d = net.get_json(f"https://boards-api.greenhouse.io/v1/boards/{company['token']}/jobs?content=true")
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


BOARD = Board("greenhouse", fetch_greenhouse, link=r"greenhouse\.io/([\w-]+)")
