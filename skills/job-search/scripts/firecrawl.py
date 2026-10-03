"""Firecrawl REST client with a per-run credit budget (markdown scrapes only: 1 credit/page).

Budget per run = max(base_credits_per_run, (remainingCredits - reserve) / days left in billing period),
falling back to base_credits_per_run when usage/period is unavailable. Spend is logged per run in
data/runs/<date>/firecrawl.json so re-running a step the same day doesn't overspend.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import urllib.error
import urllib.request

from common import confined, rel, run_dir

API = "https://api.firecrawl.dev"


class Budget:
    def __init__(self, cfg: dict, date: str | None = None):
        fc = cfg.get("firecrawl") or {}
        self.enabled = bool(fc.get("enabled"))
        self.base = int(fc.get("base_credits_per_run", 30))
        self.reserve = int(fc.get("reserve_credits", 50))
        self.ledger = run_dir(date) / "firecrawl.json"
        self.cache = run_dir(date) / "fc"
        self.min_interval = float(fc.get("min_seconds_between_requests", 6))  # free plan: ~10 req/min
        self.country = cfg.get("country", "IN")  # scrape as a visitor from this country (geo-targeted career pages)
        self._last = 0.0
        state = json.loads(self.ledger.read_text()) if self.ledger.exists() else {}
        self.spent = state.get("spent", 0)
        self.start_remaining = state.get("start_remaining")
        self.allowance = state.get("allowance")
        self.note = state.get("note", "")
        self.key_path = (confined(fc.get("api_key_file"), ".secrets/firecrawl.key", "firecrawl.api_key_file")
                         if self.enabled else None)  # a disabled Firecrawl never reads (or checks) the file
        self.key = self.key_path.read_text().strip() if self.key_path and self.key_path.exists() else None
        if self.enabled and not self.key:
            self.enabled, self.note = False, f"no API key at {rel(self.key_path)}"
        if self.enabled and self.allowance is None:
            self.allowance, self.note = (self._compute_allowance() if fc.get("dynamic_budget", True)
                                         else (self.base, f"fixed {self.base}"))
            self._save()

    def _req(self, method: str, path: str, body: dict | None = None, timeout: int = 60):
        req = urllib.request.Request(f"{API}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def _compute_allowance(self):
        try:
            try:
                d = self._req("GET", "/v2/team/credit-usage")["data"]
            except urllib.error.HTTPError:
                d = self._req("GET", "/v1/team/credit-usage")["data"]
        except Exception as e:
            return self.base, f"credit usage unavailable ({type(e).__name__}); base {self.base}"
        remaining = d.get("remainingCredits") or d.get("remaining_credits")
        end = d.get("billingPeriodEnd")
        if remaining is None or not end:
            return self.base, f"remaining={remaining}, no billing period; base {self.base}"
        self.start_remaining = remaining
        days_left = max(1, (dt.date.fromisoformat(end[:10]) - dt.date.today()).days + 1)
        avg = int(max(0, remaining - self.reserve) / days_left)
        allowance = min(max(self.base, avg), max(0, remaining - self.reserve))
        return allowance, f"remaining {remaining}, {days_left}d left -> avg {avg}/day, allowance {allowance}"

    def _save(self):
        self.ledger.write_text(json.dumps({"spent": self.spent, "allowance": self.allowance, "note": self.note,
                                           "start_remaining": self.start_remaining}))

    def reconcile(self):
        """Replace the local estimate with the real spend from the account balance."""
        if not (self.enabled and self.start_remaining is not None):
            return
        try:
            try:
                d = self._req("GET", "/v2/team/credit-usage")["data"]
            except urllib.error.HTTPError:
                d = self._req("GET", "/v1/team/credit-usage")["data"]
            self.spent = max(0, self.start_remaining - int(d["remainingCredits"]))
            self._save()
        except Exception:
            pass

    def left(self) -> int:
        return max(0, (self.allowance or 0) - self.spent) if self.enabled else 0

    def scrape_markdown(self, url: str) -> str | None:
        """1 credit. Returns main-content markdown, or None if over budget/failed."""
        cached = self.cache / (hashlib.sha1(url.encode()).hexdigest()[:16] + ".md")
        if cached.exists():  # same-day re-runs never pay twice
            return cached.read_text()
        if self.left() < 1:
            return None
        for attempt in (1, 2):
            wait = self.min_interval - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                d = self._req("POST", "/v2/scrape", {"url": url, "formats": ["markdown"], "onlyMainContent": False,
                                                    "location": {"country": self.country}})
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt == 1:  # rate-limited requests are not billed; wait as told, retry once
                    m = re.search(rb"retry after (\d+)s", e.read())
                    time.sleep(int(m.group(1)) + 1 if m else 15)
                    continue
                if e.code != 429:
                    self.spent += 1  # other failures may be billed; count conservatively (reconciled later)
                    self._save()
                return None
            except Exception:
                self.spent += 1
                self._save()
                return None
        self.spent += int(((d.get("data") or {}).get("metadata") or {}).get("creditsUsed") or 1)
        self._save()
        md = (d.get("data") or {}).get("markdown") or ""
        self.cache.mkdir(exist_ok=True)
        cached.write_text(f"<!-- {url} -->\n{md}")
        return md

    def summary(self) -> str:
        if not self.enabled:
            return f"firecrawl off ({self.note or 'disabled in config'})"
        return f"firecrawl {self.spent}/{self.allowance} credits ({self.note})"
