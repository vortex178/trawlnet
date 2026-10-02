"""Workday career sites (undocumented public endpoint used by *.myworkdayjobs.com); opt-in via sources.undocumented_ats."""
from __future__ import annotations

import datetime as dt
import re

from common import html_to_text

from . import net


def _workday_base(c: dict) -> str:
    return f"https://{c['host']}/wday/cxs/{c['token']}/{c['site']}"


def _workday_facets_flat(facets: list):
    for f in facets or []:
        yield f
        yield from _workday_facets_flat([v for v in f.get("values") or [] if "values" in v])


def _workday_country_facet(facets: list, country: str):
    """{facetParameter: [ids]} restricting to the country: a country facet if the tenant has one,
    else every value of a location facet whose name mentions the country (e.g. 'Gurgaon, India')."""
    flat = list(_workday_facets_flat(facets))
    for f in flat:
        if "country" in (f.get("facetParameter") or "").lower():
            ids = [v["id"] for v in f.get("values") or [] if (v.get("descriptor") or "").lower() == country]
            if ids:
                return {f["facetParameter"]: ids}
    for f in flat:
        if "location" in (f.get("facetParameter") or "").lower():
            places = [re.escape(p) for p in net.pack()["country_places"]] or [country]
            ids = [v["id"] for v in f.get("values") or [] if "values" not in v and
                   any(re.search(rf"\b{p}\b", (v.get("descriptor") or "").lower()) for p in places)]
            if ids:
                return {f["facetParameter"]: ids}
    for f in flat:  # opaque facet names (e.g. 'a', 'b'): any facet offering the country itself as a value
        ids = [v["id"] for v in f.get("values") or [] if (v.get("descriptor") or "").lower() == country]
        if ids and f.get("facetParameter"):
            return {f["facetParameter"]: ids}
    return None


def _workday_posted(text: str):
    t = (text or "").lower()
    if "today" in t:
        days = 0
    elif "yesterday" in t:
        days = 1
    else:
        m = re.search(r"(\d+)\+?\s*day", t)
        days = int(m.group(1)) + (1 if "+" in t else 0) if m else None
    if days is None:
        return None
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def fetch_workday(company: dict, country: str | None = None, max_pages: int = 10) -> list:
    """Server-side filtered to the pack's country (country facet, else location facet values naming its places)."""
    country = (country or net.pack()["name"]).lower()
    base = _workday_base(company)
    first = net.post_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""})
    facet = _workday_country_facet(first.get("facets"), country)
    applied = facet or {}
    out, offset, total = [], 0, None
    while offset < max_pages * 20:
        d = first if (offset == 0 and not facet) else net.post_json(
            f"{base}/jobs", {"appliedFacets": applied, "limit": 20, "offset": offset, "searchText": ""})
        total = d.get("total", total) if total is None else total
        posts = d.get("jobPostings") or []
        for j in posts:
            loc = j.get("locationsText") or ""
            if facet and re.match(r"\d+ Locations", loc):
                loc = f"{net.pack()['name']} ({loc})"  # facet guarantees at least one location in the country
            out.append({
                "source": "workday", "source_id": j.get("externalPath", ""),
                "title": j.get("title", ""), "company": company["name"],
                "location": loc, "remote": True if "remote" in loc.lower() else None,
                "region_text": net.pack()["name"] if facet else "", "eligible_countries": "",
                "posted": _workday_posted(j.get("postedOn")), "salary_text": "",
                "url": f"https://{company['host']}/{company['site']}{j.get('externalPath', '')}",
                "description": "", "detail_url": f"{base}{j.get('externalPath', '')}",
            })
        offset += 20
        if not posts or offset >= (total or 0):
            break
    return out


def workday_description(detail_url: str) -> str:
    info = net.get_json(detail_url).get("jobPostingInfo") or {}
    return html_to_text(info.get("jobDescription"))
