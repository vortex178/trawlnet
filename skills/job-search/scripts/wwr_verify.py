"""Verify that a We Work Remotely listing can be applied to for free (company ATS board or careers site).

WWR apply flows can be paywalled for job seekers, so WWR-only listings are rejected. Zero LLM tokens:
ATS lookup by company name (discover.from_probes) and the company website from the WWR description
("URL: https://…"): careers pages are checked for the title and for ATS links whose job lists contain it.
Per-company results are cached in the jobs DB (cache kind "wwr", 7 days).
"""
from __future__ import annotations

import re

import db
from common import age_days, norm, today


def _similar(a: str, b: str) -> bool:
    x = set(norm(re.sub(r"\([^)]*\)", " ", a)).split()) - {"remote", "senior", "sr", "the", "and", "of", "for"}
    y = set(norm(re.sub(r"\([^)]*\)", " ", b)).split()) - {"remote", "senior", "sr", "the", "and", "of", "for"}
    return bool(x and y) and (x <= y or y <= x or len(x & y) / len(x | y) >= 0.6)


def _board_for(company: str, site: str):
    from discover import from_pages, from_probes
    c = {"name": company, "domain": re.sub(r"^https?://(www\.)?", "", site or "").split("/")[0],
         "careers_url": "", "seed": "wwr-verify"}
    hit = (from_pages(c) if c["domain"] else None) or from_probes(c)
    return ({"ats": hit[0], "token": hit[1], **hit[3]} if hit else None), c


def _pages(site: str) -> list:
    dom = re.sub(r"^https?://(www\.)?", "", site).split("/")[0]
    return [f"https://{dom}/careers", f"https://www.{dom}/careers", f"https://{dom}/jobs", f"https://{dom}/"]


def verify(rec: dict) -> tuple:
    """-> (free_url or None, how)."""
    from discover import _get
    from sources import ATS_FETCHERS
    m = re.search(r"URL:\s*(https?://[^\s)]+)", rec.get("description") or "")
    site = m.group(1) if m else ""
    ck = norm(rec["company"]).strip()
    entry = db.cache_get("wwr", ck)
    age = age_days(entry.get("checked")) if entry else None
    if age is None or age > 7:
        board, _ = _board_for(rec["company"], site)
        entry = {"board": board, "site": site, "checked": today()}
        db.cache_put("wwr", ck, entry)
    board = entry.get("board")
    if board:
        try:
            for j in ATS_FETCHERS[board["ats"]]({"name": rec["company"], **board}):
                if _similar(j["title"], rec["title"]):
                    return j["url"], f"{board['ats']} board"
        except Exception:
            pass
    for url in _pages(entry.get("site") or site) if (entry.get("site") or site) else []:
        try:
            _, body = _get(url)
        except Exception:
            continue
        text = re.sub(r"<[^>]+>", " ", body.decode("utf-8", "ignore"))
        if norm(rec["title"]).strip() in norm(text):
            return url, "company careers page"
    return None, "not found on a free source"


def run(accepted: list, rejected: list) -> tuple:
    """Filter WWR records in `accepted`; returns (accepted, rejected, report lines)."""
    keep, report = [], []
    for r in accepted:
        if r["source"] != "wwr":
            keep.append(r)
            continue
        url, how = verify(r)
        if url:
            r["wwr_url"], r["url"] = r["url"], url
            r["flags"] = r.get("flags", []) + ["wwr-verified-free"]
            keep.append(r)
        else:
            rejected.append({"key": r["key"], "reason": "wwr-only-paywalled"})  # details added by filter
        report.append(f"{'OK ' if url else 'REJ'} {r['company'][:24]} — {r['title'][:40]} ({how})")
    return keep, rejected, report
