"""Workable public widget API; also resolves Workable job links embedded in career pages."""
from __future__ import annotations

import re
import urllib.error
import urllib.request

from common import UA, html_to_text, parse_date

from . import net
from .board import Board


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


_WORKABLE_JOB = re.compile(r"https?://apply\.workable\.com/(?:([\w-]+)/)?j/(\w+)")


def workable_description(url: str) -> str:
    """Description of a Workable job linked as apply.workable.com/[<account>/]j/<shortcode> (careers pages that embed the
    board). The bare shortlink redirects to /<account>/j/<shortcode>; the account's public widget API has the text."""
    m = _WORKABLE_JOB.match(url)
    if not m:
        return ""
    account, code = m.groups()
    if not account:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
                final = r.geturl()
        except urllib.error.HTTPError as e:  # a bot-check page still carries the redirected URL
            final = e.geturl()
        m = _WORKABLE_JOB.match(final)
        account = m.group(1) if m else None
    if not account:
        return ""
    return next((j["description"] for j in fetch_workable({"name": "", "token": account}) if j["source_id"] == code), "")


BOARD = Board("workable", fetch_workable, link=r"apply\.workable\.com/(?!j/)([\w-]+)", resolve=workable_description)
