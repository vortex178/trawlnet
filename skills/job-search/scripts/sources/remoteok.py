"""Remote OK public API (no key; terms: credit Remote OK and link to the original listing)."""
from __future__ import annotations

from common import html_to_text, parse_date

from . import net


def fetch_remoteok(_=None) -> list:
    d = net.get_json("https://remoteok.com/api")
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
