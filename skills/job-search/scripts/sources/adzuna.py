"""Adzuna aggregator (country from the pack; key in .secrets/adzuna.json)."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse

from common import confined, html_to_text, parse_date

from . import net


def fetch_adzuna(cfg: dict) -> list:
    """Profile search terms x adzuna_locations (+ a remote query), capped at adzuna_max_requests calls.
    Results carry a 500-char snippet; the full posting (<details_domain>/details/<id>) is fetched only when shortlisted."""
    import time
    from common import load_profiles
    key_path = confined(cfg.get("adzuna_key_file"), ".secrets/adzuna.json", "adzuna_key_file")
    if not key_path.exists():
        raise RuntimeError(f"no key at {key_path.name}")
    k = json.loads(key_path.read_text())
    terms = [s.lower() for s in cfg.get("adzuna_searches") or []]
    for p in ([] if terms else load_profiles().values()):
        for q in p.get("search_queries") or p["target_titles"][:2]:
            if q.lower() not in terms:
                terms.append(q.lower())
    az = cfg["pack"].get("adzuna") or {}
    spell = {k.lower(): v for k, v in (az.get("city_spellings") or {}).items()}
    locs = [spell.get(x.lower(), x) for x in cfg.get("adzuna_locations") or []]
    searches = [(q, loc) for q in terms for loc in locs]
    if cfg.get("adzuna_remote_query", True):
        searches += [(f"{q} remote", None) for q in terms]
    out, calls = [], 0
    for what, where in searches[: int(cfg.get("adzuna_max_requests", 20))]:
        params = {"app_id": k["app_id"], "app_key": k["app_key"], "results_per_page": 50, "what": what,
                  "max_days_old": cfg["max_age_days"], "content-type": "application/json"}
        if where:
            params["where"] = where
        url = f"https://api.adzuna.com/v1/api/jobs/{az.get('country', cfg.get('country', 'IN').lower())}/search/1?" \
            + urllib.parse.urlencode(params)
        d = None
        for _ in (1, 2):  # the API returns occasional 5xx
            try:
                d = net.get_json(url)
                break
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    raise
                time.sleep(3)
        calls += 1
        time.sleep(1)
        for j in (d or {}).get("results") or []:
            loc = (j.get("location") or {}).get("display_name", "")
            text = html_to_text(j.get("description"))
            title = html_to_text(j.get("title"))
            sal = ""
            if j.get("salary_min") and str(j.get("salary_is_predicted")) == "0":  # ignore Adzuna's estimates
                sal = f"{cfg['currency']} {int(j['salary_min'])} - {int(j.get('salary_max') or j['salary_min'])} per year"
            remote = bool(re.search(r"\bremote\b|work from home|\bwfh\b", f"{title} {text}", re.I))
            out.append({
                "source": "adzuna", "source_id": str(j["id"]), "title": title,
                "company": (j.get("company") or {}).get("display_name", ""),
                "location": ("Remote; " if remote else "") + loc, "remote": remote or None,
                "region_text": cfg["pack"]["name"], "eligible_countries": "", "posted": parse_date(j.get("created")),
                "salary_text": sal, "job_type": " ".join(filter(None, [j.get("contract_type"), j.get("contract_time")])),
                "url": j.get("redirect_url", ""), "description": text,
                "detail_url": f"https://{az.get('details_domain', 'www.adzuna.com')}/details/{j['id']}",
            })
    return out
