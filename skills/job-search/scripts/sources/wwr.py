"""We Work Remotely RSS feeds."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from common import html_to_text, parse_date

from . import net


def fetch_wwr(feed: str) -> list:
    url = f"https://weworkremotely.com/{feed}.rss"
    root = ET.fromstring(net.get(url))
    out = []
    for it in root.iter("item"):
        g = lambda tag: (it.findtext(tag) or "").strip()  # noqa: E731
        company, _, title = g("title").partition(":")
        if not title:
            company, title = "", company
        out.append({
            "source": "wwr", "source_id": g("guid") or g("link"),
            "title": title.strip(), "company": company.strip(),
            "location": "Remote", "remote": True,
            "region_text": g("region"), "eligible_countries": g("country"),
            "posted": parse_date(g("pubDate")), "expires": parse_date(g("expires_at")), "salary_text": "",
            "url": g("link"), "description": html_to_text(g("description")),
        })
    return out
