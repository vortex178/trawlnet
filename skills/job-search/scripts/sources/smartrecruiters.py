"""SmartRecruiters public postings API; the list endpoint has no description, so it is fetched lazily for shortlisted jobs."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net
from .board import Board


def fetch_smartrecruiters(company: dict) -> list:
    """List endpoint has no description; `shortlist` fetches it (smartrecruiters_description) for picked jobs."""
    out, offset = [], 0
    while offset < 1000:
        d = net.get_json(f"https://api.smartrecruiters.com/v1/companies/{company['token']}/postings?limit=100&offset={offset}")
        for j in d.get("content", []):
            loc = j.get("location") or {}
            out.append({
                "source": "smartrecruiters", "source_id": j["id"],
                "title": j.get("name", ""), "company": company["name"],
                "location": ("Remote; " if loc.get("remote") else "") + (loc.get("fullLocation") or ""),
                "remote": bool(loc.get("remote")), "region_text": loc.get("country", ""), "eligible_countries": "",
                "posted": parse_date(j.get("releasedDate")), "salary_text": "",
                "job_type": " ".join(filter(None, [(j.get("typeOfEmployment") or {}).get(k) for k in ("id", "label")])),
                "url": f"https://jobs.smartrecruiters.com/{company['token']}/{j['id']}",
                "description": "", "detail_url": j.get("ref", ""),
            })
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


def smartrecruiters_description(detail_url: str) -> str:
    d = net.get_json(detail_url)
    secs = (d.get("jobAd") or {}).get("sections") or {}
    parts = [f"{(secs.get(k) or {}).get('title', '')}\n{html_to_text((secs.get(k) or {}).get('text'))}"
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    return "\n\n".join(p for p in parts if p.strip())[:9000]


BOARD = Board("smartrecruiters", fetch_smartrecruiters, describe=smartrecruiters_description)
