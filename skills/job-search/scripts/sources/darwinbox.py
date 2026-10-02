"""Darwinbox career sites (public endpoint used by *.darwinbox.in); opt-in via sources.undocumented_ats."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net


def fetch_darwinbox(company: dict) -> list:
    host = company.get("host") or f"{company['token']}.darwinbox.in"
    cid = company.get("site") or "main"
    page = f"https://{host}/ms/candidatev2/{cid}/careers/allJobs"
    rows, pg = [], 1
    while pg <= 10:  # {"page", "limit"} paginate; response carries job_counts (total)
        d = net.post_json(f"https://{host}/ms/candidateapi/job/alljobs?companyId={cid}", {"page": pg, "limit": 100},
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
