"""Alignerr AI-training contract roles (public /api/jobs used by alignerr.com/jobs); opt-in."""
from __future__ import annotations

import json
import re
import urllib.parse

from common import html_to_text

from . import net
from .board import Board


def fetch_alignerr(cfg: dict) -> list:
    """Search terms from config; each role is posted as many geo-targeted copies -> one record per title,
    preferring a copy whose teaser targets the user's country. Full description fetched lazily."""
    groups = {}
    for term in cfg.get("alignerr_searches") or []:
        off = 0
        while off < 600:
            d = net.get_json(f"https://www.alignerr.com/api/jobs?search={urllib.parse.quote(term)}&limit=120&offset={off}")
            for j in d.get("jobs") or []:
                groups.setdefault(j["title"].strip(), []).append(j)
            off += 120
            if off >= (d.get("total") or 0):
                break
    out, name, places = [], cfg["pack"]["name"], net.places_rx()
    for title, js in groups.items():
        local = next((j for j in js if re.search(rf"\b({places})\b", j.get("description") or "", re.I)), None)
        j = local or js[0]
        out.append({
            "source": "alignerr", "source_id": j["id"], "title": title, "company": "Alignerr",
            "location": "Remote" + (f" - {name}" if local else ""), "remote": True,
            "region_text": name if local else "", "eligible_countries": "", "posted": None,
            "salary_text": (j.get("pay") or "").replace("$", "USD ").replace("/hr", " per hour"),
            "job_type": "Contract", "url": f"https://www.alignerr.com/jobs/{j['id']}", "description": "",
            "detail_url": f"https://www.alignerr.com/jobs/{j['id']}",
        })
    return out


def alignerr_description(detail_url: str) -> str:
    h = net.get(detail_url).decode("utf-8", "ignore")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    job = (json.loads(m.group(1))["props"]["pageProps"].get("job") or {}) if m else {}
    head = (f"Engagement: {job.get('jobType', '')} ({job.get('salaryType', '')}), AI-training work for Alignerr. "
            f"Listing location: {job.get('location', '')}. First posted: {str(job.get('firstPostDate', ''))[:10]}.\n\n")
    return (head + html_to_text(job.get("htmlLongDescription") or job.get("longDescription") or ""))[:9000]


BOARD = Board("alignerr", describe=alignerr_description)  # the feed itself is a config search, not a board
