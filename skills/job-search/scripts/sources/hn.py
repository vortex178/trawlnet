"""Hacker News "Ask HN: Who is hiring?" (monthly thread, Algolia API)."""
from __future__ import annotations

import re

from common import age_days, html_to_text, parse_date

from . import net
from .text import ROLE


def fetch_hn(cfg: dict) -> list:
    s = net.get_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    story = next((h for h in s.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story or (age_days(parse_date(story.get("created_at"))) or 0) > 35:
        return []
    item = net.get_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    out, places = [], "|".join(re.escape(p) for p in cfg["pack"]["country_places"])
    for c in item.get("children") or []:
        text = html_to_text(c.get("text"))
        if not text:
            continue
        head = text.split("\n", 1)[0]
        parts = [p.strip() for p in head.split("|") if p.strip()]
        if len(parts) < 2:
            continue
        company = parts[0][:80]
        loc_like = re.compile(r"^\s*(remote|onsite|on-site|hybrid|full[- ]time|part[- ]time|contract)\b", re.I)
        if loc_like.search(company):
            continue  # malformed header (company slot holds a location/type)
        if ROLE.search(company) and not any(ROLE.search(p) for p in parts[1:]):
            continue  # company slot holds the role and no company is given
        role_part = next((p for p in parts[1:] if ROLE.search(p)), parts[1])
        if len(role_part.split()) > 12 or re.search(r"\b(we|we're|we are|looking|join)\b", role_part, re.I):
            continue  # a sentence, not a role list
        loc = "; ".join(p for p in parts[1:] if p is not role_part and re.search(
            rf"remote|onsite|on-site|hybrid|\b({places})\b|usa|us\b|uk|europe|"
            r"worldwide|global|anywhere|[A-Z][a-z]+, [A-Z]{2}\b", p, re.I))[:120]
        for title in [t.strip() for t in role_part.split(",") if t.strip()][:5]:
            out.append({
                "source": "hn", "source_id": f"{c['id']}:{title}", "title": title[:120], "company": company,
                "location": loc or "Unspecified", "remote": bool(re.search(r"remote", loc, re.I)),
                "region_text": "", "eligible_countries": "", "posted": parse_date(c.get("created_at")),
                "salary_text": "", "url": f"https://news.ycombinator.com/item?id={c['id']}",
                "description": text[:9000],
            })
    return out
